"""Seed an isolated local review copy. Never change the user's existing database."""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from server.config import get_settings
from server.source_preview import preview_path
from server.storage import Store
from server.matcher import read_snapshot

settings = get_settings()
expected = (Path.cwd() / "tmp/preview-v52/local-data").resolve()
if settings.data_dir.resolve() != expected:
    raise SystemExit("Only the isolated preview data directory is allowed.")
store = Store(settings.database_path)
project_id = "d13d0039-7252-4900-9252-000000000052"
if store.get_project(project_id):
    raise SystemExit("Preview already seeded; original review data remains untouched.")
source = Path(r"C:\Users\00byr\OneDrive\Desktop\260819\시방서\건축_제13장_철골공사_201712.doc")
destination = settings.uploads_dir / f"{project_id}.doc"
shutil.copyfile(source, destination)
qa = Path("tmp/preview-v52")
cached = preview_path(destination, expected / "source-previews")
cached.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(qa / "steel-source.pdf", cached)
shutil.copyfile(qa / "steel-source.json", cached.with_suffix(".json"))
clauses = json.loads((qa / "clauses.json").read_text(encoding="utf-8"))
sample = json.loads((qa / "matching-sample.json").read_text(encoding="utf-8"))["clauses"]
results = {row["order"]: row["candidates"] for row in sample}
for row in clauses:
    row["candidates"] = results.get(row["source_order"], [])
snapshot, manifest = read_snapshot(settings.kcs_manifest_path)
store.create_project({
    "id": project_id, "title": "철골공사 · 수정 검증용", "source_filename": source.name,
    "source_path": str(destination), "source_sha256": cached.stem, "uploaded_at": datetime.now(timezone.utc).isoformat(),
    "kcs_snapshot": snapshot, "kcs_revision": manifest["revision"], "kcs_scope": "KCS 41 31, KCS 14 31",
    "warning": "수정 검증용 사본입니다. 5개 표본만 재매칭했으며 공개 사이트의 검토 결과는 변경하지 않았습니다.",
}, clauses)
print(project_id)
