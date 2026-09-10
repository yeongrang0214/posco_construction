from __future__ import annotations

import json
import uuid

import httpx
import pytest

import server.openai_ai as openai_ai_module
from server.matcher import (
    _candidate_rows,
    _rank_matches,
    _rank_without_embeddings,
    _rerank_from_scores,
    load_kcs_sections,
    match_clauses,
)
from server.openai_ai import OpenAIAPIError, OpenAIClient, coverage_source_segments


@pytest.mark.parametrize("target,status", [
    ("단열 모르타르는 덩어리를 풀어 물반죽하여 사용한다.", "uncertain"),
    ("내화 모르터는 덩어리를 풀어 물반죽하여 사용한다.", "fully_covered"),
])
def test_different_named_mortar_cannot_be_marked_fully_covered(target, status):
    source = "내화몰탈은 덩어리를 풀어 물반죽하여 사용한다."
    result = {"coverage_status": "fully_covered", "confidence": .98,
              "requirements": [{"source_segment_id": "S1", "status": "covered", "evidence_candidate_ids": ["C1"], "evidence": target}],
              "residual_content": "", "rationale": "같은 작업 순서"}
    client = OpenAIClient("test", http_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"output_text": json.dumps(result, ensure_ascii=False)})
    )))
    analysis = client.analyze_coverage(source, [("C1", target)])
    assert analysis.coverage_status == status
    if status == "uncertain":
        assert analysis.requirements[0]["status"] == "uncertain"
        assert analysis.residual_content == source


@pytest.mark.parametrize("keys", [{}, {"S1": {}, "S2": {}}])
def test_keyed_coverage_rejects_missing_or_unknown_source(keys):
    payload = {"confidence": .9, "requirements_by_source": keys, "residual_content": "", "rationale": "검증"}
    client = OpenAIClient("test", http_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"output_text": json.dumps(payload)})
    )))
    with pytest.raises(OpenAIAPIError):
        client.analyze_coverage("시험을 실시한다.", [("C1", "시험을 실시한다.")])


def test_keyed_coverage_retains_remaining_source_when_model_omits_residual():
    source = "견본을 제출하고 담당자의 승인을 받는다."
    payload = {"confidence": .9, "requirements_by_source": {"S1": {
        "status": "not_covered", "evidence_candidate_ids": ["C1"], "evidence": "견본 제출만 대응"}},
        "residual_content": "", "rationale": "일부만 대응"}
    client = OpenAIClient("test", http_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"output_text": json.dumps(payload)})
    )))
    result = client.analyze_coverage(source, [("C1", "견본을 제출한다.")])
    assert result.coverage_status == "partially_covered"
    assert result.residual_content == source


def test_keyed_coverage_does_not_discard_contradictory_residual():
    payload = {"confidence": .9, "requirements_by_source": {"S1": {
        "status": "covered", "evidence_candidate_ids": ["C1"], "evidence": "시험"}},
        "residual_content": "다른 의무가 남음", "rationale": "대응"}
    client = OpenAIClient("test", http_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"output_text": json.dumps(payload)})
    )))
    with pytest.raises(OpenAIAPIError, match="모순"):
        client.analyze_coverage("시험을 실시한다.", [("C1", "시험을 실시한다.")])


def test_duplicate_json_keys_are_rejected_not_silently_overwritten():
    with pytest.raises(ValueError, match="duplicate"):
        json.loads('{"S1": {"status":"uncertain"}, "S1": {"status":"covered"}}',
                   object_pairs_hook=openai_ai_module._unique_json_object)


def test_client_requires_key_without_making_request():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    client = OpenAIClient(None, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert client.available is False
    with pytest.raises(OpenAIAPIError):
        client.embed(["시험 문장"])
    assert called is False


def test_embeddings_and_structured_match_analysis_payloads():
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        assert request.headers["Authorization"] == "Bearer test-key"
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.8, 0.2]},
                        {"index": 0, "embedding": [1.0, 0.0]},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "relation_type": "partial_overlap",
                                        "confidence": 0.91,
                                        "rationale": "일부 요구조건만 같다.",
                                        "simplified_content": "사내 추가 조건만 남긴다.",
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                    }
                ]
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenAIClient("test-key", base_url="https://example.test/v1", http_client=http)

    assert client.embedding_scores("질의", ["후보"])[0] == pytest.approx(0.9701425)
    analysis = client.analyze_match("포스코 기준", "KCS 기준")

    assert analysis.relation_type == "partial_overlap"
    assert analysis.confidence == pytest.approx(0.91)
    assert analysis.simplified_content == "사내 추가 조건만 남긴다."
    assert analysis.as_dict()["simplified_content"] == "사내 추가 조건만 남긴다."
    assert requests[0][1]["model"] == "text-embedding-3-small"
    response_payload = requests[1][1]
    assert response_payload["store"] is False
    assert response_payload["text"]["format"]["type"] == "json_schema"
    assert response_payload["text"]["format"]["strict"] is True


