from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from server.storage import (
    ClauseStructureConflictError,
    ClauseStructureNotFoundError,
    Store,
)


REVISION = "structure-test-revision"


def _candidate(clause_id: str, rank: int = 1) -> dict:
    return {
        "id": str(uuid.uuid5(uuid.UUID(clause_id), f"candidate-{rank}")),
        "rank": rank,
        "kcs_code": "KCS 41 31 05",
        "document_name": "건축물 강구조공사 일반사항",
        "version": "2026",
        "update_date": "2026-08-01",
        "kcs_clause": f"3.2.{rank}",
        "title": f"KCS 후보 {rank}",
        "content": f"KCS 후보 본문 {rank}",
        "score": 0.8 - rank / 100,
        "classification": "관련성 높음",
        "reasons": ["공통 핵심어"],
        "warnings": [],
    }


def _clause(
    clause_id: str,
    order: int,
    content: str,
    *,
    source_type: str = "paragraph",
    candidates: list[dict] | None = None,
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
        "candidates": candidates or [],
    }


def _store(tmp_path) -> tuple[Store, str, list[dict]]:
    store = Store(tmp_path / "structure.sqlite3")
    project_id = "structure-project"
    first_id = str(uuid.uuid4())
    clauses = [
        _clause("heading", 1, "1 일반사항", source_type="heading"),
        _clause(first_id, 2, "첫 문장의 요구사항이다.", candidates=[_candidate(first_id)]),
        _clause(str(uuid.uuid4()), 3, " 둘째 문장의 요구사항이다."),
        _clause(str(uuid.uuid4()), 4, "마지막 요구사항이다."),
    ]
    store.create_project(
        {
            "id": project_id,
            "title": "구조편집 시험",
            "source_filename": "structure.docx",
            "source_path": str(tmp_path / "structure.docx"),
            "source_sha256": "source-hash",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "kcs_snapshot": "2026-09-05T10:00:00+09:00",
            "kcs_revision": REVISION,
            "kcs_scope": "KCS 41 31",
            "warning": "",
        },
        clauses,
    )
    return store, project_id, clauses


def _matched_replacements(prepared: dict) -> list[dict]:
    replacements = []
    for blueprint in prepared["replacements"]:
        clause_id = str(uuid.uuid4())
        replacements.append(
            {
                **blueprint,
                "id": clause_id,
                "candidates": [_candidate(clause_id)],
            }
        )
    return replacements


def test_merge_replaces_adjacent_paragraphs_and_preserves_lineage(tmp_path):
    store, project_id, clauses = _store(tmp_path)
    source_ids = [clauses[1]["id"], clauses[2]["id"]]
    prepared = store.prepare_clause_merge(project_id, source_ids)
    replacements = _matched_replacements(prepared)

    result = store.apply_clause_structure_edit(
        project_id,
        prepared["operation"],
        prepared["source_clause_ids"],
        prepared["expected_fingerprint"],
        prepared["expected_kcs_revision"],
        replacements,
    )

    assert result["active_clause_id"] == replacements[0]["id"]
    assert [item["source_order"] for item in result["clauses"]] == [1, 2, 3]
    assert store.get_clause(project_id, source_ids[0]) is None
    assert store.get_clause(project_id, source_ids[1]) is None
    merged = store.get_clause(project_id, replacements[0]["id"])
    assert merged is not None
    assert merged["content"] == "첫 문장의 요구사항이다.\n3 둘째 문장의 요구사항이다."
    assert merged["match_context"] == "1 일반사항"
    assert merged["decision"] is None
    assert merged["selected_candidate_id"] is None
    assert len(merged["candidates"]) == 1

    history = store.list_clause_structure_history(project_id)
    assert len(history) == 1
    assert history[0]["operation"] == "merge"
    assert history[0]["source_clause_ids"] == source_ids
    assert history[0]["replacement_clause_ids"] == [replacements[0]["id"]]
    assert history[0]["before"]["clauses"][0]["candidates"][0]["id"] == clauses[1]["candidates"][0]["id"]
    assert history[0]["after"]["clauses"][0]["clause"]["decision"] is None


def test_split_requires_exact_source_and_creates_two_unreviewed_clauses(tmp_path):
    store, project_id, clauses = _store(tmp_path)
    clause_id = clauses[1]["id"]
    parts = ["첫 문장의 ", "요구사항이다."]
    prepared = store.prepare_clause_split(project_id, clause_id, parts)
    replacements = _matched_replacements(prepared)

    result = store.apply_clause_structure_edit(
        project_id,
        "split",
        [clause_id],
        prepared["expected_fingerprint"],
        prepared["expected_kcs_revision"],
        replacements,
    )

    assert [item["source_order"] for item in result["clauses"]] == [1, 2, 3, 4, 5]
    first = store.get_clause(project_id, replacements[0]["id"])
    second = store.get_clause(project_id, replacements[1]["id"])
    assert first and second
    assert first["content"] == parts[0]
    assert second["content"] == parts[1]
    assert first["label"] == clauses[1]["label"]
    assert second["label"].endswith("(분할 2)")
    assert first["decision"] is None and second["decision"] is None
    assert store.list_clause_structure_history(project_id)[0]["operation"] == "split"


