from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server.storage import Store


REVISION = "structure-api-revision"


def _clause(
    clause_id: str,
    order: int,
    content: str,
    *,
    source_type: str = "paragraph",
) -> dict:
    return {
        "id": clause_id,
        "source_order": order,
        "label": str(order),
        "title": content[:30],
        "content": content,
        "source_type": source_type,
        "outline_level": 1 if source_type == "heading" else None,
        "match_context": "1 일반사항",
        "candidates": [],
    }


def _fake_match(clauses, _raw_dir, _prefixes, _ai_client):
    for clause in clauses:
        candidate_id = str(
            uuid.uuid5(uuid.UUID(clause["id"]), "KCS 41 31 05:3.2.1:2026:1")
        )
        clause["candidates"] = [
            {
                "id": candidate_id,
                "rank": 1,
                "kcs_code": "KCS 41 31 05",
                "document_name": "건축물 강구조공사 일반사항",
                "version": "2026",
                "update_date": "2026-08-01",
                "kcs_clause": "3.2.1",
                "title": "표면처리",
                "content": "KCS 재매칭 본문",
                "score": 0.82,
                "classification": "관련성 높음",
                "reasons": ["공통 핵심어"],
                "warnings": [],
            }
        ]


@pytest.fixture
def structure_client(tmp_path, monkeypatch):
    test_store = Store(tmp_path / "structure-api.sqlite3")
    project_id = "structure-api-project"
    clauses = [
        _clause("heading", 1, "1 일반사항", source_type="heading"),
        _clause(str(uuid.uuid4()), 2, "첫 문장의 요구사항이다."),
        _clause(str(uuid.uuid4()), 3, " 둘째 문장의 요구사항이다."),
        _clause(str(uuid.uuid4()), 4, "마지막 요구사항이다."),
    ]
    test_store.create_project(
        {
            "id": project_id,
            "title": "구조편집 API 시험",
            "source_filename": "건축_제13장_철골공사.docx",
            "source_path": str(tmp_path / "source.docx"),
            "source_sha256": "source-hash",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "kcs_snapshot": "2026-09-05T10:00:00+09:00",
            "kcs_revision": REVISION,
            "kcs_scope": "KCS 41 31, KCS 14 31",
            "warning": "",
        },
        clauses,
    )
    monkeypatch.setattr(app_module, "store", test_store)
    monkeypatch.setattr(
        app_module,
        "read_snapshot",
        lambda _path: (
            "2026-09-05T10:00:00+09:00",
            {"revision": REVISION},
        ),
    )
    monkeypatch.setattr(app_module, "match_clauses", _fake_match)
    with TestClient(app_module.app) as client:
        yield client, test_store, project_id, clauses


def test_merge_endpoint_rematches_and_returns_replacement(structure_client):
    client, store, project_id, clauses = structure_client
    source_ids = [clauses[1]["id"], clauses[2]["id"]]

    response = client.post(
        f"/api/projects/{project_id}/clauses/merge",
        json={"clause_ids": source_ids},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_clause_id"] not in source_ids
    assert [item["source_order"] for item in payload["clauses"]] == [1, 2, 3]
    assert payload["project"]["total_clauses"] == 3
    active = client.get(
        f"/api/projects/{project_id}/clauses/{payload['active_clause_id']}"
    ).json()["clause"]
    assert active["content"] == "첫 문장의 요구사항이다.\n3 둘째 문장의 요구사항이다."
    assert active["decision"] is None
    assert len(active["candidates"]) == 1
    assert all(store.get_clause(project_id, source_id) is None for source_id in source_ids)
    assert store.list_clause_structure_history(project_id)[0]["operation"] == "merge"


def test_split_endpoint_preserves_exact_text_and_rematches_both_parts(structure_client):
    client, store, project_id, clauses = structure_client
    clause_id = clauses[1]["id"]
    parts = ["첫 문장의 ", "요구사항이다."]

    response = client.post(
        f"/api/projects/{project_id}/clauses/{clause_id}/split",
        json={"parts": parts},
    )

    assert response.status_code == 200
    payload = response.json()
    replacement_ids = store.list_clause_structure_history(project_id)[0][
        "replacement_clause_ids"
    ]
    assert len(replacement_ids) == 2
    assert payload["active_clause_id"] == replacement_ids[0]
    replacements = [store.get_clause(project_id, item_id) for item_id in replacement_ids]
    assert [item["content"] for item in replacements] == parts
    assert all(len(item["candidates"]) == 1 for item in replacements)
    assert [item["source_order"] for item in payload["clauses"]] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("guard", ["stale", "quality", "decision"])
def test_merge_endpoint_returns_409_for_protected_project_state(
    structure_client,
    guard,
):
    client, store, project_id, clauses = structure_client
    source_ids = [clauses[1]["id"], clauses[2]["id"]]
    if guard == "stale":
        store.set_current_kcs_revision(
            "2026-09-06T10:00:00+09:00",
            "new-revision",
        )
    elif guard == "quality":
        store.ensure_quality_evaluation(project_id)
    else:
        store.update_decision(
            project_id,
            source_ids[0],
            "keep",
            "",
            "",
            None,
            decision_reason="posco_specific",
        )

    response = client.post(
        f"/api/projects/{project_id}/clauses/merge",
        json={"clause_ids": source_ids},
    )

    assert response.status_code == 409
    assert all(store.get_clause(project_id, source_id) for source_id in source_ids)
    assert store.list_clause_structure_history(project_id) == []


def test_matcher_exception_leaves_original_clauses_untouched(
    structure_client,
    monkeypatch,
):
    client, store, project_id, clauses = structure_client
    source_ids = [clauses[1]["id"], clauses[2]["id"]]

    def fail_match(*_args, **_kwargs):
        raise RuntimeError("의도한 재매칭 실패")

    monkeypatch.setattr(app_module, "match_clauses", fail_match)
    response = client.post(
        f"/api/projects/{project_id}/clauses/merge",
        json={"clause_ids": source_ids},
    )

    assert response.status_code == 409
    assert "재매칭 실패" in response.json()["detail"]
    assert all(store.get_clause(project_id, source_id) for source_id in source_ids)
    assert store.list_clause_structure_history(project_id) == []
