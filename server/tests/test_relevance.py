import json
import uuid

import httpx
import pytest

from server.relevance import apply_verdicts, clearly_unrelated, query_text, contextual_requirement, own_requirement, masonry_material_conflict, evidence_present
from server.matcher import _candidate_rows
from server.openai_ai import OpenAIClient


def clause(text, **kwargs):
    return {"id": str(uuid.uuid4()), "title": "", "content": text, "source_type": "paragraph", "match_context": "철골공사", **kwargs}


def test_oxygen_price_never_imports_neighbor_erection_safety():
    rows = [clause("현장 철골 세우기에서 가공 도급업자가 책임을 진다."), clause("산소는 지역별 도급 단가로 처리 한다.", title="산 소"), clause("가조립 시험검사비는 간접비에 포함한다.")]
    query = query_text(rows, 1)
    assert "철골 세우기" not in query and "가조립" not in query
    assert "산소는 지역별" in query


@pytest.mark.parametrize("source,target,expected", [
    ("산소는 지역별 도급 단가로 처리한다.", "강구조 설치시 안전망을 설치해야 한다.", True),
    ("시험검사비는 간접비에 포함된다.", "부재의 용접 검사를 실시한다.", True),
    ("산소는 도급 단가로 처리한다.", "산소 공급 비용은 수급인이 부담한다.", False),
    ("산소를 공급해야 한다. 비용은 수급인이 부담한다.", "산소를 공급해야 한다.", False),
    ("공작도를 작성한다.", "철골 제작도를 작성한다.", False),
])
def test_commercial_gate(source, target, expected):
    assert clearly_unrelated(source, target) is expected


def test_context_does_not_cross_heading_or_scope():
    rows = [clause("아래 기준에 따른다."), clause("재료", source_type="heading"), clause("강재 규격")]
    assert "강재" not in query_text(rows, 0)
    rows = [clause("상기 기준에 따른다.", match_context="운반"), clause("상기 기준에 따른다.", match_context="용접")]
    assert "운반" not in query_text(rows, 1)


def test_common_korean_word_does_not_trigger_reference_expansion():
    rows = [clause("산소 단가"), clause("작업을 위하여 준비한다."), clause("전기 설비 기준")]
    assert "전기" not in query_text(rows, 1)


def test_filtered_sample_does_not_treat_distant_clause_as_neighbor():
    rows = [clause("아래규격에 적합한 강재", source_order=42), clause("공작도 작성", source_order=23)]
    assert "공작도" not in query_text(rows, 0)


def test_repeated_local_numbers_in_different_sections_are_not_duplicates():
    source = clause("강재를 검사한다.")
    base = {"code": "KCS 41 31 15", "clause": "①", "document_name": "공장제작", "version": "2026", "update_date": "", "title": "1 검사", "content": "강재를 반입할 때 담당자는 품질 검사를 실시한다."}
    second = {**base, "title": "2 검사", "content": "제작 완료 후 용접 접합부의 품질 검사를 실시한다."}
    rows = _candidate_rows(source, source["content"], [(base, .5, None), (second, .5, None), (dict(base), .4, None)])
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"]


