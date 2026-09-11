import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server.storage import Store
from server.tests.test_ai_api import _seed


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path / "rename.sqlite3")
    project_id, clause_id, _ = _seed(store, tmp_path)
    with store.connect() as connection:
        connection.execute(
            "UPDATE clauses SET decision = 'keep', review_note = ? WHERE id = ?",
            ("기존 담당자 의견", clause_id),
        )
    monkeypatch.setattr(app_module, "store", store)
    return TestClient(app_module.app), store, project_id, clause_id


def test_rename_preserves_source_candidates_and_review(setup):
    client, store, project_id, clause_id = setup
    before = store.get_project(project_id)
    clause = store.get_clause(project_id, clause_id)
    response = client.patch(f"/api/projects/{project_id}/title", json={"title": " 조적공사 "})
    assert response.status_code == 200
    assert response.json()["project"]["title"] == "조적공사"
    after = store.get_project(project_id)
    for key in ("source_path", "source_filename", "source_sha256", "kcs_revision", "parser_version", "status"):
        assert after[key] == before[key]
    assert store.get_clause(project_id, clause_id) == clause
    assert client.get("/api/projects", params={"search": "조적공사"}).json()["total"] == 1


@pytest.mark.parametrize("title,code", [("", 422), ("  ", 400), ("가" * 201, 422)])
def test_invalid_title_does_not_change_project(setup, title, code):
    client, store, project_id, _ = setup
    before = store.get_project(project_id)["title"]
    response = client.patch(f"/api/projects/{project_id}/title", json={"title": title})
    assert response.status_code == code
    assert store.get_project(project_id)["title"] == before


def test_missing_project_is_not_created(setup):
    client, store, _, _ = setup
    assert client.patch("/api/projects/missing/title", json={"title": "조적공사"}).status_code == 404
    assert store.get_project("missing") is None
