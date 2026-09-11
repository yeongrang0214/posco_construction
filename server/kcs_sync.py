from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .config import Settings


BASE_URL = "https://kcsc.re.kr/OpenApi"
SYNC_LOCK = threading.Lock()
SEOUL_TIMEZONE = timezone(timedelta(hours=9))


def catalog_revision(code_list: Any) -> str:
    """Return a stable revision for the latest KCS catalog metadata."""
    if not isinstance(code_list, list):
        raise ValueError("KCS 코드 목록 형식이 올바르지 않습니다.")
    records = sorted(
        (
            {
                "code": str(item.get("code") or ""),
                "fullCode": str(item.get("fullCode") or ""),
                "name": str(item.get("name") or ""),
                "version": str(item.get("version") or ""),
                "updateDate": str(item.get("updateDate") or ""),
                "parentCodes": sorted(
                    (
                        {
                            "code": str(parent.get("code") or ""),
                            "fullCode": str(parent.get("fullCode") or ""),
                            "name": str(parent.get("name") or ""),
                        }
                        for parent in item.get("listParentCodes") or []
                        if isinstance(parent, dict)
                    ),
                    key=lambda parent: (
                        parent["code"], parent["fullCode"], parent["name"]
                    ),
                ),
            }
            for item in code_list
            if isinstance(item, dict)
            and str(item.get("codeType", "")).upper() == "KCS"
            and item.get("code")
        ),
        key=lambda item: item["code"],
    )
    if not records:
        raise ValueError("KCS 코드 목록이 비어 있습니다.")
    payload = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def snapshot_revision(code_list: Any, raw_integrity: str = "") -> str:
    """Bind the catalog revision to the exact validated KCS bodies when available."""
    catalog = catalog_revision(code_list)
    integrity = str(raw_integrity or "").strip()
    if not integrity:
        return catalog
    return hashlib.sha256(f"{catalog}:{integrity}".encode("ascii")).hexdigest()


def _now_seoul() -> str:
    return datetime.now(SEOUL_TIMEZONE).isoformat(timespec="seconds")


@contextmanager
def kcs_read_lock(*, timeout: float | None = None):
    """Keep one upload's snapshot read and matching pass consistent with KCS sync."""
    acquired = SYNC_LOCK.acquire() if timeout is None else SYNC_LOCK.acquire(timeout=timeout)
    if not acquired:
        raise RuntimeError("KCS 자료를 다른 작업에서 사용 중입니다.")
    try:
        yield
    finally:
        SYNC_LOCK.release()


