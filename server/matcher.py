from __future__ import annotations

import collections
import functools
import html
import json
import math
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from . import text_analysis
from .relevance import apply_verdicts, clearly_unrelated, own_requirement, query_text, contextual_requirement, masonry_material_conflict
from .table_evidence import table_candidates
from .kcs_sync import (
    catalog_revision,
    raw_section_is_usable,
    select_kcs_record,
    snapshot_revision,
    validate_kcs_raw_snapshot,
)
from .openai_ai import OpenAIAPIError, OpenAIClient


CURRENT_MATCHER_VERSION = "hybrid-kcs-v5.4-material-context"


CHAPTER_SCOPES: dict[int, tuple[tuple[str, ...], str]] = {
    2: (("21",), "KCS 21"),
    3: (("4134",), "KCS 41 34"),
    4: (("4135",), "KCS 41 35"),
    5: (("4133",), "KCS 41 33"),
    6: (("4151",), "KCS 41 51"),
    7: (("4146",), "KCS 41 46"),
    8: (("4148",), "KCS 41 48"),
    9: (("4155",), "KCS 41 55"),
    10: (("4149", "4131", "1431", "4156"), "KCS 41 49, KCS 41 31, KCS 14 31, KCS 41 56"),
    11: (("4131", "1431"), "KCS 41 31, KCS 14 31"),
    12: (("4156",), "KCS 41 56"),
    13: (("4131", "1431"), "KCS 41 31, KCS 14 31"),
    14: (("4140",), "KCS 41 40"),
}

KEYWORD_SCOPES = [
    (("가설",), 2), (("조적", "벽돌", "블록"), 3), (("석공", "석재"), 4),
    (("목공", "목재"), 5), (("수장", "보드", "천장"), 6), (("미장", "모르타르"), 7),
    (("타일",), 8), (("창호", "문틀", "유리"), 9), (("철물", "잡공"), 10),
    (("철골세우기",), 11), (("지붕",), 12), (("철골", "강구조"), 13), (("방수",), 14),
]

TOKEN_RE = re.compile(r"[A-Za-z가-힣]{2,}|\d+(?:\.\d+)?")
STOPWORDS = {
    "한다", "하여야", "있다", "대한", "경우", "따른다", "사용", "시공", "공사", "재료",
    "그리고", "또는", "위하여", "필요", "적용", "기준", "해당",
}
TERM_EQUIVALENT_GROUPS: tuple[tuple[str, ...], ...] = (
    ("공작도", "철골제작도", "제작도", "시공상세도", "shop drawing", "shopdrawing"),
    ("철골세우기", "철골 설치", "강구조 설치", "steel erection", "erection"),
    ("고장력볼트", "고력볼트", "high strength bolt"),
    ("앵커볼트", "기초볼트", "anchor bolt"),
    ("데크플레이트", "데크 플레이트", "steel deck"),
    ("관 이음", "배관 이음", "pipe joint", "piping joint"),
    ("보온", "단열", "thermal insulation"),
    ("가붙임", "임시용접", "가용접", "tack welding", "tack weld"),
    ("밑면 따내기", "뒷면 파내기", "백가우징", "back gouging", "back chipping"),
    ("도리", "중도리", "purlin"),
    ("띠장", "girt"),
    ("덧판", "이음판", "splice plate"),
    ("접합판", "거셋 플레이트", "gusset plate"),
    ("보강판", "스티프너", "stiffener"),
    ("솟음", "캠버", "camber"),
    ("현치도", "원척도", "full size drawing"),
)
CONTEXT_REFERENCE_RE = re.compile(
    r"(?:상기|전항|앞(?:의|서)?|위(?:의)?)\s*"
    r"(?:제?\s*)?(?:\(?[①-⑳0-9가-하]+\)?(?:\.\d+)*)?(?:항|호|목|규정|기준|내용)?",
    re.I,
)
FORWARD_CONTEXT_REFERENCE_RE = re.compile(
    r"(?:아래|다음|하기)(?:의)?\s*(?:표|기준|내용|각호|순서)?|"
    r"(?:표|그림)\s*\d+(?:[-.]\d+)*\s*(?:과|와|에|의|를|을)?\s*(?:같이|따라|의거)",
    re.I,
)
SOURCE_CONTEXT_REFERENCE_RE = re.compile(
    r"(?:상기|전항|앞|위|아래|다음|하기|그러하지|이를|이에|그\s*(?:기준|내용|방법))",
    re.I,
)
STEEL_PRIMARY_PREFIXES = ("4131", "1431")
STEEL_RELATED_SCOPES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("용접", "용접봉", "가붙임", "가우징", "weld", "gouging", "bevel", "fillet"), ("2431",)),
    (("도장", "방청", "도막", "페인트", "paint", "primer"), ("4147", "3120", "4143")),
)
LEXICAL_POOL_SIZE = 40
GLOBAL_POOL_SIZE = 80
GLOBAL_DOCUMENT_LIMIT = 5
DOCUMENT_SCOPE_LIMIT = 8
DOCUMENT_SCOPE_SAMPLE_ITEMS = 40
DOCUMENT_SCOPE_SAMPLE_CHARACTERS = 12_000
RRF_K = 60
_CACHE_REVISION_LOCK = threading.Lock()
_ACTIVE_CACHE_REVISION = ""
KCS_REFERENCE_RE = re.compile(
    r"KCS\s*[-_]?\s*(\d{2})\s*[-_]?\s*(\d{2})\s*[-_]?\s*(\d{2})",
    re.I,
)
KS_REFERENCE_RE = re.compile(r"\bKS\s*([A-Z])\s*[-_]?\s*(\d{3,})\b", re.I)


def clean_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_standard_references(value: Any) -> str:
    """Canonicalize common KS spellings such as KSD 3503 and KS D-3503."""
    return KS_REFERENCE_RE.sub(
        lambda match: f"KS {match.group(1).upper()} {match.group(2)}",
        str(value or ""),
    )


def expand_equivalent_terms(value: Any) -> str:
    """Append construction-domain equivalents without replacing the source wording."""
    text = str(value or "")
    compact = re.sub(r"\s+", "", text).lower()
    additions: list[str] = []
    for group in TERM_EQUIVALENT_GROUPS:
        if any(re.sub(r"\s+", "", term).lower() in compact for term in group):
            additions.extend(group)
    return clean_space(f"{text} {' '.join(dict.fromkeys(additions))}")


