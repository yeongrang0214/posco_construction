import copy
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server.matcher import clear_matcher_caches
from server.kcs_sync import SYNC_LOCK
from server.related_references import find_related_references
from server.storage import Store


SOURCE = "조적부위 배관시공은 일직선 (수평, 수직)으로 한다."
SECTION = "3.4.11 파이프와 배관 매설"
PASSAGE = "(2) 파이프나 배관은 슬리브를 설치하여 조적조를 수직 및 수평으로 관통할 수 있으며, 슬리브 사이 간격은 슬리브 직경의 3배 이상 떨어져 있어야 한다."


@pytest.fixture
def raw_dir(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    def write(code, name, items):
        (raw / f"KCS_{code}.json").write_text(json.dumps([{
            "codeType": "KCS", "code": code, "name": name, "version": "2021",
            "updateDate": "2021-09-03", "list": [
                {"title": title, "label": label, "contents": f"<p>{body}</p>"}
                for title, label, body in items
            ],
        }], ensure_ascii=False), encoding="utf-8")
    write("413402", "벽돌공사", [
        (SECTION, "3.4.11", SECTION),
        (SECTION, "본문", "조적조에 묻힌 파이프와 배관은 조적조의 강도와 내화성을 저하시키는 방식으로 설치해서는 안 된다."),
        (SECTION, "(1)", "(1) 전기배관의 위치가 승인된 도면에 의해 상세 설계되어 있는 경우 구조용 조적조 내부에 매설할 수 있다."),
        (SECTION, "(2)", PASSAGE),
        ("3.3 벽돌쌓기", "(1)", "벽돌의 세로줄눈은 수직 일직선상에 오도록 시공한다."),
        ("1.2 참고 기준", "본문", "배관 및 파이프 관련 시공 기준"),
    ])
    write("334020", "송유관 공사", [("3.2.2 배관 시공", "⑥", "배관의 수평, 수직 및 관 상호간의 평행간격을 정확히 맞춘다.")])
    clear_matcher_caches()
    yield raw
    clear_matcher_caches()


def source(**changes):
    return {
        "id": "source-clause", "source_order": 2, "label": "1.1", "title": SOURCE,
        "content": SOURCE, "source_type": "paragraph", "match_context": "1 일반사항",
        "candidates": [], "decision": None, **changes,
    }


def test_masonry_piping_is_reference_only_and_prioritizes_direction(raw_dir):
    clause = source()
    before = copy.deepcopy(clause)
    result = find_related_references(clause, raw_dir)
    assert clause == before  # Read-only: no candidates, coverage or decisions changed.
    assert result["applicable"]
    refs = result["references"]
    assert len(refs) == 1  # Same subsection is not repeated as multiple references.
    assert refs[0]["content"] == PASSAGE
    assert "일직선" in refs[0]["comparison_note"]
    assert all(ref["kcs_code"] == "KCS 41 34 02" for ref in refs)
    assert all(ref["usage"] == "reference_only" and ref["deletion_eligible"] is False for ref in refs)
    assert all("candidate_id" not in ref and "score" not in ref for ref in refs)


@pytest.mark.parametrize("changes", [
    {"source_type": "heading"},
    {"title": "벽돌 줄눈은 일직선으로 한다.", "content": ""},
    {"title": "배관은 일직선으로 한다.", "content": "", "match_context": "송유관"},
    {"title": "조적 배관 비용은 간접비에 포함한다.", "content": ""},
])
def test_does_not_import_unrelated_subjects_or_match_headings(raw_dir, changes):
    result = find_related_references(source(**changes), raw_dir)
    assert not result["applicable"]
    assert not result["references"]


def test_uses_hierarchy_not_chapter_number_and_avoids_wrong_material(raw_dir):
    result = find_related_references(source(label="8.7", title="전선관은 수평·수직으로 배치한다.", content="", match_context="벽돌공사"), raw_dir)
    assert result["references"][0]["content"] == PASSAGE
    result = find_related_references(source(title="ALC 블록에 전선관을 수직으로 매설한다.", content=""), raw_dir, "벽돌 및 블록공사")
    assert not result["references"]


def test_never_restores_excluded_candidates_or_duplicates_existing_ones(raw_dir):
    reference = {"kcs_code": "KCS 41 34 02", "content": PASSAGE}
    for field in ("candidates", "excluded_candidates"):
        result = find_related_references(source(**{field: [reference]}), raw_dir)
        assert all(ref["content"] != PASSAGE for ref in result["references"])


def test_api_reference_ids_cannot_be_selected_and_read_is_non_mutating(raw_dir, tmp_path, monkeypatch):
    test_store = Store(tmp_path / "related.sqlite3")
    project = {
        "id": "related-project", "title": "조적 시험", "source_filename": "new-masonry.docx",
        "source_path": str(tmp_path / "new.docx"), "source_sha256": "test",
        "uploaded_at": datetime.now(timezone.utc).isoformat(), "kcs_scope": "KCS 41 34",
        "kcs_snapshot": "2021-09-03", "kcs_revision": "test", "warning": "",
    }
    test_store.create_project(project, [source()])
    monkeypatch.setattr(app_module, "store", test_store)
    monkeypatch.setattr(app_module, "settings", replace(app_module.settings, kcs_raw_dir=raw_dir))
    client = TestClient(app_module.app)  # No workers/startup on test data.
    before = test_store.get_clause(project["id"], "source-clause")
    url = f"/api/projects/{project['id']}/clauses/source-clause"
    response = client.get(url + "/related-references")
    assert response.status_code == 200
    ref_id = response.json()["references"][0]["id"]
    assert test_store.get_clause(project["id"], "source-clause") == before
    for path, payload in [
        (url + "/quick-review", {"selected_candidate_id": ref_id}),
        (url, {"decision": "delete", "decision_reason": "fully_covered_by_kcs", "coverage_confirmed": True, "selected_candidate_id": ref_id}),
    ]:
        assert client.patch(path, json=payload).status_code == 400
        assert test_store.get_clause(project["id"], "source-clause") == before
    assert client.get(url.replace("source-clause", "missing") + "/related-references").status_code == 404
    with SYNC_LOCK:
        assert client.get(url + "/related-references").status_code == 503
    assert client.get(url + "/related-references").status_code == 200
    def unavailable(*args, **kwargs):
        raise RuntimeError("KCS unavailable")
    monkeypatch.setattr(app_module, "find_related_references", unavailable)
    assert client.get(url + "/related-references").status_code == 503
