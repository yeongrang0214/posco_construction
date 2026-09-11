"""Audited KS scope metadata, not a substitute for KS requirements or KCS text."""
from __future__ import annotations

import re

from .relevance import verification_body

STANDARD_LINK_VERSION = "ks-scope-2026-09-11-v1"
KS_RE = re.compile(r"(?<![A-Za-z0-9])KS\s*([A-Z])\s*[-_]?\s*(\d{3,})(?![A-Za-z0-9])", re.I)

# Metadata verified against the official publisher. Confirmation is not revision.
# Do not extrapolate this scope or technical requirements to unverified standards.
REFRACTORY_MORTAR = {
    "standard": "KS L 3202",
    "name": "내화 모르타르",
    "status": "2026-05-27 확인 (기술 개정일 아님)",
    "verified_at": "2026-09-11",
    "scope": "내화벽돌 및 내화 단열벽돌 쌓기에 사용하는 열경성 모르타르",
    "source_url": "https://www.kssn.net/search/stddetail.do?itemNo=K001010109603",
    "verification_level": "공식 표준명·적용 범위·이력 확인; KS 세부 전문 미대조",
}

STANDARD_COMPARISON_RULES = (
    " KS 번호의 표기 차이(KSL3202/KS L 3202)와 몰탈/모르터/모르타르는 정규화하여 비교한다. "
    "KCS가 '한국산업표준에 적합'으로 표현한 경우 번호가 없다는 사실만으로 대상이 다르거나 "
    "회사 고유 요구라고 판단하지 않는다. 제공된 verified_standard_links의 공식 적용 범위와 "
    "양쪽 본문의 자재·용도를 연결하여 의미 대응 여부를 먼저 판단한다. "
    "예를 들어 내화벽돌 조적용 내화몰탈과 내화벽돌 쌓기에 사용하는 모르타르는 같은 용도의 "
    "요구일 수 있다. 제목의 '단열'만으로 본문의 명시적 용도를 무시하지 않는다. "
    "단, 모든 단열 모르타르와 내화 모르타르가 동등하다는 뜻은 아니다. "
    "표준 연계 정보는 KCS 본문 인용이나 전체포괄 증명이 아니다. 확인되지 않은 KS 번호·등급·"
    "시험 조건을 추측하지 않는다. 간접 연계만 확인된 경우 대상·품질 적합 요구의 대응은 인정하되, "
    "세부 조건의 완전 대체 여부는 별도로 확인하고 그 불확실성을 설명한다. "
)


def ks_codes(text: str) -> set[str]:
    return {f"KS {m[0].upper()} {m[1]}" for m in KS_RE.findall(text)}


def standard_links(source: str, target: str) -> list[dict]:
    """Link only an explicit source KS to a matching use in the actual KCS body."""
    source_body, target_body = verification_body(source), verification_body(target)
    if "KS L 3202" not in ks_codes(source_body) or not re.search(r"모르타르|모르터|몰탈", source_body):
        return []
    # A heading, storage clause, or generic cement mortar cannot establish this link.
    requirement = re.search(
        r"내화\s*벽돌[^\n.!?]{0,60}(?:쌓|조적)[^\n.!?]{0,60}"
        r"(?:모르타르|모르터|몰탈)[^\n.!?]{0,80}"
        r"(?:한국\s*산업\s*표준|KS)[^\n.!?]{0,40}적합[^\n.!?]*",
        target_body,
    )
    if not requirement:
        return []
    explicit_codes = ks_codes(requirement.group(0))
    if explicit_codes and "KS L 3202" not in explicit_codes:
        return []
    return [{
        **REFRACTORY_MORTAR,
        "reference_type": "explicit" if "KS L 3202" in explicit_codes else "indirect",
        "kcs_requirement": requirement.group(0).strip(),
        "comparison_note": "같은 용도의 자재 품질 요구에 대응합니다. KS 세부 조건의 전체 대체는 별도 확인이 필요합니다.",
    }]


def indirect_standard_links(source: str, target: str) -> list[dict]:
    return [link for link in standard_links(source, target) if link["reference_type"] == "indirect"]
