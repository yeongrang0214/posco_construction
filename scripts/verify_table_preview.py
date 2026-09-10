"""Create an isolated, additive local steel-table QA copy; no production writes."""
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from server.config import get_settings
from server.documents import parse_docx
from server.matcher import read_snapshot
from server.source_preview import extract_index, map_clauses, preview_path
from server.storage import Store
from server.table_evidence import compare_table, source_fields, table_candidates


settings = get_settings()
expected = (Path.cwd() / "tmp/preview-v52/local-data").resolve()
if settings.data_dir.resolve() != expected:
    raise SystemExit("Only the isolated preview data directory is allowed.")
qa = Path("tmp/preview-v53")
qa.mkdir(exist_ok=True)
project_id = "d13d0039-7252-4900-9252-000000000053"
store = Store(settings.database_path)
source = Path("tmp/preview-v52/steel-source.docx")
source_bytes = source.read_bytes()
title, clauses, warnings = parse_docx(source_bytes, "건축_제13장_철골공사_201712.docx", project_id)
prefixes = ("4131", "1431")
for row in clauses:
    row["candidates"] = table_candidates(row, settings.kcs_raw_dir, prefixes)
sample = next(c for c in clauses if c["source_type"] == "table" and "SM 275A" in c["content"])
comparison = compare_table(sample, settings, prefixes)
assert comparison["applicable"]
assert {r["kind"] for r in comparison["evidence"]} == {"KCS", "KDS"}, comparison.get("warnings")
assert comparison["checks"][2]["status"] == "unconfirmed"
index_path = qa / "steel-source.json"
if index_path.exists():
    index = json.loads(index_path.read_text(encoding="utf-8"))
else:
    index = extract_index(Path("tmp/preview-v52/steel-source.pdf"))
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
mapping = map_clauses(index, clauses)
sample_ids = {c["id"] for c in clauses if c["source_type"] == "table" and source_fields(c) and source_fields(c)["standards"] == ["KS D 3515"]}
mapped = [m for m in mapping["items"] if m["id"] in sample_ids]
assert all(m["status"] == "table_row" for m in mapped), mapped
(qa / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
(qa / "mapping.json").write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
(qa / "clauses.json").write_text(json.dumps(clauses, ensure_ascii=False), encoding="utf-8")
if not store.get_project(project_id):
    destination = settings.uploads_dir / f"{project_id}.docx"
    shutil.copyfile(source, destination)
    cached = preview_path(destination, settings.data_dir / "source-previews")
    cached.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile("tmp/preview-v52/steel-source.pdf", cached)
    shutil.copyfile(index_path, cached.with_suffix(".json"))
    snapshot, manifest = read_snapshot(settings.kcs_manifest_path)
    store.create_project({"id": project_id, "title": "철골공사 · 표 행·KDS 검증", "source_filename": "건축_제13장_철골공사_201712.docx",
        "source_path": str(destination), "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "uploaded_at": datetime.now(timezone.utc).isoformat(), "kcs_snapshot": snapshot,
        "kcs_revision": manifest["revision"], "kcs_scope": "KCS 41 31, KCS 14 31",
        "warning": "표 행·KDS 검증용 별도 사본입니다. 표의 규격·강종만 직접 대조했으며 일반 문단 전체 재매칭은 실행하지 않았습니다.",
    }, clauses)
print(json.dumps({"project": project_id, "clauses": len(clauses), "table_rows": sum(c["source_type"] == "table" for c in clauses),
    "KS_D_3515_rows": len(mapped), "all_sample_rows_mapped": True, "sample_order": sample["source_order"],
    "sample_id": sample["id"], "sample_page": next(m["boxes"][0]["page"] for m in mapped if m["id"] == sample["id"]),
    "warnings": warnings}, ensure_ascii=False))
