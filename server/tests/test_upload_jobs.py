from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server.config import Settings
from server.openai_ai import OpenAIAPIError
from server.storage import CURRENT_PARSER_VERSION, Store


SNAPSHOT = "2026-09-05T00:00:00+09:00"
REVISION = "upload-revision-a"


def test_upload_endpoint_allows_public_vercel_beta_origin(tmp_path, monkeypatch):
    test_settings = _settings(tmp_path)
    monkeypatch.setattr(app_module, "settings", test_settings)
    monkeypatch.setattr(app_module, "store", Store(test_settings.database_path))

    with TestClient(app_module.app) as client:
        response = client.options(
            "/api/upload-jobs",
            headers={
                "Origin": "https://poscoconstruction.vercel.app",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://poscoconstruction.vercel.app"


def _settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    kcs_dir = tmp_path / "kcs"
    raw_dir = kcs_dir / "raw"
    for path in (data_dir, data_dir / "uploads", data_dir / "exports", raw_dir):
        path.mkdir(parents=True, exist_ok=True)
    return Settings(
        project_root=tmp_path,
        data_dir=data_dir,
        uploads_dir=data_dir / "uploads",
        exports_dir=data_dir / "exports",
        database_path=data_dir / "test.sqlite3",
        kcs_data_dir=kcs_dir,
        kcs_raw_dir=raw_dir,
        kcs_manifest_path=kcs_dir / "manifest.json",
        openai_api_key="test-key",
        openai_base_url="https://example.test/v1",
        openai_embedding_model="test-embedding",
        openai_embedding_dimensions=8,
        openai_rerank_model="test-model",
        openai_timeout_seconds=1,
    )


def _project(store: Store, tmp_path: Path, project_id: str, title: str = "시험 시방서") -> None:
    store.create_project(
        {
            "id": project_id,
            "title": title,
            "source_filename": f"{title}.docx",
            "source_path": str(tmp_path / f"{project_id}.docx"),
            "source_sha256": f"sha-{project_id}",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "kcs_snapshot": SNAPSHOT,
            "kcs_revision": REVISION,
            "kcs_scope": "KCS 시험 범위",
            "parser_version": CURRENT_PARSER_VERSION,
            "warning": "",
        },
        [
            {
                "id": f"{project_id}-clause",
                "source_order": 1,
                "label": "1.1",
                "title": "시험 조항",
                "content": "시험 기준을 적용한다.",
                "source_type": "paragraph",
                "outline_level": None,
                "match_context": title,
                "candidates": [],
            }
        ],
    )


def _job(store: Store, tmp_path: Path, job_id: str = "job") -> dict:
    queued_at = datetime.now(timezone.utc).isoformat()
    items = []
    for position in (1, 2):
        project_id = f"{job_id}-project-{position}"
        source_path = tmp_path / f"{project_id}.docx"
        data = f"source-{position}".encode()
        source_path.write_bytes(data)
        items.append(
            {
                "id": f"{job_id}-item-{position}",
                "position": position,
                "filename": f"시방서-{position}.docx",
                "source_path": str(source_path),
                "source_sha256": app_module.sha256_bytes(data),
                "source_size": len(data),
                "suffix": ".docx",
                "project_id": project_id,
                "queued_at": queued_at,
            }
        )
    return store.create_upload_job(
        {
            "id": job_id,
            "kcs_snapshot": SNAPSHOT,
            "kcs_revision": REVISION,
            "created_at": queued_at,
        },
        items,
    )


def test_upload_store_recovers_running_items_without_duplicate_projects(tmp_path):
    store = Store(tmp_path / "upload-recovery.sqlite3")
    store.set_current_kcs_revision(SNAPSHOT, REVISION)
    job = _job(store, tmp_path)
    first, second = job["items"]
    assert store.recover_interrupted_upload_jobs() == {"requeued": 0, "completed": 0}
    assert store.pending_upload_item_ids() == [first["id"], second["id"]]
    store.claim_upload_item(first["id"])
    store.claim_upload_item(second["id"])
    _project(store, tmp_path, second["project_id"])

    recovered = store.recover_interrupted_upload_jobs()
    current = store.get_upload_job(job["id"])

    assert recovered == {"requeued": 1, "completed": 1}
    assert [item["status"] for item in current["items"]] == ["queued", "completed"]
    assert current["completed"] == 1
    assert current["queued"] == 1
    assert current["finished_at"] is None


def test_store_enables_wal_and_busy_timeout(tmp_path):
    store = Store(tmp_path / "concurrency.sqlite3")

    with store.connect() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 30000


def test_upload_store_retry_progress_and_terminal_aggregate(tmp_path):
    store = Store(tmp_path / "upload-state.sqlite3")
    store.set_current_kcs_revision(SNAPSHOT, REVISION)
    job = _job(store, tmp_path)
    first, second = job["items"]
    store.claim_upload_item(first["id"])
    store.update_upload_item_progress(first["id"], 40, "matching")
    failed = store.fail_upload_item(first["id"], "일시적 임베딩 오류", retryable=True)
    assert failed["items"][0]["retryable"] is True
    retried = store.retry_upload_item(job["id"], first["id"])
    assert retried["items"][0]["status"] == "queued"

    store.claim_upload_item(first["id"])
    _project(store, tmp_path, first["project_id"])
    store.complete_upload_item(first["id"])
    store.claim_upload_item(second["id"])
    terminal = store.fail_upload_item(second["id"], "영구 파싱 오류")

    assert terminal["status"] == "completed"
    assert terminal["completed"] == 1
    assert terminal["failed"] == 1
    assert terminal["progress"] == 52
    assert terminal["finished_at"] is not None
    assert store.list_upload_jobs()[0]["id"] == job["id"]
    assert store.active_upload_item_count() == 0


def test_project_search_parser_filter_and_recoverable_archive(tmp_path):
    store = Store(tmp_path / "project-filter.sqlite3")
    store.set_current_kcs_revision(SNAPSHOT, REVISION)
    _project(store, tmp_path, "pipe", "배관 특수시방서")
    _project(store, tmp_path, "steel", "철골 공사시방서")
    with store.connect() as connection:
        connection.execute("UPDATE projects SET parser_version = '' WHERE id = 'steel'")

    archived = store.set_project_archived("pipe", True)

    assert archived["is_archived"] is True
    assert [project["id"] for project in store.list_projects()] == ["steel"]
    assert {project["id"] for project in store.list_projects(include_archived=True)} == {
        "pipe",
        "steel",
    }
    assert [project["id"] for project in store.list_projects(
        search="배관", include_archived=True
    )] == ["pipe"]
    assert [project["id"] for project in store.list_projects(
        parser_status="legacy", include_archived=True
    )] == ["steel"]
    assert [project["id"] for project in store.list_projects(
        archive_status="archived"
    )] == ["pipe"]
    restored = store.set_project_archived("pipe", False)
    assert restored["is_archived"] is False
    assert {project["id"] for project in store.list_projects()} == {"pipe", "steel"}

    _project(store, tmp_path, "queued", "활성 신규 시방서")
    runs = store.publish_kcs_revision("snapshot-b", "upload-revision-b")
    runs_by_project = {run["project_id"]: run for run in runs}
    assert set(runs_by_project) == {"pipe", "queued"}
    store.set_project_archived("queued", True)
    assert store.pending_kcs_rematch_run_ids() == [runs_by_project["pipe"]["id"]]


@pytest.fixture
def upload_api(tmp_path, monkeypatch):
    test_settings = _settings(tmp_path)
    test_store = Store(test_settings.database_path)
    controls = {
        "snapshot": SNAPSHOT,
        "revision": REVISION,
        "embeddings": True,
        "parse_fail": set(),
        "match_calls": [],
    }

    class FakeClient:
        available = True
        embedding_model = "test-embedding"
        embedding_dimensions = 8

        @property
        def embeddings_available(self):
            return controls["embeddings"]

        def close(self):
            pass

    def fake_parse(_data, filename, project_id):
        if filename in controls["parse_fail"]:
            raise ValueError(f"{filename} 파싱 실패")
        return (
            Path(filename).stem,
            [
                {
                    "id": f"{project_id}-clause",
                    "source_order": 1,
                    "label": "1.1",
                    "title": filename,
                    "content": f"{filename} 고유 본문",
                    "source_type": "paragraph",
                    "outline_level": None,
                    "match_context": filename,
                    "candidates": [],
                }
            ],
            [],
        )

    def fake_match(clauses, _raw_dir, prefixes, client, *, require_embeddings=False):
        if require_embeddings and not client.embeddings_available:
            raise OpenAIAPIError("OpenAI 임베딩을 사용할 수 없습니다.")
        controls["match_calls"].append(
            (clauses[0]["id"], clauses[0]["content"], prefixes, require_embeddings)
        )
        for clause in clauses:
            clause["candidates"] = []
        return {"mode": "openai_embeddings", "require_embeddings": require_embeddings}

    monkeypatch.setattr(app_module, "settings", test_settings)
    monkeypatch.setattr(app_module, "store", test_store)
    monkeypatch.setattr(app_module, "ai_client", FakeClient())
    monkeypatch.setattr(app_module.OpenAIClient, "from_settings", lambda _settings: FakeClient())
    monkeypatch.setattr(
        app_module,
        "read_snapshot",
        lambda _path: (
            controls["snapshot"],
            {"revision": controls["revision"]},
        ),
    )
    monkeypatch.setattr(app_module, "parse_docx", fake_parse)
    monkeypatch.setattr(app_module, "match_clauses", fake_match)
    monkeypatch.setattr(
        app_module,
        "infer_scope",
        lambda _filename, _title, _sample, _raw_dir=None: (("9999",), "전체 KCS 동적 범위"),
    )
    monkeypatch.setattr(app_module, "_kick_upload_worker", lambda: None)
    monkeypatch.setattr(app_module, "_kick_kcs_rematch_worker", lambda: None)
    with TestClient(app_module.app) as client:
        yield client, test_store, test_settings, controls


def _post_batch(client: TestClient, *names: str):
    return client.post(
        "/api/upload-jobs",
        files=[
            ("files", (name, f"bytes-{name}".encode(), "application/octet-stream"))
            for name in names
        ],
    )


def test_async_batch_isolated_failure_does_not_block_later_file(upload_api):
    client, store, settings, controls = upload_api
    controls["parse_fail"].add("bad.docx")
    response = _post_batch(client, "bad.docx", "good.docx")

    assert response.status_code == 202
    queued = response.json()["job"]
    assert queued["status"] == "queued"
    assert queued["total"] == 2
    for item in queued["items"]:
        app_module._execute_upload_item(item["id"])

    job = client.get(f"/api/upload-jobs/{queued['id']}").json()["job"]
    assert job["status"] == "completed"
    assert job["completed"] == 1
    assert job["failed"] == 1
    assert [item["status"] for item in job["items"]] == ["failed", "completed"]
    assert len(store.list_projects()) == 1
    assert len(controls["match_calls"]) == 1
    assert controls["match_calls"][0][3] is True
    bad, good = job["items"]
    assert not (settings.uploads_dir / f"{bad['project_id']}.docx").exists()
    assert (settings.uploads_dir / f"{good['project_id']}.docx").exists()
    listed = client.get("/api/upload-jobs").json()["jobs"]
    assert listed[0]["id"] == queued["id"]


def test_batch_validation_failure_cleans_staged_sources(upload_api):
    client, store, settings, _controls = upload_api

    response = _post_batch(client, "valid.docx", "unsupported.txt")

    assert response.status_code == 400
    assert store.list_upload_jobs() == []
    assert list(settings.uploads_dir.iterdir()) == []


def test_async_embedding_failure_is_explicit_and_retryable(upload_api):
    client, store, settings, controls = upload_api
    controls["embeddings"] = False
    queued = _post_batch(client, "retry.docx").json()["job"]
    item = queued["items"][0]

    app_module._execute_upload_item(item["id"])
    failed = store.get_upload_job(queued["id"])

    assert failed["status"] == "failed"
    assert failed["items"][0]["retryable"] is True
    assert "임베딩" in failed["items"][0]["error"]
    assert (settings.uploads_dir / f"{item['project_id']}.docx").exists()
    retry = client.post(
        f"/api/upload-jobs/{queued['id']}/items/{item['id']}/retry"
    )
    assert retry.status_code == 202
    assert retry.json()["job"]["items"][0]["status"] == "queued"

    controls["embeddings"] = True
    app_module._execute_upload_item(item["id"])
    assert store.get_upload_job(queued["id"])["status"] == "completed"


def test_retry_route_wakes_worker_from_the_async_event_loop(upload_api, monkeypatch):
    client, store, _settings_value, controls = upload_api
    controls["embeddings"] = False
    queued = _post_batch(client, "retry-loop.docx").json()["job"]
    item = queued["items"][0]
    app_module._execute_upload_item(item["id"])
    loop_checks: list[bool] = []

    def verify_running_loop():
        asyncio.get_running_loop()
        loop_checks.append(True)

    monkeypatch.setattr(app_module, "_kick_upload_worker", verify_running_loop)
    response = client.post(
        f"/api/upload-jobs/{queued['id']}/items/{item['id']}/retry"
    )

    assert response.status_code == 202
    assert loop_checks == [True]
    assert store.get_upload_job(queued["id"])["items"][0]["status"] == "queued"


def test_kcs_refresh_is_blocked_while_an_upload_is_active(upload_api, monkeypatch):
    client, _store, _settings_value, _controls = upload_api
    _post_batch(client, "active.docx")

    def guarded_sync(_settings, _publish, preflight):
        preflight()
        raise AssertionError("active upload preflight should have stopped the sync")

    monkeypatch.setattr(app_module, "sync_kcs", guarded_sync)
    response = client.post("/api/kcs/sync")

    assert response.status_code == 400
    assert "분석 대기 또는 처리 중" in response.json()["detail"]


def test_batch_total_size_limit_cleans_already_staged_files(upload_api, monkeypatch):
    client, store, settings, _controls = upload_api
    monkeypatch.setattr(app_module, "MAX_BATCH_BYTES", 10)

    response = client.post(
        "/api/upload-jobs",
        files=[
            ("files", ("first.docx", b"123456", "application/octet-stream")),
            ("files", ("second.docx", b"123456", "application/octet-stream")),
        ],
    )

    assert response.status_code == 413
    assert store.list_upload_jobs() == []
    assert list(settings.uploads_dir.iterdir()) == []


def test_restoring_project_after_kcs_change_queues_rematch(upload_api, monkeypatch):
    client, store, settings, _controls = upload_api
    _project(store, settings.data_dir, "archived-stale")
    client.put("/api/projects/archived-stale/archive", json={"archived": True})
    assert store.publish_kcs_revision("snapshot-b", "upload-revision-b") == []
    kicks: list[bool] = []
    monkeypatch.setattr(app_module, "_kick_kcs_rematch_worker", lambda: kicks.append(True))

    response = client.put(
        "/api/projects/archived-stale/archive",
        json={"archived": False},
    )

    assert response.status_code == 200
    assert response.json()["project"]["kcs_rematch"]["status"] == "pending"
    assert len(store.pending_kcs_rematch_run_ids()) == 1
    assert kicks == [True]


def test_restoring_project_reactivates_existing_pending_rematch(upload_api, monkeypatch):
    client, store, settings, _controls = upload_api
    _project(store, settings.data_dir, "archived-pending")
    runs = store.publish_kcs_revision("snapshot-b", "upload-revision-b")
    pending_id = runs[0]["id"]
    client.put("/api/projects/archived-pending/archive", json={"archived": True})
    assert store.pending_kcs_rematch_run_ids() == []
    kicks: list[bool] = []
    monkeypatch.setattr(app_module, "_kick_kcs_rematch_worker", lambda: kicks.append(True))

    response = client.put(
        "/api/projects/archived-pending/archive",
        json={"archived": False},
    )

    assert response.status_code == 200
    assert response.json()["project"]["kcs_rematch"]["id"] == pending_id
    assert store.pending_kcs_rematch_run_ids() == [pending_id]
    assert kicks == [True]


def test_async_job_rejects_mixed_kcs_snapshot_and_cleans_source(upload_api):
    client, store, settings, controls = upload_api
    queued = _post_batch(client, "snapshot.docx").json()["job"]
    item = queued["items"][0]
    controls["snapshot"] = "2026-09-06T00:00:00+09:00"
    controls["revision"] = "upload-revision-b"

    app_module._execute_upload_item(item["id"])
    failed = store.get_upload_job(queued["id"])

    assert failed["status"] == "failed"
    assert failed["items"][0]["retryable"] is False
    assert "KCS 개정이 변경" in failed["items"][0]["error"]
    assert store.get_project(item["project_id"]) is None
    assert not (settings.uploads_dir / f"{item['project_id']}.docx").exists()


def test_legacy_single_upload_contract_remains_synchronous(upload_api):
    client, _store, _settings_value, controls = upload_api

    response = client.post(
        "/api/projects/upload",
        files={"file": ("single.docx", b"single", "application/octet-stream")},
    )

    assert response.status_code == 201
    assert response.json()["project"]["source_filename"] == "single.docx"
    assert len(response.json()["clauses"]) == 1
    assert controls["match_calls"][-1][3] is False

    project_id = response.json()["project"]["id"]
    assert client.get("/api/projects", params={"search": "single"}).json()[
        "projects"
    ][0]["id"] == project_id
    archived = client.put(
        f"/api/projects/{project_id}/archive",
        json={"archived": True},
    )
    assert archived.status_code == 200
    assert archived.json()["project"]["is_archived"] is True
    assert client.get("/api/projects").json()["projects"] == []
    assert client.get(
        "/api/projects", params={"archive_status": "archived"}
    ).json()["projects"][0]["id"] == project_id
    restored = client.put(
        f"/api/projects/{project_id}/archive",
        json={"archived": False},
    )
    assert restored.json()["project"]["is_archived"] is False
