from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
import server.storage as storage_module
from server.storage import Store


def _candidate(clause_id: str, rank: int, score: float = 0.5) -> dict:
    return {
        "id": f"{clause_id}-candidate-{rank}",
        "rank": rank,
        "kcs_code": "KCS 41 31 05",
        "document_name": "건축물 강구조공사 일반사항",
        "version": "2026",
        "update_date": "2026-08-01",
        "kcs_clause": f"3.2.{rank}",
        "title": f"KCS 후보 {rank}",
        "content": f"KCS 후보 본문 {rank}",
        "score": score,
        "classification": "관련성 높음" if score >= 0.45 else "검토 필요",
        "reasons": [f"후보 {rank} 공통 핵심어"],
        "warnings": [f"후보 {rank} 수치 확인"],
    }


def _clause(
    clause_id: str,
    order: int,
    *,
    label: str | None = None,
    title: str | None = None,
    content: str | None = None,
    source_type: str = "paragraph",
    candidates: list[dict] | None = None,
) -> dict:
    return {
        "id": clause_id,
        "source_order": order,
        "label": label or str(order),
        "title": title or f"조항 {order}",
        "content": content or f"포스코 원문 {order}",
        "source_type": source_type,
        "outline_level": 1 if source_type == "heading" else None,
        "candidates": candidates or [],
    }


@pytest.fixture
def bulk_client(tmp_path, monkeypatch):
    test_store = Store(tmp_path / "bulk.sqlite3")
    project_id = "bulk-project"
    clauses = [
        _clause("heading", 1, title="1. 일반사항", source_type="heading"),
        _clause(
            "unreviewed-match",
            2,
            label="1.1",
            title="표면 처리",
            candidates=[_candidate("unreviewed-match", rank) for rank in range(1, 5)],
        ),
        _clause("keep-clause", 3, label="2.1", title="용접 기준"),
        _clause(
            "delete-clause",
            4,
            label="3.1",
            content="삭제 검토 대상",
            candidates=[{**_candidate("delete-clause", 1), "warnings": []}],
        ),
        _clause("hold-clause", 5, title="검사 보류"),
        _clause("search-clause", 6, title="특별 방수", content="100% 특별 방수 기준"),
    ]
    test_store.create_project(
        {
            "id": project_id,
            "title": "일괄 검토 시험",
            "source_filename": "bulk.docx",
            "source_path": str(tmp_path / "bulk.docx"),
            "source_sha256": "secret-source-hash",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "kcs_snapshot": "2026-09-04T10:00:00+09:00",
            "kcs_revision": "test-revision",
            "kcs_scope": "KCS 41 31",
            "warning": "",
        },
        clauses,
    )
    test_store.update_decision(
        project_id, "keep-clause", "keep", "", "남김", None,
        decision_reason="posco_specific",
    )
    test_store.update_decision(
        project_id, "delete-clause", "delete", "", "삭제", "delete-clause-candidate-1",
        decision_reason="fully_covered_by_kcs", coverage_confirmed=True,
    )
    test_store.update_decision(
        project_id, "hold-clause", "hold", "보류 수정문", "보류", None,
        decision_reason="needs_expert_review",
    )
    test_store.save_candidate_analysis(
        project_id,
        "unreviewed-match",
        "unreviewed-match-candidate-1",
        {
            "relation_type": "partial_overlap",
            "confidence": 0.77,
            "rationale": "일부 요구사항이 일치합니다.",
            "simplified_content": "사내 조건",
        },
        "test-model",
    )
    test_store.save_candidate_analysis(
        project_id,
        "unreviewed-match",
        "unreviewed-match-candidate-2",
        {
            "relation_type": "unrelated",
            "confidence": 0.9,
            "rationale": "적용 대상이 다릅니다.",
            "simplified_content": "",
        },
        "test-model",
    )
    monkeypatch.setattr(app_module, "store", test_store)
    with TestClient(app_module.app) as client:
        yield client, test_store, project_id


