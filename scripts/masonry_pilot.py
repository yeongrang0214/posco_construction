"""Reproducible masonry pilot; snapshots never mutate server reviews.

Run with python -m scripts.masonry_pilot snapshot|match|seed|coverage|report.
Coverage invokes the isolated local server's paid GPT analysis, for this one chapter.
No human quality labels or keep/delete/hold choices are created here.
"""
import argparse
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

import httpx

from server.documents import parse_docx
from server.relevance import own_requirement

PROJECT = "185f3472-fcce-43a6-8eaa-86b0f0b840ed"
BASE = "https://posco-construction-api-production.up.railway.app"
OUT = Path("tmp/masonry-pilot-v54")


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def fetch(client, suffix, base=BASE):
    response = client.get(f"{base}/api/projects/{PROJECT}{suffix}")
    response.raise_for_status()
    return response


def snapshot(client):
    if (OUT / "before.json").exists():
        raise SystemExit("Baseline already exists; do not overwrite.")
    project = fetch(client, "").json()["project"]
    rows = fetch(client, "/bulk-review?limit=200").json()
    assert rows["total"] == len(rows["items"]), "Pilot must include every clause."
    details = [fetch(client, f"/clauses/{row['id']}").json()["clause"] for row in rows["items"]]
    source = fetch(client, "/source.docx").content
    (OUT / "source.docx").write_bytes(source)
    _, parsed, warnings = parse_docx(source, project["source_filename"], PROJECT)
    save("parsed.json", parsed)
    save("before.json", {"project": project, "clauses": details})
    old = [(r["label"], own_requirement(r)) for r in details]
    new = [(r["label"], own_requirement(r)) for r in parsed]
    audit = {"saved_count": len(old), "parsed_count": len(new), "source_equal": old == new,
             "saved_texts_preserved": set(old).issubset(set(new)),
             "reviewable_count": sum(r["source_type"] != "heading" for r in parsed), "warnings": warnings,
             "split_first_line_count": sum(bool(r["content"]) and own_requirement(r) != r["content"].strip() for r in details)}
    save("source-audit.json", audit)
    print(json.dumps(audit, ensure_ascii=False), flush=True)


