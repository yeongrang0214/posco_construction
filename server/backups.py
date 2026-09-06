from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import Lock
from typing import Any, Callable

from .storage import DATABASE_SCHEMA_VERSION, Store


BACKUP_FORMAT_VERSION = 2
MAX_BACKUP_MEMBERS = 10_000
MAX_BACKUP_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
BACKUP_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
ALLOWED_REASONS = {"manual", "pre_restore"}
AUTHENTICATION_ALGORITHM = "hmac-sha256"
AUTHENTICATION_KEY_BYTES = 32
AUTHENTICATION_KEY_FILENAME = "backup-hmac-v1.key"
AUTHENTICATION_DOMAIN = b"POSCO-SPEC-BACKUP\x00v2\x00"
logger = logging.getLogger(__name__)
_authentication_key_lock = Lock()
APPLICATION_TABLES = {
    "app_metadata",
    "candidate_ai_analysis",
    "candidates",
    "clause_coverage_analysis",
    "clause_structure_history",
    "clauses",
    "decision_history",
    "kcs_clause_impacts",
    "kcs_rematch_runs",
    "project_review_events",
    "project_review_submissions",
    "projects",
    "quality_evaluation_items",
    "quality_evaluations",
    "upload_job_items",
    "upload_jobs",
}
REQUIRED_COLUMNS = {
    "projects": {"id", "source_path", "source_sha256", "status"},
    "upload_job_items": {
        "id",
        "source_path",
        "source_sha256",
        "status",
        "retryable",
    },
    "project_review_submissions": {
        "snapshot_schema_version",
        "review_snapshot_json",
        "review_snapshot_sha256",
    },
}


class BackupError(ValueError):
    """A backup is unsafe, corrupt, or incompatible."""


class BackupNotFoundError(BackupError):
    """A requested managed backup does not exist."""


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _source_basename(value: str) -> str:
    if not value or "\x00" in value:
        raise BackupError("백업의 원본 문서 경로가 올바르지 않습니다.")
    windows_name = PureWindowsPath(value).name
    posix_name = PurePosixPath(value).name
    name = windows_name if "\\" in value or ":" in value else posix_name
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise BackupError("백업의 원본 문서 이름이 올바르지 않습니다.")
    return name


