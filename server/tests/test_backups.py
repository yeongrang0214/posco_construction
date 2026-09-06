from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server.backups import BACKUP_FORMAT_VERSION, BackupError, BackupManager
from server.config import Settings
from server.storage import DATABASE_SCHEMA_VERSION, Store


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    kcs_dir = tmp_path / "kcs"
    for path in (data_dir, data_dir / "uploads", data_dir / "exports", kcs_dir / "raw"):
        path.mkdir(parents=True, exist_ok=True)
    return Settings(
        project_root=tmp_path,
        data_dir=data_dir,
        uploads_dir=data_dir / "uploads",
        exports_dir=data_dir / "exports",
        database_path=data_dir / "test.sqlite3",
        kcs_data_dir=kcs_dir,
        kcs_raw_dir=kcs_dir / "raw",
        kcs_manifest_path=kcs_dir / "manifest.json",
        openai_api_key=None,
        openai_base_url="https://example.test/v1",
        openai_embedding_model="test-embedding",
        openai_embedding_dimensions=8,
        openai_rerank_model="test-model",
        openai_timeout_seconds=1,
    )


def _seed(settings: Settings) -> tuple[Store, str, Path]:
    store = Store(settings.database_path)
    store.set_current_kcs_revision(
        "2026-09-06T00:00:00+09:00",
        "backup-revision",
    )
    project_id = "backup-project"
    source = settings.uploads_dir / f"{project_id}.docx"
    source_bytes = b"PK\x03\x04test-posco-source"
    source.write_bytes(source_bytes)
    store.create_project(
        {
            "id": project_id,
            "title": "백업 전 승인 프로젝트",
            "source_filename": "source.docx",
            "source_path": str(source),
            "source_sha256": _sha256(source_bytes),
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "kcs_snapshot": "2026-09-06T00:00:00+09:00",
            "kcs_revision": "backup-revision",
            "kcs_scope": "KCS 41 31",
            "warning": "",
        },
        [
            {
                "id": "backup-heading",
                "source_order": 1,
                "label": "1",
                "title": "일반사항",
                "content": "일반사항",
                "source_type": "heading",
                "outline_level": 1,
                "candidates": [],
            },
            {
                "id": "backup-clause",
                "source_order": 2,
                "label": "1.1",
                "title": "포스코 강화기준",
                "content": "포스코 강화기준을 적용한다.",
                "source_type": "paragraph",
                "outline_level": None,
                "candidates": [],
            },
        ],
    )
    store.update_decision(
        project_id,
        "backup-clause",
        "keep",
        "포스코 강화기준을 적용한다.",
        "승인 검토 완료",
        None,
        decision_reason="posco_specific",
    )
    workflow = store.submit_review(project_id, "작성자", "백업 시험")
    store.approve_review(
        project_id,
        workflow["latest_review_submission"]["id"],
        "승인자",
        "승인 완료",
    )
    return store, project_id, source


def _manager(settings: Settings, store: Store, retention: int = 20) -> BackupManager:
    return BackupManager(
        store,
        settings.data_dir / "backups",
        settings.uploads_dir,
        settings.kcs_data_dir,
        retention=retention,
    )


def _rewrite_zip(source: Path, target: Path, changes: dict[str, bytes], extra=None) -> None:
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(
        target, "w", compression=zipfile.ZIP_DEFLATED
    ) as output:
        for info in archive.infolist():
            output.writestr(info.filename, changes.get(info.filename, archive.read(info)))
        if extra:
            output.writestr(extra[0], extra[1])