def match(cache_only=False):
    from server.config import get_settings
    from server.matcher import match_clauses, read_snapshot, CURRENT_MATCHER_VERSION
    from server.openai_ai import OpenAIClient
    if not read("source-audit.json").get("saved_texts_preserved"):
        raise SystemExit("Source parsing changed: reconcile before matching.")
    rows = read("parsed.json")
    settings = get_settings()
    snapshot, manifest = read_snapshot(settings.kcs_manifest_path)
    ai = OpenAIClient.from_settings(settings)
    post = ai._post
    calls = Counter()
    cache_dir = OUT / "api-cache"
    cache_dir.mkdir(exist_ok=True)
    def logged_post(path, payload):
        key = hashlib.sha256(json.dumps([ai.base_url, path, payload], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        cached = cache_dir / f"{key}.json"
        if cached.exists():
            calls["cache_hits"] += 1
            return json.loads(cached.read_text(encoding="utf-8"))
        if cache_only:
            raise SystemExit(f"Cached replay stopped: missing response for {path}. No API call made.")
        result = post(path, payload)
        temporary = cached.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        temporary.replace(cached)
        calls[path] += 1
        print(json.dumps({"phase": path, "completed_calls": calls[path]}, ensure_ascii=False), flush=True)
        return result
    ai._post = logged_post
    try:
        if (OUT / "local-match.json").exists() and not (OUT / "initial-match.json").exists():
            save("initial-match.json", read("local-match.json"))
        result = match_clauses(rows, settings.kcs_raw_dir, ("4134",), ai, require_embeddings=True)
        result["matcher_version"] = CURRENT_MATCHER_VERSION
        result["kcs_snapshot"] = snapshot
        result["kcs_revision"] = manifest["revision"]
        if read_snapshot(settings.kcs_manifest_path)[1]["revision"] != manifest["revision"]:
            raise SystemExit("KCS snapshot changed while matching; output not accepted.")
        result["api_calls"] = dict(calls)
        save("local-match.json", {"matching": result, "clauses": rows})
        print(json.dumps({"done": True, "candidate_count": sum(len(r["candidates"]) for r in rows)}, ensure_ascii=False), flush=True)
    finally:
        ai.close()


def seed():
    from datetime import datetime, timezone
    from server.storage import Store, CURRENT_PARSER_VERSION
    from server.documents import sha256_bytes
    from server.matcher import read_snapshot
    from server.config import get_settings
    database = OUT / "local-data/spec_manager.sqlite3"
    store = Store(database)
    if store.get_project(PROJECT):
        raise SystemExit("Pilot already seeded; do not overwrite its review data.")
    project = dict(read("before.json")["project"])
    snapshot, manifest = read_snapshot(get_settings().kcs_manifest_path)
    matching = read("local-match.json")["matching"]
    if matching.get("kcs_revision") != manifest["revision"]:
        raise SystemExit("Match corpus not verified; do not label pilot with a different KCS revision.")
    upload = OUT / "local-data/uploads" / f"{PROJECT}.docx"
    upload.parent.mkdir(parents=True, exist_ok=True)
    if upload.exists() and upload.read_bytes() != (OUT / "source.docx").read_bytes():
        raise SystemExit("Isolated source differs; do not overwrite it.")
    shutil.copyfile(OUT / "source.docx", upload)
    project.update({"title": "조적공사 · 정합성 시험본", "source_path": str(upload.resolve()),
                    "source_filename": "건축_제03장_조적공사_201712.docx",
                    "source_sha256": sha256_bytes((OUT / "source.docx").read_bytes()),
                    "uploaded_at": datetime.now(timezone.utc).isoformat(), "parser_version": CURRENT_PARSER_VERSION,
                    "kcs_snapshot": snapshot, "kcs_revision": manifest["revision"], "kcs_scope": "KCS 41 34 · 조적공사 시험 범위",
                    "warning": "정합성 시험용 사본: 기존 본문 40개와 복원된 표 14개. 담당자 정답 판정 전이며 기존 배포본은 보존됩니다."})
    store.create_project(project, read("local-match.json")["clauses"])
    print("Isolated pilot seeded.", flush=True)


def coverage(client):
    local_base = "http://127.0.0.1:8012"
    expected = (OUT / "local-data/spec_manager.sqlite3").resolve()
    if not expected.exists():
        raise SystemExit("Seed isolated pilot first.")
    rows = fetch(client, "/bulk-review?limit=200", local_base).json()["items"]
    original = {r["id"]: r for r in read("local-match.json")["clauses"]}
    assert set(original) == {r["id"] for r in rows}, "Wrong pilot clauses."
    results = []
    failures = read("coverage-failures.json") if (OUT / "coverage-failures.json").exists() else []
    if (OUT / "coverage-error.json").exists():
        last_error = read("coverage-error.json")
        if last_error["id"] not in {failure["id"] for failure in failures}:
            failures.append(last_error)
    save("coverage-failures.json", failures)
    for row in rows:
        detail = fetch(client, f"/clauses/{row['id']}", local_base).json()["clause"]
        assert own_requirement(detail) == own_requirement(original[row["id"]]), "Source changed."
        # Never analyze a clause that the user has meanwhile reviewed.
        if detail.get("decision") or detail.get("review_note") or detail.get("selected_candidate_id"):
            results.append({"id": row["id"], "skipped": "review_exists_or_changed"})
            continue
        if row["source_type"] == "heading" or not detail["candidates"]:
            continue
        if row["id"] in {failure["id"] for failure in failures}:
            results.append({"id": row["id"], "label": row["label"], "skipped": "previous_validation_or_api_error"})
            continue
        response = client.post(f"{local_base}/api/projects/{PROJECT}/clauses/{row['id']}/coverage-analysis", json={"refresh": False})
        if response.is_error:
            save("coverage-progress.json", results)
            failure = {"id": row["id"], "label": row["label"], "http_status": response.status_code, "detail": response.text}
            failures.append(failure)
            save("coverage-failures.json", failures)
            save("coverage-error.json", failure)
            # Invalid analysis remains unverified; continue OTHER clauses, never retry it.
            if response.status_code == 502 and any(word in response.text for word in ("누락", "중복", "모순", "포괄 판정 값", "잔여 문구")):
                print(json.dumps({"failed": row["label"], "status": "not_saved"}, ensure_ascii=False), flush=True)
                continue
            raise SystemExit(f"Coverage stopped at {row['label']}: HTTP {response.status_code}. See coverage-error.json. No automatic paid retries.")
        result = response.json()
        results.append({"id": row["id"], "label": row["label"], "analysis": result["analysis"]})
        save("coverage-progress.json", results)
        print(json.dumps({"completed": len(results), "label": row["label"], "status": result["analysis"]["coverage_status"]}, ensure_ascii=False), flush=True)
    after = [fetch(client, f"/clauses/{r['id']}", local_base).json()["clause"] for r in rows]
    save("after.json", {"project": fetch(client, "", local_base).json()["project"], "clauses": after})


def report():
    def counts(data):
        rows = [r for r in data["clauses"] if r["source_type"] != "heading"]
        candidates = [c for r in rows for c in r["candidates"]]
        return {"reviewable": len(rows), "with_candidates": sum(bool(r["candidates"]) for r in rows),
                "candidates": len(candidates), "title_only": sum(c["title"].strip() == c["content"].strip() for c in candidates),
                "semantic_status": dict(Counter(c["classification"] for c in candidates)),
                "coverage_status": dict(Counter((r.get("coverage_analysis") or {}).get("coverage_status", "not_run") for r in rows))}
    result = {"source": read("source-audit.json"), "before": counts(read("before.json"))}
    for name in ("local-match", "after"):
        if (OUT / f"{name}.json").exists():
            result[name] = counts(read(f"{name}.json"))
            original_texts = {own_requirement(r) for r in read("before.json")["clauses"] if r["source_type"] != "heading"}
            result[f"{name}-common-original"] = counts({"clauses": [r for r in read(f"{name}.json")["clauses"] if own_requirement(r) in original_texts]})
    result["accuracy"] = "Not measured: no independent human-adjudicated ground truth."
    result["coverage_failures"] = read("coverage-failures.json") if (OUT / "coverage-failures.json").exists() else []
    matching = read("local-match.json")["matching"] if (OUT / "local-match.json").exists() else {}
    baseline = read("before.json")["project"]
    result["corpus"] = {
        "baseline_revision": baseline.get("kcs_revision"),
        "pilot_revision": matching.get("kcs_revision"),
        "pilot_snapshot": matching.get("kcs_snapshot"),
        "same_corpus": bool(matching.get("kcs_revision")) and baseline.get("kcs_revision") == matching.get("kcs_revision"),
        "notice": "Different KCS snapshots: baseline/pilot counts are diagnostics, not an accuracy improvement rate.",
    }
    save("summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("snapshot", "match", "seed", "coverage", "report"))
    parser.add_argument("--cache-only", action="store_true", help="Replay matching without any new API calls.")
    args = parser.parse_args()
    if args.cache_only and args.action != "match":
        parser.error("--cache-only is only supported for match")
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=180, follow_redirects=True) as client:
        if args.action == "snapshot": snapshot(client)
        elif args.action == "match": match(cache_only=args.cache_only)
        elif args.action == "seed": seed()
        elif args.action == "coverage": coverage(client)
        else: report()
