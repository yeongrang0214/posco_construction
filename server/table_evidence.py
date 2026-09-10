"""Exact material identifiers in source table rows; never full-clause coverage.

KCS is construction evidence. KDS remains a separate, read-only design reference.
No grade/thickness or substitution equivalence is inferred from similar names.
"""
from __future__ import annotations

import functools
import json
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from lxml import html

from .kcs_sync import _load_api_key, _request_json, select_kcs_record, kcs_read_lock

KS = re.compile(r"\bKS\s*([A-Z])\s*(\d{4,5})(?!\d)", re.I)
GRADE = re.compile(r"(?<![A-Z0-9])((?:SM|SS|SN|SHN|SMA|HSA|SGT|SRT|SNRT|SNT|SSC|SWH|SDP)\s*\d{2,4})([A-Z]*(?:-TMC)?)(?![A-Z0-9])")
# Retrieval route, NOT a copied standard or a claim about its contents.
# Additional disciplines can register their design reference families here.
DESIGN_ROUTES = {"4131": ("413010",)}
_design_cache: dict[str, tuple[float, dict]] = {}
_design_lock = threading.Lock()


def normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).upper()


def ks_codes(value: str) -> list[str]:
    return list(dict.fromkeys(f"KS {letter.upper()} {number}" for letter, number in KS.findall(normalized(value))))


def grades(value: str) -> set[str]:
    text = normalized(value)
    result = set()
    for match in GRADE.finditer(text):
        base = re.sub(r"\s+", "", match[1])
        result.add(base + match[2])
        # KCSC tables abbreviate SM275A, B, C, D. Expand only comma-delimited
        # suffixes following that exact base, never arbitrary nearby letters.
        tail = text[match.end():]
        while suffix := re.match(r"\s*,\s*(-TMC|[A-Z]{1,2})(?![A-Z0-9])", tail):
            # '-TMC' alone does not identify which preceding grades it qualifies.
            # Keep that notation in the excerpt, but do not invent grade variants.
            if suffix[1] != "-TMC":
                result.add(base + suffix[1])
            tail = tail[suffix.end():]
    return result


def source_fields(clause: dict) -> dict | None:
    if clause.get("source_type") != "table":
        return None
    body = str(clause.get("content") or "")
    context = str(clause.get("match_context") or "")
    # Only the nearest caption supplies the KS reference; a distant ancestor
    # must not leak a different KS number into a subsequent table.
    caption_parts = [p.strip() for p in context.split(" > ") if not p.startswith("[표 열]")]
    caption = caption_parts[-1] if caption_parts else ""
    standards = ks_codes(body) or ks_codes(caption)
    identifiers = sorted(grades(body))
    if not standards or not identifiers:
        return None
    thickness = re.findall(r"\d+(?:\.\d+)?\s*(?:mm|㎜)\s*(?:이하|이상|미만|초과)?", body, re.I)
    products = [name for name in ("강판", "강대", "형강", "평강", "봉강", "강관") if name in body]
    return {"caption": caption, "context": context, "standards": standards,
            "grades": identifiers, "thickness": thickness, "products": products, "row": body}


def record_rows(record: dict, kind: str) -> list[dict]:
    result = []
    for section in record.get("list") or []:
        markup = str(section.get("contents") or "")
        if "<table" not in markup.lower():
            continue
        try:
            root = html.fromstring(markup)
        except (ValueError, html.etree.ParserError):
            continue
        for table in root.xpath("descendant-or-self::table"):
            inherited: dict[int, tuple[str, int]] = {}
            for row in table.xpath("./tr | ./tbody/tr | ./thead/tr"):
                columns = {i: text for i, (text, remaining) in inherited.items() if remaining > 0}
                inherited = {i: (text, remaining - 1) for i, (text, remaining) in inherited.items() if remaining > 1}
                col = 0
                for cell in row.xpath("./td | ./th"):
                    while col in columns:
                        col += 1
                    value = re.sub(r"\s+", " ", " ".join(cell.itertext())).strip()
                    try:
                        rowspan = max(1, min(100, int(cell.get("rowspan", "1"))))
                        colspan = max(1, min(100, int(cell.get("colspan", "1"))))
                    except ValueError:
                        rowspan = colspan = 1
                    for offset in range(colspan):
                        columns[col + offset] = value
                        if rowspan > 1:
                            inherited[col + offset] = (value, rowspan - 1)
                    col += colspan
                excerpt = " | ".join(columns[i] for i in sorted(columns))
                if not ks_codes(excerpt) or not grades(excerpt):
                    continue
                code = re.sub(r"\D", "", str(record.get("code") or ""))
                result.append({"kind": kind, "code": f"{kind} {code[:2]} {code[2:4]} {code[4:]}",
                    "document_name": str(record.get("name") or ""), "version": str(record.get("version") or ""),
                    "update_date": str(record.get("updateDate") or ""), "section": str(section.get("title") or ""),
                    "table": str(section.get("label") or "표"), "excerpt": excerpt,
                    "standards": ks_codes(excerpt), "grades": sorted(grades(excerpt))})
    return result