def test_embedding_cache_is_bounded_without_dropping_current_batch_results():
    batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        batches.append(inputs)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(len(text)), 1.0]}
                    for index, text in enumerate(inputs)
                ]
            },
        )

    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        embedding_cache_entries=2,
    )

    vectors = client._cached_embeddings(["a", "bb", "ccc"])
    assert len(vectors) == 3
    assert list(client._embedding_cache) == ["bb", "ccc"]

    client._cached_embeddings(["bb", "dddd"])
    assert batches == [["a", "bb", "ccc"], ["dddd"]]
    assert list(client._embedding_cache) == ["bb", "dddd"]


def test_structured_combined_coverage_analysis_requires_evidence_for_every_requirement():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.update(payload)
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "coverage_status": "partially_covered",
                        "confidence": 0.93,
                        "requirements": [
                            {
                                "source_segment_id": "S1",
                                "status": "covered",
                                "evidence_candidate_ids": ["C1"],
                                "evidence": "유분을 제거하도록 규정한다.",
                            },
                            {
                                "source_segment_id": "S2",
                                "status": "not_covered",
                                "evidence_candidate_ids": [],
                                "evidence": "KCS 후보에 명시되지 않았다.",
                            },
                        ],
                        "residual_content": "강재 표면의 염분 농도를 측정하여야 한다.",
                        "rationale": "유분 제거만 KCS에 포함된다.",
                    },
                    ensure_ascii=False,
                )
            },
        )

    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    analysis = client.analyze_coverage(
        "유분을 제거한다.\n염분 농도를 측정한다.",
        [("C1", "유분을 제거한다."), ("C2", "도막 두께를 검사한다.")],
    )

    assert analysis.coverage_status == "partially_covered"
    assert analysis.requirements[1]["status"] == "not_covered"
    assert analysis.requirements[0]["requirement"] == "유분을 제거한다."
    assert analysis.requirements[1]["source_segment_ids"] == ["S2"]
    assert analysis.residual_content == "강재 표면의 염분 농도를 측정하여야 한다."
    assert captured["text"]["format"]["type"] == "json_schema"
    assert captured["text"]["format"]["strict"] is True
    source_schema = captured["text"]["format"]["schema"]["properties"]["requirements_by_source"]
    evidence_schema = source_schema["properties"]["S1"]["properties"]["evidence_candidate_ids"]["items"]
    assert evidence_schema["enum"] == ["C1", "C2"]
    assert source_schema["required"] == ["S1", "S2"]
    assert source_schema["additionalProperties"] is False


def test_single_compound_requirement_preserves_partial_overlap_evidence():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "coverage_status": "partially_covered",
                        "confidence": 0.91,
                        "requirements": [
                            {
                                "source_segment_id": "S1",
                                "status": "not_covered",
                                "evidence_candidate_ids": ["C1"],
                                "evidence": "KCS는 표면 청소 의무를 포함한다.",
                            }
                        ],
                        "residual_content": "염분 농도를 측정하여야 한다.",
                        "rationale": "복합 요구 중 표면 청소만 KCS에 포함된다.",
                    },
                    ensure_ascii=False,
                )
            },
        )

    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    analysis = client.analyze_coverage(
        "표면을 청소하고 염분 농도를 측정하여야 한다.",
        [("C1", "표면을 청소하여야 한다.")],
    )

    assert analysis.coverage_status == "partially_covered"
    assert analysis.requirements[0]["status"] == "not_covered"
    assert analysis.requirements[0]["evidence_candidate_ids"] == ["C1"]
    assert analysis.residual_content == "염분 농도를 측정하여야 한다."


def test_single_unmatched_requirement_is_posco_specific_without_evidence():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "coverage_status": "posco_specific",
                        "confidence": 0.9,
                        "requirements": [
                            {
                                "source_segment_id": "S1",
                                "status": "not_covered",
                                "evidence_candidate_ids": [],
                                "evidence": "KCS 후보에 명시된 요구가 없다.",
                            }
                        ],
                        "residual_content": "염분 농도를 측정하여야 한다.",
                        "rationale": "대응하는 KCS 근거가 없다.",
                    },
                    ensure_ascii=False,
                )
            },
        )

    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    analysis = client.analyze_coverage(
        "염분 농도를 측정하여야 한다.",
        [("C1", "표면을 청소하여야 한다.")],
    )

    assert analysis.coverage_status == "posco_specific"
    assert analysis.requirements[0]["evidence_candidate_ids"] == []


def test_combined_coverage_rejects_a_missing_source_segment():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "coverage_status": "fully_covered",
                        "confidence": 0.99,
                        "requirements": [
                            {
                                "source_segment_id": "S1",
                                "status": "covered",
                                "evidence_candidate_ids": ["C1"],
                                "evidence": "첫 요구만 근거가 있다.",
                            }
                        ],
                        "residual_content": "",
                        "rationale": "전부 포함된다고 잘못 응답했다.",
                    },
                    ensure_ascii=False,
                )
            },
        )

    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(OpenAIAPIError, match="누락하거나 중복"):
        client.analyze_coverage(
            "첫 요구다.\n둘째 요구다.",
            [("C1", "첫 요구와 둘째 요구가 있다.")],
        )


