"""Conservative query construction and candidate relevance checks, not deletion decisions."""
from __future__ import annotations

import re
from typing import Any

COMMERCIAL = re.compile(r"단가|간접비|직접비|경비|공사비|계약금|정산|대금|비용|지급|대가|부담|도급금액|책임을\s*진다|책임으로\s*한다")
TECHNICAL = re.compile(r"(?:설치|시공|시험|검사|용접|제작|측정|보관|사용|준수|공급|작성|제출)\s*(?:하여야|해야|한다|할\s*것)|이상|이하|미만|초과|금지")
BACKWARD = re.compile(r"(?:상기|전항|앞의|위의|그러하지|그\s*(?:기준|내용|방법))|(?:^|[.。]\s*)(?:이를|이에)\b")
FORWARD = re.compile(r"(?:아래|다음|하기)(?:의)?\s*(?:표|기준|내용|각호|규격|사항)|(?:표|그림)\s*\d+(?:[-.]\d+)*\s*(?:과|와|에).*?(?:같이|따라)")


def own_requirement(clause: dict[str, Any]) -> str:
    title = str(clause.get("title") or "").strip().removesuffix("…")
    content = str(clause.get("content") or "").strip()
    if content.startswith(title):
        return content
    return f"{title} {content}".strip()


def query_text(clauses: list[dict[str, Any]], index: int) -> str:
    clause = clauses[index]
    own = own_requirement(clause)
    context = str(clause.get("match_context") or "")
    parts = [own]
    # Shortness alone never justifies importing another requirement. Respect headings.
    for direction, pattern in ((-1, BACKWARD), (1, FORWARD)):
        neighbor_index = index + direction
        if not pattern.search(own) or not 0 <= neighbor_index < len(clauses):
            continue
        neighbor = clauses[neighbor_index]
        if clause.get("source_order") is not None and neighbor.get("source_order") is not None:
            if neighbor["source_order"] - clause["source_order"] != direction:
                continue
        if neighbor.get("source_type") == "heading":
            continue
        if str(neighbor.get("match_context") or "") != context:
            continue
        parts.append(own_requirement(neighbor))
    # Hierarchy disambiguates terminology, but is not repeated as if it were body text.
    if context and not commercial_only(own):
        parts.append(context[-160:])
    return " ".join(parts)


def contextual_requirement(clause: dict[str, Any]) -> str:
    own = own_requirement(clause)
    if clause.get("source_type") == "table" and clause.get("match_context"):
        return f"[원문 표 제목·열 문맥] {clause['match_context']}\n[현재 표 행] {own}"
    return own


def commercial_only(text: str) -> bool:
    return bool(COMMERCIAL.search(text)) and not bool(TECHNICAL.search(text))


def clearly_unrelated(source: str, target: str) -> bool:
    # A price/allocation requirement cannot be met by a purely technical provision.
    # Mixed clauses stay eligible for partial-overlap review.
    return commercial_only(source) and not bool(COMMERCIAL.search(target))


def evidence_present(quote: str, text: str) -> bool:
    normalize = lambda value: re.sub(r"\s+", "", str(value)).casefold()
    needle = normalize(quote)
    return len(needle) >= 4 and needle in normalize(text)


def apply_verdicts(candidates: list[dict], verdicts: list[dict] | None) -> list[dict]:
    labels = {"related": "대응 요구사항 확인", "partial": "일부 요구사항 대응", "uncertain": "의미 검증 미완료"}
    kept = []
    for index, candidate in enumerate(candidates):
        verdict = verdicts[index] if verdicts and index < len(verdicts) else {}
        relation = verdict.get("relation", "uncertain")
        if relation == "unrelated":
            continue
        row = dict(candidate)
        requires_verification = row.pop("_requires_verification", False)
        if requires_verification and relation not in {"related", "partial"}:
            continue
        row["rank"] = len(kept) + 1
        row["classification"] = labels.get(relation, labels["uncertain"])
        row["reasons"] = [f"의미 검증: {row['classification']}", *row.get("reasons", [])]
        if verdict.get("reason"):
            row["reasons"].append(str(verdict["reason"]))
        if relation in {"related", "partial"}:
            row["reasons"].append(f"대응 본문: {verdict['target_quote']}")
        else:
            row["warnings"] = [*row.get("warnings", []), "검색 후보이며 의미 대응은 확인되지 않았습니다."]
        kept.append(row)
        if len(kept) == 3:
            break
    return kept