def _database_dump(path: Path) -> tuple[str, ...]:
    connection = sqlite3.connect(path)
    try:
        return tuple(connection.iterdump())
    finally:
        connection.close()


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def test_backup_restore_round_trip_preserves_workflow_upload_and_wal(tmp_path):
    settings = _settings(tmp_path)
    store, project_id, source = _seed(settings)
    manager = _manager(settings, store)

    writer = sqlite3.connect(settings.database_path)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute(
            "INSERT INTO app_metadata(key, value) VALUES ('wal-marker', 'included')"
        )
        writer.commit()
        assert Path(f"{settings.database_path}-wal").stat().st_size > 0

        main_file_copy = tmp_path / "main-file-only.sqlite3"
        shutil.copy2(settings.database_path, main_file_copy)
        with sqlite3.connect(main_file_copy) as connection:
            assert connection.execute(
                "SELECT value FROM app_metadata WHERE key = 'wal-marker'"
            ).fetchone() is None

        backup = manager.create()
    finally:
        writer.close()

    archive_path = manager.download_path(backup["id"])
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert "database.sqlite3" in names
        assert f"uploads/{source.name}" in names
        assert not any(".env" in name.lower() for name in names)

    with store.connect() as connection:
        connection.execute(
            "UPDATE projects SET title = '복구 전 변경됨', status = 'reviewing' WHERE id = ?",
            (project_id,),
        )
        connection.execute("DELETE FROM app_metadata WHERE key = 'wal-marker'")

    result = manager.restore(backup["id"])
    restored = store.get_project(project_id)
    assert restored is not None
    assert restored["title"] == "백업 전 승인 프로젝트"
    assert restored["status"] == "approved"
    with store.connect() as connection:
        restored_source = Path(
            connection.execute(
                "SELECT source_path FROM projects WHERE id = ?", (project_id,)
            ).fetchone()[0]
        )
    assert restored_source.parent == settings.uploads_dir
    assert restored_source.read_bytes() == source.read_bytes()
    with store.connect() as connection:
        assert connection.execute(
            "SELECT value FROM app_metadata WHERE key = 'wal-marker'"
        ).fetchone()[0] == "included"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert connection.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SCHEMA_VERSION
        submission = connection.execute(
            "SELECT review_snapshot_json, review_snapshot_sha256 FROM project_review_submissions"
        ).fetchone()
        assert hashlib.sha256(submission[0].encode("utf-8")).hexdigest() == submission[1]
    assert result["safety_backup"]["reason"] == "pre_restore"
    assert len(manager.list()) == 2


@pytest.mark.parametrize("kind", ["database", "extra", "zip_slip", "future_schema"])
def test_restore_rejects_invalid_archives_without_changing_live_database(tmp_path, kind):
    settings = _settings(tmp_path)
    store, project_id, _ = _seed(settings)
    manager = _manager(settings, store)
    backup = manager.create()
    source = manager.download_path(backup["id"])
    invalid = tmp_path / f"invalid-{kind}.zip"

    if kind == "database":
        with zipfile.ZipFile(source) as archive:
            database = bytearray(archive.read("database.sqlite3"))
            manifest = json.loads(archive.read("manifest.json"))
        database[-1] ^= 0x01
        for entry in manifest["files"]:
            if entry["path"] == "database.sqlite3":
                entry["sha256"] = _sha256(bytes(database))
        _rewrite_zip(
            source,
            invalid,
            {
                "database.sqlite3": bytes(database),
                "manifest.json": json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            },
        )
    elif kind == "extra":
        _rewrite_zip(source, invalid, {}, ("unexpected.txt", b"x"))
    elif kind == "zip_slip":
        _rewrite_zip(source, invalid, {}, ("../.env", b"SECRET=leak"))
    else:
        with zipfile.ZipFile(source) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        manifest["database_schema_version"] = DATABASE_SCHEMA_VERSION + 1
        _rewrite_zip(
            source,
            invalid,
            {"manifest.json": json.dumps(manifest).encode("utf-8")},
        )

    with store.connect() as connection:
        connection.execute(
            "UPDATE projects SET title = '현재 상태 유지' WHERE id = ?", (project_id,)
        )
    before = settings.database_path.read_bytes()
    with pytest.raises(BackupError):
        manager.restore_archive(invalid)
    assert store.get_project(project_id)["title"] == "현재 상태 유지"
    assert settings.database_path.read_bytes() == before
    assert not (tmp_path / ".env").exists()
    assert len(manager.list()) == 1


def test_signed_archive_with_noncanonical_schema_is_rejected(tmp_path):
    settings = _settings(tmp_path)
    store, _, _ = _seed(settings)
    manager = _manager(settings, store)
    backup = manager.create()
    source = manager.download_path(backup["id"])
    invalid = tmp_path / "invalid-schema.zip"
    database_path = tmp_path / "invalid-schema.sqlite3"

    with zipfile.ZipFile(source) as archive:
        database_path.write_bytes(archive.read("database.sqlite3"))
        manifest = json.loads(archive.read("manifest.json"))
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE INDEX unexpected_index ON app_metadata(value)")
    database = database_path.read_bytes()
    for entry in manifest["files"]:
        if entry["path"] == "database.sqlite3":
            entry["size"] = len(database)
            entry["sha256"] = _sha256(database)
    manifest["database_schema_sha256"] = store.schema_fingerprint(database_path)
    manifest.pop("authentication")
    manifest = manager._sign_manifest(manifest)
    _rewrite_zip(
        source,
        invalid,
        {
            "database.sqlite3": database,
            "manifest.json": json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        },
    )

    with pytest.raises(BackupError, match="구조가 현재 프로그램과 다릅니다"):
        manager.inspect_archive(invalid)