def test_combined_coverage_rejects_a_duplicate_source_segment():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "coverage_status": "fully_covered",
                        "confidence": 0.99,
                        "requirements": [
                            {
                                "source_segment_id": "S1",
                                "status": "covered",
                                "evidence_candidate_ids": ["C1"],
                                "evidence": "첫 근거",
                            },
                            {
                                "source_segment_id": "S1",
                                "status": "covered",
                                "evidence_candidate_ids": ["C1"],
                                "evidence": "중복 근거",
                            },
                        ],
                        "residual_content": "",
                        "rationale": "중복된 잘못된 응답",
                    },
                    ensure_ascii=False,
                )
            },
        )

    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(OpenAIAPIError, match="누락하거나 중복"):
        client.analyze_coverage(
            "첫 요구다.\n둘째 요구다.",
            [("C1", "첫 요구와 둘째 요구가 있다.")],
        )


def test_combined_coverage_preserves_compound_source_text_verbatim():
    source = "유분을 제거하고 염분 농도를 측정한다."

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "coverage_status": "fully_covered",
                        "confidence": 0.91,
                        "requirements": [
                            {
                                "source_segment_id": "S1",
                                "status": "covered",
                                "evidence_candidate_ids": ["C1"],
                                "evidence": "두 의무가 모두 있다.",
                            }
                        ],
                        "residual_content": "",
                        "rationale": "원문 전체를 비교했다.",
                    },
                    ensure_ascii=False,
                )
            },
        )

    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    analysis = client.analyze_coverage(source, [("C1", source)])

    assert analysis.requirements[0]["requirement"] == source


def test_source_segmenter_splits_one_long_sentence_without_truncation():
    source = ("가" * 1_500) + " " + ("나" * 3_000)

    segments = coverage_source_segments(source)

    assert [source_id for source_id, _ in segments] == ["S1", "S2", "S3"]
    assert all(len(text) <= 2_000 for _, text in segments)
    assert "".join(text for _, text in segments) == source


def test_combined_coverage_rejects_global_detail_status_mismatch():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "coverage_status": "conflict",
                        "confidence": 0.91,
                        "requirements": [
                            {
                                "source_segment_id": "S1",
                                "status": "not_covered",
                                "evidence_candidate_ids": [],
                                "evidence": "명시 근거가 없다.",
                            }
                        ],
                        "residual_content": "포스코 요구사항",
                        "rationale": "전체와 세부 상태가 불일치한다.",
                    },
                    ensure_ascii=False,
                )
            },
        )

    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(OpenAIAPIError, match="모순"):
        client.analyze_coverage("포스코 요구사항", [("C1", "KCS 요구사항")])


def test_conflict_takes_priority_over_partial_overlap_evidence():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "coverage_status": "conflict",
                        "confidence": 0.87,
                        "requirements": [
                            {
                                "source_segment_id": "S1",
                                "status": "conflict",
                                "evidence_candidate_ids": ["C1"],
                                "evidence": "KCS와 수치가 충돌한다.",
                            },
                            {
                                "source_segment_id": "S2",
                                "status": "not_covered",
                                "evidence_candidate_ids": ["C1"],
                                "evidence": "KCS가 일부만 포괄한다.",
                            },
                        ],
                        "residual_content": "추가 검사를 실시한다.",
                        "rationale": "충돌을 부분 포괄보다 우선한다.",
                    },
                    ensure_ascii=False,
                )
            },
        )

    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    analysis = client.analyze_coverage(
        "도막 두께는 10 mm로 한다.\n추가 검사를 실시한다.",
        [("C1", "도막 두께는 8 mm로 하고 검사를 실시한다.")],
    )

    assert analysis.coverage_status == "conflict"


def test_merge_preserves_partial_overlap_across_source_batches():
    source_segments = (
        ("S1", "표면을 청소하고 염분을 측정한다."),
        ("S2", "도막을 검사한다."),
    )
    first = openai_ai_module.CoverageAnalysis(
        coverage_status="partially_covered",
        confidence=0.9,
        requirements=(
            {
                "requirement": source_segments[0][1],
                "source_segment_ids": ["S1"],
                "status": "not_covered",
                "evidence_candidate_ids": ["C1"],
                "evidence": "표면 청소는 KCS에 있다.",
            },
        ),
        residual_content="염분을 측정한다.",
        rationale="일부만 포괄한다.",
        model="test-model",
    )
    second = openai_ai_module.CoverageAnalysis(
        coverage_status="posco_specific",
        confidence=0.88,
        requirements=(
            {
                "requirement": source_segments[1][1],
                "source_segment_ids": ["S2"],
                "status": "not_covered",
                "evidence_candidate_ids": [],
                "evidence": "명시 근거가 없다.",
            },
        ),
        residual_content="도막을 검사한다.",
        rationale="KCS에 없다.",
        model="test-model",
    )

    merged = openai_ai_module._merge_coverage_analyses(
        source_segments,
        [((source_segments[0],), first), ((source_segments[1],), second)],
        "test-model",
    )

    assert merged.coverage_status == "partially_covered"
    assert merged.requirements[0]["evidence_candidate_ids"] == ["C1"]
    assert merged.confidence == pytest.approx(0.88)
    assert merged.residual_content == "염분을 측정한다.\n도막을 검사한다."