def test_bulk_review_paginates_in_source_order_and_batches_candidates(
    bulk_client, monkeypatch
):
    client, _, project_id = bulk_client
    statements: list[str] = []
    original_connect = storage_module.sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(storage_module.sqlite3, "connect", traced_connect)
    response = client.get(
        f"/api/projects/{project_id}/bulk-review",
        params={"offset": 0, "limit": 2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 6
    assert payload["offset"] == 0
    assert payload["limit"] == 2
    assert payload["has_more"] is True
    assert [item["id"] for item in payload["items"]] == ["heading", "unreviewed-match"]
    assert payload["items"][0]["source_type"] == "heading"
    assert payload["items"][0]["source_context"]["previous"] is None
    assert payload["items"][0]["source_context"]["next"]["label"] == "1.1"
    assert payload["items"][1]["source_context"]["previous"]["label"] == "1"
    assert payload["items"][1]["source_context"]["next"]["label"] == "2.1"
    assert payload["items"][0]["excluded_candidate_count"] == 0
    assert payload["items"][1]["excluded_candidate_count"] == 1
    candidates = payload["items"][1]["candidates"]
    assert [candidate["rank"] for candidate in candidates] == [1, 3, 4]
    assert candidates[0]["reasons"] == ["후보 1 공통 핵심어"]
    assert candidates[0]["warnings"] == ["후보 1 수치 확인"]
    assert candidates[0]["ai_analysis"]["relation_type"] == "partial_overlap"
    assert candidates[1]["ai_analysis"] is None
    assert sum("WITH filtered AS" in statement for statement in statements) == 1
    assert sum("SELECT k.*, ai.relation_type" in statement for statement in statements) == 1

    last_page = client.get(
        f"/api/projects/{project_id}/bulk-review", params={"offset": 5, "limit": 2}
    ).json()
    assert [item["id"] for item in last_page["items"]] == ["search-clause"]
    assert last_page["has_more"] is False

    beyond = client.get(
        f"/api/projects/{project_id}/bulk-review", params={"offset": 99, "limit": 2}
    ).json()
    assert beyond["items"] == []
    assert beyond["total"] == 6
    assert beyond["has_more"] is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("unreviewed", ["heading", "unreviewed-match", "search-clause"]),
        ("keep", ["keep-clause"]),
        ("delete", ["delete-clause"]),
        ("hold", ["hold-clause"]),
    ],
)
def test_bulk_review_filters_by_status(bulk_client, status, expected):
    client, _, project_id = bulk_client

    response = client.get(
        f"/api/projects/{project_id}/bulk-review", params={"status": status}
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["items"]] == expected
    assert payload["total"] == len(expected)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("3.1", ["delete-clause"]),
        ("용접", ["keep-clause"]),
        ("특별", ["search-clause"]),
        ("%", ["search-clause"]),
        ("없는 검색어", []),
    ],
)
def test_bulk_review_searches_label_title_and_content_literally(bulk_client, query, expected):
    client, _, project_id = bulk_client

    response = client.get(
        f"/api/projects/{project_id}/bulk-review", params={"q": query}
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == expected
    assert response.json()["total"] == len(expected)


@pytest.mark.parametrize(
    "params",
    [
        {"offset": -1},
        {"limit": 0},
        {"limit": 201},
        {"status": "finished"},
        {"q": "가" * 201},
    ],
)
def test_bulk_review_validates_query_parameters(bulk_client, params):
    client, _, project_id = bulk_client
    response = client.get(f"/api/projects/{project_id}/bulk-review", params=params)
    assert response.status_code == 422


def test_bulk_review_returns_404_without_private_project_fields(bulk_client):
    client, _, project_id = bulk_client

    missing = client.get("/api/projects/missing-project/bulk-review")
    response = client.get(f"/api/projects/{project_id}/bulk-review")

    assert missing.status_code == 404
    assert response.status_code == 200
    text = response.text
    assert "source_path" not in text
    assert "source_sha256" not in text
    assert "secret-source-hash" not in text
    assert all(
        set(item) == {
            "id",
            "source_order",
            "label",
            "title",
            "content",
            "source_type",
            "outline_level",
            "decision",
            "edited_content",
            "review_note",
            "decision_reason",
            "coverage_confirmed",
            "selected_candidate_id",
            "reviewed_at",
            "candidates",
            "excluded_candidate_count",
            "source_context",
        }
        for item in response.json()["items"]
    )


def test_document_map_returns_source_linking_fields_without_private_paths(bulk_client):
    client, _, project_id = bulk_client

    response = client.get(f"/api/projects/{project_id}/document-map")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == [
        "heading",
        "unreviewed-match",
        "keep-clause",
        "delete-clause",
        "hold-clause",
        "search-clause",
    ]
    assert set(items[0]) == {
        "id", "source_order", "label", "title", "content", "source_type", "decision"
    }
    assert "source_path" not in response.text


def test_source_docx_endpoint_serves_only_files_inside_upload_root(bulk_client, tmp_path, monkeypatch):
    client, test_store, project_id = bulk_client
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    source = upload_root / f"{project_id}.docx"
    source.write_bytes(b"preview-docx")
    with test_store.connect() as connection:
        connection.execute(
            "UPDATE projects SET source_path = ? WHERE id = ?",
            (str(source), project_id),
        )
    monkeypatch.setattr(app_module, "settings", replace(app_module.settings, uploads_dir=upload_root))

    response = client.get(f"/api/projects/{project_id}/source.docx")

    assert response.status_code == 200
    assert response.content == b"preview-docx"
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert response.headers["cache-control"] == "private, no-store"

    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"secret")
    with test_store.connect() as connection:
        connection.execute(
            "UPDATE projects SET source_path = ? WHERE id = ?",
            (str(outside), project_id),
        )
    blocked = client.get(f"/api/projects/{project_id}/source.docx")
    assert blocked.status_code == 404
    assert "outside" not in blocked.text