def test_backup_retention_keeps_latest_records(tmp_path):
    settings = _settings(tmp_path)
    store, _, _ = _seed(settings)
    manager = _manager(settings, store, retention=2)
    manager.create()
    manager.create()
    manager.create()
    assert len(manager.list()) == 2


def test_existing_signed_backup_prevents_silent_authentication_key_regeneration(
    tmp_path,
):
    settings = _settings(tmp_path)
    store, _, _ = _seed(settings)
    manager = _manager(settings, store)
    backup = manager.create()
    archive_path = manager.download_path(backup["id"])
    with zipfile.ZipFile(archive_path) as archive:
        assert (
            json.loads(archive.read("manifest.json"))["backup_format_version"]
            == BACKUP_FORMAT_VERSION
        )

    key_path = manager.authentication_key_path
    assert key_path.is_file()
    key_path.unlink()
    before_archives = {path.name for path in manager.backups_dir.glob("backup-*.zip")}

    with pytest.raises(BackupError, match="기존 백업의 서명 키"):
        manager.create()

    assert not key_path.exists()
    assert {path.name for path in manager.backups_dir.glob("backup-*.zip")} == before_archives


def test_restore_replace_failure_preserves_live_database_and_complete_upload_tree(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    store, project_id, _ = _seed(settings)
    manager = _manager(settings, store)
    backup = manager.create()
    unrelated = settings.uploads_dir / "unrelated.docx"
    unrelated.write_bytes(b"unrelated user file")
    with store.connect() as connection:
        connection.execute(
            "UPDATE projects SET title = '복구 실패 시 유지할 제목' WHERE id = ?",
            (project_id,),
        )
    before_database = _database_dump(settings.database_path)
    before_uploads = _tree_snapshot(settings.uploads_dir)

    def fail_replace(_replacement: Path) -> None:
        raise RuntimeError("replace failed")

    monkeypatch.setattr(store, "replace_database", fail_replace)
    with pytest.raises(RuntimeError, match="replace failed"):
        manager.restore(backup["id"])

    assert _database_dump(settings.database_path) == before_database
    assert _tree_snapshot(settings.uploads_dir) == before_uploads


def test_managed_backup_rejects_filename_uuid_mismatch(tmp_path):
    settings = _settings(tmp_path)
    store, project_id, _ = _seed(settings)
    manager = _manager(settings, store)
    backup = manager.create()
    mismatched_id = str(uuid.uuid4())
    mismatched_path = manager.backups_dir / (
        f"backup-20990101T000000000000Z-{mismatched_id}.zip"
    )
    shutil.copy2(manager.download_path(backup["id"]), mismatched_path)
    before_database = _database_dump(settings.database_path)

    with pytest.raises(BackupError, match="선택한 백업과 내부 식별값"):
        manager.restore(mismatched_id)

    assert store.get_project(project_id)["title"] == "백업 전 승인 프로젝트"
    assert _database_dump(settings.database_path) == before_database


def test_database_replace_rolls_back_when_new_database_initialization_fails(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    store, project_id, _ = _seed(settings)
    replacement = tmp_path / "replacement.sqlite3"
    store.backup_database(replacement)
    connection = sqlite3.connect(replacement)
    try:
        connection.execute(
            "UPDATE projects SET title = '적용되면 안 되는 제목' WHERE id = ?",
            (project_id,),
        )
        connection.commit()
    finally:
        connection.close()

    original_initialize = store.initialize
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("새 DB 초기화 실패")
        return original_initialize()

    monkeypatch.setattr(store, "initialize", fail_once)
    with pytest.raises(RuntimeError, match="기존 상태로 되돌렸습니다"):
        store.replace_database(replacement)
    assert store.get_project(project_id)["title"] == "백업 전 승인 프로젝트"


def test_backup_api_requires_confirmation_and_restores(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    store, project_id, _ = _seed(settings)
    monkeypatch.setattr(app_module, "settings", settings)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(
        app_module,
        "_read_current_kcs_target",
        lambda: (
            "2026-09-06T00:00:00+09:00",
            "backup-revision",
            {"revision": "backup-revision"},
        ),
    )

    with TestClient(app_module.app) as client:
        created = client.post("/api/system/backups")
        assert created.status_code == 201
        backup = created.json()["backup"]
        assert backup["project_count"] == 1
        assert "path" not in backup

        with store.connect() as connection:
            connection.execute(
                "UPDATE projects SET title = 'API 복구 전 변경' WHERE id = ?",
                (project_id,),
            )
        rejected = client.post(
            f"/api/system/backups/{backup['id']}/restore",
            json={"confirmation": "RESTORE"},
        )
        assert rejected.status_code == 422
        restored = client.post(
            f"/api/system/backups/{backup['id']}/restore",
            json={"confirmation": "복구"},
        )
        assert restored.status_code == 200
        assert restored.json()["safety_backup"]["reason"] == "pre_restore"
        assert store.get_project(project_id)["title"] == "백업 전 승인 프로젝트"

        listing = client.get("/api/system/backups")
        assert listing.status_code == 200
        assert len(listing.json()["backups"]) == 2
        downloaded = client.get(f"/api/system/backups/{backup['id']}/download")
        assert downloaded.status_code == 200
        assert downloaded.content.startswith(b"PK")

        inspected = client.post(
            "/api/system/backups/inspect-upload",
            files={"file": ("backup.zip", downloaded.content, "application/zip")},
        )
        assert inspected.status_code == 200
        assert inspected.json()["backup"]["id"] == backup["id"]
        assert inspected.json()["backup"]["project_count"] == 1

        with store.connect() as connection:
            connection.execute(
                "UPDATE projects SET title = '외부 ZIP 복구 전 변경' WHERE id = ?",
                (project_id,),
            )
        uploaded_restore = client.post(
            "/api/system/backups/restore-upload",
            data={"confirmation": "복구"},
            files={"file": ("backup.zip", downloaded.content, "application/zip")},
        )
        assert uploaded_restore.status_code == 200
        assert store.get_project(project_id)["title"] == "백업 전 승인 프로젝트"

        monkeypatch.setattr(store, "active_upload_item_count", lambda: 1)
        blocked = client.post("/api/system/backups")
        assert blocked.status_code == 409
        assert "업로드 분석 1건" in blocked.json()["detail"]


def test_inspect_uploaded_invalid_zip_returns_400_without_changing_state(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    store, _, _ = _seed(settings)
    monkeypatch.setattr(app_module, "settings", settings)
    monkeypatch.setattr(app_module, "store", store)
    before_database = _database_dump(settings.database_path)
    before_uploads = _tree_snapshot(settings.uploads_dir)
    backups_dir = settings.data_dir / "backups"

    with TestClient(app_module.app) as client:
        response = client.post(
            "/api/system/backups/inspect-upload",
            files={"file": ("invalid.zip", b"not a zip archive", "application/zip")},
        )

    assert response.status_code == 400
    assert _database_dump(settings.database_path) == before_database
    assert _tree_snapshot(settings.uploads_dir) == before_uploads
    assert list(backups_dir.glob("*")) == []


def test_api_restore_applies_current_kcs_and_invalidates_old_approval(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    store, project_id, _ = _seed(settings)
    manager = _manager(settings, store)
    backup = manager.create()
    with store.connect() as connection:
        connection.execute(
            "UPDATE projects SET title = '복구 전 현재 제목' WHERE id = ?",
            (project_id,),
        )

    monkeypatch.setattr(app_module, "settings", settings)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(
        app_module,
        "_read_current_kcs_target",
        lambda: (
            "2026-09-07T00:00:00+09:00",
            "new-kcs-revision",
            {"revision": "new-kcs-revision"},
        ),
    )
    monkeypatch.setattr(app_module, "_kick_kcs_rematch_worker", lambda: None)
    monkeypatch.setattr(app_module, "_kick_upload_worker", lambda: None)

    with TestClient(app_module.app) as client:
        response = client.post(
            f"/api/system/backups/{backup['id']}/restore",
            json={"confirmation": "복구"},
        )

    assert response.status_code == 200
    project = store.get_project(project_id)
    assert project["title"] == "백업 전 승인 프로젝트"
    assert project["status"] == "changes_requested"
    assert project["kcs_stale"] is True
    assert project["latest_review_submission"]["status"] == "superseded"
    assert store.active_kcs_rematch_count() == 1