def test_merge_of_same_source_covered_and_not_covered_is_uncertain():
    source_segments = (("S1", "표면을 청소한다."),)
    covered = openai_ai_module.CoverageAnalysis(
        coverage_status="fully_covered",
        confidence=0.95,
        requirements=(
            {
                "requirement": source_segments[0][1],
                "source_segment_ids": ["S1"],
                "status": "covered",
                "evidence_candidate_ids": ["C1"],
                "evidence": "같은 의무가 있다.",
            },
        ),
        residual_content="",
        rationale="전체 포괄한다.",
        model="test-model",
    )
    missing = openai_ai_module.CoverageAnalysis(
        coverage_status="posco_specific",
        confidence=0.9,
        requirements=(
            {
                "requirement": source_segments[0][1],
                "source_segment_ids": ["S1"],
                "status": "not_covered",
                "evidence_candidate_ids": [],
                "evidence": "명시 근거가 없다.",
            },
        ),
        residual_content=source_segments[0][1],
        rationale="포괄하지 않는다.",
        model="test-model",
    )

    merged = openai_ai_module._merge_coverage_analyses(
        source_segments,
        [(source_segments, covered), (source_segments, missing)],
        "test-model",
    )

    assert merged.coverage_status == "uncertain"
    assert merged.requirements[0]["status"] == "uncertain"


def _dynamic_coverage_response(
    payload: dict,
    *,
    conflict_source_id: str | None = None,
    confidence: float = 0.92,
) -> httpx.Response:
    source_schema = payload["text"]["format"]["schema"]["properties"]["requirements_by_source"]
    source_ids = source_schema["required"]
    requirement_schema = source_schema["properties"][source_ids[0]]["properties"]
    candidate_ids = requirement_schema["evidence_candidate_ids"]["items"]["enum"]
    requirements = []
    for source_id in source_ids:
        status = "conflict" if source_id == conflict_source_id else "covered"
        requirements.append(
            {
                "source_segment_id": source_id,
                "status": status,
                "evidence_candidate_ids": [candidate_ids[0]],
                "evidence": "KCS 후보에 명시된 근거다.",
            }
        )
    return httpx.Response(
        200,
        json={
            "output_text": json.dumps(
                {
                    "confidence": confidence,
                    "requirements_by_source": {r.pop("source_segment_id"): r for r in requirements},
                    "residual_content": "",
                    "rationale": "분할된 입력의 요구사항을 모두 확인했다.",
                },
                ensure_ascii=False,
            )
        },
    )


def _long_posco_source(count: int = 70) -> tuple[str, list[str]]:
    segments = [
        f"요구사항 {index:03d}는 " + ("가" * 150) + "."
        for index in range(1, count + 1)
    ]
    source = "\n".join(segments)
    assert len(source) > 10_000
    assert len(segments) > 64
    return source, segments


def test_combined_coverage_chunks_long_posco_and_preserves_global_source_order():
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        return _dynamic_coverage_response(
            payload,
            confidence=0.61 if len(payloads) == 2 else 0.92,
        )

    source, expected_segments = _long_posco_source()
    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    analysis = client.analyze_coverage(source, [("C1", "모든 요구사항을 적용한다.")])

    assert len(payloads) > 1
    source_ids = [
        requirement["source_segment_ids"][0]
        for requirement in analysis.requirements
    ]
    assert source_ids == [f"S{index}" for index in range(1, 71)]
    assert len(source_ids) == len(set(source_ids))
    assert [item["requirement"] for item in analysis.requirements] == expected_segments
    assert analysis.coverage_status == "fully_covered"
    assert analysis.confidence == pytest.approx(0.61)


def test_combined_coverage_chunks_long_kcs_without_losing_original_evidence_id():
    prompted_inputs: list[str] = []
    markers = [f"KCS-MARK-{index:03d}" for index in range(1, 71)]
    candidate_text = "\n".join(
        f"{marker} " + ("나" * 110) + "." for marker in markers
    )
    assert len(candidate_text) > 8_000

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompted_inputs.append(payload["input"])
        return _dynamic_coverage_response(payload)

    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    analysis = client.analyze_coverage(
        "검사를 실시하여야 한다.",
        [("C1", candidate_text)],
    )

    combined_prompts = "\n".join(prompted_inputs)
    assert "부분 1/2" in combined_prompts
    assert "부분 2/2" in combined_prompts
    assert all(marker in combined_prompts for marker in markers)
    assert analysis.requirements[0]["evidence_candidate_ids"] == ["C1"]
    assert analysis.coverage_status == "fully_covered"