def test_verifier_requires_real_quotes_and_caches_valid_results():
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        pairs = json.loads(payload["input"])
        values = [{"id": pair["id"], "relation": "related", "reason": "같은 요구사항", "source_quote": pair["source"], "target_quote": pair["target"] if i == 0 else "존재하지 않는 인용"} for i, pair in enumerate(pairs)]
        return httpx.Response(200, json={"output_text": json.dumps({"results": values}, ensure_ascii=False)})
    client = OpenAIClient("test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.verify_candidate_groups([("공작도를 작성한다.", ["철골제작도를 작성한다.", "안전망 설치 기준"] )])
    assert result[0][0]["relation"] == "related"
    assert result[0][1]["relation"] == "uncertain"
    client.verify_candidate_groups([("공작도를 작성한다.", ["철골제작도를 작성한다."])])
    assert len(calls) == 1


def test_failed_verification_stops_requests_and_never_claims_success():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429)
    client = OpenAIClient("test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.verify_candidate_groups([("강재 검사", [f"검사 기준 {i}" for i in range(40)])])
    assert len(calls) == 1
    assert all(row["relation"] == "uncertain" for row in result[0])


def test_unrelated_candidates_are_removed_without_filling_slots():
    candidates = [{"rank": i, "reasons": [], "warnings": []} for i in range(6)]
    result = apply_verdicts(candidates, [{"relation": "unrelated"} for _ in candidates])
    assert result == []
    assert apply_verdicts(candidates, None)[0]["classification"] == "의미 검증 미완료"


def test_low_search_score_requires_verified_body_before_display():
    candidate = {"rank": 1, "score": .2, "_requires_verification": True, "reasons": [], "warnings": []}
    assert apply_verdicts([candidate], None) == []
    rows = apply_verdicts([candidate], [{"relation": "partial", "target_quote": "강재는 유해한 결함이 없어야 한다."}])
    assert rows[0]["score"] == .2
    assert "_requires_verification" not in rows[0]


def test_split_first_line_and_material_heading_are_in_verification_input():
    row = clause("감독원의 승인을 받아 사용한다.", title="제품은 미리 견본품을 제출하여",
                 match_context="조적공사 > 내화벽돌")
    assert own_requirement(row) == "제품은 미리 견본품을 제출하여 감독원의 승인을 받아 사용한다."
    assert "내화벽돌" in contextual_requirement(row)
    assert own_requirement(row) in contextual_requirement(row)
    assert own_requirement(clause("제품은 미리 견본품을 제출한다.", title="제품은 미리…")) == "제품은 미리 견본품을 제출한다."


@pytest.mark.parametrize("context_side", ["source", "target"])
def test_heading_only_evidence_cannot_pass_semantic_verification(context_side):
    source = contextual_requirement(clause("제품 견본을 제출하여 승인을 받는다.", match_context="내화벽돌 시공 기준"))
    target = "[KCS 상위 문맥] 콘크리트블록 시공 기준\n[KCS 본문] 제품 견본을 제출하여 승인을 받는다."
    def handler(request):
        row = {"id": "0", "relation": "related", "reason": "제목만 인용하는 잘못된 응답",
               "source_quote": "내화벽돌 시공 기준" if context_side == "source" else "제품 견본을 제출하여 승인을 받는다.",
               "target_quote": "콘크리트블록 시공 기준" if context_side == "target" else "제품 견본을 제출하여 승인을 받는다."}
        return httpx.Response(200, json={"output_text": json.dumps({"results": [row]}, ensure_ascii=False)})
    client = OpenAIClient("test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.verify_candidate_groups([(source, [target])])[0][0]["relation"] == "uncertain"


@pytest.mark.parametrize("source,target,conflict", [
    ("2.3 내화벽돌", "콘크리트 블록공사 > 2.1 콘크리트 블록", True),
    ("내화벽돌", "벽돌공사 > 재료 반입 일반", False),
    ("점토 벽돌", "붉은벽돌쌓기", False),
    ("콘크리트 벽돌", "콘크리트 블록", True),
    ("내화벽돌 및 콘크리트 벽돌", "콘크리트 벽돌", False),
    ("모르타르", "내화벽돌공사", False),
    ("내화벽돌", "블록공사 > 블록의 치수", True),
    ("내화벽돌", "단순조적블록공사 > 인방블록쌓기", True),
    ("벽돌", "블록공사", True),
    ("블록", "ALC블록공사", False),
    ("벽돌 및 블록", "내화벽돌", False),
])
def test_masonry_scope_gate(source, target, conflict):
    assert masonry_material_conflict(source, target) is conflict


def test_malformed_batch_does_not_cancel_remaining_verification():
    calls = []
    def handler(request):
        calls.append(request)
        pairs = json.loads(json.loads(request.content)["input"])
        if len(calls) == 1:
            return httpx.Response(200, json={"output_text": "invalid"})
        rows = [{"id": p["id"], "relation": "related", "reason": "실제 본문 일치", "source_quote": p["source"], "target_quote": p["target"]} for p in pairs]
        return httpx.Response(200, json={"output_text": json.dumps({"results": rows}, ensure_ascii=False)})
    client = OpenAIClient("test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.verify_candidate_groups([("강재를 검사한다.", [f"강재를 검사한다. {i}" for i in range(9)])])[0]
    assert len(calls) == 2
    assert all(r["relation"] == "uncertain" for r in result[:8])
    assert result[8]["relation"] == "related"


def test_evidence_accepts_only_known_field_label_not_invented_or_abridged_text():
    body = "내화벽돌은 먼지 등을 청소하고 물축이기는 하지 않는다."
    assert evidence_present("[현재 요구사항] " + body, body)
    assert evidence_present("[KCS 본문] " + body, body)
    assert not evidence_present("내화벽돌은 ... 물축이기는 하지 않는다.", body)
    assert not evidence_present("[원문 상위 문맥] 내화벽돌", body)
    assert not evidence_present("[현재 요구사항] 벽돌은 물축이기를 한다.", body)


def test_verification_batches_never_mix_source_clauses():
    calls = []
    def handler(request):
        pairs = json.loads(json.loads(request.content)["input"])
        calls.append(pairs)
        values = [{"id": p["id"], "relation": "unrelated", "reason": "별개 의무", "source_quote": "", "target_quote": ""} for p in pairs]
        return httpx.Response(200, json={"output_text": json.dumps({"results": values}, ensure_ascii=False)})
    client = OpenAIClient("test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.verify_candidate_groups([("줄눈 치수 의무", ["치수 기준", "품질 기준"]), ("제품 견본 제출 의무", ["승인 기준"])])
    assert len(calls) == 2
    assert all(len({p["source"] for p in batch}) == 1 for batch in calls)
