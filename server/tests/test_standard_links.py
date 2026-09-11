import json

import httpx
import pytest

from server.openai_ai import OpenAIClient
from server.standard_links import ks_codes, standard_links

SOURCE = "내화벽돌의 조적에 사용하는 내화몰탈은 KSL 3202 (내화몰탈) 규정에 합격한 것으로 한다."
TARGET = "(1) 내화벽돌의 쌓기에 사용하는 모르타르는 한국산업표준에 적합한 제품으로 한다."


@pytest.mark.parametrize("code", ["KSL3202", "KS L 3202", "ks l-3202"])
def test_verified_scope_links_normalized_standard_to_actual_use(code):
    links = standard_links(SOURCE.replace("KSL 3202", code), TARGET)
    assert len(links) == 1
    assert links[0]["standard"] == "KS L 3202"
    assert links[0]["reference_type"] == "indirect"
    assert links[0]["verified_at"] == "2026-09-11"
    assert "전문 미대조" in links[0]["verification_level"]


@pytest.mark.parametrize("source,target", [
    (SOURCE.replace("3202", "9999"), TARGET),
    ("페인트는 KS L 3202에 적합하여야 한다.", TARGET),
    (SOURCE, "시멘트 모르타르는 한국산업표준에 적합한 제품으로 한다."),
    (SOURCE, "단열 모르타르는 덩어리를 풀어 물반죽하여 사용한다."),
    (SOURCE, "[KCS 상위 문맥] " + TARGET + "\n[KCS 본문] 제품 견본을 제출하여야 한다."),
    ("[원문 상위 문맥] " + SOURCE + "\n[현재 요구사항] 제품 견본을 제출한다.", TARGET),
    (SOURCE, TARGET.replace("한국산업표준", "KS L 3101")),
])
def test_no_invented_link_for_unverified_codes_different_products_or_headings(source, target):
    assert standard_links(source, target) == []


def test_unrelated_standard_mention_does_not_turn_indirect_reference_into_explicit():
    result = standard_links(SOURCE, TARGET + "\n별도 공사에서는 KS L 3202를 참조한다.")
    assert result[0]["reference_type"] == "indirect"
    assert ks_codes("KSL 3202, KS L 3101") == {"KS L 3202", "KS L 3101"}


def test_verifier_receives_metadata_separately_and_rejects_metadata_as_body_quote():
    def handler(request):
        payload = json.loads(request.content)
        pair = json.loads(payload["input"])[0]
        assert pair["source"] == SOURCE
        assert pair["target"].endswith(TARGET)
        assert pair["verified_standard_links"][0]["standard"] == "KS L 3202"
        assert "번호가 없다는 사실만으로" in payload["instructions"]
        row = {"id": "0", "relation": "related", "reason": "같은 자재 용도",
               "source_quote": SOURCE, "target_quote": pair["verified_standard_links"][0]["scope"]}
        return httpx.Response(200, json={"output_text": json.dumps({"results": [row]}, ensure_ascii=False)})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = OpenAIClient("test", http_client=transport)
        result = client.verify_candidate_groups([(SOURCE, ["[KCS 상위 문맥] 2.2 단열 모르타르\n[KCS 본문] " + TARGET])])
    assert result[0][0]["relation"] == "uncertain"


@pytest.mark.parametrize("explicit,expected", [(False, "uncertain"), (True, "fully_covered")])
def test_indirect_scope_is_not_full_coverage_even_when_model_says_covered(explicit, expected):
    target = TARGET.replace("한국산업표준", "KS L 3202") if explicit else TARGET
    def handler(request):
        payload = json.loads(request.content)
        assert "verified_standard_links" in payload["input"]
        response = {"confidence": .98, "requirements_by_source": {"S1": {
            "status": "covered", "evidence_candidate_ids": ["C1"], "evidence": target}},
            "residual_content": "", "rationale": "같은 적합 요구"}
        return httpx.Response(200, json={"output_text": json.dumps(response, ensure_ascii=False)})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = OpenAIClient("test", http_client=transport)
        result = client.analyze_coverage(SOURCE, [("C1", target)])
    assert result.coverage_status == expected
    assert result.requirements[0]["evidence_candidate_ids"] == ["C1"]
    if not explicit:
        assert result.residual_content == SOURCE
        assert "대상이 다르" not in result.rationale
        assert "전체 대체" in result.rationale


def test_pair_analysis_cannot_turn_scope_metadata_into_deletion_equivalence():
    response = {"relation_type": "equivalent", "confidence": .98,
                "rationale": "같은 용도", "simplified_content": ""}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"output_text": json.dumps(response, ensure_ascii=False)}))) as transport:
        result = OpenAIClient("test", http_client=transport).analyze_match(SOURCE, TARGET)
    assert result.relation_type == "partial_overlap"
    assert result.simplified_content == SOURCE


def test_linked_candidate_carries_same_subsection_conditions_only(tmp_path):
    from server.matcher import load_kcs_sections, _candidate_rows
    rows = [
        {"label": "(1)", "title": "2.2 단열 모르타르", "contents": TARGET},
        {"label": "(2)", "title": "2.2 단열 모르타르", "contents": "모르타르는 사용하는 벽돌과 같은 정도의 내화도가 있어야 한다."},
        {"label": "표", "title": "2.2 단열 모르타르", "contents": "표 2.2-1 단열 모르타르의 품질 열전도율"},
        {"label": "(1)", "title": "3.1 시공", "contents": "다른 절의 의무는 추가하지 않는다."},
    ]
    (tmp_path / "KCS_413403.json").write_text(json.dumps([
        {"code": "413403", "name": "내화벽돌쌓기", "version": "2021", "list": rows}
    ], ensure_ascii=False), encoding="utf-8")
    section = load_kcs_sections(str(tmp_path), ("413403",))[0]
    clause = {"id": "00000000-0000-0000-0000-000000000001", "content": SOURCE}
    candidate = _candidate_rows(clause, SOURCE, [(section, .4, None)])[0]
    assert section["content"] == TARGET
    assert "같은 정도의 내화도" in candidate["content"]
    assert "표 2.2-1" in candidate["content"]
    assert "다른 절의 의무" not in candidate["content"]
    unlinked = _candidate_rows({**clause, "content": "일반 모르타르를 사용한다."}, "일반 모르타르", [(section, .4, None)])[0]
    assert unlinked["content"] == TARGET


def test_read_api_exposes_verified_scope_without_changing_review(monkeypatch):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    import server.app as app
    clause = {"id": "clause", "title": "", "content": SOURCE, "decision": "keep", "review_note": "담당자 의견",
              "candidates": [{"id": "candidate", "kcs_code": "KCS 41 34 03", "title": "2.2 단열 모르타르", "kcs_clause": "(1)", "content": TARGET}]}
    monkeypatch.setattr(app, "_project_or_404", lambda project_id: {})
    monkeypatch.setattr(app, "store", SimpleNamespace(get_clause=lambda project_id, clause_id: dict(clause)))
    response = TestClient(app.app).get("/api/projects/project/clauses/clause")
    assert response.status_code == 200
    result = response.json()["clause"]
    assert result["standard_links"][0]["candidate_id"] == "candidate"
    assert result["standard_links"][0]["standard"] == "KS L 3202"
    assert result["decision"] == "keep"
    assert result["review_note"] == "담당자 의견"
