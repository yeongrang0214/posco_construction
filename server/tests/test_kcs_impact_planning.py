from __future__ import annotations

import json
import uuid

import pytest

from server.kcs_impact import (
    candidate_semantic_fingerprint,
    plan_clause_impact,
)


def _candidate(
    candidate_id: str,
    *,
    rank: int = 1,
    code: str = "KCS 41 31 05",
    clause: str = "3.2.1",
    title: str = "표면 처리",
    content: str = "강재 표면의 먼지와 유분을 제거한다.",
    warnings: list[str] | None = None,
    score: float = 0.8,
    version: str = "2026",
) -> dict:
    return {
        "id": candidate_id,
        "rank": rank,
        "kcs_code": code,
        "kcs_clause": clause,
        "title": title,
        "content": content,
        "warnings": warnings or [],
        "score": score,
        "classification": "관련성 높음",
        "reasons": ["공통 핵심어"],
        "version": version,
        "update_date": "2026-09-01",
        "document_name": "건축물 강구조공사 일반사항",
    }


def test_fingerprint_normalizes_semantics_and_ignores_retrieval_metadata():
    before = _candidate("before")
    after = {
        **before,
        "id": "after",
        "rank": 3,
        "kcs_code": "kcs-41-31-05",
        "title": "  표면   처리 ",
        "content": "강재 표면의 먼지와 유분을 제거한다.  ",
        "warnings": [],
        "score": 0.31,
        "classification": "검토 필요",
        "reasons": ["다른 검색 근거"],
        "version": "2027",
        "update_date": "2027-01-01",
        "document_name": "변경된 문서명",
    }

    assert candidate_semantic_fingerprint(before) == candidate_semantic_fingerprint(after)


@pytest.mark.parametrize(
    "change",
    [
        {"kcs_code": "KCS 41 31 06"},
        {"kcs_clause": "3.2.2"},
        {"title": "표면 검사"},
        {"content": "강재 표면의 먼지만 제거한다."},
        {"warnings": ["수치 조건이 다릅니다."]},
    ],
    ids=("code", "clause", "title", "content", "warnings"),
)
def test_fingerprint_tracks_every_semantic_field(change):
    before = _candidate("before")
    after = {**before, **change}

    assert candidate_semantic_fingerprint(before) != candidate_semantic_fingerprint(after)


def test_same_set_reordered_with_metadata_changes_reuses_ids_without_review():
    first = _candidate("first-id", rank=1, clause="3.2.1", content="첫 번째 기준")
    second = _candidate("second-id", rank=2, clause="3.2.2", content="두 번째 기준")
    after_second = {
        **second,
        "id": "temporary-second",
        "rank": 1,
        "score": 0.95,
        "version": "2027",
    }
    after_first = {
        **first,
        "id": "temporary-first",
        "rank": 2,
        "score": 0.61,
        "version": "2027",
    }

    plan = plan_clause_impact(
        [first, second],
        [after_second, after_first],
        "delete",
        "first-id",
        clause_id="clause-a",
    )

    assert plan["material_changed"] is False
    assert plan["metadata_changed"] is True
    assert plan["review_required"] is False
    assert plan["selected_candidate_id"] == "first-id"
    assert plan["selection_removed"] is False
    assert [item["id"] for item in plan["after_candidates"]] == [
        "second-id",
        "first-id",
    ]
    assert all(item["reused"] for item in plan["id_assignments"])
    assert plan["reasons"] == ["candidate_metadata_changed"]


def test_changed_selected_candidate_gets_deterministic_new_id_and_is_removed():
    before = _candidate("selected-id")
    after = {
        **before,
        "id": "matcher-temporary-id",
        "content": "강재 표면의 먼지와 유분 및 염분을 제거한다.",
    }

    first_plan = plan_clause_impact(
        [before],
        [after],
        "delete",
        "selected-id",
        clause_id="clause-a",
    )
    repeated_plan = plan_clause_impact(
        [before],
        [after],
        "delete",
        "selected-id",
        clause_id="clause-a",
    )
    other_clause_plan = plan_clause_impact(
        [before],
        [after],
        "delete",
        "selected-id",
        clause_id="clause-b",
    )

    assigned_id = first_plan["after_candidates"][0]["id"]
    assert first_plan["material_changed"] is True
    assert first_plan["review_required"] is True
    assert first_plan["selected_candidate_id"] is None
    assert first_plan["selection_removed"] is True
    assert assigned_id == repeated_plan["after_candidates"][0]["id"]
    assert assigned_id != other_clause_plan["after_candidates"][0]["id"]
    assert assigned_id != "matcher-temporary-id"
    assert first_plan["reasons"] == [
        "candidate_meaning_changed",
        "candidate_added",
        "candidate_removed",
        "selected_candidate_removed",
    ]
    assert after["id"] == "matcher-temporary-id"


def test_material_change_without_existing_decision_does_not_require_rereview():
    before = _candidate("before", warnings=[])
    after = {**before, "id": "after", "warnings": ["의무 표현이 다릅니다."]}

    plan = plan_clause_impact(
        [before],
        [after],
        None,
        None,
        clause_id="clause-a",
    )

    assert plan["material_changed"] is True
    assert plan["review_required"] is False
    assert "candidate_meaning_changed" in plan["reasons"]


def test_unchanged_selection_survives_when_another_candidate_is_added():
    selected = _candidate("selected-id", clause="3.2.1", content="선택한 기준")
    added = _candidate(
        "temporary-added",
        rank=2,
        clause="3.2.2",
        content="새로 추가된 기준",
    )

    plan = plan_clause_impact(
        [selected],
        [{**selected, "id": "temporary-selected"}, added],
        "keep",
        "selected-id",
        clause_id="clause-a",
    )

    assert plan["material_changed"] is True
    assert plan["review_required"] is True
    assert plan["selected_candidate_id"] == "selected-id"
    assert plan["selection_removed"] is False
    assert plan["after_candidates"][0]["id"] == "selected-id"
    assert plan["after_candidates"][1]["id"] != "temporary-added"
    assert plan["reasons"] == ["candidate_added"]


def test_snapshots_are_json_safe_and_inputs_are_not_mutated():
    marker = uuid.uuid4()
    before = _candidate("before")
    before["extra"] = marker
    before["reasons"] = ("첫 근거", "둘째 근거")
    after = {**before, "id": "temporary"}

    plan = plan_clause_impact(
        [before],
        [after],
        None,
        None,
        clause_id="clause-a",
    )

    json.dumps(plan["before_candidates"], ensure_ascii=False)
    json.dumps(plan["after_candidates"], ensure_ascii=False)
    assert plan["before_candidates"][0]["extra"] == str(marker)
    assert before["extra"] is marker
    assert after["id"] == "temporary"


def test_planner_requires_clause_id_for_new_candidate_ids():
    with pytest.raises(ValueError, match="조항 ID"):
        plan_clause_impact([], [_candidate("temporary")], None, None, clause_id="")