def test_combined_coverage_conservatively_merges_conflict_from_one_call():
    call_count = 0
    conflict_source_id: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count, conflict_source_id
        call_count += 1
        payload = json.loads(request.content)
        source_ids = payload["text"]["format"]["schema"]["properties"][
            "requirements_by_source"
        ]["required"]
        if call_count == 2:
            conflict_source_id = source_ids[0]
        return _dynamic_coverage_response(
            payload,
            conflict_source_id=conflict_source_id if call_count == 2 else None,
        )

    source, _ = _long_posco_source()
    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    analysis = client.analyze_coverage(source, [("C1", "검사 요구사항을 규정한다.")])

    assert call_count > 1
    assert conflict_source_id is not None
    assert analysis.coverage_status == "conflict"
    conflict_requirements = [
        item for item in analysis.requirements if item["status"] == "conflict"
    ]
    assert [item["source_segment_ids"][0] for item in conflict_requirements] == [
        conflict_source_id
    ]


def test_combined_coverage_raises_when_one_chunk_call_fails():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return httpx.Response(500)
        return _dynamic_coverage_response(json.loads(request.content))

    source, _ = _long_posco_source()
    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(OpenAIAPIError):
        client.analyze_coverage(source, [("C1", "검사 요구사항을 규정한다.")])
    assert call_count >= 2


def test_one_logical_source_split_across_batches_cannot_be_fully_covered():
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        return _dynamic_coverage_response(payload)

    source = "하나의 적용 조건과 예외를 포함한 장문 의무 " + ("가" * 8_500)
    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    analysis = client.analyze_coverage(source, [("C1", "관련 KCS 요구사항")])

    assert len(payloads) > 1
    assert analysis.coverage_status == "uncertain"
    assert {item["status"] for item in analysis.requirements} == {"uncertain"}
    assert all(item["evidence_candidate_ids"] == ["C1"] for item in analysis.requirements)
    assert "담당자 확인" in analysis.rationale
    assert analysis.residual_content == source


def test_candidate_split_across_packs_cannot_be_fully_covered():
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        return _dynamic_coverage_response(payload)

    candidate_text = "KCS 장문 본문 " + ("나" * 50_000)
    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    analysis = client.analyze_coverage(
        "적용 범위와 예외를 모두 확인한다.",
        [("C1", candidate_text)],
    )

    assert len(payloads) > 1
    assert analysis.coverage_status == "uncertain"
    assert analysis.requirements[0]["status"] == "uncertain"
    assert analysis.requirements[0]["evidence_candidate_ids"] == ["C1"]
    assert "KCS 후보가 여러 입력 묶음" in analysis.rationale
    assert analysis.residual_content == "적용 범위와 예외를 모두 확인한다."


def test_coverage_call_limit_allows_64_and_rejects_65_before_network(monkeypatch):
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        return _dynamic_coverage_response(payload)

    monkeypatch.setattr(openai_ai_module, "MAX_COVERAGE_SOURCE_SEGMENTS_PER_CALL", 1)
    client = OpenAIClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    sixty_four = "\n".join(f"독립 요구 {index}." for index in range(64))

    analysis = client.analyze_coverage(sixty_four, [("C1", "KCS")])

    assert len(payloads) == 64
    assert analysis.coverage_status == "fully_covered"

    payloads.clear()
    sixty_five = f"{sixty_four}\n독립 요구 64."
    with pytest.raises(ValueError, match="안전 상한 64회"):
        client.analyze_coverage(sixty_five, [("C1", "KCS")])
    assert payloads == []


def _write_kcs(raw_dir, records):
    raw_dir.mkdir(parents=True)
    payload = [
        {
            "codeType": "KCS",
            "code": "413105",
            "fullCode": "KCS 41 31 05",
            "name": "건축물 강구조공사 일반사항",
            "version": "2026",
            "updateDate": "2026-08-01",
            "list": records,
        }
    ]
    (raw_dir / "KCS_413105.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _clause(text: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "source_type": "paragraph",
        "title": "표면처리",
        "content": text,
    }


def test_load_kcs_sections_preserves_content_beyond_8000_characters(tmp_path):
    raw = tmp_path / "raw"
    marker = "KCS-CONTENT-AFTER-8000"
    _write_kcs(
        raw,
        [
            {
                "label": "3.2.1",
                "title": "장문 원문",
                "contents": f"<p>{'가' * 8_100}{marker}</p>",
            }
        ],
    )

    sections = load_kcs_sections(str(raw), ("4131",))

    assert len(sections) == 1
    assert len(sections[0]["content"]) > 8_000
    assert sections[0]["content"].endswith(marker)