@functools.lru_cache(maxsize=32)
def _kcs_rows(files: tuple[tuple[str, int, int], ...], catalog_stamp: tuple[str, int, int]) -> tuple[dict, ...]:
    catalog_path = Path(catalog_stamp[0])
    catalog = json.loads(catalog_path.read_text(encoding="utf-8-sig")) if catalog_path.is_file() else []
    active = {str(r.get("code")): r for r in catalog if r.get("codeType") == "KCS"}
    rows = []
    for filename, _, _ in files:
        path = Path(filename)
        code = path.stem.removeprefix("KCS_")
        if active and code not in active:
            continue
        try:
            record = select_kcs_record(json.loads(path.read_text(encoding="utf-8-sig")), code, active.get(code))
        except (OSError, ValueError):
            continue
        if record:
            rows.extend(record_rows(record, "KCS"))
    return tuple(rows)


def kcs_evidence(raw_dir: Path, fields: dict, prefixes: tuple[str, ...]) -> list[dict]:
    paths = sorted(p for p in raw_dir.glob("KCS_*.json") if any(p.stem.removeprefix("KCS_").startswith(s) for s in prefixes))
    catalog = raw_dir.parent / "code_list.json"
    stamp = (str(catalog), catalog.stat().st_mtime_ns, catalog.stat().st_size) if catalog.is_file() else (str(catalog), 0, 0)
    rows = _kcs_rows(tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths), stamp)
    matches = [r for r in rows if set(fields["standards"]) & set(r["standards"]) and set(fields["grades"]) & set(r["grades"])]
    # Prefer architectural construction standards when several families repeat
    # the same list; no approximate grade substitution or fabricated score.
    matches.sort(key=lambda r: (not r["code"].startswith("KCS 41"), r["code"], r["table"]))
    unique = {(r["code"], r["table"], r["excerpt"]): r for r in matches}
    return [dict(r) for r in list(unique.values())[:2]]


def table_candidates(clause: dict, raw_dir: Path, prefixes: tuple[str, ...]) -> list[dict]:
    fields = source_fields(clause)
    if not fields:
        return []
    return [{"id": str(uuid.uuid5(uuid.UUID(clause["id"]), f"table:{r['code']}:{r['table']}:{r['excerpt']}")),
        "rank": i + 1, "kcs_code": r["code"], "document_name": r["document_name"], "version": r["version"],
        "update_date": r["update_date"], "kcs_clause": r["table"], "title": r["section"], "content": r["excerpt"],
        "score": 0.0, "classification": "일부 요구사항 대응", "reasons": ["표의 KS 번호와 강종 기호를 정확히 대조했습니다."],
        "warnings": ["강종 목록 대응입니다. 두께·제품·적용 조건의 전체 포함을 확인한 결과가 아닙니다."]}
        for i, r in enumerate(kcs_evidence(raw_dir, fields, prefixes))]