def normalize_key(value: Any) -> str:
    normalized = normalize_standard_references(expand_equivalent_terms(value))
    return re.sub(r"[^0-9a-z가-힣]", "", clean_space(normalized).lower())


def strip_html(value: Any) -> str:
    raw = str(value or "")
    raw = re.sub(r"<img\b[^>]*>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", " ", raw, flags=re.I)
    raw = re.sub(r"<br\s*/?>", " ", raw, flags=re.I)
    raw = re.sub(r"</(?:p|div|tr|li|td|th)>", " ", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw, flags=re.S)
    return clean_space(html.unescape(raw))


def format_kcs_code(code: Any) -> str:
    digits = re.sub(r"\D", "", str(code))
    if len(digits) >= 6:
        return f"KCS {digits[:2]} {digits[2:4]} {digits[4:]}"
    if len(digits) == 4:
        return f"KCS {digits[:2]} {digits[2:]}"
    return f"KCS {digits}"


def build_scope_sample(clauses: list[dict[str, Any]]) -> str:
    """Build a bounded, whole-document sample for catalog routing."""
    if not clauses:
        return ""
    if len(clauses) <= DOCUMENT_SCOPE_SAMPLE_ITEMS:
        selected = clauses
    else:
        last = len(clauses) - 1
        indices = {
            round(index * last / (DOCUMENT_SCOPE_SAMPLE_ITEMS - 1))
            for index in range(DOCUMENT_SCOPE_SAMPLE_ITEMS)
        }
        selected = [clauses[index] for index in sorted(indices)]
    sample = " ".join(
        f"{clean_space(item.get('title'))} {clean_space(item.get('content'))[:300]}"
        for item in selected
    )
    return clean_space(sample)[:DOCUMENT_SCOPE_SAMPLE_CHARACTERS]


def infer_scope(
    filename: str,
    title: str,
    sample_text: str,
    raw_dir: Path | None = None,
) -> tuple[tuple[str, ...], str]:
    """Infer a search prior without treating POSCO chapter numbers as a schema.

    The static chapter map remains a fallback for installations whose KCS catalog is
    unavailable. Normal uploads are routed from the current KCS catalog itself, so a
    newly uploaded discipline does not have to be added to this source file first.
    Clause-level recovery in ``_rank_matches`` still broadens weak document routing.
    """
    document_identity = f"{filename} {title}"
    combined = f"{document_identity} {sample_text[:DOCUMENT_SCOPE_SAMPLE_CHARACTERS]}"
    # Chapter numbers are only a fallback because another uploaded corpus can use
    # a different numbering system.  A matching discipline keyword is stronger:
    # keep that broad KCS family in addition to the current catalog shortlist.
    chapter_hint: tuple[tuple[str, ...], str] | None = None
    chapter_match = re.search(r"제?\s*(\d{1,2})\s*장", combined)
    if chapter_match:
        chapter_hint = CHAPTER_SCOPES.get(int(chapter_match.group(1)))
    keyword_hint: tuple[tuple[str, ...], str] | None = None
    # A discipline named in the filename/title is more reliable than incidental
    # material names inside a specification that can span several trades.
    for keywords, chapter in KEYWORD_SCOPES:
        if any(keyword in document_identity for keyword in keywords):
            keyword_hint = CHAPTER_SCOPES[chapter]
            break
    if keyword_hint is None:
        scored_hints = [
            (sum(combined.count(keyword) for keyword in keywords), chapter)
            for keywords, chapter in KEYWORD_SCOPES
        ]
        hit_count, hit_chapter = max(scored_hints, default=(0, 0))
        if hit_count:
            keyword_hint = CHAPTER_SCOPES[hit_chapter]

    if raw_dir is not None:
        explicit_codes = tuple(sorted(_referenced_kcs_codes(combined)))
        try:
            catalog_codes = _catalog_prefixes(
                combined,
                Path(raw_dir),
                limit=DOCUMENT_SCOPE_LIMIT,
            )
        except RuntimeError:
            catalog_codes = ()
        keyword_codes = keyword_hint[0] if keyword_hint is not None else ()
        routed_codes = tuple(dict.fromkeys(
            (*explicit_codes, *keyword_codes, *catalog_codes)
        ))[:DOCUMENT_SCOPE_LIMIT]
        if routed_codes:
            display = ", ".join(format_kcs_code(code) for code in routed_codes)
            return routed_codes, f"{display} (KCS 목록 기반 자동 추정)"

    if keyword_hint is not None:
        return keyword_hint
    if chapter_hint is not None:
        return chapter_hint
    return ("41",), "KCS 41 (자동 범위 추정 실패)"


def read_snapshot(manifest_path: Path) -> tuple[str, dict[str, Any]]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("KCS 다운로드 manifest.json을 읽을 수 없습니다.") from exc
    if manifest.get("status") != "completed" or not manifest.get("latestOnly"):
        raise RuntimeError("최신본으로 완료된 KCS 데이터가 아닙니다.")
    snapshot = clean_space(manifest.get("completedAt")) or clean_space(manifest.get("startedAt"))
    if not snapshot:
        raise RuntimeError("KCS 다운로드 완료시각이 없어 현재 기준을 확인할 수 없습니다.")
    code_list_path = manifest_path.parent / "code_list.json"
    try:
        code_list = json.loads(code_list_path.read_text(encoding="utf-8-sig"))
        catalog_hash = catalog_revision(code_list)
    except (OSError, json.JSONDecodeError, ValueError):
        raise RuntimeError(
            "KCS 개정 식별값을 계산할 code_list.json을 읽을 수 없습니다. KCS를 다시 갱신해 주세요."
        )
    computed_revision = catalog_hash
    raw_dir = manifest_path.parent / "raw"
    if raw_dir.exists() or manifest.get("rawIntegrity"):
        raw_validation = validate_kcs_raw_snapshot(
            code_list,
            raw_dir,
            allow_legacy_empty=not bool(manifest.get("rawIntegrity")),
        )
        if manifest.get("rawIntegrity") != raw_validation["raw_integrity"] and manifest.get(
            "rawIntegrity"
        ):
            raise RuntimeError(
                "KCS 원문 무결성 값이 manifest와 일치하지 않습니다. KCS를 다시 갱신해 주세요."
            )
        expected_count = manifest.get("documentCount")
        if expected_count is not None and int(expected_count) != raw_validation["validated_count"]:
            raise RuntimeError("KCS 원문 검증 건수가 코드 목록과 일치하지 않습니다.")
        manifest = {
            **manifest,
            "downloadedCount": raw_validation["validated_count"],
            "usableDocumentCount": raw_validation["usable_count"],
            "unavailableDocumentCount": raw_validation["unavailable_count"],
        }
        computed_revision = snapshot_revision(code_list, raw_validation["raw_integrity"])
    manifest_revision = clean_space(manifest.get("revision"))
    if manifest_revision and manifest_revision != computed_revision:
        legacy_catalog_revision = (
            not manifest.get("rawIntegrity") and manifest_revision == catalog_hash
        )
        if not legacy_catalog_revision:
            raise RuntimeError(
                "KCS manifest와 코드 목록·원문의 개정 식별값이 일치하지 않습니다. KCS를 다시 갱신해 주세요."
            )
    return snapshot, {**manifest, "revision": computed_revision}


