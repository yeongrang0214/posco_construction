from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server.storage import CURRENT_PARSER_VERSION, Store


OLD_REVISION = "old-kcs-revision"
NEW_REVISION = "new-kcs-revision"
OLD_SNAPSHOT = "2026-09-01T00:00:00+09:00"
NEW_SNAPSHOT = "2026-09-05T00:00:00+09:00"


def _candidate(candidate_id: str, content: str) -> dict:
    return {
        "id": candidate_id,
        "rank": 1,
        "kcs_code": "KCS 41 31 05",
        "document_name": "건축물 강구조공사 일반사항",
        "version": "2026",
        "update_date": "2026-09-05",
        "kcs_clause": "3.2.1",
        "title": "표면처리",
        "content": content,
        "score": 0.91,
        "classification": "관련성 높음",
        "reasons": ["핵심 요구사항 일치"],
        "warnings": [],
    }


def _create_project(
    store: Store,
    project_id: str,
    *,
    parser_version: str = CURRENT_PARSER_VERSION,
) -> str:
    clause_id = f"{project_id}-clause"
    store.create_project(
        {
            "id": project_id,
            "title": "철골공사 시험 시방서",
            "source_filename": "건축_제13장_철골공사.docx",
            "source_path": "source.docx",
            "source_sha256": "source-hash",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "kcs_snapshot": OLD_SNAPSHOT,
            "kcs_revision": OLD_REVISION,
            "kcs_scope": "KCS 41 31",
            "parser_version": parser_version,
            "warning": "",
        },
        [
            {
                "id": clause_id,
                "source_order": 1,
                "label": "1.1",
                "title": "표면처리",
                "content": "강재 표면의 이물질을 제거한다.",
                "source_type": "paragraph",
                "outline_level": None,
                "match_context": "1 일반사항",
                "candidates": [_candidate("old-candidate", "기존 KCS 본문")],
            }
        ],
    )
    return clause_id


class _LocalOnlyClient:
    available = False
    embedding_model = "text-embedding-3-small"
    embedding_dimensions = 256

    def close(self) -> None:
        pass


@pytest.fixture
def rematch_client(tmp_path, monkeypatch):
    test_store = Store(tmp_path / "kcs-rematch-api.sqlite3")
    project_id = "current-parser-project"
    clause_id = _create_project(test_store, project_id)
    test_store.update_decision(
        project_id,
        clause_id,
        "hold",
        "",
        "개정 영향 확인 전 보류",
        "old-candidate",
        decision_reason="needs_expert_review",
    )
    queued = test_store.publish_kcs_revision(NEW_SNAPSHOT, NEW_REVISION)
    assert len(queued) == 1

    monkeypatch.setattr(app_module, "store", test_store)
    monkeypatch.setattr(app_module, "_kick_kcs_rematch_worker", lambda: None)
    monkeypatch.setattr(
        app_module,
        "read_snapshot",
        lambda _path: (NEW_SNAPSHOT, {"revision": NEW_REVISION}),
    )
    monkeypatch.setattr(
        app_module.OpenAIClient,
        "from_settings",
        lambda _settings: _LocalOnlyClient(),
    )

    def fake_match(clauses, _raw_dir, _prefixes, _client, *, require_embeddings=False):
        assert require_embeddings is True
        for clause in clauses:
            clause["candidates"] = [
                _candidate("temporary-new-id", "개정된 KCS 본문")
            ] if clause["source_type"] == "paragraph" else []
        return {
            "mode": "local",
            "require_embeddings": True,
            "embedding_model": "",
            "embedding_dimensions": None,
        }

    monkeypatch.setattr(app_module, "match_clauses", fake_match)
    with TestClient(app_module.app) as client:
        yield client, test_store, project_id, clause_id