def test_matcher_falls_back_locally_and_never_returns_more_than_three(tmp_path):
    raw = tmp_path / "raw"
    source = "강재의 표면은 도장 전에 먼지와 유분 및 이물질을 완전히 제거하여야 한다."
    _write_kcs(
        raw,
        [
            {
                "label": f"3.2.{index}",
                "title": "표면처리",
                "contents": f"<p>{source} 작업 순서 {index}</p>",
            }
            for index in range(1, 6)
        ]
        + [
            {
                "label": "9.9.9",
                "title": "배수",
                "contents": "<p>옥상 배수구의 위치를 확인한다.</p>",
            }
        ],
    )
    clauses = [_clause(source)]
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    failing_client = OpenAIClient(
        "test-key", http_client=httpx.Client(transport=httpx.MockTransport(fail))
    )

    signature = match_clauses(clauses, raw, ("4131",), failing_client)

    assert 1 <= len(clauses[0]["candidates"]) <= 3
    assert all(candidate["score"] >= 0.25 for candidate in clauses[0]["candidates"])
    assert all(candidate["kcs_clause"] != "9.9.9" for candidate in clauses[0]["candidates"])
    assert signature["mode"] == "local"
    assert signature["require_embeddings"] is False
    failing_client.close()


def test_matcher_strict_embeddings_succeeds_and_returns_signature(tmp_path):
    raw = tmp_path / "raw"
    source = "강재의 표면은 도장 전에 먼지와 유분을 제거하여야 한다."
    _write_kcs(
        raw,
        [
            {"label": "3.2.1", "title": "표면처리", "contents": f"<p>{source}</p>"},
            {
                "label": "3.2.2",
                "title": "표면검사",
                "contents": f"<p>{source} 이후 표면을 검사한다.</p>",
            },
        ],
    )

    class BatchEmbeddingStub:
        available = True
        embeddings_available = True
        embedding_model = "test-embedding"
        embedding_dimensions = 64

        @staticmethod
        def embedding_score_groups(queries, document_groups):
            assert len(queries) == len(document_groups) == 1
            return [[1.0 if "검사" in document else 0.1 for document in document_groups[0]]]

    clause = _clause(source)
    signature = match_clauses(
        [clause],
        raw,
        ("4131",),
        BatchEmbeddingStub(),
        require_embeddings=True,
    )

    assert clause["candidates"][0]["kcs_clause"] == "3.2.2"
    assert signature == {
        "mode": "openai_embeddings",
        "require_embeddings": True,
        "embedding_model": "test-embedding",
        "embedding_dimensions": 64,
    }


@pytest.mark.parametrize(
    "client",
    [
        None,
        type(
            "UnavailableEmbeddingStub",
            (),
            {"available": False, "embeddings_available": False},
        )(),
    ],
    ids=("missing-client", "unavailable-client"),
)
def test_matcher_strict_embeddings_rejects_unavailable_client_without_mutation(
    tmp_path,
    client,
):
    clause = _clause("엄격 임베딩이 필요한 조항")
    clause["candidates"] = [{"id": "existing-candidate"}]

    with pytest.raises(OpenAIAPIError, match="엄격 재매칭"):
        match_clauses(
            [clause],
            tmp_path / "unused-raw",
            ("4131",),
            client,
            require_embeddings=True,
        )

    assert clause["candidates"] == [{"id": "existing-candidate"}]


def test_matcher_strict_batch_failure_is_atomic(tmp_path):
    raw = tmp_path / "raw"
    source = "강재의 표면은 도장 전에 먼지와 유분을 제거하여야 한다."
    _write_kcs(
        raw,
        [{"label": "3.2.1", "title": "표면처리", "contents": f"<p>{source}</p>"}],
    )

    class FailingBatchStub:
        available = True
        embeddings_available = True

        @staticmethod
        def embedding_score_groups(_queries, _document_groups):
            raise OpenAIAPIError("의도한 batch 실패")

    heading = {
        "id": "heading",
        "source_type": "heading",
        "title": "1 일반사항",
        "content": "",
        "candidates": [{"id": "existing-heading"}],
    }
    clauses = [heading, _clause(source), _clause(source)]
    clauses[1]["candidates"] = [{"id": "existing-first"}]
    clauses[2]["candidates"] = [{"id": "existing-second"}]

    with pytest.raises(OpenAIAPIError, match="batch 실패"):
        match_clauses(
            clauses,
            raw,
            ("4131",),
            FailingBatchStub(),
            require_embeddings=True,
        )

    assert [clause["candidates"] for clause in clauses] == [
        [{"id": "existing-heading"}],
        [{"id": "existing-first"}],
        [{"id": "existing-second"}],
    ]