def _version_key(record: dict[str, Any]) -> tuple[int, str]:
    version_digits = re.sub(r"\D", "", str(record.get("version") or ""))
    return (int(version_digits or 0), str(record.get("updateDate") or ""))


@functools.lru_cache(maxsize=16)
def load_kcs_sections(raw_dir_text: str, prefixes: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
    raw_dir = Path(raw_dir_text)
    if not raw_dir.is_dir():
        raise RuntimeError(f"KCS raw 폴더를 찾을 수 없습니다: {raw_dir}")
    active_catalog: dict[str, dict[str, Any]] = {}
    code_list_path = raw_dir.parent / "code_list.json"
    if code_list_path.exists():
        try:
            code_list = json.loads(code_list_path.read_text(encoding="utf-8-sig"))
            active_catalog = {
                str(item.get("code")): item
                for item in code_list
                if isinstance(item, dict) and str(item.get("codeType", "")).upper() == "KCS" and item.get("code")
            }
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("KCS code_list.json을 읽을 수 없습니다.") from exc
    sections: list[dict[str, Any]] = []
    invalid_files: list[str] = []
    for path in sorted(raw_dir.glob("KCS_*.json")):
        code_from_name = path.stem.replace("KCS_", "")
        if active_catalog and code_from_name not in active_catalog:
            continue
        if not any(code_from_name.startswith(prefix) for prefix in prefixes):
            continue
        try:
            records = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            invalid_files.append(path.name)
            continue
        if not isinstance(records, list) or not records:
            continue
        record = select_kcs_record(
            records,
            code_from_name,
            active_catalog.get(code_from_name),
        )
        if not record:
            invalid_files.append(path.name)
            continue
        code = str(record.get("code") or code_from_name)
        usable_items: list[tuple[dict[str, Any], str, str]] = []
        for item in record.get("list") or []:
            if not raw_section_is_usable(item):
                continue
            content = strip_html(item.get("contents"))
            title = clean_space(item.get("title"))
            if not content and not title:
                continue
            usable_items.append((item, title, content))
        previous_content = ""
        previous_by_title: dict[str, str] = {}
        for item_index, (item, title, content) in enumerate(usable_items):
            context_dependent = bool(CONTEXT_REFERENCE_RE.search(content))
            reference_context = previous_by_title.get(title) or previous_content
            forward_dependent = bool(FORWARD_CONTEXT_REFERENCE_RE.search(content))
            next_content = ""
            if forward_dependent:
                for _, next_title, following_content in usable_items[item_index + 1:item_index + 4]:
                    if following_content and normalize_key(following_content) != normalize_key(next_title):
                        next_content = following_content
                        break
            context_resolved = bool(
                (context_dependent and reference_context) or (forward_dependent and next_content)
            )
            context_parts = []
            display_parts = []
            if context_dependent and reference_context:
                context_parts.append(reference_context)
                display_parts.append(f"[앞 조항] {reference_context}")
            context_parts.append(content)
            display_parts.append(f"[현재 조항] {content}" if context_resolved else content)
            if forward_dependent and next_content:
                context_parts.append(next_content)
                display_parts.append(f"[다음 조항] {next_content}")
            search_content = clean_space(" ".join(context_parts))
            display_content = "\n".join(display_parts)
            sections.append(
                {
                    "code": format_kcs_code(code),
                    "document_name": clean_space(record.get("name")),
                    "version": clean_space(record.get("version")),
                    "update_date": clean_space(record.get("updateDate")),
                    "clause": clean_space(item.get("label")),
                    "title": title,
                    "content": content,
                    "search_content": search_content,
                    "display_content": display_content,
                    "context_dependent": context_dependent,
                    "forward_dependent": forward_dependent,
                    "context_resolved": context_resolved,
                }
            )
            if content and normalize_key(content) != normalize_key(title):
                previous_content = content
                if title:
                    previous_by_title[title] = content
    if invalid_files:
        names = ", ".join(invalid_files[:5])
        raise RuntimeError(f"손상된 KCS JSON이 있습니다: {names}")
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for section in sections:
        key = (section["code"], section["clause"], section["content"])
        if key not in seen:
            seen.add(key)
            unique.append(section)
    if not unique:
        raise RuntimeError("선택된 범위에서 KCS 조항을 찾지 못했습니다.")
    return tuple(unique)


def ngrams(text: str) -> collections.Counter[str]:
    normalized = normalize_key(text)
    counts: collections.Counter[str] = collections.Counter()
    for size in (2, 3, 4):
        for index in range(max(0, len(normalized) - size + 1)):
            counts[normalized[index : index + size]] += 1
    return counts


def _section_search_text(section: dict[str, Any]) -> str:
    return (
        f"{section['code']} {section['document_name']} "
        f"{section['title']} {section['title']} "
        f"{section.get('search_content') or section['content']}"
    )


@functools.lru_cache(maxsize=16)
def build_index(raw_dir_text: str, prefixes: tuple[str, ...]):
    sections = load_kcs_sections(raw_dir_text, prefixes)
    docs: list[collections.Counter[str]] = []
    document_frequency: collections.Counter[str] = collections.Counter()
    for section in sections:
        search_text = _section_search_text(section)
        counts = ngrams(search_text)
        docs.append(counts)
        document_frequency.update(counts.keys())
    total = max(1, len(docs))
    idf = {term: math.log((total + 1) / (frequency + 1)) + 1.0 for term, frequency in document_frequency.items()}
    inverted: dict[str, list[tuple[int, float]]] = collections.defaultdict(list)
    for document_index, counts in enumerate(docs):
        weights: dict[str, float] = {}
        norm_sq = 0.0
        for term, count in counts.items():
            weight = (1.0 + math.log(count)) * idf[term]
            weights[term] = weight
            norm_sq += weight * weight
        norm = math.sqrt(norm_sq) or 1.0
        for term, weight in weights.items():
            inverted[term].append((document_index, weight / norm))
    return sections, idf, inverted


def _word_tokens(text: str) -> list[str]:
    normalized = normalize_standard_references(expand_equivalent_terms(text))
    return [
        token.lower()
        for token in TOKEN_RE.findall(normalized)
        if token.lower() not in STOPWORDS
    ]


@functools.lru_cache(maxsize=16)
def build_bm25_index(raw_dir_text: str, prefixes: tuple[str, ...]):
    sections = load_kcs_sections(raw_dir_text, prefixes)
    inverted: dict[str, list[tuple[int, int]]] = collections.defaultdict(list)
    document_lengths: list[int] = []
    document_frequency: collections.Counter[str] = collections.Counter()
    for document_index, section in enumerate(sections):
        search_text = _section_search_text(section)
        counts = collections.Counter(_word_tokens(search_text))
        document_lengths.append(sum(counts.values()))
        document_frequency.update(counts.keys())
        for term, count in counts.items():
            inverted[term].append((document_index, count))
    average_length = sum(document_lengths) / max(1, len(document_lengths))
    return sections, inverted, tuple(document_lengths), document_frequency, average_length


def _tfidf_scores(text: str, raw_dir: Path, prefixes: tuple[str, ...]) -> tuple[tuple[dict[str, Any], ...], dict[int, float]]:
    sections, idf, inverted = build_index(str(raw_dir), prefixes)
    counts = ngrams(text)
    weights: dict[str, float] = {}
    norm_sq = 0.0
    for term, count in counts.items():
        if term not in idf:
            continue
        weight = (1.0 + math.log(count)) * idf[term]
        weights[term] = weight
        norm_sq += weight * weight
    norm = math.sqrt(norm_sq) or 1.0
    scores: dict[int, float] = collections.defaultdict(float)
    for term, weight in weights.items():
        query_weight = weight / norm
        for document_index, document_weight in inverted.get(term, ()):
            scores[document_index] += query_weight * document_weight
    return sections, scores


def _bm25_scores(text: str, raw_dir: Path, prefixes: tuple[str, ...]) -> dict[int, float]:
    sections, inverted, document_lengths, document_frequency, average_length = build_bm25_index(
        str(raw_dir), prefixes
    )
    total = max(1, len(sections))
    average_length = average_length or 1.0
    scores: dict[int, float] = collections.defaultdict(float)
    query_counts = collections.Counter(_word_tokens(text))
    k1 = 1.5
    b = 0.75
    for term, query_count in query_counts.items():
        frequency = document_frequency.get(term, 0)
        if not frequency:
            continue
        idf = math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))
        query_weight = 1.0 + math.log(query_count)
        for document_index, term_frequency in inverted.get(term, ()):
            length_ratio = document_lengths[document_index] / average_length
            denominator = term_frequency + k1 * (1.0 - b + b * length_ratio)
            scores[document_index] += idf * query_weight * (
                term_frequency * (k1 + 1.0) / denominator
            )
    return scores