def _safe_member_name(name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise BackupError("백업 파일 안에 허용되지 않은 경로가 있습니다.")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupError("백업 파일 안에 허용되지 않은 경로가 있습니다.")
    canonical = "/".join(path.parts)
    if canonical != name or ":" in path.parts[0]:
        raise BackupError("백업 파일 안에 허용되지 않은 경로가 있습니다.")
    return canonical


def _allowed_payload_name(name: str) -> bool:
    if name == "database.sqlite3":
        return True
    if name in {"kcs/manifest.json", "kcs/code_list.json"}:
        return True
    parts = PurePosixPath(name).parts
    return (
        len(parts) == 2
        and parts[0] == "uploads"
        and parts[1] not in {"", ".", ".."}
        and Path(parts[1]).suffix.lower() in {".doc", ".docx"}
    )


class BackupManager:
    def __init__(
        self,
        store: Store,
        backups_dir: Path,
        uploads_dir: Path,
        kcs_data_dir: Path,
        *,
        retention: int = 20,
    ):
        self.store = store
        self.backups_dir = backups_dir
        self.uploads_dir = uploads_dir
        self.kcs_data_dir = kcs_data_dir
        self.retention = max(1, min(int(retention), 200))
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)

    @property
    def authentication_key_path(self) -> Path:
        return self.backups_dir.parent / ".secrets" / AUTHENTICATION_KEY_FILENAME

    def _has_signed_backups(self) -> bool:
        for path in self.backups_dir.glob("backup-*.zip"):
            try:
                with zipfile.ZipFile(path) as archive:
                    info = archive.getinfo("manifest.json")
                    if info.file_size > MAX_MANIFEST_BYTES:
                        continue
                    manifest = json.loads(archive.read(info).decode("utf-8"))
                if (
                    isinstance(manifest, dict)
                    and manifest.get("backup_format_version") == BACKUP_FORMAT_VERSION
                ):
                    return True
            except (KeyError, OSError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile):
                continue
        return False

    def _authentication_key(self, *, create: bool) -> bytes:
        with _authentication_key_lock:
            try:
                key = self.authentication_key_path.read_bytes()
            except OSError as exc:
                if not isinstance(exc, FileNotFoundError):
                    raise BackupError("백업 서명 키를 읽을 수 없습니다.") from exc
                if not create:
                    raise BackupError(
                        "이 백업을 검증할 로컬 서명 키가 없습니다. 원래 data/.secrets 폴더가 필요합니다."
                    ) from exc
                if self._has_signed_backups():
                    raise BackupError(
                        "기존 백업의 서명 키가 없어 새 키를 만들지 않았습니다. 원래 data/.secrets 폴더를 복구해 주세요."
                    ) from exc
                key = secrets.token_bytes(AUTHENTICATION_KEY_BYTES)
                try:
                    self.authentication_key_path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(
                        self.authentication_key_path,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_BINARY", 0),
                        0o600,
                    )
                except FileExistsError:
                    try:
                        key = self.authentication_key_path.read_bytes()
                    except OSError as read_exc:
                        raise BackupError("백업 서명 키를 읽을 수 없습니다.") from read_exc
                except OSError as create_exc:
                    raise BackupError("백업 서명 키를 만들 수 없습니다.") from create_exc
                else:
                    try:
                        view = memoryview(key)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise OSError("백업 서명 키를 기록하지 못했습니다.")
                            view = view[written:]
                        os.fsync(descriptor)
                    except OSError as write_exc:
                        os.close(descriptor)
                        descriptor = -1
                        try:
                            self.authentication_key_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        raise BackupError("백업 서명 키를 만들 수 없습니다.") from write_exc
                    finally:
                        if descriptor >= 0:
                            os.close(descriptor)
            if len(key) != AUTHENTICATION_KEY_BYTES:
                raise BackupError("백업 서명 키가 손상되었습니다.")
            return key

    def _sign_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        key = self._authentication_key(create=True)
        authentication = {
            "algorithm": AUTHENTICATION_ALGORITHM,
            "key_id": hashlib.sha256(key).hexdigest()[:16],
        }
        signed = dict(manifest)
        signed["authentication"] = authentication
        signature = hmac.new(
            key,
            AUTHENTICATION_DOMAIN + json.dumps(
                signed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        signed["authentication"] = {**authentication, "signature": signature}
        return signed

    def _verify_manifest_authentication(self, manifest: dict[str, Any]) -> None:
        authentication = manifest.get("authentication")
        if not isinstance(authentication, dict) or set(authentication) != {
            "algorithm",
            "key_id",
            "signature",
        }:
            raise BackupError("백업 서명 정보가 없거나 올바르지 않습니다.")
        key = self._authentication_key(create=False)
        expected_key_id = hashlib.sha256(key).hexdigest()[:16]
        signature = str(authentication.get("signature") or "").lower()
        if (
            authentication.get("algorithm") != AUTHENTICATION_ALGORITHM
            or authentication.get("key_id") != expected_key_id
            or not SHA256_PATTERN.fullmatch(signature)
        ):
            raise BackupError("이 프로그램에서 만든 백업인지 확인할 수 없습니다.")
        unsigned = dict(manifest)
        unsigned["authentication"] = {
            "algorithm": AUTHENTICATION_ALGORITHM,
            "key_id": expected_key_id,
        }
        expected_signature = hmac.new(
            key,
            AUTHENTICATION_DOMAIN + json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise BackupError("백업 파일이 생성된 이후 변경되어 복구할 수 없습니다.")

    def _managed_backup_path(self, backup_id: str) -> Path:
        normalized = backup_id.strip().lower()
        if not BACKUP_ID_PATTERN.fullmatch(normalized):
            raise BackupNotFoundError("백업 파일을 찾을 수 없습니다.")
        matches = sorted(self.backups_dir.glob(f"backup-*-{normalized}.zip"))
        if len(matches) != 1:
            raise BackupNotFoundError("백업 파일을 찾을 수 없습니다.")
        resolved = matches[0].resolve()
        if resolved.parent != self.backups_dir.resolve() or not resolved.is_file():
            raise BackupNotFoundError("백업 파일을 찾을 수 없습니다.")
        return resolved

    @staticmethod
    def _database_counts(database_path: Path) -> tuple[int, int]:
        connection = sqlite3.connect(database_path)
        try:
            project_count = int(
                connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            )
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            return project_count, schema_version
        finally:
            connection.close()

    def _upload_references(self, database_path: Path) -> list[tuple[Path, str, str]]:
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT source_path, source_sha256 FROM projects"
            ).fetchall()
            rows += connection.execute(
                """
                SELECT source_path, source_sha256
                FROM upload_job_items
                WHERE status IN ('queued', 'running') OR retryable = 1
                """
            ).fetchall()
        finally:
            connection.close()

        upload_root = self.uploads_dir.resolve()
        references: dict[str, tuple[Path, str]] = {}
        for row in rows:
            source_path = Path(str(row["source_path"])).resolve()
            expected_sha256 = str(row["source_sha256"] or "").lower()
            if source_path.parent != upload_root:
                raise BackupError(
                    "관리 폴더 밖의 원본 문서가 있어 백업을 만들 수 없습니다."
                )
            if not source_path.is_file():
                raise BackupError("원본 문서가 누락되어 백업을 만들 수 없습니다.")
            if not SHA256_PATTERN.fullmatch(expected_sha256):
                raise BackupError("원본 문서의 무결성 정보가 올바르지 않습니다.")
            actual_sha256 = _sha256_path(source_path)
            if actual_sha256 != expected_sha256:
                raise BackupError("원본 문서가 업로드 이후 변경되어 백업을 중단했습니다.")
            key = source_path.name.casefold()
            existing = references.get(key)
            if existing and existing[1] != expected_sha256:
                raise BackupError("서로 다른 원본 문서가 같은 저장 이름을 사용하고 있습니다.")
            references[key] = (source_path, expected_sha256)
        return [
            (path, f"uploads/{path.name}", digest)
            for path, digest in sorted(
                references.values(), key=lambda item: item[0].name.casefold()
            )
        ]

    def create(self, reason: str = "manual", *, prune: bool = True) -> dict[str, Any]:
        if reason not in ALLOWED_REASONS:
            raise BackupError("백업 생성 사유가 올바르지 않습니다.")
        backup_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        final_path = self.backups_dir / f"backup-{timestamp}-{backup_id}.zip"
        pending_path = self.backups_dir / f".{backup_id}.tmp"

        try:
            with tempfile.TemporaryDirectory(
                prefix=".backup-stage-", dir=self.backups_dir
            ) as temporary:
                stage = Path(temporary)
                source_snapshot = stage / "source.sqlite3"
                database_snapshot = stage / "database.sqlite3"
                self.store.backup_database(source_snapshot)
                try:
                    self.store.build_canonical_snapshot(source_snapshot, database_snapshot)
                except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
                    raise BackupError(str(exc) or "백업 데이터베이스를 표준화하지 못했습니다.") from exc
                project_count, schema_version = self._database_counts(database_snapshot)
                if schema_version != DATABASE_SCHEMA_VERSION:
                    raise BackupError("현재 데이터베이스 스키마 버전을 확인할 수 없습니다.")
                schema_fingerprint = self.store.schema_fingerprint(database_snapshot)
                if schema_fingerprint != self.store.expected_schema_fingerprint():
                    raise BackupError("현재 데이터베이스 구조를 안전하게 백업할 수 없습니다.")

                file_sources: list[tuple[Path, str, str | None]] = [
                    (database_snapshot, "database.sqlite3", None),
                    *self._upload_references(database_snapshot),
                ]
                for filename in ("manifest.json", "code_list.json"):
                    source = self.kcs_data_dir / filename
                    if source.is_file():
                        file_sources.append((source, f"kcs/{filename}", None))

                files: list[dict[str, Any]] = []
                for source, archive_name, known_digest in file_sources:
                    files.append(
                        {
                            "path": archive_name,
                            "size": source.stat().st_size,
                            "sha256": known_digest or _sha256_path(source),
                        }
                    )
                files.sort(key=lambda item: item["path"])
                manifest = self._sign_manifest({
                    "backup_format_version": BACKUP_FORMAT_VERSION,
                    "backup_id": backup_id,
                    "created_at": created_at,
                    "reason": reason,
                    "database_schema_version": schema_version,
                    "database_schema_sha256": schema_fingerprint,
                    "project_count": project_count,
                    "upload_count": sum(
                        1 for item in files if item["path"].startswith("uploads/")
                    ),
                    "files": files,
                })

                with zipfile.ZipFile(
                    pending_path,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    allowZip64=True,
                ) as archive:
                    for source, archive_name, _ in file_sources:
                        archive.write(source, archive_name)
                    archive.writestr(
                        "manifest.json",
                        json.dumps(
                            manifest,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8"),
                    )
                os.replace(pending_path, final_path)
        finally:
            pending_path.unlink(missing_ok=True)

        try:
            record = self.inspect_archive(final_path, expected_backup_id=backup_id)
        except Exception:
            final_path.unlink(missing_ok=True)
            raise
        if prune:
            self._prune()
        return record

    def _record_from_path(self, path: Path) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(path) as archive:
                manifest = self._read_manifest(archive, verify_payload=False)
            filename_backup_id = path.stem[-36:].lower()
            if (
                not BACKUP_ID_PATTERN.fullmatch(filename_backup_id)
                or manifest["backup_id"] != filename_backup_id
            ):
                raise BackupError("백업 파일명과 내부 식별값이 일치하지 않습니다.")
            database_entry = next(
                item for item in manifest["files"] if item["path"] == "database.sqlite3"
            )
            return {
                "id": manifest["backup_id"],
                "created_at": manifest["created_at"],
                "reason": manifest["reason"],
                "database_schema_version": manifest["database_schema_version"],
                "project_count": manifest["project_count"],
                "upload_count": manifest["upload_count"],
                "size_bytes": path.stat().st_size,
                "sha256": database_entry["sha256"],
                "restorable": True,
                "error": "",
            }
        except (BackupError, OSError, ValueError, zipfile.BadZipFile):
            backup_id = path.stem.rsplit("-", 5)[-5:]
            candidate_id = "-".join(backup_id)
            return {
                "id": candidate_id if BACKUP_ID_PATTERN.fullmatch(candidate_id) else path.stem,
                "created_at": datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                "reason": "unknown",
                "database_schema_version": 0,
                "project_count": 0,
                "upload_count": 0,
                "size_bytes": path.stat().st_size,
                "sha256": "",
                "restorable": False,
                "error": "백업 파일 검증에 실패했습니다.",
            }

    def list(self) -> list[dict[str, Any]]:
        records = [
            self._record_from_path(path)
            for path in self.backups_dir.glob("backup-*.zip")
            if path.is_file()
        ]
        return sorted(records, key=lambda item: item["created_at"], reverse=True)

    def download_path(self, backup_id: str) -> Path:
        path = self._managed_backup_path(backup_id)
        record = self._record_from_path(path)
        if not record["restorable"] or record["id"] != backup_id.strip().lower():
            raise BackupNotFoundError("백업 파일을 찾을 수 없습니다.")
        return path

    def _prune(self) -> None:
        candidates: list[tuple[int, str, Path]] = []
        for path in self.backups_dir.glob("backup-*.zip"):
            try:
                if path.is_file():
                    candidates.append((path.stat().st_mtime_ns, path.name, path))
            except OSError:
                logger.warning("백업 보존정책에서 파일 정보를 읽지 못했습니다: %s", path)
        paths = [item[2] for item in sorted(candidates, reverse=True)]
        for path in paths[self.retention :]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("오래된 백업 파일을 정리하지 못했습니다: %s", path)

    def _read_manifest(
        self,
        archive: zipfile.ZipFile,
        *,
        verify_payload: bool,
    ) -> dict[str, Any]:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_BACKUP_MEMBERS:
            raise BackupError("백업 파일의 항목 수가 허용 범위를 벗어났습니다.")

        by_name: dict[str, zipfile.ZipInfo] = {}
        casefold_names: set[str] = set()
        total_size = 0
        for info in infos:
            if info.is_dir():
                raise BackupError("백업 파일 안에 허용되지 않은 폴더 항목이 있습니다.")
            name = _safe_member_name(info.filename)
            folded = name.casefold()
            if name in by_name or folded in casefold_names:
                raise BackupError("백업 파일 안에 중복된 항목이 있습니다.")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000 or info.flag_bits & 0x1:
                raise BackupError("백업 파일 안에 허용되지 않은 링크 또는 암호화 항목이 있습니다.")
            total_size += info.file_size
            if total_size > MAX_BACKUP_UNCOMPRESSED_BYTES:
                raise BackupError("백업 파일의 압축 해제 크기가 허용 범위를 초과합니다.")
            if (
                info.file_size > 100 * 1024 * 1024
                and info.compress_size > 0
                and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
            ):
                raise BackupError("비정상적인 압축률의 백업 파일은 사용할 수 없습니다.")
            by_name[name] = info
            casefold_names.add(folded)

        manifest_info = by_name.get("manifest.json")
        if not manifest_info or manifest_info.file_size > MAX_MANIFEST_BYTES:
            raise BackupError("백업 설명 파일이 없거나 너무 큽니다.")
        try:
            manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError("백업 설명 파일을 읽을 수 없습니다.") from exc
        if not isinstance(manifest, dict):
            raise BackupError("백업 설명 형식이 올바르지 않습니다.")
        if manifest.get("backup_format_version") != BACKUP_FORMAT_VERSION:
            raise BackupError("지원하지 않는 백업 형식입니다.")
        self._verify_manifest_authentication(manifest)
        backup_id = str(manifest.get("backup_id") or "").lower()
        if not BACKUP_ID_PATTERN.fullmatch(backup_id):
            raise BackupError("백업 식별값이 올바르지 않습니다.")
        manifest["backup_id"] = backup_id
        if manifest.get("reason") not in ALLOWED_REASONS:
            raise BackupError("백업 생성 사유가 올바르지 않습니다.")
        if manifest.get("database_schema_version") != DATABASE_SCHEMA_VERSION:
            raise BackupError("현재 프로그램과 데이터베이스 버전이 맞지 않습니다.")
        schema_fingerprint = str(manifest.get("database_schema_sha256") or "").lower()
        if not SHA256_PATTERN.fullmatch(schema_fingerprint):
            raise BackupError("백업 데이터베이스 구조 정보가 올바르지 않습니다.")
        if not isinstance(manifest.get("created_at"), str):
            raise BackupError("백업 생성 시각이 올바르지 않습니다.")
        try:
            created_at = datetime.fromisoformat(manifest["created_at"])
        except ValueError as exc:
            raise BackupError("백업 생성 시각이 올바르지 않습니다.") from exc
        if created_at.tzinfo is None:
            raise BackupError("백업 생성 시각에 시간대 정보가 없습니다.")
        for field in ("project_count", "upload_count"):
            if not isinstance(manifest.get(field), int) or manifest[field] < 0:
                raise BackupError("백업 집계 정보가 올바르지 않습니다.")

        raw_files = manifest.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise BackupError("백업 파일 목록이 올바르지 않습니다.")
        manifest_files: dict[str, dict[str, Any]] = {}
        folded_payload_names: set[str] = set()
        for raw_file in raw_files:
            if not isinstance(raw_file, dict):
                raise BackupError("백업 파일 목록이 올바르지 않습니다.")
            name = _safe_member_name(str(raw_file.get("path") or ""))
            if not _allowed_payload_name(name):
                raise BackupError("백업에 허용되지 않은 파일이 포함되어 있습니다.")
            folded = name.casefold()
            if name in manifest_files or folded in folded_payload_names:
                raise BackupError("백업 설명에 중복된 파일이 있습니다.")
            size = raw_file.get("size")
            digest = str(raw_file.get("sha256") or "").lower()
            if not isinstance(size, int) or size < 0 or not SHA256_PATTERN.fullmatch(digest):
                raise BackupError("백업 파일의 무결성 정보가 올바르지 않습니다.")
            manifest_files[name] = {"path": name, "size": size, "sha256": digest}
            folded_payload_names.add(folded)
        if "database.sqlite3" not in manifest_files:
            raise BackupError("백업 데이터베이스가 없습니다.")
        if set(by_name) != {"manifest.json", *manifest_files}:
            raise BackupError("백업 설명에 없는 파일이 포함되어 있습니다.")
        for name, entry in manifest_files.items():
            info = by_name.get(name)
            if not info or info.file_size != entry["size"]:
                raise BackupError("백업 파일 크기가 설명과 일치하지 않습니다.")
            if verify_payload:
                digest = hashlib.sha256()
                with archive.open(info) as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != entry["sha256"]:
                    raise BackupError("백업 파일의 무결성 검증에 실패했습니다.")
        manifest["files"] = list(manifest_files.values())
        return manifest

    def _extract_verified(
        self,
        archive_path: Path,
        destination: Path,
    ) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                manifest = self._read_manifest(
                    archive,
                    verify_payload=True,
                )
                for entry in manifest["files"]:
                    name = entry["path"]
                    target = destination.joinpath(*PurePosixPath(name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                return manifest
        except zipfile.BadZipFile as exc:
            raise BackupError("ZIP 백업 파일을 읽을 수 없습니다.") from exc

    def _validate_database(
        self,
        database_path: Path,
        manifest: dict[str, Any],
        extracted_uploads: Path,
    ) -> dict[str, tuple[Path, str]]:
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise BackupError("백업 데이터베이스가 손상되었습니다.")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise BackupError("백업 데이터베이스의 연결 무결성이 손상되었습니다.")
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if (
                schema_version != DATABASE_SCHEMA_VERSION
                or schema_version != manifest["database_schema_version"]
            ):
                raise BackupError("백업 데이터베이스 버전이 설명과 일치하지 않습니다.")
            schema_fingerprint = self.store.schema_fingerprint(database_path)
            if (
                schema_fingerprint != manifest["database_schema_sha256"]
                or schema_fingerprint != self.store.expected_schema_fingerprint()
            ):
                raise BackupError("백업 데이터베이스 구조가 현재 프로그램과 다릅니다.")
            tables = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_schema
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    """
                ).fetchall()
            }
            if tables != APPLICATION_TABLES:
                raise BackupError("백업 데이터베이스 구조가 현재 프로그램과 다릅니다.")
            triggers = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'trigger'"
            ).fetchall()
            if triggers:
                raise BackupError("백업 데이터베이스에 허용되지 않은 동작이 포함되어 있습니다.")
            for table, required in REQUIRED_COLUMNS.items():
                columns = {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if not required.issubset(columns):
                    raise BackupError("백업 데이터베이스의 필수 항목이 누락되었습니다.")

            submission_rows = connection.execute(
                """
                SELECT snapshot_schema_version, review_snapshot_json,
                       review_snapshot_sha256
                FROM project_review_submissions
                """
            ).fetchall()
            for row in submission_rows:
                try:
                    snapshot = json.loads(row["review_snapshot_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise BackupError("승인 제출본의 무결성 정보가 손상되었습니다.") from exc
                if (
                    row["snapshot_schema_version"] != 1
                    or not isinstance(snapshot, dict)
                    or snapshot.get("snapshot_schema_version") != 1
                    or _canonical_json_sha256(snapshot) != row["review_snapshot_sha256"]
                ):
                    raise BackupError("승인 제출본의 무결성 정보가 손상되었습니다.")

            upload_entries = {
                PurePosixPath(entry["path"]).name: entry
                for entry in manifest["files"]
                if entry["path"].startswith("uploads/")
            }
            uploads_by_folded = {name.casefold(): name for name in upload_entries}
            if len(uploads_by_folded) != len(upload_entries):
                raise BackupError("백업 원본 문서 이름이 중복되었습니다.")

            mappings: dict[str, tuple[Path, str]] = {}
            project_rows = connection.execute(
                "SELECT source_path, source_sha256 FROM projects"
            ).fetchall()
            active_upload_rows = connection.execute(
                """
                SELECT source_path, source_sha256
                FROM upload_job_items
                WHERE status IN ('queued', 'running') OR retryable = 1
                """
            ).fetchall()
            for row in [*project_rows, *active_upload_rows]:
                basename = _source_basename(str(row["source_path"] or ""))
                archived_name = uploads_by_folded.get(basename.casefold())
                if not archived_name:
                    raise BackupError("백업에 필요한 원본 문서가 누락되었습니다.")
                entry = upload_entries[archived_name]
                expected_sha256 = str(row["source_sha256"] or "").lower()
                if entry["sha256"] != expected_sha256:
                    raise BackupError("원본 문서와 데이터베이스의 무결성 값이 다릅니다.")
                source = extracted_uploads / archived_name
                mappings[basename.casefold()] = (source, expected_sha256)
            if len(project_rows) != manifest["project_count"]:
                raise BackupError("백업 프로젝트 집계가 데이터베이스와 일치하지 않습니다.")
            if len(upload_entries) != manifest["upload_count"]:
                raise BackupError("백업 원본 문서 집계가 설명과 일치하지 않습니다.")
            return mappings
        except sqlite3.DatabaseError as exc:
            raise BackupError("백업 데이터베이스를 읽을 수 없습니다.") from exc
        finally:
            connection.close()

    def _rewrite_source_paths(
        self,
        database_path: Path,
        mappings: dict[str, tuple[Path, str]],
    ) -> dict[str, tuple[Path, Path]]:
        destinations: dict[str, tuple[Path, Path]] = {}
        for folded_name, (source, digest) in mappings.items():
            suffix = source.suffix.lower()
            destination = self.uploads_dir / f"restored-{digest[:24]}{suffix}"
            destinations[folded_name] = (source, destination)

        connection = sqlite3.connect(database_path)
        try:
            for table in ("projects", "upload_job_items"):
                rows = connection.execute(
                    f"SELECT rowid, source_path FROM {table}"
                ).fetchall()
                for rowid, source_path in rows:
                    try:
                        folded = _source_basename(str(source_path or "")).casefold()
                    except BackupError:
                        continue
                    mapping = destinations.get(folded)
                    if mapping:
                        connection.execute(
                            f"UPDATE {table} SET source_path = ? WHERE rowid = ?",
                            (str(mapping[1]), rowid),
                        )
            connection.commit()
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("복구 경로를 반영한 데이터베이스가 손상되었습니다.")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise BackupError("복구 경로 반영 후 연결 무결성 검증에 실패했습니다.")
        finally:
            connection.close()
        return destinations

    def _validate_replacement_database(self, database_path: Path) -> None:
        connection = sqlite3.connect(database_path)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise BackupError("복구 준비 중 데이터베이스가 손상되었습니다.")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise BackupError("복구 준비 중 연결 무결성이 손상되었습니다.")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != DATABASE_SCHEMA_VERSION:
                raise BackupError("복구 준비 중 데이터베이스 버전이 변경되었습니다.")
        except sqlite3.DatabaseError as exc:
            raise BackupError("복구 준비 데이터베이스를 읽을 수 없습니다.") from exc
        finally:
            connection.close()
        if (
            self.store.schema_fingerprint(database_path)
            != self.store.expected_schema_fingerprint()
        ):
            raise BackupError("복구 준비 중 데이터베이스 구조가 변경되었습니다.")

    def restore_archive(
        self,
        archive_path: Path,
        *,
        expected_backup_id: str = "",
        prepare_database: Callable[[Path], None] | None = None,
    ) -> dict[str, Any]:
        if not archive_path.is_file():
            raise BackupNotFoundError("백업 파일을 찾을 수 없습니다.")
        with tempfile.TemporaryDirectory(
            prefix=".restore-stage-", dir=self.backups_dir
        ) as temporary:
            stage = Path(temporary)
            stable_archive = stage / "restore.zip"
            shutil.copy2(archive_path, stable_archive)
            extracted = stage / "payload"
            extracted.mkdir()
            manifest = self._extract_verified(stable_archive, extracted)
            if expected_backup_id and manifest["backup_id"] != expected_backup_id:
                raise BackupError("선택한 백업과 내부 식별값이 일치하지 않습니다.")
            database_path = extracted / "database.sqlite3"
            mappings = self._validate_database(
                database_path,
                manifest,
                extracted / "uploads",
            )
            destinations = self._rewrite_source_paths(database_path, mappings)
            if prepare_database is not None:
                prepare_database(database_path)
                self._validate_replacement_database(database_path)

            created_uploads: list[Path] = []
            with self.store.maintenance():
                safety_backup = self.create("pre_restore", prune=False)
                try:
                    for source, destination in destinations.values():
                        if destination.exists():
                            if _sha256_path(destination) != _sha256_path(source):
                                raise BackupError(
                                    "복구 원본 문서와 같은 이름의 다른 파일이 이미 있습니다."
                                )
                            continue
                        temporary_upload = self.uploads_dir / f".{uuid.uuid4().hex}.tmp"
                        try:
                            shutil.copy2(source, temporary_upload)
                            os.replace(temporary_upload, destination)
                        finally:
                            temporary_upload.unlink(missing_ok=True)
                        created_uploads.append(destination)
                    self.store.replace_database(database_path)
                except Exception:
                    for path in created_uploads:
                        path.unlink(missing_ok=True)
                    raise
                self._prune()

            restored_record = {
                "id": manifest["backup_id"],
                "created_at": manifest["created_at"],
                "reason": manifest["reason"],
                "database_schema_version": manifest["database_schema_version"],
                "project_count": manifest["project_count"],
                "upload_count": manifest["upload_count"],
                "size_bytes": stable_archive.stat().st_size,
                "sha256": next(
                    item["sha256"]
                    for item in manifest["files"]
                    if item["path"] == "database.sqlite3"
                ),
                "restorable": True,
                "error": "",
            }
            return {
                "restored_backup": restored_record,
                "safety_backup": safety_backup,
                "restart_required": False,
            }

    def inspect_archive(
        self,
        archive_path: Path,
        *,
        expected_backup_id: str = "",
    ) -> dict[str, Any]:
        if not archive_path.is_file():
            raise BackupNotFoundError("백업 파일을 찾을 수 없습니다.")
        with tempfile.TemporaryDirectory(
            prefix=".inspect-stage-", dir=self.backups_dir
        ) as temporary:
            stage = Path(temporary)
            extracted = stage / "payload"
            extracted.mkdir()
            manifest = self._extract_verified(archive_path, extracted)
            if expected_backup_id and manifest["backup_id"] != expected_backup_id:
                raise BackupError("선택한 백업과 내부 식별값이 일치하지 않습니다.")
            self._validate_database(
                extracted / "database.sqlite3",
                manifest,
                extracted / "uploads",
            )
            database_entry = next(
                item for item in manifest["files"] if item["path"] == "database.sqlite3"
            )
            return {
                "id": manifest["backup_id"],
                "created_at": manifest["created_at"],
                "reason": manifest["reason"],
                "database_schema_version": manifest["database_schema_version"],
                "project_count": manifest["project_count"],
                "upload_count": manifest["upload_count"],
                "size_bytes": archive_path.stat().st_size,
                "sha256": database_entry["sha256"],
                "restorable": True,
                "error": "",
            }

    def restore(
        self,
        backup_id: str,
        *,
        prepare_database: Callable[[Path], None] | None = None,
    ) -> dict[str, Any]:
        normalized = backup_id.strip().lower()
        return self.restore_archive(
            self._managed_backup_path(normalized),
            expected_backup_id=normalized,
            prepare_database=prepare_database,
        )