def _design_record(settings, code: str) -> dict:
    # One bounded refresh per reference/day; failed reads are never cached as a
    # valid absence. A short failure cache avoids repeated blocked-network calls.
    with _design_lock:
        cached = _design_cache.get(code)
        if cached and time.monotonic() - cached[0] < (86400 if cached[1].get("record") else 60):
            return cached[1]
        try:
            key = _load_api_key(settings)
            catalog = _request_json("CodeList", key, 12)
            current = next((r for r in catalog if r.get("codeType") == "KDS" and str(r.get("code")) == code), None)
            if current is None:
                raise ValueError("현재 KDS 목록에서 기준을 확인하지 못했습니다.")
            records = _request_json(f"CodeViewer/KDS/{code}", key, 12)
            record = next((r for r in records if str(r.get("code")) == code and all(str(r.get(f) or "") == str(current.get(f) or "") for f in ("version", "updateDate"))), None)
            if record is None:
                raise ValueError("KDS 목록과 원문의 개정 정보가 다릅니다.")
            result = {"record": record}
        except (RuntimeError, OSError, ValueError, TypeError):
            result = {"warning": "KDS 최신 원문을 조회하지 못했습니다. KDS 대응 없음으로 판단하지 마세요."}
        _design_cache[code] = (time.monotonic(), result)
        return result


def compare_table(clause: dict, settings, prefixes: tuple[str, ...]) -> dict:
    fields = source_fields(clause)
    if not fields:
        return {"applicable": False}
    with kcs_read_lock():
        evidence = kcs_evidence(settings.kcs_raw_dir, fields, prefixes)
    warnings = []
    routes = list(dict.fromkeys(code for family, codes in DESIGN_ROUTES.items()
        if any(family.startswith(prefix) or prefix.startswith(family) for prefix in prefixes)
        for code in codes))[:1]
    for code in routes:
        response = _design_record(settings, code)
        if response.get("warning"):
            warnings.append(response["warning"])
        else:
            matches = [r for r in record_rows(response["record"], "KDS") if set(fields["standards"]) & set(r["standards"]) and set(fields["grades"]) & set(r["grades"])]
            evidence.extend(matches[:1])
            if not matches:
                warnings.append(f"KDS {code[:2]} {code[2:4]} {code[4:]}에서 동일 KS 번호·강종의 표 행을 확인하지 못했습니다.")
    if not routes:
        warnings.append("이 분야의 KDS 검색 범위는 아직 연결되지 않았습니다. 현재 설계 참고 연결 범위는 건축 강구조입니다.")
    for row in evidence:
        row["matched_grades"] = sorted(set(fields["grades"]) & set(row["grades"]))
        row["candidate_id"] = next((c["id"] for c in clause.get("candidates", []) if row["kind"] == "KCS"
            and c["kcs_code"] == row["code"] and c["kcs_clause"] == row["table"]
            and c["version"] == row["version"] and c["update_date"] == row["update_date"]
            and c["content"] == row["excerpt"]), None)
    known = {g for row in evidence for g in row["matched_grades"]}
    known_standards = {s for row in evidence for s in row["standards"]}
    all_standards = set(fields["standards"]) <= known_standards
    checks = [
        {"field": "규격", "source": ", ".join(fields["standards"]), "status": "found" if all_standards else "unconfirmed", "result": "동일 KS 번호 확인" if all_standards else "일부 또는 전체 대응 표 미확인"},
        {"field": "강종", "source": ", ".join(fields["grades"]), "status": "found" if set(fields["grades"]) <= known else "unconfirmed", "result": "기준의 강종 목록에 있음" if set(fields["grades"]) <= known else "일부 또는 전체 강종 미확인"},
        {"field": "두께 조건", "source": ", ".join(fields["thickness"]) or "별도 표기 없음", "status": "unconfirmed", "result": "별도 확인 필요"},
        {"field": "제품·적용", "source": ", ".join(fields["products"]) or "원문 행 확인", "status": "unconfirmed", "result": "별도 확인 필요"},
    ]
    return {"applicable": True, "source": fields, "evidence": evidence[:3], "checks": checks, "warnings": warnings,
            "notice": "강종 목록 대응은 두께·제품·적용 조건의 전체 일치를 뜻하지 않습니다. KDS는 설계 참고이며 KCS 중복 삭제 근거와 구분됩니다."}