def test_matcher_strict_individual_failure_is_atomic(tmp_path):
    raw = tmp_path / "raw"
    source = "강재의 표면은 도장 전에 먼지와 유분을 제거하여야 한다."
    _write_kcs(
        raw,
        [{"label": "3.2.1", "title": "표면처리", "contents": f"<p>{source}</p>"}],
    )

    class FailingIndividualStub:
        available = True
        embeddings_available = True

        def __init__(self):
            self.calls = 0

        def embedding_scores(self, _query, documents):
            self.calls += 1
            if self.calls == 2:
                raise OpenAIAPIError("의도한 individual 실패")
            return [0.9] * len(documents)

    clauses = [_clause(source), _clause(source)]
    clauses[0]["candidates"] = [{"id": "existing-first"}]
    clauses[1]["candidates"] = [{"id": "existing-second"}]

    with pytest.raises(OpenAIAPIError, match="individual 실패"):
        match_clauses(
            clauses,
            raw,
            ("4131",),
            FailingIndividualStub(),
            require_embeddings=True,
        )

    assert [clause["candidates"] for clause in clauses] == [
        [{"id": "existing-first"}],
        [{"id": "existing-second"}],
    ]


def test_embedding_rerank_changes_order_and_keeps_unrelated_below_cutoff(tmp_path):
    raw = tmp_path / "raw"
    first = "강재 표면 도장 전에 먼지 유분 이물질을 제거하여야 한다."
    second = "강재 표면 도장 전에 먼지 유분 이물질을 제거하고 검사하여야 한다."
    _write_kcs(
        raw,
        [
            {"label": "3.2.1", "title": "표면처리", "contents": f"<p>{first}</p>"},
            {"label": "3.2.2", "title": "표면처리", "contents": f"<p>{second}</p>"},
            {"label": "9.9.9", "title": "배수", "contents": "<p>옥상 배수구를 설치한다.</p>"},
        ],
    )

    class SemanticStub:
        available = True

        @staticmethod
        def embedding_scores(query, documents):
            return [1.0 if "검사" in document else 0.0 for document in documents]

    clauses = [_clause(first)]
    match_clauses(clauses, raw, ("4131",), SemanticStub())

    assert clauses[0]["candidates"][0]["kcs_clause"] == "3.2.2"
    assert all(candidate["kcs_clause"] != "9.9.9" for candidate in clauses[0]["candidates"])


def test_semantic_score_can_promote_a_weak_lexical_shortlist_item(tmp_path):
    raw = tmp_path / "raw"
    source = "모든 작업원은 추락방지용 안전대를 몸에 매어야 한다."
    target = "근로자는 고소 위치에서 개인용 낙상 보호구를 착용한다."
    _write_kcs(
        raw,
        [{"label": "1.1", "title": "개인 보호", "contents": f"<p>{target}</p>"}],
    )
    clause = {
        "id": str(uuid.uuid4()),
        "source_type": "paragraph",
        "title": "작업자 안전",
        "content": source,
    }
    local = _rank_matches(f"{clause['title']} {clause['title']} {source}", raw, ("4131",))
    assert local and local[0][1] < 0.25

    class SemanticStub:
        available = True
        embeddings_available = True

        @staticmethod
        def embedding_scores(query, documents):
            assert documents
            return [0.95] * len(documents)

    match_clauses([clause], raw, ("4131",), SemanticStub())

    assert clause["candidates"][0]["kcs_clause"] == "1.1"
    assert clause["candidates"][0]["score"] >= 0.25