def test_rematch_endpoint_worker_impact_and_atomic_acknowledgement(rematch_client):
    client, store, project_id, clause_id = rematch_client

    queued = client.post(f"/api/projects/{project_id}/kcs-rematch")
    assert queued.status_code == 200
    run_id = queued.json()["run"]["id"]
    assert queued.json()["run"]["status"] == "pending"

    app_module._execute_kcs_rematch(run_id)

    impact_response = client.get(f"/api/projects/{project_id}/kcs-impact")
    assert impact_response.status_code == 200
    payload = impact_response.json()
    assert payload["run"]["status"] == "completed"
    assert payload["run"]["unacknowledged_count"] == 1
    assert payload["impacts"][0]["clause_id"] == clause_id
    assert payload["impacts"][0]["review_required"] is True
    assert payload["impacts"][0]["reason"]

    acknowledged = client.patch(
        f"/api/projects/{project_id}/clauses/{clause_id}/quick-review",
        json={
            "decision": "hold",
            "decision_reason": "needs_expert_review",
            "selected_candidate_id": None,
            "expected_kcs_revision": NEW_REVISION,
            "impact_run_id": run_id,
            "acknowledge_kcs_impact": True,
        },
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["clause"]["kcs_impact"]["acknowledged_at"]
    assert store.get_kcs_rematch_run(run_id)["unacknowledged_count"] == 0


def test_acknowledgement_revision_conflict_rolls_back_decision(rematch_client):
    client, store, project_id, clause_id = rematch_client
    run_id = client.post(f"/api/projects/{project_id}/kcs-rematch").json()["run"]["id"]
    app_module._execute_kcs_rematch(run_id)
    before = store.get_clause(project_id, clause_id)

    response = client.patch(
        f"/api/projects/{project_id}/clauses/{clause_id}/quick-review",
        json={
            "decision": "keep",
            "decision_reason": "posco_specific",
            "expected_kcs_revision": "wrong-revision",
            "impact_run_id": run_id,
            "acknowledge_kcs_impact": True,
        },
    )

    assert response.status_code == 409
    after = store.get_clause(project_id, clause_id)
    assert after["decision"] == before["decision"]
    assert after["decision_reason"] == before["decision_reason"]
    assert after["kcs_impact"]["acknowledged_at"] is None


def test_legacy_parser_project_requires_one_time_reupload(tmp_path, monkeypatch):
    test_store = Store(tmp_path / "legacy-rematch-api.sqlite3")
    project_id = "legacy-parser-project"
    _create_project(test_store, project_id, parser_version="legacy-parser")
    test_store.publish_kcs_revision(NEW_SNAPSHOT, NEW_REVISION)
    monkeypatch.setattr(app_module, "store", test_store)
    monkeypatch.setattr(app_module, "_kick_kcs_rematch_worker", lambda: None)

    with TestClient(app_module.app) as client:
        response = client.post(f"/api/projects/{project_id}/kcs-rematch")
        project = client.get(f"/api/projects/{project_id}").json()["project"]

    assert response.status_code == 409
    assert "다시 업로드" in response.json()["detail"]
    assert project["requires_source_reupload"] is True
    assert project["kcs_rematch"] is None


def test_app_startup_requeues_interrupted_running_rematch(tmp_path, monkeypatch):
    test_store = Store(tmp_path / "startup-recovery.sqlite3")
    project_id = "interrupted-project"
    _create_project(test_store, project_id)
    run = test_store.publish_kcs_revision(NEW_SNAPSHOT, NEW_REVISION)[0]
    test_store.claim_kcs_rematch(run["id"], {"mode": "interrupted"})
    monkeypatch.setattr(app_module, "store", test_store)
    monkeypatch.setattr(app_module, "_kick_kcs_rematch_worker", lambda: None)

    with TestClient(app_module.app) as client:
        response = client.get(f"/api/projects/{project_id}/kcs-impact")

    assert response.status_code == 200
    recovered = test_store.get_kcs_rematch_run(run["id"])
    assert recovered["status"] == "pending"
    assert recovered["expected_state_sha256"] == ""
    assert recovered["matcher_signature"] == {}
