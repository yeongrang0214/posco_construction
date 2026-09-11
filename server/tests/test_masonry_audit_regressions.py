import copy
import json

import httpx
import pytest

from server.documents import _title_and_content, _is_table_header
from server.ks_test_comparison import fields_for, fingerprint
from server.openai_ai import OpenAIClient
from server.relevance import own_requirement
from server.source_context import enrich_source_context
from server.standard_links import ks_codes, ks_references
from server.text_analysis import analyze_text_differences
from server.tests.test_matcher_domain_context import _write_kcs
from server.matcher import _rank_matches, _rank_without_embeddings, _candidate_rows


def row(order, label, content, kind="table"):
    return {"id": str(order), "source_order": order, "label": label, "title": "",
            "content": content, "source_type": kind, "decision": "keep", "review_note": "보존"}


@pytest.mark.parametrize("text", ["사춤용 몰탈은 1 : 3으로 한다.", "배합은 1:2:4로 한다."])
def test_ratios_are_not_title_delimiters(text):
    title, content = _title_and_content(text, False)
    assert own_requirement({"title": title, "content": content}) == text


def test_legacy_ratio_compatibility_and_normal_title_separator():
    old = {"title": "블록쌓기용 사춤용 몰탈은 1", "content": "3으로 하고 줄눈용은 1 : 1을 사용한다."}
    original = copy.deepcopy(old)
    assert "1 : 3" in own_requirement(old)
    assert old == original
    assert _title_and_content("줄눈: 가로 세로 10mm", False) == ("줄눈", "가로 세로 10mm")


def test_dimension_header_and_ditto_preserve_source_and_decisions():
    clauses = [row(1, "표 1-1", "구 분 | 100x190x390 | 150x190x390 | 190x190x390"),
               row(2, "표 1-2", "나 비 | 80 | 120 | 160"),
               row(3, "표 2-1", "구분 | 시험종류 | 시험규격 | 시험빈도 | 비고", "heading"),
               row(4, "표 2-2", "블록 | 강도 | KSF 4002 | 공사개시전 1회 | 6일"),
               row(5, "표 2-3", "벽돌 | 강도 | KSF 4004 | 상 동 | 6일"),
               row(6, "표 3-1", "내화벽돌 | \" | 2개 | \"")]
    originals = copy.deepcopy(clauses)
    enrich_source_context(clauses)
    assert _is_table_header(originals[0]["content"].split("|"))
    assert "100x190x390: 80" in clauses[1]["analysis_context"]
    assert clauses[4]["resolved_table_cells"][3] == "공사개시전 1회"
    assert clauses[5]["resolved_table_cells"][1] == '"'  # no cross-table guess
    assert "미확인" in clauses[5]["analysis_context"]
    for before, after in zip(originals, clauses):
        assert all(after[key] == value for key, value in before.items())


def test_four_column_test_uses_explicit_same_material_standard_and_ditto():
    clauses = [row(1, "표 1-1", "내화벽돌 | 내화도 | KSL 3101 | 개시전 | 조건"),
               row(2, "표 2-1", "구분 | 시험종류 | 시료량 | 비고", "heading"),
               row(3, "표 2-2", "블록 | 압축강도 | 5개 | 품질 이상시"),
               row(4, "표 2-3", "내 화 벽 돌 | \" | 2개 | \"")]
    enrich_source_context(clauses)
    fields = fields_for(clauses[-1])
    assert [f["source"] for f in fields] == ["압축강도", "2개", "품질 이상시"]
    assert not fields_for(clauses[2])  # no standard invented from material name
    before = fingerprint(clauses[-1])
    clauses[2]["content"] = "블록 | 압축강도 | 5개 | 매회"
    enrich_source_context(clauses)
    assert fingerprint(clauses[-1]) != before


def test_ks_elided_prefix_iso_and_ambiguous_suffix():
    codes, ambiguous = ks_references("KSL 3101 -5 3111, 3113, 3115 KSL 3201 -5")
    assert set(codes) == {"KS L 3101", "KS L 3111", "KS L 3113", "KS L 3115", "KS L 3201"}
    assert ambiguous
    assert ks_codes("KSL ISO 5019-1~6, KSL 3201,3111,3113") == {"KS L ISO 5019", "KS L 3201", "KS L 3111", "KS L 3113"}
    assert ks_references("KSL 3201,3111,3113")[1] is False
    assert ks_codes("KS F 4004 제품 1000개 2026년도") == {"KS F 4004"}


def test_height_same_but_course_count_different_is_detected():
    result = analyze_text_differences("쌓기 높이는 1.2m(17켜)로 한다.", "쌓기 높이는 1200mm(18켜)로 한다.")
    assert any(d["category"] == "quantity" for d in result["differences"])
    same = analyze_text_differences("연결재 수평 간격은 90cm로 한다.", "연결재 수평 간격은 900mm로 한다.")
    assert not any(d["category"] == "quantity" for d in same["differences"])


def test_gpt_cannot_claim_full_coverage_for_changed_courses():
    source = "쌓기 높이는 1.2m(17켜)로 한다."
    target = "쌓기 높이는 1200mm(18켜)로 한다."
    response = {"requirements_by_source": {"S1": {"status": "covered", "evidence_candidate_ids": ["C1"], "evidence": target}},
                "confidence": .99, "rationale": "같은 높이", "residual_content": ""}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"output_text": json.dumps(response)}))) as transport:
        analysis = OpenAIClient("test", http_client=transport).analyze_coverage(source, [("C1", target)])
    assert analysis.coverage_status == "uncertain"
    assert analysis.residual_content == source


def test_masonry_operation_survives_generic_concrete_recovery(tmp_path):
    _write_kcs(tmp_path, "413406", "단순조적 블록공사", [
        {"label": "(6)", "title": "3.3.2 쌓기", "contents": "<p>줄눈 모르타르는 쌓은 후 줄눈누르기 및 줄눈파기를 한다.</p>"}])
    _write_kcs(tmp_path, "142010", "콘크리트 공사", [
        {"label": "(5)", "title": "콘크리트 펌프", "contents": "<p>콘크리트 펌프 장비를 설치하고 검사하여야 한다.</p>"}])
    text = "콘크리트 블록을 쌓은 후 줄눈몰탈을 눌러두고 줄파기를 확인한다."
    ranked = _rank_matches(text, tmp_path, ("4134",))
    candidates = _candidate_rows({"id": "00000000-0000-0000-0000-000000000001", "content": text}, text,
                                 _rank_without_embeddings(ranked), limit=12, min_relevance=.1)
    assert candidates[0]["kcs_code"] == "KCS 41 34 06"