@pytest.mark.parametrize(
    "parts",
    [
        ["첫 문장의 ", "요구사항이다"],
        ["", "첫 문장의 요구사항이다."],
        ["첫 문장의 요구사항이다.", ""],
        ["첫 문장", "의 요구사항이다.", "추가"],
    ],
)
def test_split_rejects_loss_addition_and_empty_parts(tmp_path, parts):
    store, project_id, clauses = _store(tmp_path)
    with pytest.raises(ValueError):
        store.prepare_clause_split(project_id, clauses[1]["id"], parts)


def test_merge_rejects_missing_nonadjacent_and_nonparagraph_sources(tmp_path):
    store, project_id, clauses = _store(tmp_path)
    with pytest.raises(ValueError, match="바로 이어진"):
        store.prepare_clause_merge(project_id, [clauses[1]["id"], clauses[3]["id"]])
    with pytest.raises(ValueError, match="일반 문단"):
        store.prepare_clause_merge(project_id, [clauses[0]["id"], clauses[1]["id"]])
    with pytest.raises(ClauseStructureNotFoundError):
        store.prepare_clause_merge(project_id, [clauses[1]["id"], "missing"])


def test_structure_edit_blocks_decisions_history_and_quality_samples(tmp_path):
    store, project_id, clauses = _store(tmp_path)
    first_id, second_id = clauses[1]["id"], clauses[2]["id"]
    store.update_decision(
        project_id,
        first_id,
        "keep",
        "",
        "",
        None,
        decision_reason="posco_specific",
    )
    store.update_decision(project_id, first_id, None, "", "", None)
    with pytest.raises(ClauseStructureConflictError, match="판정 이력"):
        store.prepare_clause_merge(project_id, [first_id, second_id])

    clean_store, clean_project_id, clean_clauses = _store(tmp_path / "quality")
    clean_store.ensure_quality_evaluation(clean_project_id)
    with pytest.raises(ClauseStructureConflictError, match="품질평가"):
        clean_store.prepare_clause_merge(
            clean_project_id,
            [clean_clauses[1]["id"], clean_clauses[2]["id"]],
        )


def test_structure_edit_blocks_stale_kcs_and_revision_change_during_matching(tmp_path):
    store, project_id, clauses = _store(tmp_path)
    source_ids = [clauses[1]["id"], clauses[2]["id"]]
    prepared = store.prepare_clause_merge(project_id, source_ids)
    replacements = _matched_replacements(prepared)
    store.set_current_kcs_revision("2026-09-06T10:00:00+09:00", "new-revision")

    with pytest.raises(ClauseStructureConflictError, match="KCS 기준"):
        store.apply_clause_structure_edit(
            project_id,
            "merge",
            source_ids,
            prepared["expected_fingerprint"],
            prepared["expected_kcs_revision"],
            replacements,
        )
    assert all(store.get_clause(project_id, source_id) for source_id in source_ids)
    assert store.get_clause(project_id, replacements[0]["id"]) is None
    with pytest.raises(ClauseStructureConflictError, match="KCS 기준"):
        store.prepare_clause_merge(project_id, source_ids)


def test_structure_edit_detects_fingerprint_change_and_rolls_back(tmp_path):
    store, project_id, clauses = _store(tmp_path)
    source_ids = [clauses[1]["id"], clauses[2]["id"]]
    prepared = store.prepare_clause_merge(project_id, source_ids)
    replacements = _matched_replacements(prepared)
    with store.connect() as connection:
        connection.execute(
            "UPDATE clauses SET content = ? WHERE id = ?",
            ("동시에 변경된 원문", source_ids[0]),
        )

    with pytest.raises(ClauseStructureConflictError, match="준비 이후 변경"):
        store.apply_clause_structure_edit(
            project_id,
            "merge",
            source_ids,
            prepared["expected_fingerprint"],
            prepared["expected_kcs_revision"],
            replacements,
        )
    assert store.get_clause(project_id, source_ids[0])["content"] == "동시에 변경된 원문"
    assert store.get_clause(project_id, replacements[0]["id"]) is None
    assert store.list_clause_structure_history(project_id) == []


def test_apply_rejects_more_than_three_candidates_without_mutation(tmp_path):
    store, project_id, clauses = _store(tmp_path)
    source_ids = [clauses[1]["id"], clauses[2]["id"]]
    prepared = store.prepare_clause_merge(project_id, source_ids)
    replacements = _matched_replacements(prepared)
    replacements[0]["candidates"] = [
        _candidate(replacements[0]["id"], rank) for rank in range(1, 5)
    ]

    with pytest.raises(ValueError, match="최대 3개"):
        store.apply_clause_structure_edit(
            project_id,
            "merge",
            source_ids,
            prepared["expected_fingerprint"],
            prepared["expected_kcs_revision"],
            replacements,
        )
    assert all(store.get_clause(project_id, source_id) for source_id in source_ids)
    assert store.list_clause_structure_history(project_id) == []
