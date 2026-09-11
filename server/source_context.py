"""Read-only interpretation of source table references, never a source rewrite."""
from __future__ import annotations

import re

from .documents import _is_table_header
from .standard_links import ks_codes

DITTO = re.compile(r'^(?:상동|동상|위와같음|동일|["〃″])$')


def enrich_source_context(clauses):
    """Add derived context in source order; never inherit across table boundaries.

    Keep raw cells for exports and audit. Empty cells are NOT ditto marks. Only
    explicit KS references attached to the exact same material can be inherited.
    """
    tables, materials = {}, {}
    for clause in sorted(clauses, key=lambda row: row.get("source_order", 0)):
        clause.pop("analysis_context", None)
        clause.pop("resolved_table_cells", None)
        clause.pop("table_standard_notation", None)
        match = re.fullmatch(r"표\s*(\d+)-(\d+)", str(clause.get("label") or ""))
        if not match or "|" not in str(clause.get("content") or ""):
            continue
        cells = [cell.strip() for cell in clause["content"].split("|")]
        state = tables.setdefault(match[1], {"headers": [], "previous": []})
        if _is_table_header(cells) or clause.get("source_type") == "heading":
            state.update(headers=cells, previous=[])
            continue
        resolved, notes = list(cells), []
        previous = state["previous"]
        for index, cell in enumerate(cells):
            if DITTO.fullmatch(re.sub(r"\s+", "", cell)):
                if len(previous) == len(cells) and previous[index] and not DITTO.fullmatch(re.sub(r"\s+", "", previous[index])):
                    resolved[index] = previous[index]
                    notes.append(f"{index + 1}열 '{cell}'은 같은 표 앞 행의 '{previous[index]}'을 참조")
                else:
                    notes.append(f"{index + 1}열 '{cell}'의 참조 원문 미확인; 추정 금지")
        headers = state["headers"]
        if len(headers) == len(cells):
            notes.insert(0, "[표 열별 원문] " + " | ".join(f"{h}: {v}" for h, v in zip(headers, resolved)))
        state["previous"] = resolved
        clause["resolved_table_cells"] = resolved
        material = re.sub(r"\s+", "", resolved[0])
        if len(cells) == 5 and ks_codes(resolved[2]):
            clause["table_standard_notation"] = resolved[2]
            materials.setdefault(material, set()).add(resolved[2])
        elif len(cells) == 4 and len(headers) == 4 and "시료" in re.sub(r"\s+", "", headers[2]):
            references = materials.get(material, set())
            if references:
                notation = "; ".join(sorted(references))
                clause["table_standard_notation"] = notation
                notes.append(f"[같은 문서의 동일 자재 시험규격 참조] {notation}; 시험방법과 현장 빈도·시료량은 별도로 비교")
        clause["analysis_context"] = "\n".join(notes)
    return clauses
