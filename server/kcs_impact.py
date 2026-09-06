from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from typing import Any


def _normalize_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _normalize_code(value: Any) -> str:
    return "".join(character for character in _normalize_text(value) if character.isalnum())


def _normalized_warnings(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return sorted({_normalize_text(item) for item in values if _normalize_text(item)})


def _semantic_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kcs_code": _normalize_code(candidate.get("kcs_code")),
        "kcs_clause": _normalize_text(candidate.get("kcs_clause")),
        "title": _normalize_text(candidate.get("title")),
        "content": _normalize_text(candidate.get("content")),
        "warnings": _normalized_warnings(candidate.get("warnings", [])),
    }


def candidate_semantic_fingerprint(candidate: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _semantic_payload(candidate),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _logical_key(candidate: Mapping[str, Any]) -> tuple[str, str]:
    return (
        _normalize_code(candidate.get("kcs_code")),
        _normalize_text(candidate.get("kcs_clause")),
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=str)
    return str(value)


def _metadata_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "rank": candidate.get("rank"),
        "score": candidate.get("score"),
        "classification": _normalize_text(candidate.get("classification")),
        "reasons": sorted(
            _normalize_text(item) for item in candidate.get("reasons", [])
        ),
        "version": _normalize_text(candidate.get("version")),
        "update_date": _normalize_text(candidate.get("update_date")),
        "document_name": _normalize_text(candidate.get("document_name")),
    }


def _new_candidate_id(clause_id: str, fingerprint: str, occurrence: int) -> str:
    name = f"posco-kcs-candidate:{clause_id}:{fingerprint}:{occurrence}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def plan_clause_impact(
    before_candidates: Sequence[Mapping[str, Any]],
    after_candidates: Sequence[Mapping[str, Any]],
    decision: str | None,
    selected_candidate_id: str | None,
    *,
    clause_id: str,
) -> dict[str, Any]:
    """Plan candidate IDs and review impact without mutating either input list."""
    clause_id = str(clause_id or "").strip()
    if not clause_id:
        raise ValueError("후보 ID를 생성할 조항 ID가 필요합니다.")

    before_by_fingerprint: dict[str, deque[Mapping[str, Any]]] = defaultdict(deque)
    before_fingerprints: set[str] = set()
    after_fingerprints: set[str] = set()
    before_snapshot: list[dict[str, Any]] = []

    for candidate in before_candidates:
        fingerprint = candidate_semantic_fingerprint(candidate)
        before_by_fingerprint[fingerprint].append(candidate)
        before_fingerprints.add(fingerprint)
        snapshot = _json_safe(candidate)
        snapshot["semantic_fingerprint"] = fingerprint
        before_snapshot.append(snapshot)
    assignments: list[dict[str, Any]] = []
    after_snapshot: list[dict[str, Any]] = []
    occurrence_by_fingerprint: dict[str, int] = defaultdict(int)
    mapped_selected_id: str | None = None
    metadata_changed = False

    for index, candidate in enumerate(after_candidates):
        fingerprint = candidate_semantic_fingerprint(candidate)
        after_fingerprints.add(fingerprint)
        occurrence_by_fingerprint[fingerprint] += 1
        prior = (
            before_by_fingerprint[fingerprint].popleft()
            if before_by_fingerprint[fingerprint]
            else None
        )
        prior_id = str(prior.get("id") or "") if prior else ""
        assigned_id = prior_id or _new_candidate_id(
            clause_id,
            fingerprint,
            occurrence_by_fingerprint[fingerprint],
        )
        reused = bool(prior_id)
        if prior is not None and _metadata_payload(prior) != _metadata_payload(candidate):
            metadata_changed = True
        if (
            prior is not None
            and selected_candidate_id is not None
            and prior_id == str(selected_candidate_id)
        ):
            mapped_selected_id = assigned_id

        snapshot = _json_safe(candidate)
        snapshot["id"] = assigned_id
        snapshot["semantic_fingerprint"] = fingerprint
        after_snapshot.append(snapshot)
        assignments.append(
            {
                "after_index": index,
                "input_id": str(candidate.get("id") or "") or None,
                "assigned_id": assigned_id,
                "preserved_id": prior_id or None,
                "reused": reused,
                "semantic_fingerprint": fingerprint,
            }
        )

    material_changed = before_fingerprints != after_fingerprints
    selection_removed = bool(
        selected_candidate_id is not None and mapped_selected_id is None
    )
    review_required = decision is not None and material_changed

    added_fingerprints = after_fingerprints - before_fingerprints
    removed_fingerprints = before_fingerprints - after_fingerprints
    before_by_logical: dict[tuple[str, str], set[str]] = defaultdict(set)
    after_by_logical: dict[tuple[str, str], set[str]] = defaultdict(set)
    for candidate in before_candidates:
        before_by_logical[_logical_key(candidate)].add(
            candidate_semantic_fingerprint(candidate)
        )
    for candidate in after_candidates:
        after_by_logical[_logical_key(candidate)].add(
            candidate_semantic_fingerprint(candidate)
        )
    meaning_changed = any(
        before_by_logical[key] != after_by_logical[key]
        for key in before_by_logical.keys() & after_by_logical.keys()
    )
    reasons: list[str] = []
    if meaning_changed:
        reasons.append("candidate_meaning_changed")
    if added_fingerprints:
        reasons.append("candidate_added")
    if removed_fingerprints:
        reasons.append("candidate_removed")
    if metadata_changed:
        reasons.append("candidate_metadata_changed")
    if selection_removed:
        reasons.append("selected_candidate_removed")

    return {
        "material_changed": material_changed,
        "metadata_changed": metadata_changed,
        "review_required": review_required,
        "selected_candidate_id": mapped_selected_id,
        "selected_candidate_mapping": {
            "before_id": selected_candidate_id,
            "after_id": mapped_selected_id,
        },
        "selection_removed": selection_removed,
        "reasons": reasons,
        "id_assignments": assignments,
        "before_candidates": before_snapshot,
        "after_candidates": after_snapshot,
    }