def test_pdf_preview_pending_ready_and_path_boundary(bulk_client, tmp_path, monkeypatch):
    import json
    client, test_store, project_id = bulk_client
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    source = upload_root / "source.docx"
    source.write_bytes(b"source")
    with test_store.connect() as connection:
        connection.execute("UPDATE projects SET source_path = ? WHERE id = ?", (str(source), project_id))
    monkeypatch.setattr(app_module, "settings", replace(app_module.settings, uploads_dir=upload_root, data_dir=tmp_path))
    pdf = tmp_path / "cached.pdf"
    monkeypatch.setattr(app_module, "request_preview", lambda *_: ("processing", pdf))
    assert client.get(f"/api/projects/{project_id}/source-preview").json()["status"] == "processing"
    assert client.get(f"/api/projects/{project_id}/source.pdf").status_code == 409
    pdf.write_bytes(b"%PDF-1.7 test")
    pdf.with_suffix(".json").write_text(json.dumps({"pages": [{"page": 1, "width": 595, "height": 842, "chars": []}]}), encoding="utf-8")
    monkeypatch.setattr(app_module, "request_preview", lambda *_: ("ready", pdf))
    payload = client.get(f"/api/projects/{project_id}/source-preview").json()
    assert payload["status"] == "ready" and "chars" not in payload["pages"][0]
    response = client.get(f"/api/projects/{project_id}/source.pdf")
    assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
    with test_store.connect() as connection:
        connection.execute("UPDATE projects SET source_path = ? WHERE id = ?", (str(pdf), project_id))
    assert client.get(f"/api/projects/{project_id}/source-preview").status_code == 404
    assert client.get(f"/api/projects/{project_id}/source.pdf").status_code == 404


def test_title_only_candidate_cannot_be_used_as_delete_evidence(bulk_client):
    _, test_store, project_id = bulk_client
    with test_store.connect() as connection:
        connection.execute(
            """
            UPDATE candidates
            SET content = title, warnings_json = '[]'
            WHERE id = 'delete-clause-candidate-1'
            """
        )

    with pytest.raises(ValueError, match="제목만 있는 KCS 후보"):
        test_store.update_decision(
            project_id,
            "delete-clause",
            "delete",
            "",
            "삭제",
            "delete-clause-candidate-1",
            decision_reason="fully_covered_by_kcs",
            coverage_confirmed=True,
        )