def _rank_lexical(text: str, raw_dir: Path, prefixes: tuple[str, ...], limit: int):
    sections, tfidf_scores = _tfidf_scores(text, raw_dir, prefixes)
    bm25_scores = _bm25_scores(text, raw_dir, prefixes)
    maximum_bm25 = max(bm25_scores.values(), default=0.0)
    ranked: list[tuple[int, float]] = []
    document_indices = set(tfidf_scores) | set(bm25_scores)
    for document_index in document_indices:
        tfidf_score = tfidf_scores.get(document_index, 0.0)
        bm25_score = bm25_scores.get(document_index, 0.0) / maximum_bm25 if maximum_bm25 else 0.0
        hybrid_score = 0.82 * tfidf_score + 0.18 * bm25_score
        ranked.append((document_index, hybrid_score))
    ranked.sort(key=lambda pair: (-pair[1], pair[0]))
    return [(sections[index], score) for index, score in ranked[:limit]]


@functools.lru_cache(maxsize=8)
def _build_catalog_index(raw_dir_text: str):
    """Build a small document-level index used before any cross-KCS search.

    A full section-level character n-gram index is too large for interactive use.
    The code list contains only a few hundred KCS documents, so it is safe to use
    as a first-stage router and then build a section index for the best documents.
    """
    code_list_path = Path(raw_dir_text).parent / "code_list.json"
    try:
        records = json.loads(code_list_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        records = []
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("KCS code_list.json을 읽을 수 없습니다.") from exc

    if not isinstance(records, list) or not records:
        records = []
        for path in sorted(Path(raw_dir_text).glob("KCS_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            versions = [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
            if not versions:
                continue
            latest = max(versions, key=_version_key)
            records.append(
                {
                    "codeType": "KCS",
                    "code": latest.get("code") or path.stem.replace("KCS_", ""),
                    "name": latest.get("name") or "",
                    "listParentCodes": [],
                }
            )

    catalog: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    for item in records if isinstance(records, list) else []:
        if not isinstance(item, dict) or str(item.get("codeType", "")).upper() != "KCS":
            continue
        code = _code_digits(str(item.get("code") or ""))
        if len(code) < 6 or code in seen_codes:
            continue
        seen_codes.add(code)
        parent_names = " ".join(
            sorted(
                clean_space(parent.get("name"))
                for parent in item.get("listParentCodes") or []
                if isinstance(parent, dict) and clean_space(parent.get("name"))
            )
        )
        catalog.append(
            {
                "code": code,
                "text": clean_space(f"KCS {code} {item.get('name', '')} {parent_names}"),
            }
        )
    if not catalog:
        raise RuntimeError("KCS 문서 목록에서 검색 대상을 찾지 못했습니다.")

    docs: list[collections.Counter[str]] = []
    document_frequency: collections.Counter[str] = collections.Counter()
    for item in catalog:
        counts = ngrams(item["text"])
        docs.append(counts)
        document_frequency.update(counts.keys())
    total = len(docs)
    idf = {
        term: math.log((total + 1) / (frequency + 1)) + 1.0
        for term, frequency in document_frequency.items()
    }
    normalized_docs: list[dict[str, float]] = []
    for counts in docs:
        weights = {
            term: (1.0 + math.log(count)) * idf[term]
            for term, count in counts.items()
        }
        norm = math.sqrt(sum(weight * weight for weight in weights.values())) or 1.0
        normalized_docs.append({term: weight / norm for term, weight in weights.items()})
    return tuple(catalog), idf, tuple(normalized_docs)


def _catalog_prefixes(
    text: str,
    raw_dir: Path,
    limit: int = GLOBAL_DOCUMENT_LIMIT,
    allowed_prefixes: tuple[str, ...] = (),
) -> tuple[str, ...]:
    catalog, idf, documents = _build_catalog_index(str(raw_dir))
    query_counts = ngrams(text)
    query_weights = {
        term: (1.0 + math.log(count)) * idf[term]
        for term, count in query_counts.items()
        if term in idf
    }
    query_norm = math.sqrt(sum(weight * weight for weight in query_weights.values())) or 1.0
    normalized_query = {term: weight / query_norm for term, weight in query_weights.items()}
    ranked: list[tuple[int, float]] = []
    for index, document in enumerate(documents):
        if allowed_prefixes and not any(
            catalog[index]["code"].startswith(prefix) for prefix in allowed_prefixes
        ):
            continue
        score = sum(weight * document.get(term, 0.0) for term, weight in normalized_query.items())
        if score > 0:
            ranked.append((index, score))
    ranked.sort(key=lambda item: (-item[1], catalog[item[0]]["code"]))
    return tuple(catalog[index]["code"] for index, _ in ranked[:limit])


def _stable_section_score(
    text: str,
    section: dict[str, Any],
    domain_prefixes: tuple[str, ...] = (),
) -> float:
    """Corpus-independent score used to compare results from different documents."""
    query_counts = ngrams(text)
    document_counts = ngrams(_section_search_text(section))
    query_norm = math.sqrt(sum(value * value for value in query_counts.values())) or 1.0
    document_norm = math.sqrt(sum(value * value for value in document_counts.values())) or 1.0
    cosine = sum(
        value * document_counts.get(term, 0)
        for term, value in query_counts.items()
    ) / (query_norm * document_norm)
    query_tokens = set(_word_tokens(text))
    document_tokens = set(
        _word_tokens(_section_search_text(section))
    )
    token_coverage = (
        len(query_tokens & document_tokens) / len(query_tokens)
        if query_tokens
        else 0.0
    )
    score = min(1.0, 0.9 * min(1.0, 1.8 * cosine) + 0.1 * token_coverage)
    code = _code_digits(section["code"])
    if domain_prefixes and any(code.startswith(prefix) for prefix in domain_prefixes):
        score = 0.82 * score + 0.18
    steel_document = any(
        prefix.startswith(STEEL_PRIMARY_PREFIXES) or any(primary.startswith(prefix) for primary in STEEL_PRIMARY_PREFIXES)
        for prefix in domain_prefixes
    )
    if steel_document and not code.startswith(STEEL_PRIMARY_PREFIXES):
        related = any(
            any(term.lower() in text.lower() for term in terms)
            and code.startswith(allowed_prefixes)
            for terms, allowed_prefixes in STEEL_RELATED_SCOPES
        )
        if not related:
            score *= 0.72
    if section.get("context_dependent"):
        score *= 0.88 if section.get("context_resolved") else 0.45
    return min(1.0, score)


def _rank_catalog_shortlist(
    text: str,
    raw_dir: Path,
    prefixes: tuple[str, ...],
    limit: int = GLOBAL_POOL_SIZE,
    domain_prefixes: tuple[str, ...] = (),
) -> list[tuple[dict[str, Any], float]]:
    """Isolate bad files and recalibrate all shortlisted sections on one scale."""
    per_document_limit = max(12, math.ceil(limit / max(1, len(prefixes))) * 2)
    recovered: dict[tuple[str, str, str], tuple[dict[str, Any], float]] = {}
    for prefix in prefixes:
        try:
            ranked = _rank_lexical(text, raw_dir, (prefix,), per_document_limit)
        except RuntimeError:
            continue
        for section, _ in ranked:
            score = _stable_section_score(text, section, domain_prefixes)
            key = _section_key(section)
            previous = recovered.get(key)
            if previous is None or score > previous[1]:
                recovered[key] = (section, score)
    result = sorted(recovered.values(), key=lambda item: (-item[1], _section_key(item[0])))
    return result[:limit]


def _section_key(section: dict[str, Any]) -> tuple[str, str, str]:
    return section["code"], section["clause"], section["content"]


def _code_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _referenced_kcs_codes(text: str) -> set[str]:
    return {"".join(match.groups()) for match in KCS_REFERENCE_RE.finditer(text)}


def _empty_scope_error(error: RuntimeError) -> bool:
    return str(error) == "선택된 범위에서 KCS 조항을 찾지 못했습니다."


def _keyword_recovery_prefixes(text: str) -> tuple[str, ...]:
    recovered: list[str] = []
    for keywords, chapter in KEYWORD_SCOPES:
        if any(keyword in text for keyword in keywords):
            recovered.extend(CHAPTER_SCOPES[chapter][0])
    return tuple(dict.fromkeys(recovered))


def _matched_equivalent_groups(text: str) -> tuple[tuple[str, ...], ...]:
    compact = re.sub(r"\s+", "", text).lower()
    return tuple(
        group
        for group in TERM_EQUIVALENT_GROUPS
        if any(re.sub(r"\s+", "", term).lower() in compact for term in group)
    )


def _rank_equivalent_sections(
    text: str,
    raw_dir: Path,
    prefixes: tuple[str, ...],
    limit: int = LEXICAL_POOL_SIZE,
) -> list[tuple[dict[str, Any], float]]:
    groups = _matched_equivalent_groups(text)
    if not groups:
        return []
    sections = load_kcs_sections(str(raw_dir), prefixes)
    ranked = []
    for section in sections:
        section_text = f"{section['document_name']} {section['title']} {section['search_content']}"
        section_groups = set(_matched_equivalent_groups(section_text))
        if not section_groups.intersection(groups):
            continue
        marked = dict(section)
        marked["_equivalent_scope"] = True
        ranked.append((marked, _stable_section_score(text, marked, prefixes)))
    ranked.sort(key=lambda item: (-item[1], _section_key(item[0])))
    return ranked[:limit]


def _rank_matches(
    text: str,
    raw_dir: Path,
    prefixes: tuple[str, ...],
    limit: int = LEXICAL_POOL_SIZE,
):
    try:
        if prefixes and all(len(_code_digits(prefix)) >= 6 for prefix in prefixes):
            scoped = _rank_catalog_shortlist(
                text,
                raw_dir,
                prefixes,
                limit,
                domain_prefixes=prefixes,
            )
        else:
            scoped = _rank_lexical(text, raw_dir, prefixes, limit)
    except RuntimeError as exc:
        if not _empty_scope_error(exc):
            raise
        scoped = []
    referenced_codes = _referenced_kcs_codes(text)
    visible_scoped = [score for _, score in scoped if score >= 0.25]
    scoped_is_weak = (
        len(scoped) < limit
        or len(visible_scoped) < 3
        or not scoped
        or scoped[0][1] < 0.45
    )
    if not scoped_is_weak and not referenced_codes and not _matched_equivalent_groups(text):
        return scoped[:limit]

    ranked_groups: list[tuple[str, list[tuple[dict[str, Any], float]]]] = [("scoped", scoped)]

    equivalent_ranked = _rank_equivalent_sections(text, raw_dir, prefixes)
    if equivalent_ranked:
        ranked_groups.append(("equivalent", equivalent_ranked))

    keyword_prefixes = tuple(
        prefix for prefix in _keyword_recovery_prefixes(text) if prefix not in prefixes
    )
    if keyword_prefixes:
        keyword_documents = _catalog_prefixes(
            text,
            raw_dir,
            allowed_prefixes=keyword_prefixes,
        )
        keyword_ranked = _rank_catalog_shortlist(
            text,
            raw_dir,
            keyword_documents,
            limit,
            domain_prefixes=keyword_prefixes,
        )
        if keyword_ranked:
            ranked_groups.append(("keyword", keyword_ranked))

    # Resolve explicit references independently. A failure in broad recovery must
    # never hide a KCS code that the POSCO source names directly.
    for code in sorted(referenced_codes):
        try:
            explicit = []
            for section, score in _rank_lexical(text, raw_dir, (code,), limit):
                marked = dict(section)
                marked["_explicit_reference"] = True
                explicit.append((marked, score))
            ranked_groups.append(("explicit", explicit))
        except RuntimeError as exc:
            if not _empty_scope_error(exc):
                raise

    if scoped_is_weak and not keyword_prefixes:
        catalog_prefixes = _catalog_prefixes(text, raw_dir)
        if catalog_prefixes:
            ranked_groups.append(
                (
                    "global",
                    _rank_catalog_shortlist(text, raw_dir, catalog_prefixes),
                )
            )

    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    source_weights = {
        "scoped": 1.08,
        "equivalent": 2.0,
        "keyword": 1.35,
        "global": 1.0,
        "explicit": 2.5,
    }
    for source, group in ranked_groups:
        for rank, (section, score) in enumerate(group, start=1):
            key = _section_key(section)
            entry = merged.setdefault(
                key,
                {
                    "section": section,
                    "score": score,
                    "priority": 0.0,
                    "explicit": False,
                    "keyword": False,
                    "has_scoped_score": False,
                },
            )
            entry["priority"] += source_weights[source] / (RRF_K + rank)
            if source == "scoped":
                entry["score"] = score
                entry["has_scoped_score"] = True
            elif not entry["has_scoped_score"] and entry["score"] < score:
                entry["score"] = score
            if section.get("_explicit_reference"):
                entry["explicit"] = True
                entry["section"] = section
            if source == "keyword":
                entry["keyword"] = True
            if source == "equivalent":
                entry["section"]["_equivalent_scope"] = True

    maximum_priority = max((entry["priority"] for entry in merged.values()), default=1.0)
    combined: list[tuple[dict[str, Any], float]] = []
    for entry in merged.values():
        section = dict(entry["section"])
        section["_retrieval_priority"] = entry["priority"] / maximum_priority
        if entry["explicit"]:
            section["_explicit_reference"] = True
        if entry["keyword"]:
            section["_keyword_scope"] = True
        combined.append((section, entry["score"]))
    combined.sort(
        key=lambda item: (
            -(
                item[1]
                + 0.08 * float(item[0].get("_retrieval_priority", 0.0))
                + (0.16 if item[0].get("_keyword_scope") else 0.0)
                + (0.2 if item[0].get("_equivalent_scope") else 0.0)
                + (1.0 if item[0].get("_explicit_reference") else 0.0)
            ),
            _section_key(item[0]),
        )
    )
    return combined[:limit]


def clear_matcher_caches() -> None:
    global _ACTIVE_CACHE_REVISION
    load_kcs_sections.cache_clear()
    build_index.cache_clear()
    build_bm25_index.cache_clear()
    _build_catalog_index.cache_clear()
    _ACTIVE_CACHE_REVISION = ""


def activate_matcher_revision(revision: str) -> None:
    """Invalidate path-keyed matcher caches exactly when the KCS catalog changes."""
    global _ACTIVE_CACHE_REVISION
    normalized = str(revision or "").strip()
    if not normalized:
        raise ValueError("KCS 개정 식별값이 없습니다.")
    with _CACHE_REVISION_LOCK:
        if normalized == _ACTIVE_CACHE_REVISION:
            return
        load_kcs_sections.cache_clear()
        build_index.cache_clear()
        build_bm25_index.cache_clear()
        _build_catalog_index.cache_clear()
        _ACTIVE_CACHE_REVISION = normalized


def _rerank_with_embeddings(
    source_text: str,
    ranked: list[tuple[dict[str, Any], float]],
    ai_client: OpenAIClient | None,
) -> list[tuple[dict[str, Any], float, float | None]]:
    local = _rank_without_embeddings(ranked)
    if (
        not ranked
        or ai_client is None
        or not ai_client.available
        or not getattr(ai_client, "embeddings_available", True)
    ):
        return local
    documents = [_section_search_text(section) for section, _ in ranked]
    try:
        semantic_scores = ai_client.embedding_scores(source_text, documents)
    except OpenAIAPIError:
        return local
    return _rerank_from_scores(ranked, semantic_scores)


def _final_rank_score(
    section: dict[str, Any],
    relevance_score: float,
    *,
    embeddings_used: bool,
) -> float:
    """Return the same calibrated score used for both ordering and display."""

    priority_weight = 0.08
    base_weight = 0.84 if embeddings_used else 0.82
    score = (
        base_weight * relevance_score
        + priority_weight * float(section.get("_retrieval_priority", 0.0))
        + (0.03 if section.get("_keyword_scope") else 0.0)
        + ((0.05 if embeddings_used else 0.07) if section.get("_equivalent_scope") else 0.0)
    )
    if section.get("_explicit_reference"):
        return 1.0
    return max(0.0, min(1.0, score))


def _rank_without_embeddings(
    ranked: list[tuple[dict[str, Any], float]],
) -> list[tuple[dict[str, Any], float, float | None]]:
    calibrated = []
    for section, score in ranked:
        marked = dict(section)
        marked["_candidate_relevance_score"] = score
        calibrated.append((marked, _final_rank_score(marked, score, embeddings_used=False), None))
    calibrated.sort(key=lambda item: (-item[1], _section_key(item[0])))
    return calibrated


def _rerank_from_scores(
    ranked: list[tuple[dict[str, Any], float]],
    semantic_scores: list[float],
) -> list[tuple[dict[str, Any], float, float | None]]:
    local = _rank_without_embeddings(ranked)
    if len(semantic_scores) != len(ranked):
        return local
    enriched = []
    for (section, local_score), semantic_score in zip(ranked, semantic_scores):
        semantic_score = max(0.0, min(1.0, semantic_score))
        combined_score = 0.65 * local_score + 0.35 * semantic_score
        if local_score >= 0.25:
            effective_score = max(0.25, combined_score)
        elif semantic_score >= 0.62:
            effective_score = combined_score
        else:
            effective_score = local_score
        marked = dict(section)
        marked["_candidate_relevance_score"] = effective_score
        final_score = _final_rank_score(marked, effective_score, embeddings_used=True)
        enriched.append((marked, final_score, semantic_score))
    enriched.sort(
        key=lambda item: (
            -item[1],
            -(item[2] or 0.0),
        )
    )
    return enriched


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(text) if token.lower() not in STOPWORDS}


def _comparison_metadata(source: str, target: str) -> tuple[list[str], list[str]]:
    overlap = sorted(_tokens(source) & _tokens(target), key=lambda token: (-len(token), token))[:6]
    reasons = [f"공통 핵심어: {', '.join(overlap)}"] if overlap else []
    compare = getattr(text_analysis, "compare_requirements", text_analysis.analyze_text_differences)
    analysis = compare(source, target)
    warnings = list(analysis.get("warnings") or [])
    return reasons, warnings


def classify(score: float) -> str:
    return "관련성 높음" if score >= 0.45 else "검토 필요"


def _candidate_rows(
    clause: dict[str, Any],
    source_text: str,
    ranked: list[tuple[dict[str, Any], float, float | None]],
    limit: int = 3,
    min_relevance: float = 0.25,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    reserved: list[tuple[dict[str, Any], float, float | None]] = []
    reserved_codes: set[str] = set()
    for item in ranked:
        section = item[0]
        if not section.get("_explicit_reference"):
            continue
        code = _code_digits(section["code"])
        if code not in reserved_codes:
            reserved_codes.add(code)
            reserved.append(item)
    reserved_keys = {_section_key(item[0]) for item in reserved}
    ordered = [*reserved, *(item for item in ranked if _section_key(item[0]) not in reserved_keys)]

    for section, score, semantic_score in ordered:
        if masonry_material_conflict(
            str(clause.get("match_context") or ""),
            f"{section.get('document_name', '')} > {section.get('title', '')}",
        ):
            continue
        if clearly_unrelated(own_requirement(clause) or source_text, section.get("content", "")):
            continue
        explicit_reference = bool(section.get("_explicit_reference"))
        title_only = normalize_key(section["content"]) == normalize_key(section["title"])
        content_key = re.sub(r"[^0-9a-z가-힣]", "", section["content"].lower())
        non_substantive_fragment = (
            len(content_key) < 20
            and not re.search(r"(?:한다|된다|있다|없다|따른다|하여야|해야|원칙)", section["content"])
        )
        if (title_only or non_substantive_fragment) and not explicit_reference:
            continue
        relevance_score = float(section.get("_candidate_relevance_score", score))
        if relevance_score < min_relevance and not explicit_reference:
            continue
        raw_clause = clean_space(section.get("clause"))
        clause_key = re.sub(r"\s+", "", raw_clause).casefold()
        if not clause_key:
            clause_key = normalize_key(f"{section.get('title', '')} {section.get('content', '')}")
        key = (_code_digits(section["code"]), normalize_key(f"{section.get('title', '')} {clause_key} {section['content']}"))
        if key in seen:
            continue
        seen.add(key)
        candidate_content = section.get("display_content") or section["content"]
        reasons, warnings = _comparison_metadata(own_requirement(clause) or source_text, candidate_content)
        if section.get("context_resolved"):
            if section.get("forward_dependent"):
                reasons.append("참조 표현의 인접 조항을 함께 비교했습니다.")
            else:
                reasons.append("참조 표현의 앞 조항을 함께 비교했습니다.")
        if explicit_reference:
            reasons.insert(0, "포스코 원문이 이 KCS 코드를 직접 참조합니다.")
        if semantic_score is not None:
            reasons.append(f"GPT 임베딩 의미 유사도: {semantic_score:.0%}")
        rank = len(candidates) + 1
        candidate_id = str(
            uuid.uuid5(
                uuid.UUID(clause["id"]),
                f"{section['code']}:{section['clause']}:{section['version']}:{key[1]}",
            )
        )
        candidates.append(
            {
                "id": candidate_id,
                "rank": rank,
                "kcs_code": section["code"],
                "document_name": section["document_name"],
                "version": section["version"],
                "update_date": section["update_date"],
                "kcs_clause": section["clause"],
                "title": section["title"],
                "content": candidate_content,
                "score": round(score, 4),
                "classification": "명시 KCS 참조" if explicit_reference else classify(score),
                "_requires_verification": relevance_score < 0.25 and not explicit_reference,
                "reasons": reasons,
                "warnings": warnings,
            }
        )
        if len(candidates) == limit:
            break
    return candidates


def match_clauses(
    clauses: list[dict[str, Any]],
    raw_dir: Path,
    prefixes: tuple[str, ...],
    ai_client: OpenAIClient | None = None,
    *,
    require_embeddings: bool = False,
) -> dict[str, Any]:
    if require_embeddings and (
        ai_client is None
        or not ai_client.available
        or not getattr(ai_client, "embeddings_available", True)
    ):
        raise OpenAIAPIError(
            "엄격 재매칭에는 사용 가능한 OpenAI 임베딩이 필요합니다."
        )

    prepared: list[
        tuple[dict[str, Any], str, list[tuple[dict[str, Any], float]]]
    ] = []
    staged_candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for clause_index, clause in enumerate(clauses):
        if clause.get("source_type") not in {"paragraph", "table"}:
            staged_candidates.append((clause, []))
            continue
        source_text = clean_space(query_text(clauses, clause_index))
        prepared.append((clause, source_text, _rank_matches(source_text, raw_dir, prefixes)))

    batch_method = getattr(ai_client, "embedding_score_groups", None)
    batch_scores: dict[int, list[float]] | None = None
    embeddings_used = False
    if (
        ai_client is not None
        and ai_client.available
        and getattr(ai_client, "embeddings_available", True)
        and callable(batch_method)
    ):
        active = [(index, row) for index, row in enumerate(prepared) if row[2]]
        try:
            score_groups = batch_method(
                [row[1] for _, row in active],
                [
                    [_section_search_text(section) for section, _ in row[2]]
                    for _, row in active
                ],
            )
            if require_embeddings and (
                len(score_groups) != len(active)
                or any(
                    len(scores) != len(row[2])
                    for scores, (_, row) in zip(score_groups, active)
                )
            ):
                raise OpenAIAPIError("임베딩 점수 개수가 재매칭 후보와 일치하지 않습니다.")
            batch_scores = {
                index: scores for (index, _), scores in zip(active, score_groups)
            }
            embeddings_used = bool(active)
        except OpenAIAPIError:
            if require_embeddings:
                raise
            batch_scores = {}

    for index, (clause, source_text, local_ranked) in enumerate(prepared):
        if batch_scores is not None:
            ranked = _rerank_from_scores(local_ranked, batch_scores[index]) \
                if index in batch_scores else _rank_without_embeddings(local_ranked)
        elif require_embeddings:
            embedding_method = getattr(ai_client, "embedding_scores", None)
            if not callable(embedding_method):
                raise OpenAIAPIError(
                    "엄격 재매칭에 사용할 OpenAI 임베딩 함수를 찾을 수 없습니다."
                )
            semantic_scores = embedding_method(
                source_text,
                [_section_search_text(section) for section, _ in local_ranked],
            )
            if len(semantic_scores) != len(local_ranked):
                raise OpenAIAPIError("임베딩 점수 개수가 재매칭 후보와 일치하지 않습니다.")
            ranked = _rerank_from_scores(local_ranked, semantic_scores)
            embeddings_used = embeddings_used or bool(local_ranked)
        else:
            ranked = _rerank_with_embeddings(source_text, local_ranked, ai_client)
            embeddings_used = embeddings_used or any(
                semantic_score is not None for _, _, semantic_score in ranked
            )
        can_verify = callable(getattr(ai_client, "verify_candidate_groups", None)) and getattr(ai_client, "available", False) and getattr(ai_client, "embeddings_available", True)
        staged_candidates.append((clause, _candidate_rows(
            clause, source_text, ranked, limit=6, min_relevance=0.1 if can_verify else 0.25,
        )))

    verifier = getattr(ai_client, "verify_candidate_groups", None)
    active = [(clause, rows) for clause, rows in staged_candidates if rows]
    verified = None
    if active and callable(verifier) and getattr(ai_client, "available", False) and getattr(ai_client, "embeddings_available", True):
        try:
            verified = verifier([
                (contextual_requirement(clause), [
                    f"[KCS 상위 문맥] {row['document_name']} > {row['title']}\n[KCS 본문] {row['content']}"
                    for row in rows
                ])
                for clause, rows in active
            ])
        except OpenAIAPIError:
            # Never silently call a failed semantic check an equivalent requirement.
            verified = None
    verdict_map = {clause["id"]: results for (clause, _), results in zip(active, verified or [])}
    for clause, candidates in staged_candidates:
        clause["candidates"] = apply_verdicts(candidates, verdict_map.get(clause["id"]))
        exact_rows = table_candidates(clause, raw_dir, prefixes)
        if exact_rows:
            keys = {(c["kcs_code"], c["kcs_clause"]) for c in exact_rows}
            clause["candidates"] = (exact_rows + [c for c in clause["candidates"] if (c["kcs_code"], c["kcs_clause"]) not in keys])[:3]
            for rank, candidate in enumerate(clause["candidates"], 1):
                candidate["rank"] = rank

    return {
        "mode": "openai_embeddings" if embeddings_used else "local",
        "require_embeddings": require_embeddings,
        "embedding_model": str(getattr(ai_client, "embedding_model", "") or ""),
        "embedding_dimensions": getattr(ai_client, "embedding_dimensions", None),
    }
