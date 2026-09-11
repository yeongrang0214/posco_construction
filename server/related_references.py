"""Read-only, topic-scoped references. Never create selectable match candidates."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .matcher import load_kcs_sections, normalize_key
from .relevance import commercial_only, masonry_material_conflict, own_requirement


def find_related_references(
    clause: dict[str, Any], raw_dir: Path, document_context: str = ""
) -> dict[str, Any]:
    source = own_requirement(clause)
    context = f"{document_context} {clause.get('match_context', '')} {source}"
    # This first audited topic is masonry piping, not all vaguely similar text.
    # No label/chapter number or particular uploaded document is required.
    applicable = (
        clause.get("source_type") == "paragraph"
        and bool(re.search(r"배관|전선관|파이프", source))
        and bool(re.search(r"조적|벽돌|블록", context))
        and not commercial_only(source)
    )
    result: dict[str, Any] = {
        "applicable": applicable,
        "references": [],
        "search_scope": "KCS 41 34 조적공사 · 배관 매설" if applicable else "",
        "notice": "참고 조항은 요구사항의 일치·전체 포괄을 검증한 근거가 아닙니다. KCS 중복 삭제 근거로 선택되지 않습니다.",
    }
    if not applicable:
        return result

    existing = {
        (candidate.get("kcs_code"), normalize_key(candidate.get("content", "")))
        for candidate in [*clause.get("candidates", []), *clause.get("excluded_candidates", [])]
    }
    ranked = []
    for section in load_kcs_sections(str(raw_dir), ("4134",)):
        content = section["content"]
        # Require the actual section subject AND its body to concern piping.
        # Straight joints, floor ducts and oil pipelines are not masonry piping.
        if not re.search(r"배관|파이프|전선관", section["title"]):
            continue
        if not re.search(r"배관|파이프|전선관", content):
            continue
        if normalize_key(content) == normalize_key(section["title"]):
            continue
        target_context = f"{section['document_name']} {section['title']}"
        if masonry_material_conflict(source, target_context) or masonry_material_conflict(context, target_context):
            continue
        if (section["code"], normalize_key(section.get("display_content") or content)) in existing:
            continue
        shared_directions = [word for word in ("수평", "수직") if word in source and word in content]
        directional = bool(re.search(r"일직선|직선", source))
        note = "배관 매설에 관한 참고 원문입니다. 현재 조항을 직접 대체하는지는 확인하지 않았습니다."
        if directional and re.search(r"수직.*수평|수평.*수직", content) and "관통" in content and not re.search(r"직선|직각|곧게", content):
            note = "관통 방향·슬리브 조건에 관한 조항입니다. 벽체 내 배관 경로를 일직선으로 시공하라는 요구까지 직접 정한 문구는 아닙니다."
        identity = f"{section['code']}:{section['version']}:{section['title']}:{section['clause']}:{content}"
        reference = {
            "id": "reference:" + hashlib.sha256(identity.encode()).hexdigest()[:24],
            "usage": "reference_only",
            "deletion_eligible": False,
            "kcs_code": section["code"],
            "document_name": section["document_name"],
            "version": section["version"],
            "update_date": section["update_date"],
            "title": section["title"],
            "kcs_clause": section["clause"],
            "content": section.get("display_content") or content,
            "comparison_note": note,
            "scope_note": f"{section['document_name']}의 기준입니다. 해당 조적 재료와 시공 부위에 적용되는지 확인하세요.",
        }
        ranked.append((len(shared_directions), reference))
    ranked.sort(key=lambda item: (-item[0], item[1]["kcs_code"], item[1]["title"], item[1]["kcs_clause"]))
    # Neighboring rows often carry the same expanded context. One best passage
    # per subsection avoids showing those as three different related standards.
    seen = set()
    for _, reference in ranked:
        key = (reference["kcs_code"], reference["title"])
        if key in seen:
            continue
        seen.add(key)
        result["references"].append(reference)
        if len(result["references"]) == 3:
            break
    return result
