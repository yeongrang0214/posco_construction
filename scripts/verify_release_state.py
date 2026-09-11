"""Read-only release QA: snapshot and compare human decisions and source content."""
import argparse
import json
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("project")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args()
    url = args.base.rstrip("/") + "/api/projects/" + args.project
    with httpx.Client(timeout=60) as client:
        project = client.get(url)
        project.raise_for_status()
        bulk = client.get(url + "/bulk-review?limit=200")
        bulk.raise_for_status()
        data = bulk.json()
        if data.get("has_more"):
            raise RuntimeError("This bounded check supports at most 200 clauses; snapshot pagination required.")
        snapshot = {"project": project.json(), "items": data["items"]}
        job = client.get(url + "/detailed-analysis")
        job.raise_for_status()
    if not args.compare:
        if args.snapshot.exists():
            raise RuntimeError("Refusing to overwrite a release baseline")
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"snapshot": str(args.snapshot), "items": len(data["items"])}))
        return
    previous = json.loads(args.snapshot.read_text(encoding="utf-8"))
    fields = ("id", "label", "title", "content", "source_type", "source_order", "decision", "decision_reason", "review_note", "edited_content")
    def human(rows):
        return {r["id"]: {key: r.get(key) for key in fields} for r in rows}
    changed = [key for key in set(human(previous["items"])) | set(human(data["items"]))
               if human(previous["items"]).get(key) != human(data["items"]).get(key)]
    p = snapshot["project"].get("project", snapshot["project"])
    print(json.dumps({"source_and_decisions_preserved": not changed, "changed_ids": changed,
                      "clauses": len(data["items"]), "matcher_version": p.get("matcher_version"),
                      "rematch": p.get("kcs_rematch"), "analysis_job": job.json()}, ensure_ascii=False))
    if changed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