def test_embedding_failure_opens_circuit_for_remaining_clauses(tmp_path):
    raw = tmp_path / "raw"
    source = "강재의 표면은 도장 전에 먼지와 유분을 제거하여야 한다."
    _write_kcs(
        raw,
        [{"label": "3.2.1", "title": "표면처리", "contents": f"<p>{source}</p>"}],
    )
    calls = 0

    def fail(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline", request=request)

    client = OpenAIClient(
        "test-key", http_client=httpx.Client(transport=httpx.MockTransport(fail))
    )
    clauses = [_clause(source) for _ in range(3)]

    match_clauses(clauses, raw, ("4131",), client)

    assert calls == 1
    assert client.embeddings_available is False
    assert all(clause["candidates"] for clause in clauses)


def test_weak_scoped_search_is_supplemented_from_all_kcs(tmp_path):
    raw = tmp_path / "raw"
    _write_kcs(
        raw,
        [{"label": "3.2.1", "title": "용접", "contents": "<p>강재를 용접한다.</p>"}],
    )
    global_payload = [
        {
            "codeType": "KCS",
            "code": "414001",
            "fullCode": "KCS 41 40 01",
            "name": "건축물 방수공사 일반사항",
            "version": "2026",
            "updateDate": "2026-08-01",
            "list": [
                {
                    "label": "3.1.1",
                    "title": "방수 바탕면",
                    "contents": "<p>방수 바탕면의 먼지와 유분 및 이물질을 완전히 제거하여야 한다.</p>",
                }
            ],
        }
    ]
    (raw / "KCS_414001.json").write_text(
        json.dumps(global_payload, ensure_ascii=False), encoding="utf-8"
    )
    clause = {
        "id": str(uuid.uuid4()),
        "source_type": "paragraph",
        "title": "방수 바탕면",
        "content": "방수 바탕면의 먼지와 유분 및 이물질을 완전히 제거하여야 한다.",
    }

    match_clauses([clause], raw, ("4131",))

    assert clause["candidates"]
    assert clause["candidates"][0]["kcs_code"] == "KCS 41 40 01"


def test_explicit_kcs_references_reserve_one_candidate_per_code_without_score_boost(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    for code, name in (
        ("111111", "첫 번째 기준"),
        ("222222", "두 번째 기준"),
        ("333333", "세 번째 기준"),
    ):
        payload = [
            {
                "codeType": "KCS",
                "code": code,
                "fullCode": f"KCS {code[:2]} {code[2:4]} {code[4:]}",
                "name": name,
                "version": "2026",
                "updateDate": "2026-08-01",
                "list": [
                    {
                        "label": "1.1",
                        "title": name,
                        "contents": "<p>서로 다른 기술 요구사항을 규정한다.</p>",
                    }
                ],
            }
        ]
        (raw / f"KCS_{code}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    clause = {
        "id": str(uuid.uuid4()),
        "source_type": "paragraph",
        "title": "참고 기준",
        "content": "KCS 11 11 11, KCS 22 22 22 및 KCS 33 33 33을 따른다.",
    }

    match_clauses([clause], raw, ("9999",))

    assert {item["kcs_code"] for item in clause["candidates"]} == {
        "KCS 11 11 11",
        "KCS 22 22 22",
        "KCS 33 33 33",
    }
    assert all(item["classification"] == "의미 검증 미완료" for item in clause["candidates"])
    assert all("포스코 원문이 이 KCS 코드를 직접 참조합니다." in item["reasons"] for item in clause["candidates"])


def test_scoped_data_corruption_is_not_silently_treated_as_no_match(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    (raw / "KCS_413105.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="손상된 KCS JSON"):
        _rank_matches("강구조 검사", raw, ("4131",))


def test_internal_lexical_pool_keeps_forty_items_for_semantic_reranking(tmp_path):
    raw = tmp_path / "raw"
    source = "강재 표면의 이물질과 유분을 제거하여야 한다."
    _write_kcs(
        raw,
        [
            {
                "label": f"3.2.{index}",
                "title": "표면처리",
                "contents": f"<p>{source} 검사 순서 {index}</p>",
            }
            for index in range(55)
        ],
    )

    ranked = _rank_matches(source, raw, ("4131",))

    assert len(ranked) == 40


def test_displayed_scores_use_the_same_order_as_scope_aware_ranking():
    ranked = [
        ({"code": "KCS 41 31 15", "clause": "1.1", "content": "공작도", "_equivalent_scope": True}, 0.5),
        ({"code": "KCS 41 31 20", "clause": "1.2", "content": "철골 제작"}, 0.55),
    ]

    result = _rerank_from_scores(ranked, [0.5, 0.5])

    assert result[0][0]["clause"] == "1.1"
    assert result[0][1] >= result[1][1]


def test_candidate_rows_remove_duplicate_versions_of_the_same_kcs_clause():
    clause_id = str(uuid.uuid4())
    clause = {"id": clause_id}
    shared = {
        "code": "KCS 41 31 15",
        "document_name": "건축물 강구조공사",
        "version": "2026",
        "update_date": "2026-08-01",
        "clause": "3.2.1",
        "title": "공작도",
    }
    ranked = [
        ({**shared, "content": "공작도를 작성한다."}, 0.7, None),
        ({**shared, "content": "공작도를 작성하고 승인받는다."}, 0.69, None),
    ]

    candidates = _candidate_rows(clause, "공작도를 작성한다.", ranked)

    assert len(candidates) == 1


def test_candidate_rows_preserve_distinct_bodies_with_same_symbol_label():
    clause = {"id": str(uuid.uuid4())}
    shared = {
        "code": "KCS 24 31 10",
        "document_name": "강구조공사",
        "version": "2026",
        "update_date": "2026-08-01",
        "clause": "①",
        "title": "검사방법",
    }
    ranked = [
        ({**shared, "content": "용접부를 검사한다."}, 0.51, None),
        ({**shared, "content": "스터드 용접부를 검사한다."}, 0.48, None),
    ]

    assert len(_candidate_rows(clause, "용접부를 검사한다.", ranked)) == 2


def test_scope_bonus_does_not_publish_a_low_relevance_candidate():
    clause = {"id": str(uuid.uuid4())}
    section = {
        "code": "KCS 41 31 15",
        "document_name": "건축물 강구조공사",
        "version": "2026",
        "update_date": "2026-08-01",
        "clause": "3.2.1",
        "title": "공작도",
        "content": "공작도를 작성한다.",
        "_equivalent_scope": True,
    }

    ranked = _rank_without_embeddings([(section, 0.04)])
    candidates = _candidate_rows(clause, "관련 없는 원문", ranked)

    assert ranked[0][1] > ranked[0][0]["_candidate_relevance_score"]
    assert candidates == []