def _request_json(path: str, api_key: str, timeout: float = 45.0) -> Any:
    url = f"{BASE_URL}/{path}?{urlencode({'key': api_key})}"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "posco-spec-manager/0.1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            payload = json.loads(response.read().decode(charset))
    except HTTPError as exc:
        raise RuntimeError(f"KCSC API가 HTTP {exc.code}을 반환했습니다.") from exc
    except URLError as exc:
        raise RuntimeError("KCSC API에 연결하지 못했습니다.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("KCSC API 응답이 올바른 JSON이 아닙니다.") from exc
    if isinstance(payload, dict) and payload.get("message"):
        raise RuntimeError(str(payload["message"]))
    return payload


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _metadata_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def raw_section_is_usable(section: Any) -> bool:
    if not isinstance(section, dict):
        return False
    title = _metadata_text(section.get("title"))
    contents = html.unescape(re.sub(r"<[^>]+>", " ", str(section.get("contents") or "")))
    return bool(title or _metadata_text(contents))


def _record_matches_catalog(
    record: dict[str, Any],
    expected_code: str,
    catalog_item: dict[str, Any] | None,
) -> bool:
    if _metadata_text(record.get("code")) != expected_code:
        return False
    code_type = _metadata_text(record.get("codeType")).upper()
    if code_type and code_type != "KCS":
        return False
    if not catalog_item:
        return True
    for field in ("fullCode", "name", "version", "updateDate"):
        expected = _metadata_text(catalog_item.get(field))
        if expected and _metadata_text(record.get(field)) != expected:
            return False
    return True


def select_kcs_record(
    payload: Any,
    expected_code: str,
    catalog_item: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(payload, list):
        return None
    candidates = [
        record
        for record in payload
        if isinstance(record, dict)
        and _record_matches_catalog(record, expected_code, catalog_item)
        and isinstance(record.get("list"), list)
    ]
    if not candidates:
        return None

    def version_key(record: dict[str, Any]) -> tuple[int, str]:
        digits = re.sub(r"\D", "", _metadata_text(record.get("version")))
        return int(digits or 0), _metadata_text(record.get("updateDate"))

    return max(candidates, key=version_key)


def _validate_raw_payload(
    payload: Any,
    expected_code: str,
    catalog_item: dict[str, Any] | None = None,
    *,
    allow_legacy_empty: bool = False,
) -> tuple[bool, bool]:
    """Return (valid, usable) for one CodeViewer response."""
    if not isinstance(payload, list):
        return False, False
    if not payload:
        return allow_legacy_empty, False

    if not all(isinstance(record, dict) for record in payload):
        return False, False
    record = select_kcs_record(payload, expected_code, catalog_item)
    if not record:
        return False, False
    return True, any(raw_section_is_usable(section) for section in record["list"])


def _normalize_download_payload(
    payload: Any,
    expected_code: str,
    catalog_item: dict[str, Any],
) -> list[Any]:
    def unavailable(reason: str, response_codes: list[str] | None = None) -> list[Any]:
        return [
            {
                "codeType": "KCS",
                "code": expected_code,
                "fullCode": catalog_item.get("fullCode"),
                "name": catalog_item.get("name"),
                "version": catalog_item.get("version"),
                "updateDate": catalog_item.get("updateDate"),
                "list": [],
                "bodyUnavailable": True,
                "bodyUnavailableReason": reason,
                "providerResponseCodes": response_codes or [],
            }
        ]

    if payload == []:
        return unavailable("empty_response")

    if not isinstance(payload, list) or not all(
        isinstance(candidate, dict) for candidate in payload
    ):
        raise RuntimeError(f"KCS {expected_code} 본문 응답 형식 또는 코드가 예상과 다릅니다.")

    response_codes = sorted(
        {
            _metadata_text(candidate.get("code"))
            for candidate in payload
            if _metadata_text(candidate.get("code"))
        }
    )
    matching_code_records = [
        candidate
        for candidate in payload
        if _metadata_text(candidate.get("code")) == expected_code
        and _metadata_text(candidate.get("codeType")).upper() in {"", "KCS"}
    ]
    if not matching_code_records:
        if response_codes and all(
            isinstance(candidate.get("list"), list) for candidate in payload
        ):
            reason = (
                "provider_metadata_mismatch"
                if expected_code in response_codes
                else "provider_code_mismatch"
            )
            return unavailable(reason, response_codes)
        raise RuntimeError(f"KCS {expected_code} 본문 응답 형식 또는 코드가 예상과 다릅니다.")

    catalog_exact_records = [
        candidate
        for candidate in matching_code_records
        if _record_matches_catalog(candidate, expected_code, catalog_item)
    ]
    if not catalog_exact_records:
        return unavailable("provider_metadata_mismatch", response_codes)
    record = select_kcs_record(payload, expected_code, catalog_item)
    if not record:
        raise RuntimeError(f"KCS {expected_code} 본문 응답 형식 또는 코드가 예상과 다릅니다.")
    sections = record["list"]
    if not all(isinstance(section, dict) for section in sections):
        raise RuntimeError(f"KCS {expected_code} 본문 응답 형식 또는 코드가 예상과 다릅니다.")
    if sections and not any(raw_section_is_usable(section) for section in sections):
        raise RuntimeError(f"KCS {expected_code} 본문에 사용할 수 있는 조항이 없습니다.")
    if not sections:
        return unavailable("no_usable_sections")
    return [record]


def _raw_document_is_valid(
    path: Path,
    expected_code: str,
    catalog_item: dict[str, Any],
) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    valid, usable = _validate_raw_payload(payload, expected_code, catalog_item)
    return valid and usable


def validate_kcs_raw_snapshot(
    code_list: Any,
    raw_dir: Path,
    *,
    allow_legacy_empty: bool = False,
) -> dict[str, Any]:
    records = {
        str(item.get("code")): item
        for item in code_list
        if isinstance(item, dict)
        and str(item.get("codeType", "")).upper() == "KCS"
        and item.get("code")
    }
    if not records:
        raise RuntimeError("검증할 KCS 코드 목록이 비어 있습니다.")

    digest = hashlib.sha256()
    usable_count = 0
    unavailable_count = 0
    invalid_codes: list[str] = []
    for code in sorted(records):
        path = raw_dir / f"KCS_{code}.json"
        try:
            raw_bytes = path.read_bytes()
            payload = json.loads(raw_bytes.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            invalid_codes.append(code)
            continue
        valid, usable = _validate_raw_payload(
            payload,
            code,
            records[code],
            allow_legacy_empty=allow_legacy_empty,
        )
        if not valid:
            invalid_codes.append(code)
            continue
        digest.update(code.encode("ascii", errors="ignore"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(raw_bytes).digest())
        if usable:
            usable_count += 1
        else:
            unavailable_count += 1

    if invalid_codes:
        preview = ", ".join(invalid_codes[:10])
        raise RuntimeError(
            f"KCS 원문 파일 {len(invalid_codes)}건이 없거나 손상·코드 불일치 상태입니다: {preview}"
        )
    return {
        "validated_count": len(records),
        "usable_count": usable_count,
        "unavailable_count": unavailable_count,
        "raw_integrity": digest.hexdigest(),
    }


def _load_api_key(settings: Settings) -> str:
    api_key = os.getenv("KCSC_API_KEY", "").strip()
    if api_key:
        return api_key

    explicit_env = os.getenv("KCSC_ENV_FILE")
    candidates = [
        Path(explicit_env) if explicit_env else None,
        settings.project_root / ".env",
        settings.kcs_data_dir.parent / ".env",
        settings.kcs_data_dir.parent / "spec-matcher" / ".env",
    ]
    for path in candidates:
        if not path or not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except UnicodeDecodeError:
            lines = path.read_text(encoding="cp949").splitlines()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
                continue
            name, value = stripped.split("=", 1)
            if name.removeprefix("export ").strip() != "KCSC_API_KEY":
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if value:
                return value
    raise RuntimeError("KCSC_API_KEY를 찾지 못했습니다. 프로젝트 또는 260819 폴더의 .env를 확인하세요.")


def sync_kcs(
    settings: Settings,
    publish_callback: Callable[[dict[str, Any]], None] | None = None,
    preflight_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if not SYNC_LOCK.acquire(blocking=False):
        raise RuntimeError("KCS 갱신이 이미 진행 중입니다.")
    try:
        # This runs under the same lock used when an upload captures its KCS
        # snapshot, preventing a job from entering between the check and sync.
        if preflight_callback is not None:
            preflight_callback()
        api_key = _load_api_key(settings)
        started_at = _now_seoul()
        code_list = _request_json("CodeList", api_key)
        if not isinstance(code_list, list):
            raise RuntimeError("KCSC CodeList 응답 형식이 예상과 다릅니다.")
        current_kcs = {
            str(item.get("code")): item
            for item in code_list
            if isinstance(item, dict) and str(item.get("codeType", "")).upper() == "KCS" and item.get("code")
        }
        if not current_kcs:
            raise RuntimeError("KCS 코드 목록이 비어 있습니다.")

        previous: dict[str, dict[str, Any]] = {}
        old_list: list[Any] = []
        previous_manifest: dict[str, Any] = {}
        if settings.kcs_manifest_path.exists():
            try:
                loaded_manifest = json.loads(
                    settings.kcs_manifest_path.read_text(encoding="utf-8-sig")
                )
                if isinstance(loaded_manifest, dict):
                    previous_manifest = loaded_manifest
            except (OSError, json.JSONDecodeError):
                previous_manifest = {}
        if (settings.kcs_data_dir / "code_list.json").exists():
            try:
                old_list = json.loads((settings.kcs_data_dir / "code_list.json").read_text(encoding="utf-8-sig"))
                previous = {
                    str(item.get("code")): item
                    for item in old_list
                    if isinstance(item, dict) and str(item.get("codeType", "")).upper() == "KCS" and item.get("code")
                }
            except (OSError, json.JSONDecodeError):
                previous = {}
                old_list = []

        catalog_hash = catalog_revision(code_list)
        try:
            previous_catalog_hash = catalog_revision(old_list)
        except ValueError:
            previous_catalog_hash = ""

        prior_integrity_broken = False
        previous_revision = str(previous_manifest.get("revision") or "").strip()
        trusted_raw_integrity = str(previous_manifest.get("rawIntegrity") or "").strip()
        if trusted_raw_integrity and old_list:
            try:
                previous_raw_validation = validate_kcs_raw_snapshot(
                    old_list,
                    settings.kcs_raw_dir,
                )
                prior_integrity_broken = (
                    previous_raw_validation["raw_integrity"] != trusted_raw_integrity
                )
            except RuntimeError:
                prior_integrity_broken = True
        elif (
            previous_revision
            and previous_revision == previous_catalog_hash
            and old_list
        ):
            # Legacy manifests stored only the catalog hash. Normalize that value
            # to the body-bound revision that read_snapshot() exposes so an
            # unchanged legacy snapshot is not reported as a KCS revision change.
            try:
                previous_raw_validation = validate_kcs_raw_snapshot(
                    old_list,
                    settings.kcs_raw_dir,
                    allow_legacy_empty=True,
                )
            except RuntimeError:
                pass
            else:
                previous_revision = snapshot_revision(
                    old_list,
                    previous_raw_validation["raw_integrity"],
                )
        trusted_catalog_hash = str(
            previous_manifest.get("catalogRevision") or ""
        ).strip()
        if trusted_catalog_hash and trusted_catalog_hash != previous_catalog_hash:
            prior_integrity_broken = True

        changed_codes = [
            code
            for code, item in current_kcs.items()
            if prior_integrity_broken
            or not _raw_document_is_valid(
                settings.kcs_raw_dir / f"KCS_{code}.json",
                code,
                item,
            )
            or previous.get(code, {}).get("updateDate") != item.get("updateDate")
            or previous.get(code, {}).get("version") != item.get("version")
            or previous.get(code, {}).get("name") != item.get("name")
            or previous.get(code, {}).get("fullCode") != item.get("fullCode")
        ]
        if changed_codes or catalog_hash != previous_catalog_hash:
            _write_json_atomic(
                settings.kcs_manifest_path,
                {
                    "source": "국가건설기준센터 OpenAPI",
                    "sourceUrl": BASE_URL,
                    "startedAt": started_at,
                    "codeType": "KCS",
                    "latestOnly": False,
                    "catalogRevision": catalog_hash,
                    "rawIntegrity": trusted_raw_integrity or None,
                    "revision": previous_manifest.get("revision"),
                    "status": "updating",
                },
            )

        settings.kcs_raw_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".kcs-sync-", dir=settings.kcs_raw_dir) as temporary_dir:
            staging = Path(temporary_dir)

            def download(code: str) -> tuple[str, Any]:
                payload = _request_json(f"CodeViewer/KCS/{code}", api_key)
                return code, _normalize_download_payload(
                    payload,
                    code,
                    current_kcs[code],
                )

            failures: list[str] = []
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {executor.submit(download, code): code for code in changed_codes}
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        downloaded_code, payload = future.result()
                        (staging / f"KCS_{downloaded_code}.json").write_text(
                            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                    except Exception:
                        failures.append(code)
            if failures:
                preview = ", ".join(sorted(failures)[:10])
                raise RuntimeError(f"KCS 본문 {len(failures)}건을 내려받지 못했습니다: {preview}")

            for staged_file in staging.glob("KCS_*.json"):
                os.replace(staged_file, settings.kcs_raw_dir / staged_file.name)

        raw_validation = validate_kcs_raw_snapshot(
            code_list,
            settings.kcs_raw_dir,
        )
        revision = snapshot_revision(code_list, raw_validation["raw_integrity"])
        completed_at = _now_seoul()
        _write_json_atomic(settings.kcs_data_dir / "code_list.json", code_list)
        manifest = {
            "source": "국가건설기준센터 OpenAPI",
            "sourceUrl": BASE_URL,
            "startedAt": started_at,
            "completedAt": completed_at,
            "codeType": "KCS",
            "codePrefix": None,
            "latestOnly": True,
            "documentCount": len(current_kcs),
            "downloadedCount": raw_validation["validated_count"],
            "usableDocumentCount": raw_validation["usable_count"],
            "unavailableDocumentCount": raw_validation["unavailable_count"],
            "rawIntegrity": raw_validation["raw_integrity"],
            "changedCount": len(changed_codes),
            "revision": revision,
            "catalogRevision": catalog_hash,
            "status": "completed",
        }
        _write_json_atomic(settings.kcs_manifest_path, manifest)
        result = {
            "kcs_snapshot": completed_at,
            "kcs_document_count": len(current_kcs),
            "changed_count": len(changed_codes),
            "kcs_revision": revision,
            "previous_revision": previous_revision,
            "revision_changed": previous_revision != revision,
            "usable_document_count": raw_validation["usable_count"],
            "unavailable_document_count": raw_validation["unavailable_count"],
        }
        if publish_callback:
            publish_callback(result)
        return result
    finally:
        SYNC_LOCK.release()
