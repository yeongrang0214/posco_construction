from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server.storage import CURRENT_PARSER_VERSION, Store


SNAPSHOT = "2026-09-06T00:00:00+09:00"
REVISION = "catalog-revision"
UPLOADED_AT = "2026-09-06T01:00:00+00:00"


def _seed_catalog(store: Store, tmp_path: Path, count: int = 125) -> list[str]:
    projects = []
    clauses = []
    rematch_runs = []
    impacts = []
    for index in range(count):
        project_id = f"project-{index:03d}"
        title = (
            "중복 시방서"
            if index in {23, 74, 115, 124}
            else f"프로젝트 {index:03d}"
        )
        review_status = (
            "submitted"
            if index % 3 == 0
            else "changes_requested"
            if index % 3 == 1
            else "reviewing"
        )
        projects.append(
            (
                project_id,
                title,
                f"{title}.docx",
                str(tmp_path / f"{project_id}.docx"),
                f"sha-{project_id}",
                UPLOADED_AT,
                SNAPSHOT,
                REVISION,
                "KCS 시험 범위",
                "" if index % 4 == 0 else CURRENT_PARSER_VERSION,
                UPLOADED_AT if index % 5 == 0 else None,
                review_status,
            )
        )
        clause_id = f"{project_id}-clause"
        clauses.append(
            (
                clause_id,
                project_id,
                1,
                "1.1",
                "시험 조항",
                "시험 기준을 적용한다.",
                "paragraph",
                title,
            )
        )
        if index % 7 == 0:
            run_id = f"run-{index:03d}"
            rematch_runs.append(
                (
                    run_id,
                    project_id,
                    "previous-revision",
                    REVISION,
                    SNAPSHOT,
                    "completed",
                    UPLOADED_AT,
                    UPLOADED_AT,
                )
            )
            impacts.append((run_id, clause_id, "candidate_added", 1))

    with store.connect() as connection:
        connection.executemany(
            """
            INSERT INTO projects(
                id, title, source_filename, source_path, source_sha256,
                uploaded_at, kcs_snapshot, kcs_revision, kcs_scope,
                parser_version, archived_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            projects,
        )
        connection.executemany(
            """
            INSERT INTO clauses(
                id, project_id, source_order, label, title, content,
                source_type, match_context
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            clauses,
        )
        connection.executemany(
            """
            INSERT INTO kcs_rematch_runs(
                id, project_id, from_revision, target_revision,
                target_snapshot, status, queued_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rematch_runs,
        )
        connection.executemany(
            """
            INSERT INTO kcs_clause_impacts(
                run_id, clause_id, impact_type, review_required
            ) VALUES (?, ?, ?, ?)
            """,
            impacts,
        )
    return [project[0] for project in projects]


@pytest.fixture
def catalog_api(tmp_path, monkeypatch):
    store = Store(tmp_path / "catalog.sqlite3")
    store.set_current_kcs_revision(SNAPSHOT, REVISION)
    project_ids = _seed_catalog(store, tmp_path)
    monkeypatch.setattr(app_module, "store", store)
    client = TestClient(app_module.app)
    yield client, store, project_ids
    client.close()


def test_catalog_cursor_pages_125_projects_without_gaps_or_duplicates(catalog_api):
    client, _store, project_ids = catalog_api
    expected_ids = sorted(project_ids, reverse=True)
    received = []
    cursor = ""
    page_sizes = []

    while True:
        response = client.get(
            "/api/projects",
            params={"include_archived": True, "limit": 50, "cursor": cursor},
        )
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"projects", "total", "limit", "has_more", "next_cursor"}
        assert payload["total"] == 125
        assert payload["limit"] == 50
        page_sizes.append(len(payload["projects"]))
        received.extend(project["id"] for project in payload["projects"])
        if not payload["has_more"]:
            assert payload["next_cursor"] is None
            break
        assert payload["next_cursor"]
        cursor = payload["next_cursor"]

    assert page_sizes == [50, 50, 25]
    assert received == expected_ids
    assert len(received) == len(set(received)) == 125


def test_catalog_filters_totals_and_global_latest_metadata(catalog_api):
    client, _store, _project_ids = catalog_api

    active = client.get("/api/projects", params={"limit": 100}).json()
    assert active["total"] == 100
    assert active["has_more"] is False

    archived = client.get(
        "/api/projects", params={"archive_status": "archived", "limit": 100}
    ).json()
    assert archived["total"] == 25
    assert all(project["is_archived"] for project in archived["projects"])

    legacy = client.get(
        "/api/projects",
        params={"parser_status": "legacy", "include_archived": True, "limit": 100},
    ).json()
    current = client.get(
        "/api/projects",
        params={"parser_status": "current", "include_archived": True, "limit": 100},
    ).json()
    assert legacy["total"] == 32
    assert current["total"] == 93

    duplicates = client.get(
        "/api/projects",
        params={"search": "중복", "include_archived": True, "limit": 100},
    ).json()
    assert duplicates["total"] == 4
    duplicate_by_id = {project["id"]: project for project in duplicates["projects"]}
    assert duplicate_by_id["project-074"]["duplicate_title_count"] == 2
    assert duplicate_by_id["project-023"]["duplicate_title_count"] == 2
    assert duplicate_by_id["project-074"]["is_latest_for_title"] is True
    assert duplicate_by_id["project-023"]["is_latest_for_title"] is False
    assert duplicate_by_id["project-124"]["duplicate_title_count"] == 0
    assert duplicate_by_id["project-124"]["is_latest_for_title"] is False
    assert duplicate_by_id["project-115"]["duplicate_title_count"] == 0
    assert duplicate_by_id["project-115"]["is_latest_for_title"] is False
    assert all("source_path" not in project for project in duplicates["projects"])

    all_impacts = client.get(
        "/api/projects",
        params={"include_archived": True, "kcs_impact_only": True, "limit": 100},
    ).json()
    active_impacts = client.get(
        "/api/projects", params={"kcs_impact_only": True, "limit": 100}
    ).json()
    archived_impacts = client.get(
        "/api/projects",
        params={
            "archive_status": "archived",
            "kcs_impact_only": True,
            "limit": 100,
        },
    ).json()
    assert all_impacts["total"] == 18
    assert active_impacts["total"] == 14
    assert archived_impacts["total"] == 4
    assert all(
        project["unacknowledged_kcs_impact_count"] == 1
        for project in all_impacts["projects"]
    )


def test_project_detail_keeps_global_latest_metadata_when_project_is_off_page(
    catalog_api,
):
    client, store, _project_ids = catalog_api
    first_page = client.get(
        "/api/projects", params={"include_archived": True, "limit": 50}
    ).json()
    assert "project-074" not in {project["id"] for project in first_page["projects"]}

    detail = client.get("/api/projects/project-074")
    assert detail.status_code == 200
    project = detail.json()["project"]
    assert project["duplicate_title_count"] == 2
    assert project["is_latest_for_title"] is True

    older_current = store.get_project("project-023")
    newer_archived = store.get_project("project-115")
    newer_legacy = store.get_project("project-124")
    assert older_current["duplicate_title_count"] == 2
    assert older_current["is_latest_for_title"] is False
    assert newer_archived["duplicate_title_count"] == 0
    assert newer_archived["is_latest_for_title"] is False
    assert newer_legacy["duplicate_title_count"] == 0
    assert newer_legacy["is_latest_for_title"] is False


def test_catalog_rejects_malformed_and_filter_mismatched_cursors(catalog_api):
    client, _store, _project_ids = catalog_api

    malformed = client.get("/api/projects", params={"cursor": "not-a-cursor"})
    assert malformed.status_code == 400
    assert "커서" in malformed.json()["detail"]

    first = client.get(
        "/api/projects", params={"include_archived": True, "limit": 10}
    ).json()
    mismatch = client.get(
        "/api/projects",
        params={"include_archived": False, "limit": 10, "cursor": first["next_cursor"]},
    )
    assert mismatch.status_code == 400
    assert "검색 조건" in mismatch.json()["detail"]


def test_catalog_filters_workflow_queues_and_binds_cursor_to_status(catalog_api):
    client, _store, _project_ids = catalog_api

    submitted_ids: list[str] = []
    cursor = ""
    while True:
        response = client.get(
            "/api/projects",
            params={
                "include_archived": True,
                "review_status": "submitted",
                "limit": 20,
                "cursor": cursor,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 42
        assert all(project["status"] == "submitted" for project in payload["projects"])
        submitted_ids.extend(project["id"] for project in payload["projects"])
        if not payload["has_more"]:
            break
        cursor = payload["next_cursor"]
    assert len(submitted_ids) == len(set(submitted_ids)) == 42

    active_submitted = client.get(
        "/api/projects",
        params={"archive_status": "active", "review_status": "submitted", "limit": 100},
    ).json()
    archived_submitted = client.get(
        "/api/projects",
        params={"archive_status": "archived", "review_status": "submitted", "limit": 100},
    ).json()
    assert active_submitted["total"] == 33
    assert archived_submitted["total"] == 9

    requested_duplicates = client.get(
        "/api/projects",
        params={
            "search": "중복",
            "include_archived": True,
            "review_status": "changes_requested",
            "limit": 100,
        },
    ).json()
    assert requested_duplicates["total"] == 2
    assert {project["id"] for project in requested_duplicates["projects"]} == {
        "project-115",
        "project-124",
    }

    first = client.get(
        "/api/projects",
        params={"include_archived": True, "review_status": "submitted", "limit": 10},
    ).json()
    mismatched_cursor = client.get(
        "/api/projects",
        params={
            "include_archived": True,
            "review_status": "changes_requested",
            "limit": 10,
            "cursor": first["next_cursor"],
        },
    )
    assert mismatched_cursor.status_code == 400
    assert "검색 조건" in mismatched_cursor.json()["detail"]

    invalid = client.get("/api/projects", params={"review_status": "waiting"})
    assert invalid.status_code == 422


def test_catalog_page_uses_constant_sql_statement_count(catalog_api, monkeypatch):
    _client, store, _project_ids = catalog_api
    statements: list[str] = []
    original_connect = store.connect

    @contextmanager
    def traced_connect():
        with original_connect() as connection:
            connection.set_trace_callback(statements.append)
            try:
                yield connection
            finally:
                connection.set_trace_callback(None)

    monkeypatch.setattr(store, "connect", traced_connect)

    def select_count(limit: int) -> int:
        statements.clear()
        page = store.list_projects_page(include_archived=True, limit=limit)
        assert len(page["projects"]) == limit
        return sum(
            statement.lstrip().upper().startswith(("SELECT", "WITH"))
            for statement in statements
        )

    assert select_count(1) == select_count(100) == 3
    with original_connect() as connection:
        indexes = {
            row["name"] for row in connection.execute("PRAGMA index_list(projects)")
        }
    assert "idx_projects_catalog" in indexes
