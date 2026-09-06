from __future__ import annotations

import re
from decimal import Decimal


_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_LATIN_UNIT = (
    r"(?:GPa|MPa|kPa|Pa|kN|N|mg|kg|g|t|km|"
    r"mm(?:2|3|²|³)?|cm(?:2|3|²|³)?|m(?:2|3|²|³)?|°C)"
)
_SYMBOL_UNIT = r"(?:㎜|㎝|㎞|㎟|㎠|㎡|㎥|㎎|㎏|℃|%|시간|분|초|일|회|개)"
_QUANTITY_RE = re.compile(
    rf"(?P<number>{_NUMBER})\s*(?P<unit>{_LATIN_UNIT}(?![A-Za-z])|{_SYMBOL_UNIT})",
    re.IGNORECASE,
)
_KOREAN_WORD_UNITS = {"시간", "분", "초", "일", "회", "개"}
_QUANTITY_SUFFIX_RE = re.compile(
    r"^(?:이상|이하|초과|미만|동안|개월|으로|에서|마다|당|씩|간|월|차|"
    r"을|를|이|가|은|는|의|에|로|[,.;:)\]])"
)
_LIMIT_RE = re.compile(r"(이상|이하|초과|미만)(?!적)")
_PROHIBITION_RE = re.compile(
    r"금지|불가|"
    r"(?:하여서는|해서는|하면)\s*(?:안|아니)\s*된다|"
    r"할\s*수\s*없다|하지\s*않는다|아니한다"
)
_OBLIGATION_RE = re.compile(
    r"(?:하여야|해야(?:만)?|되어야|이어야)\s*(?:한다|하며|하고|함)|"
    r"반드시|필수|의무(?!\s*(?:가\s*)?없)|하도록\s*한다"
)

_UNIT_ALIASES = {
    "㎜": "mm",
    "㎝": "cm",
    "㎞": "km",
    "㎟": "mm²",
    "㎠": "cm²",
    "㎡": "m²",
    "㎥": "m³",
    "㎎": "mg",
    "㎏": "kg",
    "°c": "℃",
    "℃": "℃",
    "gpa": "GPa",
    "mpa": "MPa",
    "kpa": "kPa",
    "pa": "Pa",
    "kn": "kN",
    "n": "N",
}


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def starts_with_quantity(text: str | None) -> bool:
    """Return whether text begins with a recognized number-and-unit quantity."""

    cleaned = _clean(text)
    match = _QUANTITY_RE.match(cleaned)
    if not match:
        return False
    if match.group("unit") not in _KOREAN_WORD_UNITS:
        return True
    remainder = cleaned[match.end():].lstrip()
    return not remainder or bool(_QUANTITY_SUFFIX_RE.match(remainder))


def _normalise_number(raw: str) -> str:
    value = format(Decimal(raw.replace(",", "")), "f")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return "0" if value in {"-0", "+0"} else value.lstrip("+")


def _normalise_unit(raw: str) -> str:
    key = raw.casefold()
    if key in _UNIT_ALIASES:
        return _UNIT_ALIASES[key]
    return key.replace("2", "²").replace("3", "³")


def _quantities(text: str) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in _QUANTITY_RE.finditer(text):
        number = _normalise_number(match.group("number"))
        unit = _normalise_unit(match.group("unit"))
        key = (number, unit)
        if key not in seen:
            seen.add(key)
            values.append({"number": number, "unit": unit})
    return sorted(values, key=lambda item: (Decimal(item["number"]), item["unit"]))


def _matches(pattern: re.Pattern[str], text: str) -> list[str]:
    return sorted({match.group(0) for match in pattern.finditer(text)})


def analyze_text_differences(posco_text: str, kcs_text: str) -> dict[str, list]:
    """Compare review-sensitive quantities and normative expressions."""

    posco = _clean(posco_text)
    kcs = _clean(kcs_text)
    warnings: list[str] = []
    differences: list[dict[str, object]] = []

    posco_quantities = _quantities(posco)
    kcs_quantities = _quantities(kcs)
    if posco_quantities != kcs_quantities:
        warnings.append("수치·단위가 다르므로 기술 검토가 필요합니다.")
        differences.append(
            {"category": "quantity", "posco": posco_quantities, "kcs": kcs_quantities}
        )

    posco_limits = _matches(_LIMIT_RE, posco)
    kcs_limits = _matches(_LIMIT_RE, kcs)
    if posco_limits != kcs_limits:
        warnings.append("이상·이하·초과·미만 표현이 다릅니다.")
        differences.append({"category": "limit", "posco": posco_limits, "kcs": kcs_limits})

    posco_prohibitions = _matches(_PROHIBITION_RE, posco)
    kcs_prohibitions = _matches(_PROHIBITION_RE, kcs)
    if bool(posco_prohibitions) != bool(kcs_prohibitions):
        warnings.append("금지 표현의 유무가 다릅니다.")
        differences.append(
            {
                "category": "prohibition",
                "posco": posco_prohibitions,
                "kcs": kcs_prohibitions,
            }
        )

    posco_obligations = _matches(_OBLIGATION_RE, posco)
    kcs_obligations = _matches(_OBLIGATION_RE, kcs)
    if bool(posco_obligations) != bool(kcs_obligations):
        warnings.append("의무 표현의 유무가 다릅니다.")
        differences.append(
            {"category": "obligation", "posco": posco_obligations, "kcs": kcs_obligations}
        )

    return {"warnings": warnings, "differences": differences}