def test_quick_review_merges_only_fields_actually_sent(bulk_client):
    client, _, project_id = bulk_client
    clause_id = "unreviewed-match"
    candidate_id = "unreviewed-match-candidate-1"
    detailed = client.patch(
        f"/api/projects/{project_id}/clauses/{clause_id}",
        json={
            "decision": "hold",
            "decision_reason": "needs_expert_review",
            "edited_content": "다른 탭에서 저장한 최신 수정문",
            "review_note": "다른 탭에서 저장한 최신 메모",
            "selected_candidate_id": candidate_id,
        },
    )
    assert detailed.status_code == 200

    quick = client.patch(
        f"/api/projects/{project_id}/clauses/{clause_id}/quick-review",
        json={
            "decision": "keep",
            "decision_reason": "posco_specific",
            "edited_content": "일괄 화면의 오래된 수정문",
            "review_note": "일괄 화면의 오래된 메모",
        },
    )

    assert quick.status_code == 200
    clause = quick.json()["clause"]
    assert clause["decision"] == "keep"
    assert clause["edited_content"] == "다른 탭에서 저장한 최신 수정문"
    assert clause["review_note"] == "다른 탭에서 저장한 최신 메모"
    assert clause["selected_candidate_id"] == candidate_id
    assert "source_path" not in quick.json()["project"]
    assert "source_sha256" not in quick.json()["project"]

    no_match = client.patch(
        f"/api/projects/{project_id}/clauses/{clause_id}/quick-review",
        json={
            "decision": "keep",
            "decision_reason": "no_kcs_match",
        },
    )
    assert no_match.status_code == 200
    clause = no_match.json()["clause"]
    assert clause["selected_candidate_id"] is None
    assert clause["decision_reason"] == "no_kcs_match"

    clear_selection = client.patch(
        f"/api/projects/{project_id}/clauses/{clause_id}/quick-review",
        json={"selected_candidate_id": None},
    )
    assert clear_selection.status_code == 200
    clause = clear_selection.json()["clause"]
    assert clause["decision"] == "keep"
    assert clause["selected_candidate_id"] is None
    assert clause["edited_content"] == "다른 탭에서 저장한 최신 수정문"
    assert clause["review_note"] == "다른 탭에서 저장한 최신 메모"

    clear_decision = client.patch(
        f"/api/projects/{project_id}/clauses/{clause_id}/quick-review",
        json={"decision": None},
    )
    assert clear_decision.status_code == 200
    assert clear_decision.json()["clause"]["decision"] is None
    assert clear_decision.json()["clause"]["edited_content"] == "다른 탭에서 저장한 최신 수정문"


def test_quick_review_rejects_empty_invalid_and_missing_targets(bulk_client):
    client, _, project_id = bulk_client
    base = f"/api/projects/{project_id}/clauses/unreviewed-match/quick-review"

    assert client.patch(base, json={}).status_code == 400
    assert client.patch(base, json={"edited_content": "무시될 값"}).status_code == 400
    assert client.patch(base, json={"decision": "finished"}).status_code == 422
    assert client.patch(base, json={"selected_candidate_id": ""}).status_code == 422

    hidden = client.patch(
        base,
        json={"selected_candidate_id": "unreviewed-match-candidate-2"},
    )
    assert hidden.status_code == 400
    assert "관련성 낮음" in hidden.json()["detail"]

    nonexistent = client.patch(base, json={"selected_candidate_id": "not-a-candidate"})
    assert nonexistent.status_code == 400
    assert client.patch(
        f"/api/projects/{project_id}/clauses/missing-clause/quick-review",
        json={"decision": "keep"},
    ).status_code == 404
    assert client.patch(
        "/api/projects/missing-project/clauses/missing-clause/quick-review",
        json={"decision": "keep"},
    ).status_code == 404
