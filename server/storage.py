from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from .kcs_impact import plan_clause_impact
from .openai_ai import coverage_source_segments


KCS_DELETE_REASON = "fully_covered_by_kcs"
NON_KCS_DELETE_REASONS = {
    "internal_duplicate",
    "obsolete_requirement",
    "out_of_scope",
    "editorial_cleanup",
    "management_decision",
}
DELETE_REASONS = {KCS_DELETE_REASON, *NON_KCS_DELETE_REASONS}

DECISION_REASONS = {
    "keep": {"posco_specific", "posco_stricter", "partial_overlap_residual", "no_kcs_match"},
    "delete": DELETE_REASONS,
    "hold": {"needs_expert_review", "candidate_uncertain", "kcs_conflict"},
}
SAFE_DELETE_RELATIONS = {"equivalent", "kcs_covers"}
COVERAGE_STATUSES = {"fully_covered", "partially_covered", "posco_specific", "conflict", "uncertain"}
REQUIREMENT_STATUSES = {"covered", "not_covered", "conflict", "uncertain"}
REVIEW_WORKFLOW_STATUSES = {"reviewing", "submitted", "changes_requested", "approved"}
REVIEW_EDITABLE_STATUSES = {"reviewing", "changes_requested"}
CURRENT_PARSER_VERSION = "2026-09-numeric-context-v2"
DATABASE_SCHEMA_VERSION = 11
KCS_IMPACT_REASON_LABELS = {
    "candidate_meaning_changed": "후보 KCS의 의미가 변경됨",
    "candidate_added": "새 후보 KCS가 추가됨",
    "candidate_removed": "기존 후보 KCS가 제거됨",
    "candidate_metadata_changed": "후보의 순위·점수·개정 정보가 변경됨",
    "selected_candidate_removed": "선택한 KCS 후보가 제거됨",
}


class ClauseStructureConflictError(ValueError):
    """The requested structure edit conflicts with mutable project state."""


class ClauseStructureNotFoundError(ValueError):
    """A project or clause targeted by a structure edit does not exist."""


class KCSRematchConflictError(ValueError):
    """A KCS rematch operation no longer matches current project state."""


class KCSRematchNotFoundError(ValueError):
    """A KCS rematch run or impact does not exist."""


class UploadJobConflictError(ValueError):
    """An upload job item is not in the state required by the operation."""


class UploadJobNotFoundError(ValueError):
    """An upload job or item does not exist."""


class ReviewWorkflowConflictError(ValueError):
    """A review workflow transition conflicts with current project state."""


class ReviewWorkflowNotFoundError(ValueError):
    """A review workflow project or submission does not exist."""


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    kcs_snapshot TEXT NOT NULL,
    kcs_revision TEXT NOT NULL DEFAULT '',
    kcs_scope TEXT NOT NULL,
    parser_version TEXT NOT NULL DEFAULT '',
    archived_at TEXT,
    status TEXT NOT NULL DEFAULT 'reviewing',
    warning TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clauses (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_order INTEGER NOT NULL,
    label TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_type TEXT NOT NULL,
    outline_level INTEGER,
    match_context TEXT NOT NULL DEFAULT '',
    decision TEXT CHECK(decision IN ('keep', 'delete', 'hold') OR decision IS NULL),
    edited_content TEXT NOT NULL DEFAULT '',
    review_note TEXT NOT NULL DEFAULT '',
    decision_reason TEXT NOT NULL DEFAULT '',
    coverage_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(coverage_confirmed IN (0, 1)),
    selected_candidate_id TEXT,
    reviewed_at TEXT,
    UNIQUE(project_id, source_order)
);

CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    clause_id TEXT NOT NULL REFERENCES clauses(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    kcs_code TEXT NOT NULL,
    document_name TEXT NOT NULL,
    version TEXT NOT NULL,
    update_date TEXT NOT NULL,
    kcs_clause TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    score REAL NOT NULL,
    classification TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(clause_id, rank)
);

CREATE TABLE IF NOT EXISTS decision_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clause_id TEXT NOT NULL REFERENCES clauses(id) ON DELETE CASCADE,
    previous_decision TEXT,
    new_decision TEXT,
    edited_content TEXT NOT NULL,
    review_note TEXT NOT NULL,
    decision_reason TEXT NOT NULL DEFAULT '',
    coverage_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(coverage_confirmed IN (0, 1)),
    selected_candidate_id TEXT,
    coverage_analysis_json TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_ai_analysis (
    candidate_id TEXT PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK(relation_type IN (
        'equivalent', 'kcs_covers', 'partial_overlap',
        'posco_specific', 'conflict', 'unrelated'
    )),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    rationale TEXT NOT NULL,
    simplified_content TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    analyzed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clause_coverage_analysis (
    clause_id TEXT PRIMARY KEY REFERENCES clauses(id) ON DELETE CASCADE,
    candidate_ids_json TEXT NOT NULL,
    evidence_candidate_ids_json TEXT NOT NULL,
    coverage_status TEXT NOT NULL CHECK(coverage_status IN (
        'fully_covered', 'partially_covered', 'posco_specific', 'conflict', 'uncertain'
    )),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    requirements_json TEXT NOT NULL,
    residual_content TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL,
    deletion_safe INTEGER NOT NULL DEFAULT 0 CHECK(deletion_safe IN (0, 1)),
    model TEXT NOT NULL,
    analyzed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quality_evaluations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
    target_sample_size INTEGER NOT NULL DEFAULT 50 CHECK(target_sample_size = 50),
    population_size INTEGER NOT NULL,
    sample_seed TEXT NOT NULL,
    sampling_version TEXT NOT NULL DEFAULT 'stratified-v1',
    reviewer_name TEXT NOT NULL DEFAULT '담당자',
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS quality_evaluation_items (
    evaluation_id TEXT NOT NULL REFERENCES quality_evaluations(id) ON DELETE CASCADE,
    clause_id TEXT NOT NULL REFERENCES clauses(id) ON DELETE CASCADE,
    sample_order INTEGER NOT NULL,
    cohort TEXT NOT NULL,
    verdict TEXT CHECK(verdict IN (
        'candidate_selected', 'all_candidates_incorrect',
        'no_candidate_correct', 'kcs_missing'
    ) OR verdict IS NULL),
    relevant_candidate_id TEXT REFERENCES candidates(id) ON DELETE SET NULL,
    candidates_snapshot_json TEXT NOT NULL DEFAULT '[]',
    relevant_candidate_key TEXT NOT NULL DEFAULT '',
    relevant_rank_snapshot INTEGER,
    expected_kcs_code TEXT NOT NULL DEFAULT '',
    expected_kcs_clause TEXT NOT NULL DEFAULT '',
    quality_note TEXT NOT NULL DEFAULT '',
    evaluated_at TEXT,
    updated_at TEXT,
    PRIMARY KEY(evaluation_id, clause_id),
    UNIQUE(evaluation_id, sample_order)
);

CREATE TABLE IF NOT EXISTS clause_structure_history (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK(operation IN ('merge', 'split')),
    source_clause_ids_json TEXT NOT NULL,
    replacement_clause_ids_json TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kcs_rematch_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    from_revision TEXT NOT NULL,
    target_revision TEXT NOT NULL,
    target_snapshot TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'running', 'completed', 'failed', 'superseded'
    )),
    expected_state_sha256 TEXT NOT NULL DEFAULT '',
    matcher_signature_json TEXT NOT NULL DEFAULT '{}',
    total_clauses INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    material_change_count INTEGER NOT NULL DEFAULT 0,
    review_required_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(project_id, target_revision)
);

CREATE TABLE IF NOT EXISTS kcs_clause_impacts (
    run_id TEXT NOT NULL REFERENCES kcs_rematch_runs(id) ON DELETE CASCADE,
    clause_id TEXT NOT NULL REFERENCES clauses(id) ON DELETE CASCADE,
    impact_type TEXT NOT NULL,
    review_required INTEGER NOT NULL DEFAULT 0 CHECK(review_required IN (0, 1)),
    reason_json TEXT NOT NULL DEFAULT '[]',
    before_candidates_json TEXT NOT NULL DEFAULT '[]',
    after_candidates_json TEXT NOT NULL DEFAULT '[]',
    before_decision_json TEXT NOT NULL DEFAULT '{}',
    acknowledged_at TEXT,
    PRIMARY KEY(run_id, clause_id)
);

CREATE TABLE IF NOT EXISTS upload_jobs (
    id TEXT PRIMARY KEY,
    kcs_snapshot TEXT NOT NULL,
    kcs_revision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS upload_job_items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES upload_jobs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    original_filename TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_size INTEGER NOT NULL,
    suffix TEXT NOT NULL,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN (
        'queued', 'running', 'completed', 'failed'
    )),
    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
    phase TEXT NOT NULL DEFAULT 'queued',
    error TEXT NOT NULL DEFAULT '',
    retryable INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN (0, 1)),
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(job_id, position)
);

CREATE TABLE IF NOT EXISTS project_review_submissions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'submitted', 'approved', 'changes_requested', 'superseded'
    )),
    author_name TEXT NOT NULL,
    author_note TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL,
    decided_by TEXT NOT NULL DEFAULT '',
    decision_note TEXT NOT NULL DEFAULT '',
    decided_at TEXT,
    kcs_snapshot TEXT NOT NULL,
    kcs_revision TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    snapshot_schema_version INTEGER NOT NULL DEFAULT 1,
    review_snapshot_json TEXT NOT NULL,
    review_snapshot_sha256 TEXT NOT NULL,
    UNIQUE(project_id, revision_no)
);

CREATE TABLE IF NOT EXISTS project_review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id TEXT NOT NULL REFERENCES project_review_submissions(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'submitted', 'approved', 'changes_requested', 'superseded'
    )),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor_name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clauses_project_order
ON clauses(project_id, source_order);

CREATE INDEX IF NOT EXISTS idx_clauses_project_decision
ON clauses(project_id, decision);

CREATE INDEX IF NOT EXISTS idx_candidates_clause_rank
ON candidates(clause_id, rank);

CREATE INDEX IF NOT EXISTS idx_history_clause_changed
ON decision_history(clause_id, changed_at);

CREATE INDEX IF NOT EXISTS idx_candidate_ai_relation
ON candidate_ai_analysis(relation_type);

CREATE INDEX IF NOT EXISTS idx_clause_coverage_status
ON clause_coverage_analysis(coverage_status, deletion_safe);

CREATE INDEX IF NOT EXISTS idx_quality_items_progress
ON quality_evaluation_items(evaluation_id, verdict, sample_order);

CREATE INDEX IF NOT EXISTS idx_structure_history_project_changed
ON clause_structure_history(project_id, changed_at);

CREATE INDEX IF NOT EXISTS idx_kcs_rematch_runs_project_status
ON kcs_rematch_runs(project_id, status, queued_at);

CREATE INDEX IF NOT EXISTS idx_kcs_rematch_runs_status_queued
ON kcs_rematch_runs(status, queued_at);

CREATE INDEX IF NOT EXISTS idx_kcs_clause_impacts_clause
ON kcs_clause_impacts(clause_id, review_required, acknowledged_at);

CREATE INDEX IF NOT EXISTS idx_upload_job_items_status_queued
ON upload_job_items(status, queued_at, position);

CREATE INDEX IF NOT EXISTS idx_upload_job_items_job_position
ON upload_job_items(job_id, position);

CREATE UNIQUE INDEX IF NOT EXISTS idx_project_review_one_submitted
ON project_review_submissions(project_id)
WHERE status = 'submitted';

CREATE INDEX IF NOT EXISTS idx_project_review_submissions_project_revision
ON project_review_submissions(project_id, revision_no DESC);

CREATE INDEX IF NOT EXISTS idx_project_review_events_submission_time
ON project_review_events(submission_id, occurred_at, id);
"""


def _spaced_sample(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """Choose a deterministic spread across document order."""
    if count <= 0 or not rows:
        return []
    if count >= len(rows):
        return list(rows)
    if count == 1:
        return [rows[len(rows) // 2]]
    indices = {
        round(index * (len(rows) - 1) / (count - 1))
        for index in range(count)
    }
    return [rows[index] for index in sorted(indices)]


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _project_catalog_filter_signature(
    search: str,
    parser_status: str,
    include_archived: bool,
    archive_status: str,
    kcs_impact_only: bool,
    review_status: str,
    discipline: str,
) -> str:
    raw = json.dumps(
        {
            "search": search,
            "parser_status": parser_status,
            "include_archived": include_archived,
            "archive_status": archive_status,
            "kcs_impact_only": kcs_impact_only,
            "review_status": review_status,
            "discipline": discipline,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _encode_project_catalog_cursor(
    uploaded_at: str,
    project_id: str,
    filter_signature: str,
) -> str:
    raw = json.dumps(
        {
            "v": 1,
            "uploaded_at": uploaded_at,
            "id": project_id,
            "filters": filter_signature,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_project_catalog_cursor(
    cursor: str,
    filter_signature: str,
) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("프로젝트 페이지 커서가 올바르지 않습니다.") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or not isinstance(payload.get("uploaded_at"), str)
        or not payload["uploaded_at"]
        or not isinstance(payload.get("id"), str)
        or not payload["id"]
        or not isinstance(payload.get("filters"), str)
    ):
        raise ValueError("프로젝트 페이지 커서가 올바르지 않습니다.")
    if payload["filters"] != filter_signature:
        raise ValueError("프로젝트 페이지 커서가 현재 검색 조건과 일치하지 않습니다.")
    return payload["uploaded_at"], payload["id"]


def _clause_source_text(clause: sqlite3.Row | dict[str, Any]) -> str:
    return str(clause["content"] or clause["title"] or "")


def _structure_title(text: str) -> str:
    normalized = " ".join(text.split())
    return normalized if len(normalized) <= 90 else f"{normalized[:90].rstrip()}…"


def _structure_fingerprint(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _quality_candidate_key(candidate: sqlite3.Row | dict[str, Any]) -> str:
    """Return an ID-independent identity for one frozen KCS candidate."""
    identity = {
        "kcs_code": str(candidate["kcs_code"] or ""),
        "kcs_clause": str(candidate["kcs_clause"] or ""),
        "title": str(candidate["title"] or ""),
        "document_name": str(candidate["document_name"] or ""),
        "version": str(candidate["version"] or ""),
        "update_date": str(candidate["update_date"] or ""),
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_quality_candidates_snapshot(raw_snapshot: Any) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw_snapshot or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    candidates: list[dict[str, Any]] = []
    for raw_candidate in parsed[:3]:
        if not isinstance(raw_candidate, dict):
            continue
        candidate = dict(raw_candidate)
        required_key_fields = (
            "kcs_code",
            "kcs_clause",
            "title",
            "document_name",
            "version",
            "update_date",
        )
        for field in required_key_fields:
            candidate.setdefault(field, "")
        candidate.setdefault("candidate_key", _quality_candidate_key(candidate))
        candidates.append(candidate)
    return candidates


class Store:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self._database_lock = RLock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._database_lock:
            connection = sqlite3.connect(self.database_path, timeout=30)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 30000")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextmanager
    def maintenance(self) -> Iterator[None]:
        """Wait for current DB users and block new connections during maintenance."""
        with self._database_lock:
            yield

    def _database_sidecars(self) -> tuple[Path, Path]:
        """Return only SQLite's two exact WAL-mode sidecars for this database."""
        return (
            self.database_path.with_name(f"{self.database_path.name}-wal"),
            self.database_path.with_name(f"{self.database_path.name}-shm"),
        )

    @staticmethod
    def _unlink_best_effort(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _remove_database_sidecars(self) -> None:
        first_error: OSError | None = None
        for sidecar in self._database_sidecars():
            try:
                sidecar.unlink(missing_ok=True)
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _reject_database_artifact_path(self, path: Path, *, label: str) -> None:
        protected_paths = (self.database_path, *self._database_sidecars())
        resolved_path = path.resolve()
        if any(resolved_path == protected.resolve() for protected in protected_paths):
            raise ValueError(f"{label} 경로는 현재 데이터베이스 파일과 달라야 합니다.")

    def backup_database(self, destination: Path) -> None:
        """Create a transactionally consistent SQLite snapshot, including WAL data."""
        self._reject_database_artifact_path(destination, label="백업 대상")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)
        try:
            with self._database_lock:
                source = sqlite3.connect(self.database_path, timeout=30)
                try:
                    source.execute("PRAGMA busy_timeout = 30000")
                    target = sqlite3.connect(destination)
                    try:
                        source.backup(target)
                        target.commit()
                    finally:
                        target.close()
                finally:
                    source.close()
        except Exception:
            self._unlink_best_effort(destination)
            raise

    def schema_fingerprint(self, database_path: Path | None = None) -> str:
        """Hash the complete application schema without modifying the database."""
        target_path = (database_path or self.database_path).resolve()
        if not target_path.is_file():
            raise FileNotFoundError(target_path)

        uri = f"{target_path.as_uri()}?mode=ro"
        with self._database_lock:
            connection = sqlite3.connect(uri, uri=True, timeout=30)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA busy_timeout = 30000")
                schema_objects = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT type, name, tbl_name, sql
                        FROM sqlite_schema
                        WHERE name NOT GLOB 'sqlite_*'
                        ORDER BY type, name, tbl_name
                        """
                    ).fetchall()
                ]
                table_names = sorted(
                    str(item["name"])
                    for item in schema_objects
                    if item["type"] == "table"
                )
                tables: list[dict[str, Any]] = []
                for table_name in table_names:
                    table_xinfo = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT * FROM pragma_table_xinfo(?) ORDER BY cid",
                            (table_name,),
                        ).fetchall()
                    ]
                    foreign_keys = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT * FROM pragma_foreign_key_list(?) ORDER BY id, seq",
                            (table_name,),
                        ).fetchall()
                    ]
                    index_rows = connection.execute(
                        "SELECT * FROM pragma_index_list(?) ORDER BY name",
                        (table_name,),
                    ).fetchall()
                    indexes = []
                    for index_row in index_rows:
                        index = dict(index_row)
                        index["xinfo"] = [
                            dict(row)
                            for row in connection.execute(
                                "SELECT * FROM pragma_index_xinfo(?) ORDER BY seqno",
                                (str(index["name"]),),
                            ).fetchall()
                        ]
                        indexes.append(index)
                    tables.append(
                        {
                            "name": table_name,
                            "table_xinfo": table_xinfo,
                            "foreign_key_list": foreign_keys,
                            "indexes": indexes,
                        }
                    )
            finally:
                connection.close()

        canonical = json.dumps(
            {
                "sqlite_schema": schema_objects,
                "tables": tables,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _quote_schema_identifier(identifier: str) -> str:
        """Quote an identifier discovered from the authoritative local schema."""
        escaped = identifier.replace('"', '""')
        return f'"{escaped}"'

    @staticmethod
    def _application_table_columns(
        connection: sqlite3.Connection,
    ) -> dict[str, tuple[str, ...]]:
        table_names = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_schema
                WHERE type = 'table' AND name NOT GLOB 'sqlite_*'
                ORDER BY name
                """
            ).fetchall()
        ]
        return {
            table_name: tuple(
                str(row[1])
                for row in connection.execute(
                    "SELECT * FROM pragma_table_xinfo(?) ORDER BY cid",
                    (table_name,),
                ).fetchall()
            )
            for table_name in table_names
        }

    def expected_schema_fingerprint(self) -> str:
        """Return the fingerprint of a freshly initialized current schema."""
        with tempfile.TemporaryDirectory(prefix="spec-schema-") as temporary:
            database_path = Path(temporary) / "database.sqlite3"
            canonical_store = Store(database_path)
            return canonical_store.schema_fingerprint()

    def build_canonical_snapshot(
        self,
        source_snapshot: Path,
        destination: Path,
    ) -> None:
        """Copy validated application data into a fresh current-schema database."""
        source_path = source_snapshot.resolve()
        destination_path = destination.resolve()
        if not source_path.is_file():
            raise ValueError("원본 데이터베이스 스냅샷을 찾을 수 없습니다.")
        if source_path == destination_path:
            raise ValueError("원본과 정규화 대상 데이터베이스 경로가 같을 수 없습니다.")
        self._reject_database_artifact_path(destination_path, label="정규화 대상")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path = destination_path.with_name(
            f".{destination_path.name}.canonical-{uuid.uuid4().hex}.tmp"
        )
        staging_sidecars = (
            staging_path.with_name(f"{staging_path.name}-wal"),
            staging_path.with_name(f"{staging_path.name}-shm"),
        )

        try:
            with self._database_lock:
                canonical_store = Store(staging_path)
                source_uri = f"{source_path.as_uri()}?mode=ro"
                source = sqlite3.connect(source_uri, uri=True, timeout=30)
                target = sqlite3.connect(staging_path, timeout=30)
                try:
                    source.execute("PRAGMA query_only = ON")
                    source.execute("PRAGMA busy_timeout = 30000")
                    source.execute("BEGIN")
                    target.execute("PRAGMA busy_timeout = 30000")
                    target.execute("PRAGMA foreign_keys = OFF")

                    canonical_tables = self._application_table_columns(target)
                    source_tables = self._application_table_columns(source)
                    if set(source_tables) != set(canonical_tables):
                        raise ValueError(
                            "원본 데이터베이스의 테이블 구성이 현재 프로그램과 다릅니다."
                        )
                    for table_name, columns in canonical_tables.items():
                        if set(source_tables[table_name]) != set(columns):
                            raise ValueError(
                                "원본 데이터베이스의 컬럼 구성이 현재 프로그램과 다릅니다."
                            )

                    target.execute("BEGIN IMMEDIATE")
                    source_counts: dict[str, int] = {}
                    for table_name, columns in canonical_tables.items():
                        quoted_table = self._quote_schema_identifier(table_name)
                        quoted_columns = ", ".join(
                            self._quote_schema_identifier(column) for column in columns
                        )
                        source_counts[table_name] = int(
                            source.execute(
                                f"SELECT COUNT(*) FROM {quoted_table}"
                            ).fetchone()[0]
                        )
                        rows = source.execute(
                            f"SELECT {quoted_columns} FROM {quoted_table}"
                        )
                        insert_sql = (
                            f"INSERT INTO {quoted_table} ({quoted_columns}) VALUES "
                            f"({', '.join('?' for _ in columns)})"
                        )
                        while batch := rows.fetchmany(1000):
                            target.executemany(insert_sql, batch)
                    target.commit()

                    target.execute("PRAGMA foreign_keys = ON")
                    for table_name, expected_count in source_counts.items():
                        quoted_table = self._quote_schema_identifier(table_name)
                        actual_count = int(
                            target.execute(
                                f"SELECT COUNT(*) FROM {quoted_table}"
                            ).fetchone()[0]
                        )
                        if actual_count != expected_count:
                            raise RuntimeError(
                                "정규화 데이터베이스의 행 수 검증에 실패했습니다."
                            )
                    integrity = [
                        str(row[0])
                        for row in target.execute("PRAGMA integrity_check").fetchall()
                    ]
                    if integrity != ["ok"]:
                        raise RuntimeError(
                            "정규화 데이터베이스의 무결성 검증에 실패했습니다."
                        )
                    if target.execute("PRAGMA foreign_key_check").fetchone():
                        raise RuntimeError(
                            "정규화 데이터베이스의 연결 무결성 검증에 실패했습니다."
                        )
                    schema_version = int(
                        target.execute("PRAGMA user_version").fetchone()[0]
                    )
                    if schema_version != DATABASE_SCHEMA_VERSION:
                        raise RuntimeError(
                            "정규화 데이터베이스의 스키마 버전이 올바르지 않습니다."
                        )
                    checkpoint = target.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                    if checkpoint and int(checkpoint[0]) != 0:
                        raise RuntimeError(
                            "정규화 데이터베이스의 WAL을 통합하지 못했습니다."
                        )
                except Exception:
                    target.rollback()
                    raise
                finally:
                    target.close()
                    source.close()

                if (
                    canonical_store.schema_fingerprint(staging_path)
                    != self.expected_schema_fingerprint()
                ):
                    raise RuntimeError(
                        "정규화 데이터베이스 구조가 현재 프로그램과 다릅니다."
                    )

                for sidecar in staging_sidecars:
                    sidecar.unlink(missing_ok=True)
                for suffix in ("-wal", "-shm"):
                    destination_path.with_name(
                        f"{destination_path.name}{suffix}"
                    ).unlink(missing_ok=True)
                os.replace(staging_path, destination_path)
        finally:
            self._unlink_best_effort(staging_path)
            for sidecar in staging_sidecars:
                self._unlink_best_effort(sidecar)

    def replace_database(self, replacement: Path) -> None:
        """Atomically install a validated DB and roll back if initialization fails."""
        self._reject_database_artifact_path(replacement, label="복구 원본")
        if not replacement.is_file():
            raise ValueError("복구 원본 데이터베이스 파일을 찾을 수 없습니다.")
        rollback = self.database_path.with_name(
            f".{self.database_path.name}.restore-{uuid.uuid4().hex}.rollback"
        )
        with self._database_lock:
            try:
                source = sqlite3.connect(self.database_path, timeout=30)
                try:
                    source.execute("PRAGMA busy_timeout = 30000")
                    checkpoint = source.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                    if checkpoint and int(checkpoint[0]) != 0:
                        raise RuntimeError(
                            "사용 중인 데이터베이스 연결 때문에 복구를 시작할 수 없습니다."
                        )
                    target = sqlite3.connect(rollback)
                    try:
                        source.backup(target)
                        target.commit()
                    finally:
                        target.close()
                finally:
                    source.close()
            except Exception:
                self._unlink_best_effort(rollback)
                raise

            try:
                self._remove_database_sidecars()
            except OSError as exc:
                self._unlink_best_effort(rollback)
                raise RuntimeError(
                    "데이터베이스 보조 파일을 정리하지 못해 기존 상태를 유지했습니다."
                ) from exc

            try:
                os.replace(replacement, self.database_path)
            except OSError as exc:
                raise RuntimeError(
                    "복구 데이터베이스를 적용하지 못했습니다. 기존 데이터베이스와 "
                    "롤백 스냅샷을 보존했습니다."
                ) from exc

            try:
                self.initialize()
            except Exception as exc:
                try:
                    self._remove_database_sidecars()
                    os.replace(rollback, self.database_path)
                except (OSError, RuntimeError) as rollback_exc:
                    raise RuntimeError(
                        "복구 적용과 기존 상태 복원에 실패했습니다. 원본 롤백 "
                        "스냅샷은 보존했습니다."
                    ) from rollback_exc
                try:
                    self.initialize()
                except Exception as rollback_initialize_exc:
                    raise RuntimeError(
                        "기존 데이터베이스는 복원했지만 다시 열지 못했습니다."
                    ) from rollback_initialize_exc
                raise RuntimeError(
                    "복구 데이터베이스를 적용하지 못해 기존 상태로 되돌렸습니다."
                ) from exc

            # A cleanup problem must not turn an otherwise successful restore
            # into a reported failure. The stale file is safe to remove later.
            self._unlink_best_effort(rollback)

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            project_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(projects)")
            }
            if "kcs_revision" not in project_columns:
                connection.execute(
                    "ALTER TABLE projects ADD COLUMN kcs_revision TEXT NOT NULL DEFAULT ''"
                )
            if "parser_version" not in project_columns:
                connection.execute(
                    "ALTER TABLE projects ADD COLUMN parser_version TEXT NOT NULL DEFAULT ''"
                )
            if "archived_at" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN archived_at TEXT")
            connection.execute(
                """
                UPDATE projects
                SET status = 'reviewing'
                WHERE status NOT IN (
                    'reviewing', 'submitted', 'changes_requested', 'approved'
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_projects_catalog
                ON projects(archived_at, uploaded_at DESC, id DESC)
                """
            )
            clause_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(clauses)")
            }
            if "decision_reason" not in clause_columns:
                connection.execute(
                    "ALTER TABLE clauses ADD COLUMN decision_reason TEXT NOT NULL DEFAULT ''"
                )
            if "coverage_confirmed" not in clause_columns:
                connection.execute(
                    "ALTER TABLE clauses ADD COLUMN coverage_confirmed INTEGER NOT NULL DEFAULT 0"
                )
            if "match_context" not in clause_columns:
                connection.execute(
                    "ALTER TABLE clauses ADD COLUMN match_context TEXT NOT NULL DEFAULT ''"
                )
            history_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(decision_history)")
            }
            if "decision_reason" not in history_columns:
                connection.execute(
                    "ALTER TABLE decision_history ADD COLUMN decision_reason TEXT NOT NULL DEFAULT ''"
                )
            if "coverage_confirmed" not in history_columns:
                connection.execute(
                    "ALTER TABLE decision_history ADD COLUMN coverage_confirmed INTEGER NOT NULL DEFAULT 0"
                )
            if "coverage_analysis_json" not in history_columns:
                connection.execute(
                    "ALTER TABLE decision_history ADD COLUMN coverage_analysis_json TEXT NOT NULL DEFAULT ''"
                )
            inconsistent_no_match_rows = connection.execute(
                """
                SELECT c.*
                FROM clauses c
                JOIN projects p ON p.id = c.project_id
                WHERE p.status IN ('reviewing', 'changes_requested')
                  AND c.decision = 'keep'
                  AND c.decision_reason = 'no_kcs_match'
                  AND (
                    c.selected_candidate_id IS NOT NULL
                    OR c.coverage_confirmed != 0
                  )
                """
            ).fetchall()
            for row in inconsistent_no_match_rows:
                changed_at = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    UPDATE clauses
                    SET selected_candidate_id = NULL,
                        coverage_confirmed = 0,
                        reviewed_at = ?
                    WHERE id = ? AND project_id = ?
                    """,
                    (changed_at, row["id"], row["project_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO decision_history(
                        clause_id, previous_decision, new_decision,
                        edited_content, review_note, decision_reason,
                        coverage_confirmed, selected_candidate_id,
                        coverage_analysis_json, changed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
                    """,
                    (
                        row["id"],
                        row["decision"],
                        row["decision"],
                        row["edited_content"],
                        row["review_note"],
                        row["decision_reason"],
                        self._coverage_analysis_snapshot(connection, row["id"]),
                        changed_at,
                    ),
                )
            quality_item_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(quality_evaluation_items)")
            }
            snapshot_added = "candidates_snapshot_json" not in quality_item_columns
            quality_snapshot_schema_changed = snapshot_added
            if snapshot_added:
                connection.execute(
                    "ALTER TABLE quality_evaluation_items "
                    "ADD COLUMN candidates_snapshot_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "relevant_candidate_key" not in quality_item_columns:
                connection.execute(
                    "ALTER TABLE quality_evaluation_items "
                    "ADD COLUMN relevant_candidate_key TEXT NOT NULL DEFAULT ''"
                )
                quality_snapshot_schema_changed = True
            if "relevant_rank_snapshot" not in quality_item_columns:
                connection.execute(
                    "ALTER TABLE quality_evaluation_items "
                    "ADD COLUMN relevant_rank_snapshot INTEGER"
                )
                quality_snapshot_schema_changed = True
            if quality_snapshot_schema_changed:
                self._backfill_quality_candidate_snapshots(
                    connection,
                    refresh_candidates=snapshot_added,
                )
            upload_item_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(upload_job_items)")
            }
            if upload_item_columns and "retryable" not in upload_item_columns:
                connection.execute(
                    "ALTER TABLE upload_job_items "
                    "ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0"
                )
            legacy_coverage_rows = connection.execute(
                """
                SELECT ca.clause_id, ca.requirements_json, ca.coverage_status,
                       ca.rationale, c.id, c.content, c.title, c.decision,
                       c.edited_content, c.review_note, c.decision_reason,
                       c.coverage_confirmed, c.selected_candidate_id
                FROM clause_coverage_analysis ca
                JOIN clauses c ON c.id = ca.clause_id
                """
            ).fetchall()
            migration_rationale = (
                "원문 구간 완전성 검증이 추가되어 기존 전체포괄 분석이 무효화되었습니다. "
                "전체포괄 분석을 다시 실행해 주세요."
            )
            for row in legacy_coverage_rows:
                try:
                    requirements = json.loads(row["requirements_json"] or "[]")
                    source_segments = coverage_source_segments(
                        row["content"] or row["title"]
                    )
                    source_text_by_id = dict(source_segments)
                    expected_ids = list(source_text_by_id)
                    assigned_ids: list[str] = []
                    exact_source_text = bool(requirements)
                    for requirement in requirements:
                        source_ids = requirement.get("source_segment_ids", [])
                        if (
                            not isinstance(source_ids, list)
                            or len(source_ids) != 1
                            or requirement.get("requirement")
                            != source_text_by_id.get(str(source_ids[0]))
                        ):
                            exact_source_text = False
                            break
                        assigned_ids.append(str(source_ids[0]))
                    source_complete = (
                        exact_source_text
                        and len(assigned_ids) == len(expected_ids)
                        and len(set(assigned_ids)) == len(assigned_ids)
                        and set(assigned_ids) == set(expected_ids)
                    )
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                    source_complete = False
                if source_complete:
                    if (
                        row["decision"] == "delete"
                        and not row["coverage_confirmed"]
                        and row["coverage_status"] == "uncertain"
                        and row["rationale"] == migration_rationale
                    ):
                        latest_history = connection.execute(
                            """
                            SELECT coverage_confirmed
                            FROM decision_history
                            WHERE clause_id = ?
                            ORDER BY id DESC
                            LIMIT 1
                            """,
                            (row["clause_id"],),
                        ).fetchone()
                        if latest_history and latest_history["coverage_confirmed"]:
                            self._record_coverage_confirmation_reset(
                                connection,
                                row,
                                datetime.now(timezone.utc).isoformat(),
                            )
                    continue
                try:
                    canonical_requirements = [
                        {
                            "requirement": source_text,
                            "source_segment_ids": [source_id],
                            "status": "uncertain",
                            "evidence_candidate_ids": [],
                            "evidence": "원문 구간 완전성 검증 후 재분석이 필요합니다.",
                        }
                        for source_id, source_text in coverage_source_segments(
                            row["content"] or row["title"]
                        )
                    ]
                except ValueError:
                    canonical_requirements = []
                changed_at = datetime.now(timezone.utc).isoformat()
                confirmation_reset = bool(
                    row["decision"] == "delete" and row["coverage_confirmed"]
                )
                connection.execute(
                    """
                    UPDATE clause_coverage_analysis
                    SET coverage_status = 'uncertain', confidence = 0,
                        deletion_safe = 0, requirements_json = ?,
                        rationale = ?, analyzed_at = ?
                    WHERE clause_id = ?
                    """,
                    (
                        json.dumps(canonical_requirements, ensure_ascii=False),
                        migration_rationale,
                        changed_at,
                        row["clause_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE clauses SET coverage_confirmed = 0
                    WHERE id = ? AND decision = 'delete'
                    """,
                    (row["clause_id"],),
                )
                if confirmation_reset:
                    self._record_coverage_confirmation_reset(
                        connection,
                        row,
                        changed_at,
                    )
            connection.execute("PRAGMA optimize")
            connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")

    @staticmethod
    def _quality_candidates_snapshot(
        connection: sqlite3.Connection,
        clause_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT k.id, k.rank, k.kcs_code, k.document_name, k.version,
                   k.update_date, k.kcs_clause, k.title, k.score
            FROM candidates k
            WHERE k.clause_id = ?
              AND NOT EXISTS(
                  SELECT 1 FROM candidate_ai_analysis ai
                  WHERE ai.candidate_id = k.id AND ai.relation_type = 'unrelated'
                    AND ai.confidence >= 0.85
              )
            ORDER BY k.rank
            LIMIT 3
            """,
            (clause_id,),
        ).fetchall()
        snapshots: list[dict[str, Any]] = []
        for row in rows:
            candidate = dict(row)
            candidate["candidate_key"] = _quality_candidate_key(candidate)
            snapshots.append(candidate)
        return snapshots

    @classmethod
    def _backfill_quality_candidate_snapshots(
        cls,
        connection: sqlite3.Connection,
        *,
        refresh_candidates: bool,
    ) -> None:
        """Populate immutable candidate data when upgrading an older database."""
        rows = connection.execute(
            """
            SELECT evaluation_id, clause_id, relevant_candidate_id,
                   candidates_snapshot_json, relevant_candidate_key,
                   relevant_rank_snapshot
            FROM quality_evaluation_items
            """
        ).fetchall()
        for row in rows:
            candidates = (
                cls._quality_candidates_snapshot(connection, row["clause_id"])
                if refresh_candidates
                else _parse_quality_candidates_snapshot(row["candidates_snapshot_json"])
            )
            selected = next(
                (
                    candidate
                    for candidate in candidates
                    if (
                        row["relevant_candidate_id"]
                        and candidate.get("id") == row["relevant_candidate_id"]
                    )
                    or (
                        row["relevant_candidate_key"]
                        and candidate.get("candidate_key")
                        == row["relevant_candidate_key"]
                    )
                ),
                None,
            )
            relevant_key = str(row["relevant_candidate_key"] or "")
            relevant_rank = row["relevant_rank_snapshot"]
            if selected:
                relevant_key = relevant_key or str(selected.get("candidate_key") or "")
                if relevant_rank is None:
                    relevant_rank = selected.get("rank")
            connection.execute(
                """
                UPDATE quality_evaluation_items
                SET candidates_snapshot_json = ?, relevant_candidate_key = ?,
                    relevant_rank_snapshot = ?
                WHERE evaluation_id = ? AND clause_id = ?
                """,
                (
                    json.dumps(candidates, ensure_ascii=False),
                    relevant_key,
                    relevant_rank,
                    row["evaluation_id"],
                    row["clause_id"],
                ),
            )

    @staticmethod
    def _public_kcs_rematch_run(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        raw_signature = item.pop("matcher_signature_json", "{}") or "{}"
        try:
            signature = json.loads(raw_signature)
        except json.JSONDecodeError:
            signature = {}
        item["matcher_signature"] = signature if isinstance(signature, dict) else {}
        item["unacknowledged_count"] = int(item.get("unacknowledged_count") or 0)
        return item

    @staticmethod
    def _current_kcs_target(connection: sqlite3.Connection) -> tuple[str, str]:
        rows = {
            row["key"]: str(row["value"] or "")
            for row in connection.execute(
                """
                SELECT key, value FROM app_metadata
                WHERE key IN ('current_kcs_snapshot', 'current_kcs_revision')
                """
            ).fetchall()
        }
        return rows.get("current_kcs_snapshot", ""), rows.get("current_kcs_revision", "")

    @staticmethod
    def _queue_kcs_rematch(
        connection: sqlite3.Connection,
        project_id: str,
        from_revision: str,
        target_snapshot: str,
        target_revision: str,
        queued_at: str,
    ) -> str:
        run_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"spec-kcs-rematch:{project_id}:{target_revision}",
            )
        )
        connection.execute(
            """
            INSERT INTO kcs_rematch_runs(
                id, project_id, from_revision, target_revision,
                target_snapshot, status, queued_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(project_id, target_revision) DO NOTHING
            """,
            (
                run_id,
                project_id,
                from_revision,
                target_revision,
                target_snapshot,
                queued_at,
            ),
        )
        return run_id

    def publish_kcs_revision(self, snapshot: str, revision: str) -> list[dict[str, Any]]:
        snapshot = str(snapshot or "").strip()
        revision = str(revision or "").strip()
        if not snapshot or not revision:
            raise ValueError("현재 KCS 스냅샷과 개정 식별값이 필요합니다.")
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE projects
                SET kcs_revision = ?
                WHERE kcs_revision = '' AND kcs_snapshot = ?
                """,
                (revision, snapshot),
            )
            connection.executemany(
                """
                INSERT INTO app_metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    ("current_kcs_snapshot", snapshot),
                    ("current_kcs_revision", revision),
                ),
            )
            connection.execute(
                """
                UPDATE kcs_rematch_runs
                SET status = 'superseded', finished_at = ?,
                    error = '더 최신 KCS 개정이 게시되어 이 작업이 대체되었습니다.'
                WHERE status IN ('pending', 'running') AND target_revision != ?
                """,
                (now, revision),
            )
            connection.execute(
                """
                UPDATE kcs_rematch_runs
                SET status = 'superseded', target_snapshot = ?, finished_at = ?,
                    error = '동일 개정의 KCS 스냅샷이 교체되어 이 작업이 대체되었습니다.'
                WHERE status = 'running' AND target_revision = ?
                  AND target_snapshot != ?
                """,
                (snapshot, now, revision, snapshot),
            )
            connection.execute(
                """
                UPDATE kcs_rematch_runs
                SET target_snapshot = ?, queued_at = ?
                WHERE status = 'pending' AND target_revision = ?
                  AND target_snapshot != ?
                """,
                (snapshot, now, revision, snapshot),
            )
            projects = connection.execute(
                """
                SELECT id, kcs_revision, parser_version
                FROM projects
                WHERE archived_at IS NULL
                ORDER BY uploaded_at, id
                """
            ).fetchall()
            stale_project_ids: list[str] = []
            for project in projects:
                project_revision = str(project["kcs_revision"] or "")
                if (
                    project_revision == revision
                    or project["parser_version"] != CURRENT_PARSER_VERSION
                ):
                    continue
                stale_project_ids.append(project["id"])
                self._supersede_locked_review(
                    connection,
                    project["id"],
                    "KCS 개정이 변경되어 기존 승인 요청 또는 승인이 무효화되었습니다.",
                    now,
                )
                self._queue_kcs_rematch(
                    connection,
                    project["id"],
                    project_revision,
                    snapshot,
                    revision,
                    now,
                )
            if not stale_project_ids:
                return []
            placeholders = ",".join("?" for _ in stale_project_ids)
            rows = connection.execute(
                f"""
                SELECT * FROM kcs_rematch_runs
                WHERE target_revision = ? AND project_id IN ({placeholders})
                ORDER BY queued_at, id
                """,
                (revision, *stale_project_ids),
            ).fetchall()
            return [self._public_kcs_rematch_run(row) for row in rows]

    def set_current_kcs_revision(self, snapshot: str, revision: str) -> None:
        self.publish_kcs_revision(snapshot, revision)

    def recover_interrupted_kcs_rematches(self) -> dict[str, list[dict[str, Any]]]:
        """Requeue valid running work and supersede work that can no longer apply."""
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_snapshot, current_revision = self._current_kcs_target(connection)
            running = connection.execute(
                """
                SELECT r.*, p.kcs_revision AS project_kcs_revision,
                       p.parser_version AS project_parser_version
                FROM kcs_rematch_runs r
                LEFT JOIN projects p ON p.id = r.project_id
                WHERE r.status = 'running'
                ORDER BY r.queued_at, r.id
                """
            ).fetchall()
            requeued_ids: list[str] = []
            superseded_ids: list[str] = []
            for run in running:
                invalid_reason = ""
                if not current_snapshot or not current_revision:
                    invalid_reason = "현재 KCS 개정 정보가 없어 중단된 작업을 복구할 수 없습니다."
                elif (
                    run["target_revision"] != current_revision
                    or run["target_snapshot"] != current_snapshot
                ):
                    invalid_reason = "현재 KCS 개정과 일치하지 않아 중단된 작업이 대체되었습니다."
                elif run["project_parser_version"] != CURRENT_PARSER_VERSION:
                    invalid_reason = "원본 시방서 재업로드가 필요해 중단된 작업이 대체되었습니다."
                elif str(run["project_kcs_revision"] or "") != str(
                    run["from_revision"] or ""
                ):
                    invalid_reason = "프로젝트 KCS 개정 상태가 변경되어 중단된 작업이 대체되었습니다."
                elif str(run["project_kcs_revision"] or "") == current_revision:
                    invalid_reason = "프로젝트가 이미 현재 KCS 개정을 적용해 중단된 작업이 대체되었습니다."

                connection.execute(
                    "DELETE FROM kcs_clause_impacts WHERE run_id = ?",
                    (run["id"],),
                )
                if invalid_reason:
                    connection.execute(
                        """
                        UPDATE kcs_rematch_runs
                        SET status = 'superseded', expected_state_sha256 = '',
                            matcher_signature_json = '{}', total_clauses = 0,
                            matched_count = 0, material_change_count = 0,
                            review_required_count = 0, error = ?, finished_at = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (invalid_reason, now, run["id"]),
                    )
                    superseded_ids.append(run["id"])
                else:
                    connection.execute(
                        """
                        UPDATE kcs_rematch_runs
                        SET status = 'pending', expected_state_sha256 = '',
                            matcher_signature_json = '{}', total_clauses = 0,
                            matched_count = 0, material_change_count = 0,
                            review_required_count = 0, error = '', queued_at = ?,
                            started_at = NULL, finished_at = NULL
                        WHERE id = ? AND status = 'running'
                        """,
                        (now, run["id"]),
                    )
                    requeued_ids.append(run["id"])

            def public_runs(run_ids: list[str]) -> list[dict[str, Any]]:
                if not run_ids:
                    return []
                placeholders = ",".join("?" for _ in run_ids)
                recovered = connection.execute(
                    f"""
                    SELECT * FROM kcs_rematch_runs
                    WHERE id IN ({placeholders})
                    ORDER BY queued_at, id
                    """,
                    tuple(run_ids),
                ).fetchall()
                return [self._public_kcs_rematch_run(row) for row in recovered]

            return {
                "requeued": public_runs(requeued_ids),
                "superseded": public_runs(superseded_ids),
            }

    def ensure_kcs_rematch(self, project_id: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            project = connection.execute(
                "SELECT * FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if not project:
                raise KCSRematchNotFoundError("검토 프로젝트를 찾을 수 없습니다.")
            if project["parser_version"] != CURRENT_PARSER_VERSION:
                raise KCSRematchConflictError(
                    "이 프로젝트는 이전 문서 해석기로 생성되어 원본 시방서를 다시 업로드해야 합니다."
                )
            target_snapshot, target_revision = self._current_kcs_target(connection)
            if not target_snapshot or not target_revision:
                raise KCSRematchConflictError("현재 KCS 개정 정보가 없습니다.")
            existing = connection.execute(
                """
                SELECT r.*,
                       (SELECT COUNT(*) FROM kcs_clause_impacts i
                        WHERE i.run_id = r.id AND i.review_required = 1
                          AND i.acknowledged_at IS NULL) AS unacknowledged_count
                FROM kcs_rematch_runs r
                WHERE r.project_id = ? AND r.target_revision = ?
                """,
                (project_id, target_revision),
            ).fetchone()
            if existing:
                return self._public_kcs_rematch_run(existing)
            if str(project["kcs_revision"] or "") == target_revision:
                return None
            run_id = self._queue_kcs_rematch(
                connection,
                project_id,
                str(project["kcs_revision"] or ""),
                target_snapshot,
                target_revision,
                now,
            )
            row = connection.execute(
                "SELECT * FROM kcs_rematch_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            return self._public_kcs_rematch_run(row)

    def get_kcs_rematch_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT r.*,
                       (SELECT COUNT(*) FROM kcs_clause_impacts i
                        WHERE i.run_id = r.id AND i.review_required = 1
                          AND i.acknowledged_at IS NULL) AS unacknowledged_count
                FROM kcs_rematch_runs r
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if not row:
                return None
            result = self._public_kcs_rematch_run(row)
            impacts = connection.execute(
                """
                SELECT i.*, r.target_revision, r.finished_at
                FROM kcs_clause_impacts i
                JOIN kcs_rematch_runs r ON r.id = i.run_id
                WHERE i.run_id = ?
                ORDER BY i.clause_id
                """,
                (run_id,),
            ).fetchall()
            result["impacts"] = [self._public_kcs_impact(impact, full=True) for impact in impacts]
            return result

    def list_kcs_rematch_runs(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.*,
                       (SELECT COUNT(*) FROM kcs_clause_impacts i
                        WHERE i.run_id = r.id AND i.review_required = 1
                          AND i.acknowledged_at IS NULL) AS unacknowledged_count
                FROM kcs_rematch_runs r
                WHERE r.project_id = ?
                ORDER BY r.queued_at DESC, r.id DESC
                """,
                (project_id,),
            ).fetchall()
            return [self._public_kcs_rematch_run(row) for row in rows]

    def pending_kcs_rematch_run_ids(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.id
                FROM kcs_rematch_runs r
                JOIN projects p ON p.id = r.project_id
                WHERE r.status = 'pending' AND p.archived_at IS NULL
                ORDER BY r.queued_at, r.id
                """
            ).fetchall()
            return [str(row["id"]) for row in rows]

    def active_kcs_rematch_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM kcs_rematch_runs r
                JOIN projects p ON p.id = r.project_id
                WHERE r.status = 'running'
                   OR (r.status = 'pending' AND p.archived_at IS NULL)
                """
            ).fetchone()
            return int(row["count"] if row else 0)

    def retry_kcs_rematch(self, run_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM kcs_rematch_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not run:
                raise KCSRematchNotFoundError("KCS 재매칭 작업을 찾을 수 없습니다.")
            if run["status"] not in {"failed", "superseded"}:
                raise KCSRematchConflictError("실패했거나 대체된 재매칭 작업만 다시 시도할 수 있습니다.")
            target_snapshot, target_revision = self._current_kcs_target(connection)
            if run["target_revision"] != target_revision:
                raise KCSRematchConflictError("현재 KCS 개정과 일치하는 작업만 다시 시도할 수 있습니다.")
            project = connection.execute(
                "SELECT kcs_revision FROM projects WHERE id = ?",
                (run["project_id"],),
            ).fetchone()
            if not project:
                raise KCSRematchNotFoundError("검토 프로젝트를 찾을 수 없습니다.")
            if str(project["kcs_revision"] or "") == target_revision:
                raise KCSRematchConflictError("프로젝트가 이미 현재 KCS 개정으로 재매칭되었습니다.")
            connection.execute("DELETE FROM kcs_clause_impacts WHERE run_id = ?", (run_id,))
            connection.execute(
                """
                UPDATE kcs_rematch_runs
                SET from_revision = ?, target_snapshot = ?, status = 'pending',
                    expected_state_sha256 = '', matcher_signature_json = '{}',
                    total_clauses = 0, matched_count = 0,
                    material_change_count = 0, review_required_count = 0,
                    error = '', queued_at = ?, started_at = NULL, finished_at = NULL
                WHERE id = ?
                """,
                (str(project["kcs_revision"] or ""), target_snapshot, now, run_id),
            )
            updated = connection.execute(
                "SELECT * FROM kcs_rematch_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            return self._public_kcs_rematch_run(updated)

    def fail_kcs_rematch(
        self,
        run_id: str,
        error: str,
        expected_state_sha256: str | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM kcs_rematch_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not run:
                raise KCSRematchNotFoundError("KCS 재매칭 작업을 찾을 수 없습니다.")
            if run["status"] == "superseded":
                return self._public_kcs_rematch_run(run)
            if run["status"] != "running":
                raise KCSRematchConflictError("실행 중인 재매칭 작업만 실패로 종료할 수 있습니다.")
            if (
                expected_state_sha256 is not None
                and expected_state_sha256 != run["expected_state_sha256"]
            ):
                raise KCSRematchConflictError("재매칭 작업 상태가 변경되었습니다.")
            connection.execute(
                """
                UPDATE kcs_rematch_runs
                SET status = 'failed', error = ?, finished_at = ?
                WHERE id = ?
                """,
                ((str(error or "재매칭 중 오류가 발생했습니다.").strip())[:2000], now, run_id),
            )
            updated = connection.execute(
                "SELECT * FROM kcs_rematch_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            return self._public_kcs_rematch_run(updated)

    @staticmethod
    def _public_kcs_impact(
        row: sqlite3.Row | dict[str, Any],
        *,
        full: bool,
    ) -> dict[str, Any]:
        item = dict(row)
        item["review_required"] = bool(item["review_required"])
        json_fields = (
            ("reason_json", "reasons", []),
            ("before_candidates_json", "before_candidates", []),
            ("after_candidates_json", "after_candidates", []),
            ("before_decision_json", "before_decision", {}),
        )
        for source, destination, fallback in json_fields:
            raw_value = item.pop(source, "") or ""
            try:
                parsed = json.loads(raw_value) if raw_value else fallback
            except json.JSONDecodeError:
                parsed = fallback
            if full or source == "reason_json":
                item[destination] = parsed
        reasons = item.get("reasons")
        if not isinstance(reasons, list):
            reasons = []
            item["reasons"] = reasons
        reason_labels = [
            KCS_IMPACT_REASON_LABELS.get(str(reason), str(reason))
            for reason in reasons
            if str(reason).strip()
        ]
        item["reason"] = " · ".join(reason_labels)
        return item

    @staticmethod
    def _rematch_state_snapshot(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> dict[str, Any]:
        project = connection.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not project:
            raise KCSRematchNotFoundError("검토 프로젝트를 찾을 수 없습니다.")
        clause_rows = connection.execute(
            """
            SELECT * FROM clauses
            WHERE project_id = ?
            ORDER BY source_order
            """,
            (project_id,),
        ).fetchall()
        clauses_by_id: dict[str, dict[str, Any]] = {}
        clauses: list[dict[str, Any]] = []
        for row in clause_rows:
            clause = dict(row)
            decision_snapshot = {
                "decision": clause.get("decision"),
                "decision_reason": clause.get("decision_reason") or "",
                "selected_candidate_id": clause.get("selected_candidate_id"),
                "edited_content": clause.get("edited_content") or "",
                "review_note": clause.get("review_note") or "",
                "coverage_confirmed": bool(clause.get("coverage_confirmed")),
                "reviewed_at": clause.get("reviewed_at"),
            }
            clause["decision_snapshot"] = decision_snapshot
            clause["candidates"] = []
            clause["coverage_analysis"] = None
            clauses.append(clause)
            clauses_by_id[clause["id"]] = clause

        candidate_rows = connection.execute(
            """
            SELECT k.*, ai.relation_type, ai.confidence,
                   ai.rationale AS ai_rationale,
                   ai.simplified_content AS ai_simplified_content,
                   ai.model AS ai_model, ai.analyzed_at AS ai_analyzed_at
            FROM candidates k
            JOIN clauses c ON c.id = k.clause_id
            LEFT JOIN candidate_ai_analysis ai ON ai.candidate_id = k.id
            WHERE c.project_id = ?
            ORDER BY k.clause_id, k.rank
            """,
            (project_id,),
        ).fetchall()
        for row in candidate_rows:
            candidate = dict(row)
            try:
                candidate["reasons"] = json.loads(candidate.pop("reasons_json") or "[]")
            except json.JSONDecodeError:
                candidate["reasons"] = []
            try:
                candidate["warnings"] = json.loads(candidate.pop("warnings_json") or "[]")
            except json.JSONDecodeError:
                candidate["warnings"] = []
            relation_type = candidate.pop("relation_type")
            confidence = candidate.pop("confidence")
            rationale = candidate.pop("ai_rationale")
            simplified_content = candidate.pop("ai_simplified_content")
            model = candidate.pop("ai_model")
            analyzed_at = candidate.pop("ai_analyzed_at")
            candidate["ai_analysis"] = (
                {
                    "relation_type": relation_type,
                    "confidence": confidence,
                    "rationale": rationale,
                    "simplified_content": simplified_content,
                    "model": model,
                    "analyzed_at": analyzed_at,
                }
                if relation_type
                else None
            )
            clauses_by_id[row["clause_id"]]["candidates"].append(candidate)

        coverage_rows = connection.execute(
            """
            SELECT ca.*
            FROM clause_coverage_analysis ca
            JOIN clauses c ON c.id = ca.clause_id
            WHERE c.project_id = ?
            """,
            (project_id,),
        ).fetchall()
        for row in coverage_rows:
            clauses_by_id[row["clause_id"]]["coverage_analysis"] = dict(row)

        return {
            "snapshot_version": 1,
            "project": dict(project),
            "clauses": clauses,
        }

    @classmethod
    def _rematch_preparation(
        cls,
        run: sqlite3.Row,
        state_snapshot: dict[str, Any],
        state_sha256: str,
    ) -> dict[str, Any]:
        return {
            "run": cls._public_kcs_rematch_run(run),
            "project": state_snapshot["project"],
            "clauses": state_snapshot["clauses"],
            "expected_state_sha256": state_sha256,
        }

    @classmethod
    def _validate_rematch_target(
        cls,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
    ) -> None:
        current_snapshot, current_revision = cls._current_kcs_target(connection)
        if (
            run["target_revision"] != current_revision
            or run["target_snapshot"] != current_snapshot
        ):
            raise KCSRematchConflictError("KCS 개정이 다시 변경되어 이 작업을 적용할 수 없습니다.")
        project = connection.execute(
            "SELECT kcs_revision, parser_version, status FROM projects WHERE id = ?",
            (run["project_id"],),
        ).fetchone()
        if not project:
            raise KCSRematchNotFoundError("검토 프로젝트를 찾을 수 없습니다.")
        if project["parser_version"] != CURRENT_PARSER_VERSION:
            raise KCSRematchConflictError(
                "이 프로젝트는 원본 시방서를 다시 업로드해야 합니다."
            )
        if str(project["kcs_revision"] or "") != str(run["from_revision"] or ""):
            raise KCSRematchConflictError("프로젝트의 KCS 개정 상태가 변경되었습니다.")
        if project["status"] in {"submitted", "approved"}:
            cls._supersede_locked_review(
                connection,
                run["project_id"],
                "KCS 자동 재매칭이 시작되어 기존 승인 요청 또는 승인이 무효화되었습니다.",
                datetime.now(timezone.utc).isoformat(),
            )

    def claim_kcs_rematch(
        self,
        run_id: str,
        matcher_signature: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if matcher_signature is not None and not isinstance(matcher_signature, dict):
            raise ValueError("재매칭 실행 정보 형식이 올바르지 않습니다.")
        signature_json = json.dumps(
            matcher_signature or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM kcs_rematch_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not run:
                raise KCSRematchNotFoundError("KCS 재매칭 작업을 찾을 수 없습니다.")
            if run["status"] != "pending":
                raise KCSRematchConflictError("대기 중인 재매칭 작업만 실행할 수 있습니다.")
            self._validate_rematch_target(connection, run)
            state_snapshot = self._rematch_state_snapshot(connection, run["project_id"])
            state_sha256 = _structure_fingerprint(state_snapshot)
            total_clauses = sum(
                clause["source_type"] in {"paragraph", "table"}
                for clause in state_snapshot["clauses"]
            )
            connection.execute(
                """
                UPDATE kcs_rematch_runs
                SET status = 'running', expected_state_sha256 = ?,
                    matcher_signature_json = ?, total_clauses = ?,
                    error = '', started_at = ?, finished_at = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (state_sha256, signature_json, total_clauses, now, run_id),
            )
            claimed = connection.execute(
                "SELECT * FROM kcs_rematch_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            return self._rematch_preparation(claimed, state_snapshot, state_sha256)

    def prepare_kcs_rematch(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN")
            run = connection.execute(
                "SELECT * FROM kcs_rematch_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not run:
                raise KCSRematchNotFoundError("KCS 재매칭 작업을 찾을 수 없습니다.")
            if run["status"] != "running":
                raise KCSRematchConflictError("실행 중인 재매칭 작업만 준비할 수 있습니다.")
            self._validate_rematch_target(connection, run)
            state_snapshot = self._rematch_state_snapshot(connection, run["project_id"])
            state_sha256 = _structure_fingerprint(state_snapshot)
            if state_sha256 != run["expected_state_sha256"]:
                raise KCSRematchConflictError(
                    "프로젝트가 재매칭 준비 후 변경되었습니다. 작업을 다시 시작해 주세요."
                )
            return self._rematch_preparation(run, state_snapshot, state_sha256)

    @staticmethod
    def _rematch_candidate_row(
        candidate: dict[str, Any],
        clause_id: str,
    ) -> dict[str, Any]:
        try:
            rank = int(candidate["rank"])
            score = float(candidate["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("재매칭 후보의 순위 또는 점수가 올바르지 않습니다.") from exc
        if rank not in {1, 2, 3}:
            raise ValueError("재매칭 후보 순위는 1~3이어야 합니다.")
        candidate_id = str(candidate.get("id") or "").strip()
        if not candidate_id:
            raise ValueError("재매칭 후보 식별값이 없습니다.")
        reasons = candidate.get("reasons", [])
        warnings = candidate.get("warnings", [])
        if not isinstance(reasons, list) or not isinstance(warnings, list):
            raise ValueError("재매칭 후보 근거 또는 경고 형식이 올바르지 않습니다.")
        return {
            "id": candidate_id,
            "clause_id": clause_id,
            "rank": rank,
            "kcs_code": str(candidate.get("kcs_code") or ""),
            "document_name": str(candidate.get("document_name") or ""),
            "version": str(candidate.get("version") or ""),
            "update_date": str(candidate.get("update_date") or ""),
            "kcs_clause": str(candidate.get("kcs_clause") or ""),
            "title": str(candidate.get("title") or ""),
            "content": str(candidate.get("content") or ""),
            "score": score,
            "classification": str(candidate.get("classification") or ""),
            "reasons": reasons,
            "warnings": warnings,
        }

    def apply_kcs_rematch(
        self,
        run_id: str,
        expected_state_sha256: str,
        clauses: list[dict[str, Any]],
        matcher_signature: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not expected_state_sha256:
            raise ValueError("재매칭 준비 상태 식별값이 없습니다.")
        if not isinstance(clauses, list):
            raise ValueError("재매칭 결과 형식이 올바르지 않습니다.")
        if matcher_signature is not None and not isinstance(matcher_signature, dict):
            raise ValueError("재매칭 실행 정보 형식이 올바르지 않습니다.")
        finished_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM kcs_rematch_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not run:
                raise KCSRematchNotFoundError("KCS 재매칭 작업을 찾을 수 없습니다.")
            if run["status"] != "running":
                raise KCSRematchConflictError("실행 중인 재매칭 작업만 적용할 수 있습니다.")
            if expected_state_sha256 != run["expected_state_sha256"]:
                raise KCSRematchConflictError("재매칭 준비 상태 식별값이 일치하지 않습니다.")
            if matcher_signature is not None:
                signature_json = json.dumps(
                    matcher_signature,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if signature_json != run["matcher_signature_json"]:
                    raise KCSRematchConflictError("재매칭 실행 정보가 준비 시점과 다릅니다.")
            self._validate_rematch_target(connection, run)
            state_snapshot = self._rematch_state_snapshot(connection, run["project_id"])
            actual_state_sha256 = _structure_fingerprint(state_snapshot)
            if actual_state_sha256 != expected_state_sha256:
                raise KCSRematchConflictError(
                    "프로젝트가 재매칭 중 변경되었습니다. 최신 상태로 다시 시도해 주세요."
                )

            source_clauses = {
                clause["id"]: clause for clause in state_snapshot["clauses"]
            }
            results_by_clause: dict[str, list[dict[str, Any]]] = {}
            for clause in clauses:
                if not isinstance(clause, dict):
                    raise ValueError("재매칭 조항 형식이 올바르지 않습니다.")
                clause_id = str(clause.get("clause_id") or clause.get("id") or "").strip()
                if not clause_id or clause_id in results_by_clause:
                    raise ValueError("재매칭 결과에 조항이 없거나 중복되었습니다.")
                candidates = clause.get("candidates")
                if not isinstance(candidates, list) or len(candidates) > 3:
                    raise ValueError("조항별 재매칭 후보는 최대 3개여야 합니다.")
                results_by_clause[clause_id] = candidates
            if set(results_by_clause) != set(source_clauses):
                raise ValueError("프로젝트 전체 조항의 재매칭 결과가 필요합니다.")

            plans: dict[str, dict[str, Any]] = {}
            stored_candidates: dict[str, list[dict[str, Any]]] = {}
            for clause_id, source_clause in source_clauses.items():
                if (
                    source_clause["source_type"] not in {"paragraph", "table"}
                    and results_by_clause[clause_id]
                ):
                    raise ValueError("목차·제목 조항에는 KCS 후보를 저장할 수 없습니다.")
                plan = plan_clause_impact(
                    source_clause["candidates"],
                    results_by_clause[clause_id],
                    source_clause.get("decision"),
                    source_clause.get("selected_candidate_id"),
                    clause_id=clause_id,
                )
                candidates = [
                    self._rematch_candidate_row(candidate, clause_id)
                    for candidate in plan["after_candidates"]
                ]
                ranks = [candidate["rank"] for candidate in candidates]
                if len(ranks) != len(set(ranks)):
                    raise ValueError("한 조항 안에서 재매칭 후보 순위가 중복되었습니다.")
                plans[clause_id] = plan
                stored_candidates[clause_id] = candidates

            connection.execute(
                """
                UPDATE candidates
                SET rank = -1000000 - rank
                WHERE clause_id IN (
                    SELECT id FROM clauses WHERE project_id = ?
                )
                """,
                (run["project_id"],),
            )
            for clause_id, candidates in stored_candidates.items():
                retained_ids = [candidate["id"] for candidate in candidates]
                if retained_ids:
                    placeholders = ",".join("?" for _ in retained_ids)
                    connection.execute(
                        f"DELETE FROM candidates WHERE clause_id = ? AND id NOT IN ({placeholders})",
                        (clause_id, *retained_ids),
                    )
                else:
                    connection.execute(
                        "DELETE FROM candidates WHERE clause_id = ?",
                        (clause_id,),
                    )
                for candidate in candidates:
                    existing = connection.execute(
                        "SELECT 1 FROM candidates WHERE id = ? AND clause_id = ?",
                        (candidate["id"], clause_id),
                    ).fetchone()
                    values = (
                        candidate["rank"],
                        candidate["kcs_code"],
                        candidate["document_name"],
                        candidate["version"],
                        candidate["update_date"],
                        candidate["kcs_clause"],
                        candidate["title"],
                        candidate["content"],
                        candidate["score"],
                        candidate["classification"],
                        json.dumps(candidate["reasons"], ensure_ascii=False),
                        json.dumps(candidate["warnings"], ensure_ascii=False),
                    )
                    if existing:
                        connection.execute(
                            """
                            UPDATE candidates
                            SET rank = ?, kcs_code = ?, document_name = ?,
                                version = ?, update_date = ?, kcs_clause = ?,
                                title = ?, content = ?, score = ?, classification = ?,
                                reasons_json = ?, warnings_json = ?
                            WHERE id = ? AND clause_id = ?
                            """,
                            (*values, candidate["id"], clause_id),
                        )
                    else:
                        connection.execute(
                            """
                            INSERT INTO candidates(
                                id, clause_id, rank, kcs_code, document_name,
                                version, update_date, kcs_clause, title, content,
                                score, classification, reasons_json, warnings_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                candidate["id"],
                                clause_id,
                                candidate["rank"],
                                candidate["kcs_code"],
                                candidate["document_name"],
                                candidate["version"],
                                candidate["update_date"],
                                candidate["kcs_clause"],
                                candidate["title"],
                                candidate["content"],
                                candidate["score"],
                                candidate["classification"],
                                json.dumps(candidate["reasons"], ensure_ascii=False),
                                json.dumps(candidate["warnings"], ensure_ascii=False),
                            ),
                        )

            material_change_count = 0
            review_required_count = 0
            connection.execute("DELETE FROM kcs_clause_impacts WHERE run_id = ?", (run_id,))
            for clause_id, plan in plans.items():
                source_clause = source_clauses[clause_id]
                if plan["material_changed"]:
                    material_change_count += 1
                    connection.execute(
                        "DELETE FROM clause_coverage_analysis WHERE clause_id = ?",
                        (clause_id,),
                    )
                    connection.execute(
                        """
                        UPDATE clauses
                        SET selected_candidate_id = ?, coverage_confirmed = 0
                        WHERE id = ?
                        """,
                        (plan["selected_candidate_id"], clause_id),
                    )
                else:
                    connection.execute(
                        "UPDATE clauses SET selected_candidate_id = ? WHERE id = ?",
                        (plan["selected_candidate_id"], clause_id),
                    )
                    coverage = source_clause.get("coverage_analysis")
                    if coverage:
                        visible_ids = []
                        for candidate in sorted(
                            stored_candidates[clause_id],
                            key=lambda item: item["rank"],
                        ):
                            ai = connection.execute(
                                """
                                SELECT relation_type, confidence
                                FROM candidate_ai_analysis
                                WHERE candidate_id = ?
                                """,
                                (candidate["id"],),
                            ).fetchone()
                            if (
                                ai
                                and ai["relation_type"] == "unrelated"
                                and float(ai["confidence"] or 0) >= 0.85
                            ):
                                continue
                            visible_ids.append(candidate["id"])
                            if len(visible_ids) == 3:
                                break
                        connection.execute(
                            """
                            UPDATE clause_coverage_analysis
                            SET candidate_ids_json = ?
                            WHERE clause_id = ?
                            """,
                            (json.dumps(visible_ids, ensure_ascii=False), clause_id),
                        )

                if not (plan["material_changed"] or plan["metadata_changed"]):
                    continue
                review_required = bool(plan["review_required"])
                review_required_count += int(review_required)
                before_decision = dict(source_clause["decision_snapshot"])
                before_decision["coverage_analysis"] = source_clause.get(
                    "coverage_analysis"
                )
                connection.execute(
                    """
                    INSERT INTO kcs_clause_impacts(
                        run_id, clause_id, impact_type, review_required,
                        reason_json, before_candidates_json, after_candidates_json,
                        before_decision_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        clause_id,
                        "material_change" if plan["material_changed"] else "metadata_change",
                        int(review_required),
                        json.dumps(plan["reasons"], ensure_ascii=False),
                        json.dumps(plan["before_candidates"], ensure_ascii=False),
                        json.dumps(plan["after_candidates"], ensure_ascii=False),
                        json.dumps(before_decision, ensure_ascii=False),
                    ),
                )

            matched_count = sum(
                source_clauses[clause_id]["source_type"] in {"paragraph", "table"}
                for clause_id in results_by_clause
            )
            connection.execute(
                """
                UPDATE projects
                SET kcs_revision = ?, kcs_snapshot = ?
                WHERE id = ?
                """,
                (run["target_revision"], run["target_snapshot"], run["project_id"]),
            )
            connection.execute(
                """
                UPDATE kcs_rematch_runs
                SET status = 'completed', matched_count = ?,
                    material_change_count = ?, review_required_count = ?,
                    error = '', finished_at = ?
                WHERE id = ?
                """,
                (
                    matched_count,
                    material_change_count,
                    review_required_count,
                    finished_at,
                    run_id,
                ),
            )
        result = self.get_kcs_rematch_run(run_id)
        if not result:
            raise KCSRematchNotFoundError("완료된 KCS 재매칭 작업을 찾을 수 없습니다.")
        return result

    @staticmethod
    def _invalidate_coverage_analysis(
        connection: sqlite3.Connection,
        clause_id: str,
    ) -> sqlite3.Row | None:
        """Keep prior evidence visible, but require a fresh combined analysis."""
        clause = connection.execute(
            "SELECT * FROM clauses WHERE id = ?",
            (clause_id,),
        ).fetchone()
        invalidated = connection.execute(
            """
            UPDATE clause_coverage_analysis
            SET coverage_status = 'uncertain',
                confidence = 0,
                deletion_safe = 0,
                rationale = ?,
                analyzed_at = ?
            WHERE clause_id = ?
            """,
            (
                "KCS 후보 분석이 변경되어 기존 전체포괄 결과가 무효화되었습니다. "
                "최신 후보로 전체포괄 분석을 다시 실행해 주세요.",
                datetime.now(timezone.utc).isoformat(),
                clause_id,
            ),
        )
        confirmation_reset = bool(
            invalidated.rowcount
            and clause
            and clause["decision"] == "delete"
            and clause["coverage_confirmed"]
        )
        if confirmation_reset:
            connection.execute(
                """
                UPDATE clauses
                SET coverage_confirmed = 0
                WHERE id = ? AND decision = 'delete'
                """,
                (clause_id,),
            )
        return clause if confirmation_reset else None

    @staticmethod
    def _record_coverage_confirmation_reset(
        connection: sqlite3.Connection,
        clause: sqlite3.Row | None,
        changed_at: str,
    ) -> None:
        if not clause:
            return
        connection.execute(
            """
            INSERT INTO decision_history(
                clause_id, previous_decision, new_decision, edited_content,
                review_note, decision_reason, coverage_confirmed,
                selected_candidate_id, coverage_analysis_json, changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                clause["id"],
                clause["decision"],
                clause["decision"],
                clause["edited_content"],
                clause["review_note"],
                clause["decision_reason"],
                clause["selected_candidate_id"],
                Store._coverage_analysis_snapshot(connection, clause["id"]),
                changed_at,
            ),
        )

    @classmethod
    def _latest_kcs_rematch_for_project(
        cls,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT r.*,
                   (SELECT COUNT(*) FROM kcs_clause_impacts i
                    WHERE i.run_id = r.id AND i.review_required = 1
                      AND i.acknowledged_at IS NULL) AS unacknowledged_count
            FROM kcs_rematch_runs r
            WHERE r.project_id = ?
            ORDER BY r.queued_at DESC, r.id DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        return cls._public_kcs_rematch_run(row) if row else None

    @classmethod
    def _attach_clause_kcs_impacts(
        cls,
        connection: sqlite3.Connection,
        clauses: list[dict[str, Any]],
        *,
        full: bool,
    ) -> None:
        clause_ids = [clause["id"] for clause in clauses]
        if not clause_ids:
            return
        placeholders = ",".join("?" for _ in clause_ids)
        rows = connection.execute(
            f"""
            SELECT i.*, r.target_revision, r.status AS run_status,
                   r.queued_at, r.finished_at
            FROM kcs_clause_impacts i
            JOIN kcs_rematch_runs r ON r.id = i.run_id
            WHERE i.clause_id IN ({placeholders})
            ORDER BY i.clause_id, r.queued_at DESC, r.id DESC
            """,
            tuple(clause_ids),
        ).fetchall()
        latest_by_clause: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["clause_id"] not in latest_by_clause:
                latest_by_clause[row["clause_id"]] = cls._public_kcs_impact(
                    row,
                    full=full,
                )
        for clause in clauses:
            impact = latest_by_clause.get(clause["id"])
            if impact:
                clause["kcs_impact"] = impact

    @staticmethod
    def _review_snapshot(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> dict[str, Any]:
        project = connection.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not project:
            raise ReviewWorkflowNotFoundError("검토 프로젝트를 찾을 수 없습니다.")
        clause_rows = connection.execute(
            """
            SELECT * FROM clauses
            WHERE project_id = ?
            ORDER BY source_order, id
            """,
            (project_id,),
        ).fetchall()
        clause_ids = [str(row["id"]) for row in clause_rows]
        candidates_by_clause: dict[str, list[dict[str, Any]]] = {
            clause_id: [] for clause_id in clause_ids
        }
        coverage_by_clause: dict[str, dict[str, Any]] = {}
        if clause_ids:
            placeholders = ",".join("?" for _ in clause_ids)
            candidate_rows = connection.execute(
                f"""
                SELECT k.*,
                       ai.relation_type AS ai_relation_type,
                       ai.confidence AS ai_confidence,
                       ai.rationale AS ai_rationale,
                       ai.simplified_content AS ai_simplified_content,
                       ai.model AS ai_model,
                       ai.analyzed_at AS ai_analyzed_at
                FROM candidates k
                LEFT JOIN candidate_ai_analysis ai ON ai.candidate_id = k.id
                WHERE k.clause_id IN ({placeholders})
                  AND NOT EXISTS(
                      SELECT 1 FROM candidate_ai_analysis excluded
                      WHERE excluded.candidate_id = k.id
                        AND excluded.relation_type = 'unrelated'
                        AND excluded.confidence >= 0.85
                  )
                ORDER BY k.clause_id, k.rank, k.id
                """,
                tuple(clause_ids),
            ).fetchall()
            for row in candidate_rows:
                candidate_items = candidates_by_clause[str(row["clause_id"])]
                if len(candidate_items) < 3:
                    candidate_items.append(dict(row))
            coverage_rows = connection.execute(
                f"""
                SELECT * FROM clause_coverage_analysis
                WHERE clause_id IN ({placeholders})
                """,
                tuple(clause_ids),
            ).fetchall()
            coverage_by_clause = {
                str(row["clause_id"]): dict(row) for row in coverage_rows
            }

        project_fields = (
            "id", "title", "source_filename", "source_sha256", "uploaded_at",
            "kcs_snapshot", "kcs_revision", "kcs_scope", "parser_version", "warning",
        )
        clause_fields = (
            "id", "source_order", "label", "title", "content", "source_type",
            "outline_level", "match_context", "decision", "edited_content",
            "review_note", "decision_reason", "coverage_confirmed",
            "selected_candidate_id", "reviewed_at",
        )
        clauses: list[dict[str, Any]] = []
        for row in clause_rows:
            clause_id = str(row["id"])
            clause = {field: row[field] for field in clause_fields}
            clause["coverage_confirmed"] = bool(clause["coverage_confirmed"])
            clause["candidates"] = candidates_by_clause.get(clause_id, [])
            clause["coverage_analysis"] = coverage_by_clause.get(clause_id)
            clauses.append(clause)
        return {
            "snapshot_schema_version": 1,
            "project": {field: project[field] for field in project_fields},
            "clauses": clauses,
        }

    @staticmethod
    def _canonical_review_snapshot(snapshot: dict[str, Any]) -> tuple[str, str]:
        raw = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _public_review_submission(
        row: sqlite3.Row | dict[str, Any],
    ) -> dict[str, Any]:
        raw = dict(row)
        return {
            key: raw.get(key)
            for key in (
                "id", "project_id", "revision_no", "status", "author_name",
                "author_note", "submitted_at", "decided_by", "decision_note",
                "decided_at", "kcs_snapshot", "kcs_revision",
                "snapshot_schema_version", "review_snapshot_sha256",
            )
        }

    @classmethod
    def _latest_review_submission(
        cls,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT * FROM project_review_submissions
            WHERE project_id = ?
            ORDER BY revision_no DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        return cls._public_review_submission(row) if row else None

    @staticmethod
    def _assert_review_editable(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> sqlite3.Row:
        project = connection.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not project:
            raise ReviewWorkflowNotFoundError("검토 프로젝트를 찾을 수 없습니다.")
        if str(project["status"] or "reviewing") not in REVIEW_EDITABLE_STATUSES:
            raise ReviewWorkflowConflictError(
                "승인 요청 중이거나 승인 완료된 프로젝트는 검토 내용을 변경할 수 없습니다."
            )
        return project

    @staticmethod
    def _supersede_locked_review(
        connection: sqlite3.Connection,
        project_id: str,
        reason: str,
        occurred_at: str,
    ) -> bool:
        project = connection.execute(
            "SELECT status FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not project or project["status"] not in {"submitted", "approved"}:
            return False
        submission = connection.execute(
            """
            SELECT * FROM project_review_submissions
            WHERE project_id = ?
            ORDER BY revision_no DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if not submission:
            connection.execute(
                "UPDATE projects SET status = 'changes_requested' WHERE id = ?",
                (project_id,),
            )
            return True
        connection.execute(
            "UPDATE project_review_submissions SET status = 'superseded' WHERE id = ?",
            (submission["id"],),
        )
        connection.execute(
            "UPDATE projects SET status = 'changes_requested' WHERE id = ?",
            (project_id,),
        )
        connection.execute(
            """
            INSERT INTO project_review_events(
                submission_id, event_type, from_status, to_status,
                actor_name, note, occurred_at
            ) VALUES (?, 'superseded', ?, 'changes_requested', '시스템', ?, ?)
            """,
            (submission["id"], project["status"], reason, occurred_at),
        )
        return True

    @staticmethod
    def _finish_project_summary(
        project: dict[str, Any],
        *,
        reviewable: int,
        unreviewed: int,
        holds: int,
        invalid_decisions: int,
        unsafe_deletes: int,
        current_revision: str,
        latest_rematch: dict[str, Any] | None,
        unacknowledged_impacts: int,
        latest_submission: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project_revision = str(project.get("kcs_revision") or "")
        kcs_stale = not current_revision or not project_revision or project_revision != current_revision
        requires_source_reupload = (
            str(project.get("parser_version") or "") != CURRENT_PARSER_VERSION
        )
        technical_blockers: list[str] = []
        if requires_source_reupload:
            technical_blockers.append("원본 시방서 재업로드 필요")
        if (
            kcs_stale
            and not requires_source_reupload
            and not (
                latest_rematch
                and latest_rematch["target_revision"] == current_revision
                and latest_rematch["status"] in {"pending", "running", "failed"}
            )
        ):
            technical_blockers.append("최신 KCS 자동 재매칭 필요")
        if (
            latest_rematch
            and latest_rematch["target_revision"] == current_revision
            and latest_rematch["status"] in {"pending", "running", "failed"}
        ):
            status_label = {
                "pending": "대기 중",
                "running": "진행 중",
                "failed": "실패",
            }[latest_rematch["status"]]
            technical_blockers.append(f"최신 KCS 재매칭 {status_label}")
        if unacknowledged_impacts:
            technical_blockers.append(f"KCS 변경 영향 재검토 {unacknowledged_impacts}개")
        if unreviewed:
            technical_blockers.append(f"미검토 조항 {unreviewed}개")
        if invalid_decisions:
            technical_blockers.append(f"판정 사유 미완료 {invalid_decisions}개")
        if holds:
            technical_blockers.append(f"보류 조항 {holds}개")
        if unsafe_deletes:
            technical_blockers.append(f"삭제 근거 미충족 {unsafe_deletes}개")
        workflow_status = str(project.get("status") or "reviewing")
        submission_blockers = list(technical_blockers)
        if workflow_status == "submitted":
            submission_blockers.append("이미 승인 요청 중")
        elif workflow_status == "approved":
            submission_blockers.append("이미 승인 완료")
        elif workflow_status not in REVIEW_EDITABLE_STATUSES:
            submission_blockers.append("프로젝트 승인 상태 확인 필요")

        final_blockers = list(technical_blockers)
        if workflow_status == "reviewing":
            final_blockers.append("작성자 제출 및 승인 필요")
        elif workflow_status == "submitted":
            final_blockers.append("승인자 승인 대기 중")
        elif workflow_status == "changes_requested":
            final_blockers.append("반려사항 반영 후 재제출 필요")
        elif workflow_status != "approved":
            final_blockers.append("프로젝트 승인 상태 확인 필요")
        elif not latest_submission or latest_submission.get("status") != "approved":
            final_blockers.append("승인 기록 확인 필요")
        project.update(
            {
                "reviewable_clauses": reviewable,
                "unreviewed_clauses": unreviewed,
                "invalid_decision_count": invalid_decisions,
                "unsafe_delete_count": unsafe_deletes,
                "kcs_stale": kcs_stale,
                "requires_source_reupload": requires_source_reupload,
                "is_archived": bool(project.get("archived_at")),
                "kcs_rematch": latest_rematch,
                "unacknowledged_kcs_impact_count": unacknowledged_impacts,
                "reviewed_clauses": max(0, reviewable - unreviewed - invalid_decisions),
                "latest_review_submission": latest_submission,
                "_review_technical_blockers": technical_blockers,
                "review_locked": workflow_status in {"submitted", "approved"},
                "review_submission_ready": not submission_blockers,
                "review_submission_blockers": submission_blockers,
                "final_export_ready": not final_blockers,
                "final_export_blockers": final_blockers,
            }
        )
        return project

    @staticmethod
    def _add_export_readiness(
        connection: sqlite3.Connection,
        project: dict[str, Any],
    ) -> dict[str, Any]:
        counts = connection.execute(
            """
            SELECT
                SUM(CASE WHEN source_type != 'heading' THEN 1 ELSE 0 END) AS reviewable,
                SUM(CASE WHEN source_type != 'heading' AND decision IS NULL THEN 1 ELSE 0 END) AS unreviewed,
                SUM(CASE WHEN source_type != 'heading' AND decision = 'hold' THEN 1 ELSE 0 END) AS holds,
                SUM(CASE WHEN source_type != 'heading' AND (
                    (decision = 'keep' AND decision_reason NOT IN (
                        'posco_specific', 'posco_stricter', 'partial_overlap_residual', 'no_kcs_match'
                    ))
                    OR (decision = 'keep' AND decision_reason = 'partial_overlap_residual' AND (
                        TRIM(edited_content) = '' OR TRIM(edited_content) = TRIM(content)
                    ))
                    OR (decision = 'delete' AND (
                        decision_reason NOT IN (
                            'fully_covered_by_kcs', 'internal_duplicate',
                            'obsolete_requirement', 'out_of_scope',
                            'editorial_cleanup', 'management_decision'
                        )
                        OR (decision_reason = 'management_decision' AND TRIM(review_note) = '')
                    ))
                    OR (decision = 'hold' AND decision_reason NOT IN (
                        'needs_expert_review', 'candidate_uncertain', 'kcs_conflict'
                    ))
                ) THEN 1 ELSE 0 END) AS invalid_decisions
            FROM clauses
            WHERE project_id = ?
            """,
            (project["id"],),
        ).fetchone()
        unsafe_delete_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM clauses c
            WHERE c.project_id = ? AND c.source_type != 'heading' AND c.decision = 'delete'
              AND c.decision_reason = 'fully_covered_by_kcs'
              AND (
                  c.selected_candidate_id IS NULL
                  OR c.coverage_confirmed != 1
                  OR NOT EXISTS(
                      SELECT 1
                      FROM candidates k
                      WHERE k.id = c.selected_candidate_id AND k.clause_id = c.id
                        AND k.warnings_json = '[]'
                  )
                  OR EXISTS(
                      SELECT 1 FROM clause_coverage_analysis ca
                      WHERE ca.clause_id = c.id
                        AND (
                            ca.deletion_safe != 1
                            OR NOT EXISTS(
                                SELECT 1 FROM json_each(ca.evidence_candidate_ids_json) evidence
                                WHERE evidence.value = c.selected_candidate_id
                            )
                            OR EXISTS(
                                SELECT 1
                                FROM json_each(ca.evidence_candidate_ids_json) evidence
                                JOIN candidate_ai_analysis evidence_ai
                                  ON evidence_ai.candidate_id = evidence.value
                                WHERE evidence_ai.relation_type NOT IN ('equivalent', 'kcs_covers')
                                   OR evidence_ai.confidence < 0.85
                            )
                        )
                  )
                  OR EXISTS(
                      SELECT 1 FROM candidate_ai_analysis ai
                      WHERE ai.candidate_id = c.selected_candidate_id
                        AND (
                            ai.relation_type NOT IN ('equivalent', 'kcs_covers')
                            OR ai.confidence < 0.85
                        )
                  )
              )
            """,
            (project["id"],),
        ).fetchone()[0]

        reviewable = int(counts["reviewable"] or 0)
        unreviewed = int(counts["unreviewed"] or 0)
        holds = int(counts["holds"] or 0)
        invalid_decisions = int(counts["invalid_decisions"] or 0)
        unsafe_deletes = int(unsafe_delete_count or 0)
        current_revision_row = connection.execute(
            "SELECT value FROM app_metadata WHERE key = 'current_kcs_revision'"
        ).fetchone()
        current_revision = str(current_revision_row["value"] or "") if current_revision_row else ""
        project_revision = str(project.get("kcs_revision") or "")
        latest_rematch = Store._latest_kcs_rematch_for_project(
            connection,
            project["id"],
        )
        latest_submission = Store._latest_review_submission(
            connection,
            project["id"],
        )
        unacknowledged_impacts = 0
        if project_revision:
            unacknowledged_impacts = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM kcs_clause_impacts i
                    JOIN kcs_rematch_runs r ON r.id = i.run_id
                    WHERE r.project_id = ? AND r.status = 'completed'
                      AND r.target_revision = ?
                      AND i.review_required = 1 AND i.acknowledged_at IS NULL
                    """,
                    (project["id"], project_revision),
                ).fetchone()[0]
                or 0
            )
        return Store._finish_project_summary(
            project,
            reviewable=reviewable,
            unreviewed=unreviewed,
            holds=holds,
            invalid_decisions=invalid_decisions,
            unsafe_deletes=unsafe_deletes,
            current_revision=current_revision,
            latest_rematch=latest_rematch,
            unacknowledged_impacts=unacknowledged_impacts,
            latest_submission=latest_submission,
        )

    def create_project(self, project: dict[str, Any], clauses: list[dict[str, Any]]) -> None:
        project_revision = str(project.get("kcs_revision") or "").strip()
        if not project_revision:
            raise ValueError("프로젝트에 사용한 KCS 개정 식별값이 없습니다.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_revision_row = connection.execute(
                "SELECT value FROM app_metadata WHERE key = 'current_kcs_revision'"
            ).fetchone()
            if current_revision_row and current_revision_row["value"] != project_revision:
                raise ValueError(
                    "KCS 기준이 매칭 중 갱신되었습니다. 최신 기준으로 문서를 다시 업로드해 주세요."
                )
            if not current_revision_row:
                connection.executemany(
                    "INSERT INTO app_metadata(key, value) VALUES (?, ?)",
                    (
                        ("current_kcs_snapshot", project["kcs_snapshot"]),
                        ("current_kcs_revision", project_revision),
                    ),
                )
            connection.execute(
                """
                INSERT INTO projects(
                    id, title, source_filename, source_path, source_sha256,
                    uploaded_at, kcs_snapshot, kcs_revision, kcs_scope,
                    parser_version, status, warning
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project["id"],
                    project["title"],
                    project["source_filename"],
                    project["source_path"],
                    project["source_sha256"],
                    project["uploaded_at"],
                    project["kcs_snapshot"],
                    project_revision,
                    project["kcs_scope"],
                    str(project.get("parser_version") or CURRENT_PARSER_VERSION),
                    "reviewing",
                    project.get("warning", ""),
                ),
            )
            for clause in clauses:
                connection.execute(
                    """
                    INSERT INTO clauses(
                        id, project_id, source_order, label, title, content,
                        source_type, outline_level, match_context
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clause["id"],
                        project["id"],
                        clause["source_order"],
                        clause["label"],
                        clause["title"],
                        clause["content"],
                        clause["source_type"],
                        clause.get("outline_level"),
                        clause.get("match_context", ""),
                    ),
                )
                for candidate in clause.get("candidates", []):
                    connection.execute(
                        """
                        INSERT INTO candidates(
                            id, clause_id, rank, kcs_code, document_name, version,
                            update_date, kcs_clause, title, content, score,
                            classification, reasons_json, warnings_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            candidate["id"],
                            clause["id"],
                            candidate["rank"],
                            candidate["kcs_code"],
                            candidate["document_name"],
                            candidate["version"],
                            candidate["update_date"],
                            candidate["kcs_clause"],
                            candidate["title"],
                            candidate["content"],
                            candidate["score"],
                            candidate["classification"],
                            json.dumps(candidate.get("reasons", []), ensure_ascii=False),
                            json.dumps(candidate.get("warnings", []), ensure_ascii=False),
                        ),
                    )

    @staticmethod
    def _structure_snapshot(
        connection: sqlite3.Connection,
        clause_ids: list[str],
    ) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in clause_ids)
        clauses = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT * FROM clauses
                WHERE id IN ({placeholders})
                ORDER BY source_order
                """,
                tuple(clause_ids),
            ).fetchall()
        ]
        by_id = {
            clause["id"]: {
                "clause": clause,
                "candidates": [],
                "coverage_analysis": None,
            }
            for clause in clauses
        }
        if not by_id:
            return {"snapshot_version": 1, "clauses": []}

        candidate_rows = connection.execute(
            f"""
            SELECT k.*, ai.relation_type, ai.confidence,
                   ai.rationale AS ai_rationale,
                   ai.simplified_content AS ai_simplified_content,
                   ai.model AS ai_model, ai.analyzed_at AS ai_analyzed_at
            FROM candidates k
            LEFT JOIN candidate_ai_analysis ai ON ai.candidate_id = k.id
            WHERE k.clause_id IN ({placeholders})
            ORDER BY k.clause_id, k.rank
            """,
            tuple(clause_ids),
        ).fetchall()
        for row in candidate_rows:
            by_id[row["clause_id"]]["candidates"].append(dict(row))

        coverage_rows = connection.execute(
            f"""
            SELECT * FROM clause_coverage_analysis
            WHERE clause_id IN ({placeholders})
            """,
            tuple(clause_ids),
        ).fetchall()
        for row in coverage_rows:
            by_id[row["clause_id"]]["coverage_analysis"] = dict(row)

        return {
            "snapshot_version": 1,
            "clauses": [by_id[clause["id"]] for clause in clauses],
        }

    @staticmethod
    def _structure_project_guard(
        connection: sqlite3.Connection,
        project_id: str,
        expected_kcs_revision: str | None = None,
    ) -> sqlite3.Row:
        project = connection.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not project:
            raise ClauseStructureNotFoundError("검토 프로젝트를 찾을 수 없습니다.")
        if str(project["status"] or "reviewing") not in REVIEW_EDITABLE_STATUSES:
            raise ClauseStructureConflictError(
                "승인 요청 중이거나 승인 완료된 프로젝트는 조항 구조를 변경할 수 없습니다."
            )
        current_row = connection.execute(
            "SELECT value FROM app_metadata WHERE key = 'current_kcs_revision'"
        ).fetchone()
        current_revision = str(current_row["value"] or "") if current_row else ""
        project_revision = str(project["kcs_revision"] or "")
        if (
            not current_revision
            or not project_revision
            or project_revision != current_revision
            or (
                expected_kcs_revision is not None
                and expected_kcs_revision != current_revision
            )
        ):
            raise ClauseStructureConflictError(
                "KCS 기준이 갱신되었습니다. 최신 기준으로 문서를 다시 업로드해 주세요."
            )
        if connection.execute(
            "SELECT 1 FROM quality_evaluations WHERE project_id = ?",
            (project_id,),
        ).fetchone():
            raise ClauseStructureConflictError(
                "품질평가가 시작된 프로젝트는 표본과 지표 보호를 위해 조항 구조를 변경할 수 없습니다."
            )
        return project

    @staticmethod
    def _structure_source_rows(
        connection: sqlite3.Connection,
        project_id: str,
        clause_ids: list[str],
        operation: str,
    ) -> list[sqlite3.Row]:
        if len(set(clause_ids)) != len(clause_ids):
            raise ValueError("같은 조항을 중복해서 선택할 수 없습니다.")
        expected_count = 2 if operation == "merge" else 1
        if len(clause_ids) != expected_count:
            message = "병합할 조항은 정확히 2개를 선택하세요." if operation == "merge" else "분할할 조항은 1개여야 합니다."
            raise ValueError(message)
        placeholders = ",".join("?" for _ in clause_ids)
        rows = connection.execute(
            f"""
            SELECT * FROM clauses
            WHERE project_id = ? AND id IN ({placeholders})
            ORDER BY source_order
            """,
            (project_id, *clause_ids),
        ).fetchall()
        if len(rows) != expected_count:
            raise ClauseStructureNotFoundError("선택한 조항을 찾을 수 없습니다.")
        if any(row["source_type"] != "paragraph" for row in rows):
            raise ValueError("일반 문단만 병합하거나 분할할 수 있습니다.")
        if operation == "merge" and rows[1]["source_order"] != rows[0]["source_order"] + 1:
            raise ValueError("서로 바로 이어진 두 문단만 병합할 수 있습니다.")
        if any(
            row["decision"] is not None
            or row["selected_candidate_id"]
            or row["decision_reason"]
            or row["coverage_confirmed"]
            or row["reviewed_at"]
            for row in rows
        ):
            raise ClauseStructureConflictError(
                "이미 판정한 조항은 구조를 변경할 수 없습니다. 판정 전에 문단 경계를 조정해 주세요."
            )
        if connection.execute(
            f"SELECT 1 FROM decision_history WHERE clause_id IN ({placeholders}) LIMIT 1",
            tuple(row["id"] for row in rows),
        ).fetchone():
            raise ClauseStructureConflictError(
                "판정 이력이 있는 조항은 감사 기록 보호를 위해 구조를 변경할 수 없습니다."
            )
        return rows

    @staticmethod
    def _merge_replacement(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        first_text = str(rows[0]["content"] or "").strip()
        second_label = str(rows[1]["label"] or "").strip()
        second_title = str(rows[1]["title"] or "").strip()
        second_content = str(rows[1]["content"] or "").strip()
        second_header = " ".join(part for part in (second_label, second_title) if part)
        second_text = "\n".join(
            part
            for part in (
                second_header,
                second_content if second_content and second_content != second_title else "",
            )
            if part
        )
        content = "\n".join(part for part in (first_text, second_text) if part)
        return [
            {
                "label": rows[0]["label"],
                "title": rows[0]["title"] or _structure_title(content),
                "content": content,
                "source_type": "paragraph",
                "outline_level": rows[0]["outline_level"],
                "match_context": rows[0]["match_context"],
            }
        ]

    @staticmethod
    def _split_replacements(
        row: sqlite3.Row,
        parts: list[str],
    ) -> list[dict[str, Any]]:
        if not str(row["content"] or "").strip():
            raise ValueError("본문이 있는 일반 문단만 나눌 수 있습니다.")
        if len(parts) != 2 or any(not isinstance(part, str) for part in parts):
            raise ValueError("문단은 정확히 두 부분으로 나누어야 합니다.")
        if any(not part.strip() for part in parts):
            raise ValueError("분할한 두 문단은 모두 내용이 있어야 합니다.")
        original = _clause_source_text(row)
        if "".join(parts) != original:
            raise ValueError("분할 과정에서 포스코 원문을 추가, 삭제 또는 변경할 수 없습니다.")
        return [
            {
                "label": row["label"] if index == 0 else f"{row['label']} (분할 2)",
                "title": (
                    row["title"]
                    if index == 0
                    else (
                        f"{str(row['title']).strip()} (계속)"
                        if str(row["title"] or "").strip()
                        else _structure_title(part)
                    )
                ),
                "content": part,
                "source_type": "paragraph",
                "outline_level": row["outline_level"],
                "match_context": (
                    row["match_context"]
                    if index == 0
                    else " ".join(
                        part
                        for part in (
                            str(row["match_context"] or "").strip(),
                            str(row["label"] or "").strip(),
                            str(row["title"] or "").strip(),
                        )
                        if part
                    )
                ),
            }
            for index, part in enumerate(parts)
        ]

    def prepare_clause_merge(
        self,
        project_id: str,
        clause_ids: list[str],
    ) -> dict[str, Any]:
        with self.connect() as connection:
            project = self._structure_project_guard(connection, project_id)
            rows = self._structure_source_rows(connection, project_id, clause_ids, "merge")
            snapshot = self._structure_snapshot(connection, [row["id"] for row in rows])
            return {
                "operation": "merge",
                "project_id": project_id,
                "source_clause_ids": [row["id"] for row in rows],
                "expected_fingerprint": _structure_fingerprint(snapshot),
                "expected_kcs_revision": project["kcs_revision"],
                "replacements": self._merge_replacement(rows),
            }

    def prepare_clause_split(
        self,
        project_id: str,
        clause_id: str,
        parts: list[str],
    ) -> dict[str, Any]:
        with self.connect() as connection:
            project = self._structure_project_guard(connection, project_id)
            rows = self._structure_source_rows(connection, project_id, [clause_id], "split")
            replacements = self._split_replacements(rows[0], parts)
            snapshot = self._structure_snapshot(connection, [clause_id])
            return {
                "operation": "split",
                "project_id": project_id,
                "source_clause_ids": [clause_id],
                "expected_fingerprint": _structure_fingerprint(snapshot),
                "expected_kcs_revision": project["kcs_revision"],
                "replacements": replacements,
            }

    @staticmethod
    def _validate_replacement_candidates(candidates: Any) -> list[dict[str, Any]]:
        if not isinstance(candidates, list) or len(candidates) > 3:
            raise ValueError("재매칭 후보는 최대 3개여야 합니다.")
        if any(not isinstance(candidate, dict) for candidate in candidates):
            raise ValueError("재매칭 후보 형식이 올바르지 않습니다.")
        if [candidate.get("rank") for candidate in candidates] != list(
            range(1, len(candidates) + 1)
        ):
            raise ValueError("재매칭 후보 순위는 1부터 연속되어야 합니다.")
        required = {
            "id", "rank", "kcs_code", "document_name", "version", "update_date",
            "kcs_clause", "title", "content", "score", "classification",
        }
        if any(not required.issubset(candidate) for candidate in candidates):
            raise ValueError("재매칭 후보 형식이 올바르지 않습니다.")
        return candidates

    def apply_clause_structure_edit(
        self,
        project_id: str,
        operation: str,
        source_clause_ids: list[str],
        expected_fingerprint: str,
        expected_kcs_revision: str,
        replacement_clauses: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if operation not in {"merge", "split"}:
            raise ValueError("지원하지 않는 조항 구조 변경입니다.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._structure_project_guard(
                connection,
                project_id,
                expected_kcs_revision,
            )
            rows = self._structure_source_rows(
                connection,
                project_id,
                source_clause_ids,
                operation,
            )
            ordered_ids = [row["id"] for row in rows]
            before = self._structure_snapshot(connection, ordered_ids)
            if _structure_fingerprint(before) != expected_fingerprint:
                raise ClauseStructureConflictError(
                    "조항이 준비 이후 변경되었습니다. 최신 내용을 불러와 다시 시도해 주세요."
                )

            expected_count = 1 if operation == "merge" else 2
            if len(replacement_clauses) != expected_count:
                raise ValueError("구조 변경 결과 문단 수가 올바르지 않습니다.")
            if operation == "merge":
                expected_replacements = self._merge_replacement(rows)
            else:
                split_parts = [
                    replacement.get("content")
                    for replacement in replacement_clauses
                    if isinstance(replacement, dict)
                ]
                expected_replacements = self._split_replacements(rows[0], split_parts)

            structural_fields = (
                "label", "title", "content", "source_type", "outline_level", "match_context",
            )
            replacement_ids: list[str] = []
            all_candidate_ids: list[str] = []
            for replacement, expected in zip(replacement_clauses, expected_replacements):
                if not isinstance(replacement, dict) or any(
                    replacement.get(field) != expected[field] for field in structural_fields
                ):
                    raise ValueError("준비된 조항 구조와 재매칭 결과가 일치하지 않습니다.")
                replacement_id = str(replacement.get("id") or "")
                try:
                    uuid.UUID(replacement_id)
                except (ValueError, AttributeError) as exc:
                    raise ValueError("새 조항 ID는 유효한 UUID여야 합니다.") from exc
                if replacement_id in ordered_ids or replacement_id in replacement_ids:
                    raise ValueError("새 조항에는 기존과 다른 고유 ID가 필요합니다.")
                candidates = self._validate_replacement_candidates(
                    replacement.get("candidates", [])
                )
                replacement_ids.append(replacement_id)
                all_candidate_ids.extend(str(candidate["id"]) for candidate in candidates)

            if len(set(all_candidate_ids)) != len(all_candidate_ids):
                raise ValueError("재매칭 후보 ID가 중복되었습니다.")
            clause_placeholders = ",".join("?" for _ in replacement_ids)
            if connection.execute(
                f"SELECT 1 FROM clauses WHERE id IN ({clause_placeholders}) LIMIT 1",
                tuple(replacement_ids),
            ).fetchone():
                raise ValueError("새 조항 ID가 이미 사용 중입니다.")
            if all_candidate_ids:
                candidate_placeholders = ",".join("?" for _ in all_candidate_ids)
                if connection.execute(
                    f"SELECT 1 FROM candidates WHERE id IN ({candidate_placeholders}) LIMIT 1",
                    tuple(all_candidate_ids),
                ).fetchone():
                    raise ValueError("재매칭 후보 ID가 이미 사용 중입니다.")

            all_rows = connection.execute(
                "SELECT id, source_order FROM clauses WHERE project_id = ? ORDER BY source_order",
                (project_id,),
            ).fetchall()
            first_source_id = ordered_ids[0]
            source_set = set(ordered_ids)
            final_entries: list[str | dict[str, Any]] = []
            for row in all_rows:
                if row["id"] == first_source_id:
                    final_entries.extend(replacement_clauses)
                if row["id"] not in source_set:
                    final_entries.append(row["id"])

            order_offset = max((row["source_order"] for row in all_rows), default=0) + len(final_entries) + 1000
            connection.execute(
                "UPDATE clauses SET source_order = source_order + ? WHERE project_id = ?",
                (order_offset, project_id),
            )
            source_placeholders = ",".join("?" for _ in ordered_ids)
            connection.execute(
                f"DELETE FROM clauses WHERE project_id = ? AND id IN ({source_placeholders})",
                (project_id, *ordered_ids),
            )

            for source_order, entry in enumerate(final_entries, start=1):
                if isinstance(entry, str):
                    connection.execute(
                        "UPDATE clauses SET source_order = ? WHERE id = ? AND project_id = ?",
                        (source_order, entry, project_id),
                    )
                    continue
                connection.execute(
                    """
                    INSERT INTO clauses(
                        id, project_id, source_order, label, title, content,
                        source_type, outline_level, match_context
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry["id"], project_id, source_order, entry["label"],
                        entry["title"], entry["content"], entry["source_type"],
                        entry.get("outline_level"), entry.get("match_context", ""),
                    ),
                )
                for candidate in entry.get("candidates", []):
                    connection.execute(
                        """
                        INSERT INTO candidates(
                            id, clause_id, rank, kcs_code, document_name, version,
                            update_date, kcs_clause, title, content, score,
                            classification, reasons_json, warnings_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            candidate["id"], entry["id"], candidate["rank"],
                            candidate["kcs_code"], candidate["document_name"],
                            candidate["version"], candidate["update_date"],
                            candidate["kcs_clause"], candidate["title"],
                            candidate["content"], candidate["score"],
                            candidate["classification"],
                            json.dumps(candidate.get("reasons", []), ensure_ascii=False),
                            json.dumps(candidate.get("warnings", []), ensure_ascii=False),
                        ),
                    )

            after = self._structure_snapshot(connection, replacement_ids)
            changed_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT INTO clause_structure_history(
                    id, project_id, operation, source_clause_ids_json,
                    replacement_clause_ids_json, before_json, after_json, changed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), project_id, operation,
                    json.dumps(ordered_ids, ensure_ascii=False),
                    json.dumps(replacement_ids, ensure_ascii=False),
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                    changed_at,
                ),
            )

        return {
            "active_clause_id": replacement_ids[0],
            "clauses": self.list_clauses(project_id),
        }

    def list_clause_structure_history(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM clause_structure_history
                WHERE project_id = ?
                ORDER BY changed_at, id
                """,
                (project_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for source, destination in (
                ("source_clause_ids_json", "source_clause_ids"),
                ("replacement_clause_ids_json", "replacement_clause_ids"),
                ("before_json", "before"),
                ("after_json", "after"),
            ):
                item[destination] = json.loads(item.pop(source))
            result.append(item)
        return result

    @staticmethod
    def _public_upload_item(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item.pop("source_path", None)
        item.pop("source_sha256", None)
        item.pop("suffix", None)
        item["filename"] = item.pop("original_filename", "")
        item["progress"] = int(item.get("progress") or 0)
        item["position"] = int(item.get("position") or 0)
        item["source_size"] = int(item.get("source_size") or 0)
        item["retryable"] = bool(item.get("retryable"))
        return item

    @classmethod
    def _upload_job_from_connection(
        cls,
        connection: sqlite3.Connection,
        job_id: str,
    ) -> dict[str, Any] | None:
        job_row = connection.execute(
            "SELECT * FROM upload_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if not job_row:
            return None
        item_rows = connection.execute(
            """
            SELECT * FROM upload_job_items
            WHERE job_id = ?
            ORDER BY position, id
            """,
            (job_id,),
        ).fetchall()
        items = [cls._public_upload_item(row) for row in item_rows]
        total = len(items)
        completed = sum(item["status"] == "completed" for item in items)
        failed = sum(item["status"] == "failed" for item in items)
        running = sum(item["status"] == "running" for item in items)
        queued = sum(item["status"] == "queued" for item in items)
        if total and failed == total:
            status = "failed"
        elif total and completed + failed == total:
            status = "completed"
        elif running or completed or failed or job_row["started_at"]:
            status = "running"
        else:
            status = "queued"
        progress = round(
            sum(int(item.get("progress") or 0) for item in items) / total
        ) if total else 0
        errors = [str(item.get("error") or "") for item in items if item.get("error")]
        return {
            "id": job_row["id"],
            "status": status,
            "total": total,
            "queued": queued,
            "running": running,
            "completed": completed,
            "failed": failed,
            "progress": progress,
            "error": errors[0] if errors else "",
            "kcs_snapshot": job_row["kcs_snapshot"],
            "kcs_revision": job_row["kcs_revision"],
            "created_at": job_row["created_at"],
            "started_at": job_row["started_at"],
            "finished_at": job_row["finished_at"],
            "items": items,
        }

    def create_upload_job(
        self,
        job: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not items:
            raise ValueError("업로드할 파일이 없습니다.")
        if not str(job.get("kcs_snapshot") or "") or not str(
            job.get("kcs_revision") or ""
        ):
            raise ValueError("업로드 작업의 KCS 개정 정보가 없습니다.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO upload_jobs(
                    id, kcs_snapshot, kcs_revision, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    job["id"],
                    job["kcs_snapshot"],
                    job["kcs_revision"],
                    job["created_at"],
                ),
            )
            for item in items:
                connection.execute(
                    """
                    INSERT INTO upload_job_items(
                        id, job_id, position, original_filename, source_path,
                        source_sha256, source_size, suffix, project_id, queued_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        job["id"],
                        item["position"],
                        item["filename"],
                        item["source_path"],
                        item["source_sha256"],
                        item["source_size"],
                        item["suffix"],
                        item["project_id"],
                        item.get("queued_at") or job["created_at"],
                    ),
                )
            created = self._upload_job_from_connection(connection, job["id"])
            if not created:
                raise RuntimeError("업로드 작업을 저장하지 못했습니다.")
            return created

    def get_upload_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._upload_job_from_connection(connection, job_id)

    def list_upload_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM upload_jobs
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                job
                for row in rows
                if (job := self._upload_job_from_connection(connection, row["id"]))
            ]

    def pending_upload_item_ids(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM upload_job_items
                WHERE status = 'queued'
                ORDER BY queued_at, position, id
                """
            ).fetchall()
            return [str(row["id"]) for row in rows]

    def active_upload_item_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM upload_job_items
                WHERE status IN ('queued', 'running')
                """
            ).fetchone()
            return int(row["count"] or 0)

    def claim_upload_item(self, item_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT i.*, j.kcs_snapshot, j.kcs_revision
                FROM upload_job_items i
                JOIN upload_jobs j ON j.id = i.job_id
                WHERE i.id = ?
                """,
                (item_id,),
            ).fetchone()
            if not row:
                raise UploadJobNotFoundError("업로드 작업 파일을 찾을 수 없습니다.")
            if row["status"] != "queued":
                raise UploadJobConflictError("대기 중인 업로드 파일만 처리할 수 있습니다.")
            connection.execute(
                """
                UPDATE upload_job_items
                SET status = 'running', progress = 5, phase = 'preparing',
                    error = '', retryable = 0, started_at = ?, finished_at = NULL
                WHERE id = ? AND status = 'queued'
                """,
                (now, item_id),
            )
            connection.execute(
                """
                UPDATE upload_jobs
                SET started_at = COALESCE(started_at, ?), finished_at = NULL
                WHERE id = ?
                """,
                (now, row["job_id"]),
            )
            claimed = connection.execute(
                """
                SELECT i.*, j.kcs_snapshot, j.kcs_revision
                FROM upload_job_items i
                JOIN upload_jobs j ON j.id = i.job_id
                WHERE i.id = ?
                """,
                (item_id,),
            ).fetchone()
            return dict(claimed)

    def update_upload_item_progress(
        self,
        item_id: str,
        progress: int,
        phase: str,
    ) -> None:
        progress = int(progress)
        if progress < 0 or progress > 99:
            raise ValueError("진행률은 완료 전 0~99 범위여야 합니다.")
        phase = str(phase or "").strip()
        if not phase:
            raise ValueError("업로드 처리 단계가 필요합니다.")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE upload_job_items
                SET progress = ?, phase = ?
                WHERE id = ? AND status = 'running'
                """,
                (progress, phase, item_id),
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM upload_job_items WHERE id = ?",
                    (item_id,),
                ).fetchone()
                if not exists:
                    raise UploadJobNotFoundError("업로드 작업 파일을 찾을 수 없습니다.")
                raise UploadJobConflictError("실행 중인 업로드 파일만 진행률을 변경할 수 있습니다.")

    @staticmethod
    def _finish_upload_job_if_terminal(
        connection: sqlite3.Connection,
        job_id: str,
        finished_at: str,
    ) -> None:
        active = connection.execute(
            """
            SELECT 1 FROM upload_job_items
            WHERE job_id = ? AND status IN ('queued', 'running')
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if not active:
            connection.execute(
                "UPDATE upload_jobs SET finished_at = ? WHERE id = ?",
                (finished_at, job_id),
            )
        else:
            connection.execute(
                "UPDATE upload_jobs SET finished_at = NULL WHERE id = ?",
                (job_id,),
            )

    def complete_upload_item(self, item_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT * FROM upload_job_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if not item:
                raise UploadJobNotFoundError("업로드 작업 파일을 찾을 수 없습니다.")
            if item["status"] == "completed":
                job = self._upload_job_from_connection(connection, item["job_id"])
                if not job:
                    raise UploadJobNotFoundError("업로드 작업을 찾을 수 없습니다.")
                return job
            if item["status"] != "running":
                raise UploadJobConflictError("실행 중인 업로드 파일만 완료할 수 있습니다.")
            project = connection.execute(
                "SELECT 1 FROM projects WHERE id = ?",
                (item["project_id"],),
            ).fetchone()
            if not project:
                raise UploadJobConflictError("완료할 검토 프로젝트가 저장되지 않았습니다.")
            connection.execute(
                """
                UPDATE upload_job_items
                SET status = 'completed', progress = 100, phase = 'completed',
                    error = '', retryable = 0, finished_at = ?
                WHERE id = ?
                """,
                (now, item_id),
            )
            self._finish_upload_job_if_terminal(connection, item["job_id"], now)
            job = self._upload_job_from_connection(connection, item["job_id"])
            if not job:
                raise UploadJobNotFoundError("업로드 작업을 찾을 수 없습니다.")
            return job

    def fail_upload_item(
        self,
        item_id: str,
        error: str,
        *,
        retryable: bool = False,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT * FROM upload_job_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if not item:
                raise UploadJobNotFoundError("업로드 작업 파일을 찾을 수 없습니다.")
            if item["status"] == "failed":
                job = self._upload_job_from_connection(connection, item["job_id"])
                if not job:
                    raise UploadJobNotFoundError("업로드 작업을 찾을 수 없습니다.")
                return job
            if item["status"] != "running":
                raise UploadJobConflictError("실행 중인 업로드 파일만 실패로 종료할 수 있습니다.")
            connection.execute(
                """
                UPDATE upload_job_items
                SET status = 'failed', phase = 'failed', error = ?,
                    retryable = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    (str(error or "파일 처리 중 오류가 발생했습니다.").strip())[:2000],
                    int(bool(retryable)),
                    now,
                    item_id,
                ),
            )
            self._finish_upload_job_if_terminal(connection, item["job_id"], now)
            job = self._upload_job_from_connection(connection, item["job_id"])
            if not job:
                raise UploadJobNotFoundError("업로드 작업을 찾을 수 없습니다.")
            return job

    def retry_upload_item(self, job_id: str, item_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                """
                SELECT * FROM upload_job_items
                WHERE id = ? AND job_id = ?
                """,
                (item_id, job_id),
            ).fetchone()
            if not item:
                raise UploadJobNotFoundError("업로드 작업 파일을 찾을 수 없습니다.")
            if item["status"] != "failed" or not item["retryable"]:
                raise UploadJobConflictError("다시 시도할 수 있는 실패 파일이 아닙니다.")
            connection.execute(
                """
                UPDATE upload_job_items
                SET status = 'queued', progress = 0, phase = 'queued',
                    error = '', retryable = 0, queued_at = ?,
                    started_at = NULL, finished_at = NULL
                WHERE id = ?
                """,
                (now, item_id),
            )
            connection.execute(
                "UPDATE upload_jobs SET finished_at = NULL WHERE id = ?",
                (job_id,),
            )
            job = self._upload_job_from_connection(connection, job_id)
            if not job:
                raise UploadJobNotFoundError("업로드 작업을 찾을 수 없습니다.")
            return job

    def recover_interrupted_upload_jobs(self) -> dict[str, int]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            completed = connection.execute(
                """
                UPDATE upload_job_items
                SET status = 'completed', progress = 100, phase = 'completed',
                    error = '', retryable = 0, finished_at = ?
                WHERE status = 'running' AND EXISTS(
                    SELECT 1 FROM projects p
                    WHERE p.id = upload_job_items.project_id
                )
                """,
                (now,),
            ).rowcount
            requeued = connection.execute(
                """
                UPDATE upload_job_items
                SET status = 'queued', progress = 0, phase = 'queued',
                    error = '', retryable = 0,
                    started_at = NULL, finished_at = NULL
                WHERE status = 'running'
                """
            ).rowcount
            job_ids = connection.execute(
                "SELECT id FROM upload_jobs"
            ).fetchall()
            for job in job_ids:
                self._finish_upload_job_if_terminal(connection, job["id"], now)
            return {"requeued": int(requeued), "completed": int(completed)}

    def list_projects(
        self,
        search: str = "",
        parser_status: str = "",
        include_archived: bool = False,
        archive_status: str = "",
    ) -> list[dict[str, Any]]:
        search = str(search or "").strip()
        parser_status = str(parser_status or "").strip()
        archive_status = str(archive_status or "").strip()
        if parser_status not in {"", "current", "legacy"}:
            raise ValueError("문서 해석기 상태 필터가 올바르지 않습니다.")
        if archive_status not in {"", "active", "archived"}:
            raise ValueError("프로젝트 보관 상태 필터가 올바르지 않습니다.")
        pattern = _like_pattern(search) if search else ""
        archive_predicate = {
            "active": "p.archived_at IS NULL",
            "archived": "p.archived_at IS NOT NULL",
        }.get(
            archive_status,
            "1 = 1" if include_archived else "p.archived_at IS NULL",
        )
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT p.*,
                       COUNT(c.id) AS total_clauses,
                       SUM(CASE WHEN c.source_type != 'heading' AND c.decision IS NOT NULL THEN 1 ELSE 0 END) AS reviewed_clauses,
                       SUM(CASE WHEN c.source_type != 'heading' AND c.decision = 'keep' THEN 1 ELSE 0 END) AS keep_count,
                       SUM(CASE WHEN c.source_type != 'heading' AND c.decision = 'delete' THEN 1 ELSE 0 END) AS delete_count,
                       SUM(CASE WHEN c.source_type != 'heading' AND c.decision = 'hold' THEN 1 ELSE 0 END) AS hold_count,
                       SUM(CASE WHEN EXISTS(
                           SELECT 1 FROM candidates k
                           WHERE k.clause_id = c.id
                             AND NOT EXISTS(
                                 SELECT 1 FROM candidate_ai_analysis ai
                                 WHERE ai.candidate_id = k.id AND ai.relation_type = 'unrelated'
                                   AND ai.confidence >= 0.85
                             )
                       ) THEN 1 ELSE 0 END) AS candidate_clauses,
                       SUM(CASE WHEN EXISTS(
                           SELECT 1 FROM candidates k
                           WHERE k.clause_id = c.id AND k.score >= 0.45
                             AND NOT EXISTS(
                                 SELECT 1 FROM candidate_ai_analysis ai
                                 WHERE ai.candidate_id = k.id AND ai.relation_type = 'unrelated'
                                   AND ai.confidence >= 0.85
                             )
                       ) THEN 1 ELSE 0 END) AS high_match_clauses,
                       SUM(CASE WHEN c.source_type = 'table' THEN 1 ELSE 0 END) AS table_clauses
                 FROM projects p
                 LEFT JOIN clauses c ON c.project_id = p.id
                 WHERE (
                     ? = '' OR p.title LIKE ? ESCAPE '\\'
                     OR p.source_filename LIKE ? ESCAPE '\\'
                     OR p.kcs_scope LIKE ? ESCAPE '\\'
                 ) AND ({archive_predicate}) AND (
                     ? = ''
                     OR (? = 'current' AND p.parser_version = ?)
                     OR (? = 'legacy' AND p.parser_version != ?)
                 )
                 GROUP BY p.id
                 ORDER BY p.uploaded_at DESC
                """,
                (
                    search,
                    pattern,
                    pattern,
                    pattern,
                    parser_status,
                    parser_status,
                    CURRENT_PARSER_VERSION,
                    parser_status,
                    CURRENT_PARSER_VERSION,
                ),
            ).fetchall()
            return [self._add_export_readiness(connection, dict(row)) for row in rows]

    def list_projects_page(
        self,
        search: str = "",
        parser_status: str = "",
        include_archived: bool = False,
        archive_status: str = "",
        kcs_impact_only: bool = False,
        review_status: str = "",
        discipline: str = "",
        limit: int = 50,
        cursor: str = "",
    ) -> dict[str, Any]:
        search = str(search or "").strip()
        parser_status = str(parser_status or "").strip()
        archive_status = str(archive_status or "").strip()
        review_status = str(review_status or "").strip()
        discipline = str(discipline or "").strip()
        cursor = str(cursor or "").strip()
        if len(search) > 200:
            raise ValueError("검색어는 200자 이하여야 합니다.")
        if parser_status not in {"", "current", "legacy"}:
            raise ValueError("문서 해석기 상태 필터가 올바르지 않습니다.")
        if archive_status not in {"", "active", "archived"}:
            raise ValueError("프로젝트 보관 상태 필터가 올바르지 않습니다.")
        if review_status not in {
            "", "reviewing", "submitted", "changes_requested", "approved"
        }:
            raise ValueError("프로젝트 승인 상태 필터가 올바르지 않습니다.")
        if discipline not in {"", "architecture", "mechanical", "electrical"}:
            raise ValueError("시방서 분야 필터가 올바르지 않습니다.")
        if not 1 <= limit <= 100:
            raise ValueError("페이지 크기는 1 이상 100 이하여야 합니다.")

        pattern = _like_pattern(search) if search else ""
        archive_predicate = {
            "active": "p.archived_at IS NULL",
            "archived": "p.archived_at IS NOT NULL",
        }.get(
            archive_status,
            "1 = 1" if include_archived else "p.archived_at IS NULL",
        )
        conditions = [
            """
            (? = '' OR p.title LIKE ? ESCAPE '\\'
             OR p.source_filename LIKE ? ESCAPE '\\'
             OR p.kcs_scope LIKE ? ESCAPE '\\')
            """,
            archive_predicate,
            """
            (? = ''
             OR (? = 'current' AND p.parser_version = ?)
             OR (? = 'legacy' AND p.parser_version != ?))
            """,
        ]
        parameters: list[Any] = [
            search,
            pattern,
            pattern,
            pattern,
            parser_status,
            parser_status,
            CURRENT_PARSER_VERSION,
            parser_status,
            CURRENT_PARSER_VERSION,
        ]
        if kcs_impact_only:
            conditions.append(
                """
                EXISTS(
                    SELECT 1
                    FROM kcs_rematch_runs impact_run
                    JOIN kcs_clause_impacts impact
                      ON impact.run_id = impact_run.id
                    WHERE impact_run.project_id = p.id
                      AND impact_run.status = 'completed'
                      AND impact_run.target_revision = p.kcs_revision
                      AND impact.review_required = 1
                      AND impact.acknowledged_at IS NULL
                )
                """
            )
        if review_status:
            conditions.append("p.status = ?")
            parameters.append(review_status)
        if discipline == "architecture":
            conditions.append("p.source_filename LIKE '건축\\_%' ESCAPE '\\'")
        elif discipline == "mechanical":
            conditions.append("p.source_filename LIKE '건축설비\\_%' ESCAPE '\\'")
        elif discipline == "electrical":
            conditions.append(
                "(p.source_filename LIKE '전기\\_%' ESCAPE '\\' "
                "OR p.source_filename LIKE '전기시방서\\_%' ESCAPE '\\')"
            )
        filter_where = " AND ".join(f"({condition.strip()})" for condition in conditions)
        filter_signature = _project_catalog_filter_signature(
            search,
            parser_status,
            bool(include_archived),
            archive_status,
            bool(kcs_impact_only),
            review_status,
            discipline,
        )
        cursor_parameters: list[Any] = []
        page_where = filter_where
        if cursor:
            cursor_uploaded_at, cursor_project_id = _decode_project_catalog_cursor(
                cursor,
                filter_signature,
            )
            page_where += (
                " AND (p.uploaded_at < ? OR "
                "(p.uploaded_at = ? AND p.id < ?))"
            )
            cursor_parameters.extend(
                (cursor_uploaded_at, cursor_uploaded_at, cursor_project_id)
            )

        with self.connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM projects p WHERE {filter_where}",
                    tuple(parameters),
                ).fetchone()[0]
                or 0
            )
            rows = connection.execute(
                f"""
                WITH active_current_titles AS (
                    SELECT p.id,
                           COUNT(*) OVER (PARTITION BY p.title) AS duplicate_title_count,
                           ROW_NUMBER() OVER (
                               PARTITION BY p.title
                               ORDER BY p.uploaded_at DESC, p.id DESC
                           ) AS title_position
                    FROM projects p
                    WHERE p.archived_at IS NULL AND p.parser_version = ?
                ),
                catalog AS (
                    SELECT p.*,
                           COALESCE(active.duplicate_title_count, 0) AS duplicate_title_count,
                           COALESCE(active.title_position, 0) AS title_position
                    FROM projects p
                    LEFT JOIN active_current_titles active ON active.id = p.id
                ),
                page_projects AS (
                    SELECT p.*
                    FROM catalog p
                    WHERE {page_where}
                    ORDER BY p.uploaded_at DESC, p.id DESC
                    LIMIT ?
                ),
                clause_stats AS (
                    SELECT
                        c.project_id,
                        COUNT(c.id) AS total_clauses,
                        SUM(CASE WHEN c.source_type != 'heading' AND c.decision = 'keep' THEN 1 ELSE 0 END) AS keep_count,
                        SUM(CASE WHEN c.source_type != 'heading' AND c.decision = 'delete' THEN 1 ELSE 0 END) AS delete_count,
                        SUM(CASE WHEN c.source_type != 'heading' AND c.decision = 'hold' THEN 1 ELSE 0 END) AS hold_count,
                        SUM(CASE WHEN EXISTS(
                            SELECT 1 FROM candidates k
                            WHERE k.clause_id = c.id
                              AND NOT EXISTS(
                                  SELECT 1 FROM candidate_ai_analysis ai
                                  WHERE ai.candidate_id = k.id
                                    AND ai.relation_type = 'unrelated'
                                    AND ai.confidence >= 0.85
                              )
                        ) THEN 1 ELSE 0 END) AS candidate_clauses,
                        SUM(CASE WHEN EXISTS(
                            SELECT 1 FROM candidates k
                            WHERE k.clause_id = c.id AND k.score >= 0.45
                              AND NOT EXISTS(
                                  SELECT 1 FROM candidate_ai_analysis ai
                                  WHERE ai.candidate_id = k.id
                                    AND ai.relation_type = 'unrelated'
                                    AND ai.confidence >= 0.85
                              )
                        ) THEN 1 ELSE 0 END) AS high_match_clauses,
                        SUM(CASE WHEN c.source_type = 'table' THEN 1 ELSE 0 END) AS table_clauses,
                        SUM(CASE WHEN c.source_type != 'heading' THEN 1 ELSE 0 END) AS reviewable,
                        SUM(CASE WHEN c.source_type != 'heading' AND c.decision IS NULL THEN 1 ELSE 0 END) AS unreviewed,
                        SUM(CASE WHEN c.source_type != 'heading' AND (
                            (c.decision = 'keep' AND c.decision_reason NOT IN (
                                'posco_specific', 'posco_stricter',
                                'partial_overlap_residual', 'no_kcs_match'
                            ))
                            OR (c.decision = 'keep'
                                AND c.decision_reason = 'partial_overlap_residual'
                                AND (TRIM(c.edited_content) = ''
                                     OR TRIM(c.edited_content) = TRIM(c.content)))
                            OR (c.decision = 'delete' AND (
                                c.decision_reason NOT IN (
                                    'fully_covered_by_kcs', 'internal_duplicate',
                                    'obsolete_requirement', 'out_of_scope',
                                    'editorial_cleanup', 'management_decision'
                                )
                                OR (c.decision_reason = 'management_decision'
                                    AND TRIM(c.review_note) = '')
                            ))
                            OR (c.decision = 'hold' AND c.decision_reason NOT IN (
                                'needs_expert_review', 'candidate_uncertain', 'kcs_conflict'
                            ))
                        ) THEN 1 ELSE 0 END) AS invalid_decisions,
                        SUM(CASE WHEN c.source_type != 'heading' AND c.decision = 'delete'
                          AND c.decision_reason = 'fully_covered_by_kcs'
                          AND (
                              c.selected_candidate_id IS NULL
                              OR c.coverage_confirmed != 1
                              OR NOT EXISTS(
                                  SELECT 1
                                  FROM candidates k
                                  WHERE k.id = c.selected_candidate_id
                                    AND k.clause_id = c.id
                                    AND k.warnings_json = '[]'
                              )
                              OR EXISTS(
                                  SELECT 1 FROM clause_coverage_analysis ca
                                  WHERE ca.clause_id = c.id
                                    AND (
                                        ca.deletion_safe != 1
                                        OR NOT EXISTS(
                                            SELECT 1
                                            FROM json_each(ca.evidence_candidate_ids_json) evidence
                                            WHERE evidence.value = c.selected_candidate_id
                                        )
                                        OR EXISTS(
                                            SELECT 1
                                            FROM json_each(ca.evidence_candidate_ids_json) evidence
                                            JOIN candidate_ai_analysis evidence_ai
                                              ON evidence_ai.candidate_id = evidence.value
                                            WHERE evidence_ai.relation_type NOT IN ('equivalent', 'kcs_covers')
                                               OR evidence_ai.confidence < 0.85
                                        )
                                    )
                              )
                              OR EXISTS(
                                  SELECT 1 FROM candidate_ai_analysis ai
                                  WHERE ai.candidate_id = c.selected_candidate_id
                                    AND (
                                        ai.relation_type NOT IN ('equivalent', 'kcs_covers')
                                        OR ai.confidence < 0.85
                                    )
                              )
                          ) THEN 1 ELSE 0 END) AS unsafe_delete_count
                    FROM clauses c
                    WHERE c.project_id IN (SELECT id FROM page_projects)
                    GROUP BY c.project_id
                ),
                impact_stats AS (
                    SELECT r.project_id, r.target_revision, COUNT(*) AS unacknowledged_count
                    FROM kcs_rematch_runs r
                    JOIN kcs_clause_impacts i ON i.run_id = r.id
                    WHERE r.status = 'completed'
                      AND i.review_required = 1
                      AND i.acknowledged_at IS NULL
                      AND r.project_id IN (SELECT id FROM page_projects)
                    GROUP BY r.project_id, r.target_revision
                ),
                latest_review_submission AS (
                    SELECT * FROM (
                        SELECT s.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY s.project_id
                                   ORDER BY s.revision_no DESC
                               ) AS submission_rank
                        FROM project_review_submissions s
                        WHERE s.project_id IN (SELECT id FROM page_projects)
                    ) ranked_submission
                    WHERE submission_rank = 1
                )
                SELECT
                    p.*,
                    COALESCE(stats.total_clauses, 0) AS total_clauses,
                    COALESCE(stats.keep_count, 0) AS keep_count,
                    COALESCE(stats.delete_count, 0) AS delete_count,
                    COALESCE(stats.hold_count, 0) AS hold_count,
                    COALESCE(stats.candidate_clauses, 0) AS candidate_clauses,
                    COALESCE(stats.high_match_clauses, 0) AS high_match_clauses,
                    COALESCE(stats.table_clauses, 0) AS table_clauses,
                    COALESCE(stats.reviewable, 0) AS catalog_reviewable,
                    COALESCE(stats.unreviewed, 0) AS catalog_unreviewed,
                    COALESCE(stats.invalid_decisions, 0) AS catalog_invalid_decisions,
                    COALESCE(stats.unsafe_delete_count, 0) AS catalog_unsafe_deletes,
                    COALESCE(impact.unacknowledged_count, 0) AS catalog_unacknowledged_impacts,
                    submission.id AS catalog_submission_id,
                    submission.project_id AS catalog_submission_project_id,
                    submission.revision_no AS catalog_submission_revision_no,
                    submission.status AS catalog_submission_status,
                    submission.author_name AS catalog_submission_author_name,
                    submission.author_note AS catalog_submission_author_note,
                    submission.submitted_at AS catalog_submission_submitted_at,
                    submission.decided_by AS catalog_submission_decided_by,
                    submission.decision_note AS catalog_submission_decision_note,
                    submission.decided_at AS catalog_submission_decided_at,
                    submission.kcs_snapshot AS catalog_submission_kcs_snapshot,
                    submission.kcs_revision AS catalog_submission_kcs_revision,
                    submission.snapshot_schema_version AS catalog_submission_snapshot_schema_version,
                    submission.review_snapshot_sha256 AS catalog_submission_snapshot_sha256,
                    COALESCE((
                        SELECT value FROM app_metadata
                        WHERE key = 'current_kcs_revision'
                    ), '') AS catalog_current_revision
                FROM page_projects p
                LEFT JOIN clause_stats stats ON stats.project_id = p.id
                LEFT JOIN impact_stats impact
                  ON impact.project_id = p.id
                 AND impact.target_revision = p.kcs_revision
                LEFT JOIN latest_review_submission submission
                  ON submission.project_id = p.id
                ORDER BY p.uploaded_at DESC, p.id DESC
                """,
                tuple(
                    [CURRENT_PARSER_VERSION]
                    + parameters
                    + cursor_parameters
                    + [limit + 1]
                ),
            ).fetchall()

            has_more = len(rows) > limit
            selected_rows = rows[:limit]
            project_ids = [str(row["id"]) for row in selected_rows]
            latest_rematches: dict[str, dict[str, Any]] = {}
            if project_ids:
                placeholders = ",".join("?" for _ in project_ids)
                rematch_rows = connection.execute(
                    f"""
                    WITH ranked AS (
                        SELECT r.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY r.project_id
                                   ORDER BY r.queued_at DESC, r.id DESC
                               ) AS catalog_rank
                        FROM kcs_rematch_runs r
                        WHERE r.project_id IN ({placeholders})
                    )
                    SELECT ranked.*,
                           (SELECT COUNT(*) FROM kcs_clause_impacts i
                            WHERE i.run_id = ranked.id
                              AND i.review_required = 1
                              AND i.acknowledged_at IS NULL) AS unacknowledged_count
                    FROM ranked
                    WHERE catalog_rank = 1
                    """,
                    tuple(project_ids),
                ).fetchall()
                for rematch_row in rematch_rows:
                    raw_rematch = dict(rematch_row)
                    raw_rematch.pop("catalog_rank", None)
                    latest_rematches[str(rematch_row["project_id"])] = (
                        self._public_kcs_rematch_run(raw_rematch)
                    )

            projects: list[dict[str, Any]] = []
            for row in selected_rows:
                project = dict(row)
                submission_fields = {
                    "id": project.pop("catalog_submission_id"),
                    "project_id": project.pop("catalog_submission_project_id"),
                    "revision_no": project.pop("catalog_submission_revision_no"),
                    "status": project.pop("catalog_submission_status"),
                    "author_name": project.pop("catalog_submission_author_name"),
                    "author_note": project.pop("catalog_submission_author_note"),
                    "submitted_at": project.pop("catalog_submission_submitted_at"),
                    "decided_by": project.pop("catalog_submission_decided_by"),
                    "decision_note": project.pop("catalog_submission_decision_note"),
                    "decided_at": project.pop("catalog_submission_decided_at"),
                    "kcs_snapshot": project.pop("catalog_submission_kcs_snapshot"),
                    "kcs_revision": project.pop("catalog_submission_kcs_revision"),
                    "snapshot_schema_version": project.pop(
                        "catalog_submission_snapshot_schema_version"
                    ),
                    "review_snapshot_sha256": project.pop(
                        "catalog_submission_snapshot_sha256"
                    ),
                }
                latest_submission = (
                    self._public_review_submission(submission_fields)
                    if submission_fields["id"]
                    else None
                )
                title_position = int(project.pop("title_position") or 0)
                current_revision = str(project.pop("catalog_current_revision") or "")
                reviewable = int(project.pop("catalog_reviewable") or 0)
                unreviewed = int(project.pop("catalog_unreviewed") or 0)
                invalid_decisions = int(project.pop("catalog_invalid_decisions") or 0)
                unsafe_deletes = int(project.pop("catalog_unsafe_deletes") or 0)
                unacknowledged_impacts = int(
                    project.pop("catalog_unacknowledged_impacts") or 0
                )
                project["duplicate_title_count"] = int(
                    project.get("duplicate_title_count") or 0
                )
                project["is_latest_for_title"] = title_position == 1
                projects.append(
                    self._finish_project_summary(
                        project,
                        reviewable=reviewable,
                        unreviewed=unreviewed,
                        holds=int(project.get("hold_count") or 0),
                        invalid_decisions=invalid_decisions,
                        unsafe_deletes=unsafe_deletes,
                        current_revision=current_revision,
                        latest_rematch=latest_rematches.get(str(project["id"])),
                        unacknowledged_impacts=unacknowledged_impacts,
                        latest_submission=latest_submission,
                    )
                )

            next_cursor = None
            if has_more and projects:
                last_project = projects[-1]
                next_cursor = _encode_project_catalog_cursor(
                    str(last_project["uploaded_at"]),
                    str(last_project["id"]),
                    filter_signature,
                )
            return {
                "projects": projects,
                "total": total,
                "limit": limit,
                "has_more": has_more,
                "next_cursor": next_cursor,
            }

    def set_project_archived(
        self,
        project_id: str,
        archived: bool,
    ) -> dict[str, Any]:
        archived_at = datetime.now(timezone.utc).isoformat() if archived else None
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE projects SET archived_at = ? WHERE id = ?",
                (archived_at, project_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("검토 프로젝트를 찾을 수 없습니다.")
        project = self.get_project(project_id)
        if not project:
            raise ValueError("검토 프로젝트를 찾을 수 없습니다.")
        return project

    @staticmethod
    def _project_summary_row(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
                SELECT p.*,
                       CASE
                           WHEN p.archived_at IS NULL AND p.parser_version = ?
                           THEN (
                               SELECT COUNT(*)
                               FROM projects duplicate
                               WHERE duplicate.title = p.title
                                 AND duplicate.archived_at IS NULL
                                 AND duplicate.parser_version = ?
                           )
                           ELSE 0
                       END AS duplicate_title_count,
                       CASE
                           WHEN p.archived_at IS NULL AND p.parser_version = ?
                            AND NOT EXISTS(
                                SELECT 1
                                FROM projects newer
                                WHERE newer.title = p.title
                                  AND newer.archived_at IS NULL
                                  AND newer.parser_version = ?
                                  AND (
                                      newer.uploaded_at > p.uploaded_at
                                      OR (newer.uploaded_at = p.uploaded_at AND newer.id > p.id)
                                  )
                            )
                           THEN 1 ELSE 0
                       END AS is_latest_for_title,
                       COUNT(c.id) AS total_clauses,
                       SUM(CASE WHEN c.source_type != 'heading' AND c.decision IS NOT NULL THEN 1 ELSE 0 END) AS reviewed_clauses,
                       SUM(CASE WHEN c.source_type != 'heading' AND c.decision = 'keep' THEN 1 ELSE 0 END) AS keep_count,
                       SUM(CASE WHEN c.source_type != 'heading' AND c.decision = 'delete' THEN 1 ELSE 0 END) AS delete_count,
                       SUM(CASE WHEN c.source_type != 'heading' AND c.decision = 'hold' THEN 1 ELSE 0 END) AS hold_count,
                       SUM(CASE WHEN EXISTS(
                           SELECT 1 FROM candidates k
                           WHERE k.clause_id = c.id
                             AND NOT EXISTS(
                                 SELECT 1 FROM candidate_ai_analysis ai
                                 WHERE ai.candidate_id = k.id AND ai.relation_type = 'unrelated'
                                   AND ai.confidence >= 0.85
                             )
                       ) THEN 1 ELSE 0 END) AS candidate_clauses,
                       SUM(CASE WHEN EXISTS(
                           SELECT 1 FROM candidates k
                           WHERE k.clause_id = c.id AND k.score >= 0.45
                             AND NOT EXISTS(
                                 SELECT 1 FROM candidate_ai_analysis ai
                                 WHERE ai.candidate_id = k.id AND ai.relation_type = 'unrelated'
                                   AND ai.confidence >= 0.85
                             )
                       ) THEN 1 ELSE 0 END) AS high_match_clauses,
                       SUM(CASE WHEN c.source_type = 'table' THEN 1 ELSE 0 END) AS table_clauses
                FROM projects p
                LEFT JOIN clauses c ON c.project_id = p.id
                WHERE p.id = ?
                GROUP BY p.id
            """,
            (
                CURRENT_PARSER_VERSION,
                CURRENT_PARSER_VERSION,
                CURRENT_PARSER_VERSION,
                CURRENT_PARSER_VERSION,
                project_id,
            ),
        ).fetchone()

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = self._project_summary_row(connection, project_id)
            if not row:
                return None
            project = dict(row)
            project["duplicate_title_count"] = int(
                project.get("duplicate_title_count") or 0
            )
            project["is_latest_for_title"] = bool(project.get("is_latest_for_title"))
            return self._add_export_readiness(connection, project)

    @staticmethod
    def _review_event_rows(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT e.id, e.submission_id, e.event_type, e.from_status,
                       e.to_status, e.actor_name, e.note, e.occurred_at,
                       s.revision_no, s.author_name, s.author_note,
                       s.submitted_at, s.decided_by, s.decision_note,
                       s.decided_at, s.kcs_snapshot, s.kcs_revision,
                       s.source_sha256, s.review_snapshot_sha256
                FROM project_review_events e
                JOIN project_review_submissions s ON s.id = e.submission_id
                WHERE s.project_id = ?
                ORDER BY e.occurred_at, e.id
                """,
                (project_id,),
            ).fetchall()
        ]

    def get_review_workflow(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            project = connection.execute(
                "SELECT id, status FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if not project:
                return None
            submissions = [
                self._public_review_submission(row)
                for row in connection.execute(
                    """
                    SELECT * FROM project_review_submissions
                    WHERE project_id = ?
                    ORDER BY revision_no DESC
                    """,
                    (project_id,),
                ).fetchall()
            ]
            return {
                "project_id": project_id,
                "status": str(project["status"] or "reviewing"),
                "latest_review_submission": submissions[0] if submissions else None,
                "submissions": submissions,
                "events": self._review_event_rows(connection, project_id),
            }

    def submit_review(
        self,
        project_id: str,
        author_name: str,
        note: str = "",
    ) -> dict[str, Any]:
        author_name = str(author_name or "").strip()
        note = str(note or "").strip()
        if not author_name:
            raise ValueError("작성자 이름을 입력해 주세요.")
        if len(author_name) > 100:
            raise ValueError("작성자 이름은 100자 이하여야 합니다.")
        if len(note) > 5000:
            raise ValueError("제출 의견은 5,000자 이하여야 합니다.")
        submitted_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._project_summary_row(connection, project_id)
            if not row:
                raise ReviewWorkflowNotFoundError("검토 프로젝트를 찾을 수 없습니다.")
            project = self._add_export_readiness(connection, dict(row))
            status = str(project.get("status") or "reviewing")
            if project.get("archived_at"):
                raise ReviewWorkflowConflictError("보관된 프로젝트는 승인 요청할 수 없습니다.")
            if status not in REVIEW_EDITABLE_STATUSES:
                raise ReviewWorkflowConflictError(
                    "현재 프로젝트 상태에서는 승인 요청할 수 없습니다."
                )
            technical_blockers = list(project.get("_review_technical_blockers") or [])
            if technical_blockers:
                raise ReviewWorkflowConflictError(
                    "승인 요청 조건을 충족하지 않았습니다: "
                    + ", ".join(technical_blockers)
                )
            snapshot = self._review_snapshot(connection, project_id)
            snapshot_json, snapshot_sha256 = self._canonical_review_snapshot(snapshot)
            revision_no = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(revision_no), 0) + 1
                    FROM project_review_submissions
                    WHERE project_id = ?
                    """,
                    (project_id,),
                ).fetchone()[0]
            )
            submission_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO project_review_submissions(
                    id, project_id, revision_no, status, author_name,
                    author_note, submitted_at, kcs_snapshot, kcs_revision,
                    source_sha256, snapshot_schema_version,
                    review_snapshot_json, review_snapshot_sha256
                ) VALUES (?, ?, ?, 'submitted', ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    submission_id,
                    project_id,
                    revision_no,
                    author_name,
                    note,
                    submitted_at,
                    project["kcs_snapshot"],
                    project["kcs_revision"],
                    project["source_sha256"],
                    snapshot_json,
                    snapshot_sha256,
                ),
            )
            connection.execute(
                "UPDATE projects SET status = 'submitted' WHERE id = ?",
                (project_id,),
            )
            connection.execute(
                """
                INSERT INTO project_review_events(
                    submission_id, event_type, from_status, to_status,
                    actor_name, note, occurred_at
                ) VALUES (?, 'submitted', ?, 'submitted', ?, ?, ?)
                """,
                (submission_id, status, author_name, note, submitted_at),
            )
        workflow = self.get_review_workflow(project_id)
        if not workflow:
            raise ReviewWorkflowNotFoundError("검토 프로젝트를 찾을 수 없습니다.")
        return workflow

    def _decide_review(
        self,
        project_id: str,
        submission_id: str,
        approver_name: str,
        note: str,
        *,
        approve: bool,
    ) -> dict[str, Any]:
        submission_id = str(submission_id or "").strip()
        approver_name = str(approver_name or "").strip()
        note = str(note or "").strip()
        if not approver_name:
            raise ValueError("승인자 이름을 입력해 주세요.")
        if len(approver_name) > 100:
            raise ValueError("승인자 이름은 100자 이하여야 합니다.")
        if len(note) > 5000:
            raise ValueError("승인 의견은 5,000자 이하여야 합니다.")
        if not approve and not note:
            raise ValueError("반려 사유를 입력해 주세요.")
        decided_at = datetime.now(timezone.utc).isoformat()
        target_status = "approved" if approve else "changes_requested"
        event_type = "approved" if approve else "changes_requested"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._project_summary_row(connection, project_id)
            if not row:
                raise ReviewWorkflowNotFoundError("검토 프로젝트를 찾을 수 없습니다.")
            project = self._add_export_readiness(connection, dict(row))
            if project.get("archived_at"):
                raise ReviewWorkflowConflictError("보관된 프로젝트는 승인 처리할 수 없습니다.")
            submission = connection.execute(
                """
                SELECT * FROM project_review_submissions
                WHERE id = ? AND project_id = ?
                """,
                (submission_id, project_id),
            ).fetchone()
            if not submission:
                raise ReviewWorkflowNotFoundError("승인 요청 기록을 찾을 수 없습니다.")
            latest = self._latest_review_submission(connection, project_id)
            if (
                project.get("status") != "submitted"
                or submission["status"] != "submitted"
                or not latest
                or latest["id"] != submission_id
            ):
                raise ReviewWorkflowConflictError(
                    "현재 대기 중인 최신 승인 요청만 처리할 수 있습니다."
                )
            if approver_name.casefold() == str(submission["author_name"] or "").strip().casefold():
                raise ReviewWorkflowConflictError(
                    "작성자와 다른 승인자 이름을 입력해 주세요."
                )
            if approve:
                technical_blockers = list(project.get("_review_technical_blockers") or [])
                if technical_blockers:
                    raise ReviewWorkflowConflictError(
                        "승인 처리 조건을 충족하지 않았습니다: "
                        + ", ".join(technical_blockers)
                    )
                current_revision_row = connection.execute(
                    "SELECT value FROM app_metadata WHERE key = 'current_kcs_revision'"
                ).fetchone()
                current_revision = (
                    str(current_revision_row["value"] or "")
                    if current_revision_row
                    else ""
                )
                if (
                    not current_revision
                    or project.get("kcs_revision") != current_revision
                    or submission["kcs_revision"] != current_revision
                ):
                    raise ReviewWorkflowConflictError(
                        "KCS 기준이 변경되었습니다. 최신 기준 재검토 후 다시 제출해 주세요."
                    )
                live_snapshot = self._review_snapshot(connection, project_id)
                _, live_sha256 = self._canonical_review_snapshot(live_snapshot)
                if live_sha256 != submission["review_snapshot_sha256"]:
                    raise ReviewWorkflowConflictError(
                        "제출 후 검토 내용이 변경되었습니다. 현재 내용으로 다시 제출해 주세요."
                    )
            connection.execute(
                """
                UPDATE project_review_submissions
                SET status = ?, decided_by = ?, decision_note = ?, decided_at = ?
                WHERE id = ? AND status = 'submitted'
                """,
                (target_status, approver_name, note, decided_at, submission_id),
            )
            connection.execute(
                "UPDATE projects SET status = ? WHERE id = ? AND status = 'submitted'",
                (target_status, project_id),
            )
            connection.execute(
                """
                INSERT INTO project_review_events(
                    submission_id, event_type, from_status, to_status,
                    actor_name, note, occurred_at
                ) VALUES (?, ?, 'submitted', ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    event_type,
                    target_status,
                    approver_name,
                    note,
                    decided_at,
                ),
            )
        workflow = self.get_review_workflow(project_id)
        if not workflow:
            raise ReviewWorkflowNotFoundError("검토 프로젝트를 찾을 수 없습니다.")
        return workflow

    def approve_review(
        self,
        project_id: str,
        submission_id: str,
        approver_name: str,
        note: str = "",
    ) -> dict[str, Any]:
        return self._decide_review(
            project_id,
            submission_id,
            approver_name,
            note,
            approve=True,
        )

    def request_review_changes(
        self,
        project_id: str,
        submission_id: str,
        approver_name: str,
        note: str,
    ) -> dict[str, Any]:
        return self._decide_review(
            project_id,
            submission_id,
            approver_name,
            note,
            approve=False,
        )

    def workflow_audit_rows(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return self._review_event_rows(connection, project_id)

    def list_clauses(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.source_order, c.label, c.title, c.source_type,
                       c.decision, c.decision_reason, c.coverage_confirmed,
                       c.selected_candidate_id, c.reviewed_at,
                       COUNT(k.id) AS candidate_count,
                       MAX(k.score) AS top_score,
                       EXISTS(
                           SELECT 1
                           FROM quality_evaluation_items qi
                           JOIN quality_evaluations qe ON qe.id = qi.evaluation_id
                           WHERE qi.clause_id = c.id AND qe.project_id = c.project_id
                       ) AS in_quality_sample,
                       (
                           SELECT qi.verdict
                           FROM quality_evaluation_items qi
                           JOIN quality_evaluations qe ON qe.id = qi.evaluation_id
                           WHERE qi.clause_id = c.id AND qe.project_id = c.project_id
                           LIMIT 1
                       ) AS quality_verdict
                FROM clauses c
                LEFT JOIN candidates k
                  ON k.clause_id = c.id
                 AND NOT EXISTS(
                     SELECT 1 FROM candidate_ai_analysis ai
                     WHERE ai.candidate_id = k.id AND ai.relation_type = 'unrelated'
                       AND ai.confidence >= 0.85
                 )
                WHERE c.project_id = ?
                GROUP BY c.id
                ORDER BY c.source_order
                """,
                (project_id,),
            ).fetchall()
            result = [dict(row) for row in rows]
            for item in result:
                item["coverage_confirmed"] = bool(item["coverage_confirmed"])
            self._attach_clause_kcs_impacts(connection, result, full=False)
            return result

    def document_map(self, project_id: str) -> list[dict[str, Any]]:
        """Return only the source fields needed to link the rendered DOCX to clauses."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, source_order, label, title, content, source_type,
                       decision
                FROM clauses
                WHERE project_id = ?
                ORDER BY source_order
                """,
                (project_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def bulk_review(
        self,
        project_id: str,
        offset: int = 0,
        limit: int = 60,
        status: str = "all",
        query: str = "",
    ) -> tuple[list[dict[str, Any]], int]:
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("페이지 범위가 올바르지 않습니다.")
        if status not in {"all", "unreviewed", "keep", "delete", "hold", "kcs_impact"}:
            raise ValueError("지원하지 않는 검토 상태입니다.")
        query = query.strip()
        if len(query) > 200:
            raise ValueError("검색어는 200자 이하여야 합니다.")

        conditions = ["c.project_id = ?"]
        parameters: list[Any] = [project_id]
        if status == "unreviewed":
            conditions.append("c.decision IS NULL")
        elif status == "kcs_impact":
            conditions.append(
                """
                EXISTS(
                    SELECT 1
                    FROM kcs_clause_impacts impact
                    JOIN kcs_rematch_runs rematch ON rematch.id = impact.run_id
                    JOIN projects impact_project ON impact_project.id = rematch.project_id
                    WHERE impact.clause_id = c.id
                      AND rematch.project_id = c.project_id
                      AND rematch.status = 'completed'
                      AND rematch.target_revision = impact_project.kcs_revision
                      AND impact.review_required = 1
                      AND impact.acknowledged_at IS NULL
                )
                """
            )
        elif status != "all":
            conditions.append("c.decision = ?")
            parameters.append(status)
        if query:
            pattern = _like_pattern(query)
            conditions.append(
                "(c.label LIKE ? ESCAPE '\\' OR c.title LIKE ? ESCAPE '\\' "
                "OR c.content LIKE ? ESCAPE '\\')"
            )
            parameters.extend((pattern, pattern, pattern))

        where_clause = " AND ".join(conditions)
        with self.connect() as connection:
            page_rows = connection.execute(
                f"""
                WITH filtered AS (
                    SELECT c.id, c.source_order, c.label, c.title, c.content,
                           c.source_type, c.outline_level, c.decision,
                           c.edited_content, c.review_note, c.decision_reason,
                           c.coverage_confirmed, c.selected_candidate_id,
                           c.reviewed_at, c.match_context
                    FROM clauses c
                    WHERE {where_clause}
                ),
                paged AS (
                    SELECT * FROM filtered
                    ORDER BY source_order
                    LIMIT ? OFFSET ?
                ),
                stats AS (
                    SELECT COUNT(*) AS total FROM filtered
                )
                SELECT paged.*, stats.total
                FROM stats
                LEFT JOIN paged ON 1 = 1
                ORDER BY paged.source_order
                """,
                (*parameters, limit, offset),
            ).fetchall()
            total = int(page_rows[0]["total"]) if page_rows else 0
            items = []
            for row in page_rows:
                if row["id"] is None:
                    continue
                item = dict(row)
                item.pop("total", None)
                item["coverage_confirmed"] = bool(item["coverage_confirmed"])
                item["candidates"] = []
                item["excluded_candidate_count"] = 0
                item["source_context"] = {
                    "path": str(item.pop("match_context") or "").strip(),
                    "previous": None,
                    "next": None,
                }
                items.append(item)

            if items:
                placeholders = ",".join("?" for _ in items)
                context_rows = connection.execute(
                    f"""
                    WITH ordered AS (
                        SELECT id,
                               LAG(source_order) OVER (ORDER BY source_order) AS previous_source_order,
                               LAG(label) OVER (ORDER BY source_order) AS previous_label,
                               LAG(title) OVER (ORDER BY source_order) AS previous_title,
                               LAG(content) OVER (ORDER BY source_order) AS previous_content,
                               LAG(source_type) OVER (ORDER BY source_order) AS previous_source_type,
                               LEAD(source_order) OVER (ORDER BY source_order) AS next_source_order,
                               LEAD(label) OVER (ORDER BY source_order) AS next_label,
                               LEAD(title) OVER (ORDER BY source_order) AS next_title,
                               LEAD(content) OVER (ORDER BY source_order) AS next_content,
                               LEAD(source_type) OVER (ORDER BY source_order) AS next_source_type
                        FROM clauses
                        WHERE project_id = ?
                    )
                    SELECT * FROM ordered WHERE id IN ({placeholders})
                    """,
                    (project_id, *(item["id"] for item in items)),
                ).fetchall()
                items_by_id = {item["id"]: item for item in items}
                for row in context_rows:
                    target = items_by_id[row["id"]]["source_context"]
                    if row["previous_source_order"] is not None:
                        target["previous"] = {
                            "source_order": row["previous_source_order"],
                            "label": row["previous_label"],
                            "title": row["previous_title"],
                            "content": row["previous_content"],
                            "source_type": row["previous_source_type"],
                        }
                    if row["next_source_order"] is not None:
                        target["next"] = {
                            "source_order": row["next_source_order"],
                            "label": row["next_label"],
                            "title": row["next_title"],
                            "content": row["next_content"],
                            "source_type": row["next_source_type"],
                        }
                candidate_rows = connection.execute(
                    f"""
                    SELECT k.*, ai.relation_type, ai.confidence, ai.rationale,
                           ai.simplified_content, ai.model AS ai_model, ai.analyzed_at
                    FROM candidates k
                    LEFT JOIN candidate_ai_analysis ai ON ai.candidate_id = k.id
                    WHERE k.clause_id IN ({placeholders})
                    ORDER BY k.clause_id, k.rank
                    """,
                    tuple(item["id"] for item in items),
                ).fetchall()
                by_clause = {item["id"]: item["candidates"] for item in items}
                for row in candidate_rows:
                    candidates = by_clause[row["clause_id"]]
                    if row["relation_type"] == "unrelated" and float(row["confidence"] or 0) >= 0.85:
                        items_by_id[row["clause_id"]]["excluded_candidate_count"] += 1
                        continue
                    if len(candidates) >= 3:
                        continue
                    candidate = dict(row)
                    candidate["reasons"] = json.loads(candidate.pop("reasons_json") or "[]")
                    candidate["warnings"] = json.loads(candidate.pop("warnings_json") or "[]")
                    relation_type = candidate.pop("relation_type")
                    confidence = candidate.pop("confidence")
                    rationale = candidate.pop("rationale")
                    simplified_content = candidate.pop("simplified_content")
                    ai_model = candidate.pop("ai_model")
                    analyzed_at = candidate.pop("analyzed_at")
                    candidate["ai_analysis"] = (
                        {
                            "relation_type": relation_type,
                            "confidence": confidence,
                            "rationale": rationale,
                            "simplified_content": simplified_content,
                            "model": ai_model,
                            "analyzed_at": analyzed_at,
                        }
                        if relation_type
                        else None
                    )
                    candidates.append(candidate)
            self._attach_clause_kcs_impacts(connection, items, full=False)
            return items, total

    def get_clause(self, project_id: str, clause_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            clause = connection.execute(
                "SELECT * FROM clauses WHERE id = ? AND project_id = ?",
                (clause_id, project_id),
            ).fetchone()
            if not clause:
                return None
            result = dict(clause)
            result["coverage_confirmed"] = bool(result["coverage_confirmed"])
            candidates = connection.execute(
                """
                SELECT k.*, ai.relation_type, ai.confidence, ai.rationale,
                       ai.simplified_content, ai.model AS ai_model, ai.analyzed_at
                FROM candidates k
                LEFT JOIN candidate_ai_analysis ai ON ai.candidate_id = k.id
                WHERE k.clause_id = ?
                ORDER BY k.rank
                """,
                (clause_id,),
            ).fetchall()
            result["candidates"] = []
            result["excluded_candidates"] = []
            for candidate in candidates:
                item = dict(candidate)
                item["reasons"] = json.loads(item.pop("reasons_json") or "[]")
                item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
                relation_type = item.pop("relation_type")
                ai_analysis = (
                    {
                        "relation_type": relation_type,
                        "confidence": item.pop("confidence"),
                        "rationale": item.pop("rationale"),
                        "simplified_content": item.pop("simplified_content"),
                        "model": item.pop("ai_model"),
                        "analyzed_at": item.pop("analyzed_at"),
                    }
                    if relation_type
                    else None
                )
                item["ai_analysis"] = ai_analysis
                if not relation_type:
                    item.pop("confidence")
                    item.pop("rationale")
                    item.pop("simplified_content")
                    item.pop("ai_model")
                    item.pop("analyzed_at")
                destination = (
                    "excluded_candidates"
                    if ai_analysis
                    and relation_type == "unrelated"
                    and float(ai_analysis["confidence"] or 0) >= 0.85
                    else "candidates"
                )
                result[destination].append(item)
            coverage = connection.execute(
                "SELECT * FROM clause_coverage_analysis WHERE clause_id = ?",
                (clause_id,),
            ).fetchone()
            result["coverage_analysis"] = None
            if coverage:
                coverage_item = dict(coverage)
                candidate_ids = json.loads(coverage_item.pop("candidate_ids_json") or "[]")
                visible_candidate_ids = [item["id"] for item in result["candidates"][:3]]
                if candidate_ids == visible_candidate_ids:
                    coverage_item["candidate_ids"] = candidate_ids
                    coverage_item["evidence_candidate_ids"] = json.loads(
                        coverage_item.pop("evidence_candidate_ids_json") or "[]"
                    )
                    coverage_item["requirements"] = json.loads(
                        coverage_item.pop("requirements_json") or "[]"
                    )
                    coverage_item["deletion_safe"] = bool(coverage_item["deletion_safe"])
                    result["coverage_analysis"] = coverage_item
            quality = connection.execute(
                """
                SELECT qi.sample_order, qi.cohort, qi.verdict,
                       qi.relevant_candidate_id, qi.expected_kcs_code,
                       qi.expected_kcs_clause, qi.quality_note,
                       qi.evaluated_at, qi.updated_at
                FROM quality_evaluation_items qi
                JOIN quality_evaluations qe ON qe.id = qi.evaluation_id
                WHERE qe.project_id = ? AND qi.clause_id = ?
                """,
                (project_id, clause_id),
            ).fetchone()
            result["quality_evaluation"] = dict(quality) if quality else None
            self._attach_clause_kcs_impacts(connection, [result], full=True)
            return result

    @classmethod
    def _acknowledge_kcs_impact_in_connection(
        cls,
        connection: sqlite3.Connection,
        project_id: str,
        clause_id: str,
        run_id: str,
        expected_project_revision: str,
        acknowledged_at: str,
    ) -> dict[str, Any]:
        if not run_id or not expected_project_revision:
            raise ValueError("KCS 영향 확인에는 작업 ID와 프로젝트 개정값이 필요합니다.")
        project = connection.execute(
            "SELECT kcs_revision FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not project:
            raise KCSRematchNotFoundError("검토 프로젝트를 찾을 수 없습니다.")
        _, current_revision = cls._current_kcs_target(connection)
        if (
            str(project["kcs_revision"] or "") != expected_project_revision
            or current_revision != expected_project_revision
        ):
            raise KCSRematchConflictError(
                "KCS 개정이 변경되었습니다. 최신 변경 영향을 다시 확인해 주세요."
            )
        run = connection.execute(
            """
            SELECT * FROM kcs_rematch_runs
            WHERE id = ? AND project_id = ?
            """,
            (run_id, project_id),
        ).fetchone()
        if not run:
            raise KCSRematchNotFoundError("KCS 재매칭 작업을 찾을 수 없습니다.")
        if run["status"] != "completed" or run["target_revision"] != expected_project_revision:
            raise KCSRematchConflictError("현재 프로젝트에 적용된 KCS 재매칭 작업이 아닙니다.")
        impact = connection.execute(
            """
            SELECT i.*, r.target_revision, r.status AS run_status,
                   r.queued_at, r.finished_at
            FROM kcs_clause_impacts i
            JOIN kcs_rematch_runs r ON r.id = i.run_id
            WHERE i.run_id = ? AND i.clause_id = ?
            """,
            (run_id, clause_id),
        ).fetchone()
        if not impact or not impact["review_required"]:
            raise KCSRematchNotFoundError("확인할 KCS 변경 영향이 없습니다.")
        if impact["acknowledged_at"] is None:
            connection.execute(
                """
                UPDATE kcs_clause_impacts
                SET acknowledged_at = ?
                WHERE run_id = ? AND clause_id = ?
                  AND review_required = 1 AND acknowledged_at IS NULL
                """,
                (acknowledged_at, run_id, clause_id),
            )
            impact = connection.execute(
                """
                SELECT i.*, r.target_revision, r.status AS run_status,
                       r.queued_at, r.finished_at
                FROM kcs_clause_impacts i
                JOIN kcs_rematch_runs r ON r.id = i.run_id
                WHERE i.run_id = ? AND i.clause_id = ?
                """,
                (run_id, clause_id),
            ).fetchone()
        return cls._public_kcs_impact(impact, full=True)

    def acknowledge_kcs_impact(
        self,
        project_id: str,
        clause_id: str,
        run_id: str,
        expected_project_revision: str,
    ) -> dict[str, Any]:
        acknowledged_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._acknowledge_kcs_impact_in_connection(
                connection,
                project_id,
                clause_id,
                run_id,
                expected_project_revision,
                acknowledged_at,
            )

    def update_decision(
        self,
        project_id: str,
        clause_id: str,
        decision: str | None,
        edited_content: str,
        review_note: str,
        selected_candidate_id: str | None,
        decision_reason: str = "",
        coverage_confirmed: bool = False,
        expected_kcs_revision: str = "",
        impact_run_id: str = "",
        acknowledge_kcs_impact: bool = False,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_review_editable(connection, project_id)
            previous = connection.execute(
                "SELECT * FROM clauses WHERE id = ? AND project_id = ?",
                (clause_id, project_id),
            ).fetchone()
            if not previous:
                return None
            self._write_decision(
                connection,
                previous,
                decision,
                edited_content,
                review_note,
                selected_candidate_id,
                decision_reason,
                coverage_confirmed,
            )
            if acknowledge_kcs_impact:
                self._acknowledge_kcs_impact_in_connection(
                    connection,
                    project_id,
                    clause_id,
                    impact_run_id,
                    expected_kcs_revision,
                    datetime.now(timezone.utc).isoformat(),
                )
        return self.get_clause(project_id, clause_id)

    def quick_update_decision(
        self,
        project_id: str,
        clause_id: str,
        changes: dict[str, Any],
        expected_kcs_revision: str = "",
        impact_run_id: str = "",
        acknowledge_kcs_impact: bool = False,
    ) -> dict[str, Any] | None:
        allowed = {
            "decision",
            "decision_reason",
            "coverage_confirmed",
            "selected_candidate_id",
        }
        if not changes or not set(changes).issubset(allowed):
            raise ValueError("변경할 빠른 검토 항목이 없습니다.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_review_editable(connection, project_id)
            previous = connection.execute(
                "SELECT * FROM clauses WHERE id = ? AND project_id = ?",
                (clause_id, project_id),
            ).fetchone()
            if not previous:
                return None
            self._write_decision(
                connection,
                previous,
                changes.get("decision", previous["decision"]),
                previous["edited_content"],
                previous["review_note"],
                changes.get("selected_candidate_id", previous["selected_candidate_id"]),
                changes.get("decision_reason", previous["decision_reason"]),
                changes.get("coverage_confirmed", bool(previous["coverage_confirmed"])),
            )
            if acknowledge_kcs_impact:
                self._acknowledge_kcs_impact_in_connection(
                    connection,
                    project_id,
                    clause_id,
                    impact_run_id,
                    expected_kcs_revision,
                    datetime.now(timezone.utc).isoformat(),
                )
        return self.get_clause(project_id, clause_id)

    @staticmethod
    def _coverage_analysis_snapshot(
        connection: sqlite3.Connection,
        clause_id: str,
    ) -> str:
        row = connection.execute(
            "SELECT * FROM clause_coverage_analysis WHERE clause_id = ?",
            (clause_id,),
        ).fetchone()
        snapshot = dict(row) if row else {}
        if row:
            try:
                candidate_ids = json.loads(snapshot.pop("candidate_ids_json") or "[]")
                evidence_ids = json.loads(
                    snapshot.pop("evidence_candidate_ids_json") or "[]"
                )
                requirements = json.loads(snapshot.pop("requirements_json") or "[]")
            except json.JSONDecodeError:
                candidate_ids = []
                evidence_ids = []
                requirements = []
        else:
            candidate_ids = []
            evidence_ids = []
            requirements = []
        evidence_rows: dict[str, dict[str, Any]] = {}
        if evidence_ids:
            placeholders = ",".join("?" for _ in evidence_ids)
            rows = connection.execute(
                f"""
                SELECT id, kcs_code, document_name, version, update_date,
                       kcs_clause, title, content, score, classification,
                       warnings_json
                FROM candidates
                WHERE clause_id = ? AND id IN ({placeholders})
                """,
                (clause_id, *evidence_ids),
            ).fetchall()
            for item in rows:
                candidate = dict(item)
                try:
                    candidate["warnings"] = json.loads(
                        candidate.pop("warnings_json") or "[]"
                    )
                except json.JSONDecodeError:
                    candidate["warnings"] = ["후보 경고 데이터를 해석할 수 없습니다."]
                evidence_rows[item["id"]] = candidate
        selected_row = connection.execute(
            """
            SELECT k.id, k.kcs_code, k.document_name, k.version, k.update_date,
                   k.kcs_clause, k.title, k.content, k.score, k.classification,
                   k.warnings_json, ai.relation_type, ai.confidence,
                   ai.rationale AS ai_rationale,
                   ai.simplified_content AS ai_simplified_content,
                   ai.model AS ai_model, ai.analyzed_at AS ai_analyzed_at
            FROM clauses c
            JOIN candidates k ON k.id = c.selected_candidate_id AND k.clause_id = c.id
            LEFT JOIN candidate_ai_analysis ai ON ai.candidate_id = k.id
            WHERE c.id = ?
            """,
            (clause_id,),
        ).fetchone()
        selected_candidate = dict(selected_row) if selected_row else None
        if selected_candidate:
            try:
                selected_candidate["warnings"] = json.loads(
                    selected_candidate.pop("warnings_json") or "[]"
                )
            except json.JSONDecodeError:
                selected_candidate["warnings"] = ["후보 경고 데이터를 해석할 수 없습니다."]

        if not row and not selected_candidate:
            return ""
        snapshot.update(
            {
                "snapshot_version": 1,
                "candidate_ids": candidate_ids,
                "evidence_candidate_ids": evidence_ids,
                "requirements": requirements,
                "deletion_safe": bool(snapshot.get("deletion_safe")),
                "evidence_candidates": [
                    evidence_rows[evidence_id]
                    for evidence_id in evidence_ids
                    if evidence_id in evidence_rows
                ],
                "selected_candidate": selected_candidate,
            }
        )
        return json.dumps(snapshot, ensure_ascii=False)

    @staticmethod
    def _write_decision(
        connection: sqlite3.Connection,
        previous: sqlite3.Row,
        decision: str | None,
        edited_content: str,
        review_note: str,
        selected_candidate_id: str | None,
        decision_reason: str,
        coverage_confirmed: bool,
    ) -> None:
        clause_id = previous["id"]
        project_id = previous["project_id"]
        decision_reason = decision_reason.strip()
        coverage_confirmed = bool(coverage_confirmed)

        if decision not in {*DECISION_REASONS, None}:
            raise ValueError("지원하지 않는 판정입니다.")
        if previous["source_type"] == "heading" and decision is not None:
            raise ValueError("목차와 구조 제목은 판정 대상이 아니며 하위 조항의 검색 문맥으로만 사용됩니다.")
        if decision is None:
            decision_reason = ""
            coverage_confirmed = False
        elif decision_reason not in DECISION_REASONS[decision]:
            raise ValueError("선택한 판정에 맞는 사유를 지정해 주세요.")
        elif decision != "delete":
            coverage_confirmed = False

        kcs_based_delete = decision == "delete" and decision_reason == KCS_DELETE_REASON
        no_kcs_match = decision == "keep" and decision_reason == "no_kcs_match"
        if decision == "delete" and not kcs_based_delete:
            coverage_confirmed = False
            selected_candidate_id = None
        if no_kcs_match:
            coverage_confirmed = False
            selected_candidate_id = None
        if (
            decision == "delete"
            and decision_reason == "management_decision"
            and not review_note.strip()
        ):
            raise ValueError("담당자 판단으로 삭제하려면 검토의견에 삭제 사유를 입력해 주세요.")

        if decision == "keep" and decision_reason == "partial_overlap_residual":
            residual = " ".join(edited_content.split())
            original = " ".join(
                str(previous["content"] or previous["title"] or "").split()
            )
            if not residual:
                raise ValueError("부분 중복 잔여기준을 남기려면 KCS 중복 내용을 제거한 문구를 입력해 주세요.")
            if residual == original:
                raise ValueError("부분 중복 잔여기준은 포스코 원문에서 KCS 중복 내용을 제거한 문구여야 합니다.")

        selected_candidate = None
        if selected_candidate_id:
            selected_candidate = connection.execute(
                """
                SELECT k.title, k.content, k.warnings_json,
                       ai.relation_type, ai.confidence
                FROM candidates k
                LEFT JOIN candidate_ai_analysis ai ON ai.candidate_id = k.id
                WHERE k.id = ? AND k.clause_id = ?
                """,
                (selected_candidate_id, clause_id),
            ).fetchone()
            if not selected_candidate:
                raise ValueError("선택한 KCS 후보가 현재 조항에 속하지 않습니다.")
            if (
                selected_candidate["relation_type"] == "unrelated"
                and float(selected_candidate["confidence"] or 0) >= 0.85
            ):
                raise ValueError("관련성 낮음으로 제외된 KCS 후보는 판정 근거로 선택할 수 없습니다.")

        if kcs_based_delete:
            if not selected_candidate:
                raise ValueError("삭제하려면 전체 요구사항을 포함하는 KCS 근거를 먼저 선택해 주세요.")
            candidate_title = " ".join(str(selected_candidate["title"] or "").split())
            candidate_content = " ".join(str(selected_candidate["content"] or "").split())
            if not candidate_content or candidate_content == candidate_title:
                raise ValueError("제목만 있는 KCS 후보는 삭제 근거로 사용할 수 없습니다.")
            if not coverage_confirmed:
                raise ValueError("삭제 전 포스코 요구사항 전체가 KCS에 포함되는지 확인해 주세요.")
            try:
                candidate_warnings = json.loads(selected_candidate["warnings_json"] or "[]")
            except json.JSONDecodeError:
                candidate_warnings = ["후보 경고 데이터를 확인할 수 없습니다."]
            if candidate_warnings:
                raise ValueError("수치·의무·금지 표현 차이 경고가 있는 후보로는 삭제할 수 없습니다.")
            relation_type = selected_candidate["relation_type"]
            confidence = float(selected_candidate["confidence"] or 0)
            if relation_type and (
                relation_type not in SAFE_DELETE_RELATIONS or confidence < 0.85
            ):
                raise ValueError(
                    "GPT 개별분석이 신뢰도 85% 이상으로 전체 포괄 판정한 KCS 후보만 삭제 근거로 사용할 수 있습니다."
                )
            coverage_analysis = connection.execute(
                """
                SELECT ca.deletion_safe, ca.evidence_candidate_ids_json,
                       EXISTS(
                           SELECT 1
                           FROM json_each(ca.evidence_candidate_ids_json) evidence
                           JOIN candidate_ai_analysis ai
                             ON ai.candidate_id = evidence.value
                           WHERE ai.relation_type NOT IN ('equivalent', 'kcs_covers')
                              OR ai.confidence < 0.85
                       ) AS unsafe_ai_evidence
                FROM clause_coverage_analysis ca
                WHERE ca.clause_id = ?
                """,
                (clause_id,),
            ).fetchone()
            if coverage_analysis:
                try:
                    evidence_candidate_ids = json.loads(
                        coverage_analysis["evidence_candidate_ids_json"] or "[]"
                    )
                except json.JSONDecodeError:
                    evidence_candidate_ids = []
                if (
                    not coverage_analysis["deletion_safe"]
                    or selected_candidate_id not in evidence_candidate_ids
                    or coverage_analysis["unsafe_ai_evidence"]
                ):
                    raise ValueError(
                        "GPT 전체포괄 분석에서 모든 요구사항의 대체 근거가 확인된 경우에만 삭제할 수 있습니다."
                    )

        changed_at = datetime.now(timezone.utc).isoformat()
        values_changed = any(
            [
                previous["decision"] != decision,
                previous["edited_content"] != edited_content,
                previous["review_note"] != review_note,
                previous["decision_reason"] != decision_reason,
                bool(previous["coverage_confirmed"]) != coverage_confirmed,
                previous["selected_candidate_id"] != selected_candidate_id,
            ]
        )
        connection.execute(
            """
            UPDATE clauses
            SET decision = ?, edited_content = ?, review_note = ?,
                decision_reason = ?, coverage_confirmed = ?,
                selected_candidate_id = ?, reviewed_at = ?
            WHERE id = ? AND project_id = ?
            """,
            (
                decision,
                edited_content,
                review_note,
                decision_reason,
                int(coverage_confirmed),
                selected_candidate_id,
                changed_at if decision else None,
                clause_id,
                project_id,
            ),
        )
        if values_changed:
            coverage_analysis_json = Store._coverage_analysis_snapshot(
                connection,
                clause_id,
            )
            connection.execute(
                """
                INSERT INTO decision_history(
                    clause_id, previous_decision, new_decision, edited_content,
                    review_note, decision_reason, coverage_confirmed,
                    selected_candidate_id, coverage_analysis_json, changed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clause_id,
                    previous["decision"],
                    decision,
                    edited_content,
                    review_note,
                    decision_reason,
                    int(coverage_confirmed),
                    selected_candidate_id,
                    coverage_analysis_json,
                    changed_at,
                ),
            )

    def get_candidate_context(
        self,
        project_id: str,
        clause_id: str,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT c.id AS clause_id, c.label AS posco_label,
                       c.title AS posco_title, c.content AS posco_content,
                       k.id AS candidate_id, k.kcs_code, k.document_name,
                       k.version, k.update_date, k.kcs_clause,
                       k.title AS kcs_title, k.content AS kcs_content,
                       k.score, k.classification,
                       ai.relation_type, ai.confidence, ai.rationale,
                       ai.simplified_content, ai.model, ai.analyzed_at
                FROM clauses c
                JOIN candidates k ON k.clause_id = c.id
                LEFT JOIN candidate_ai_analysis ai ON ai.candidate_id = k.id
                WHERE c.project_id = ? AND c.id = ? AND k.id = ?
                """,
                (project_id, clause_id, candidate_id),
            ).fetchone()
            return dict(row) if row else None

    def get_coverage_context(
        self,
        project_id: str,
        clause_id: str,
    ) -> dict[str, Any] | None:
        clause = self.get_clause(project_id, clause_id)
        if not clause:
            return None
        return {
            "clause_id": clause["id"],
            "source_type": clause["source_type"],
            "posco_label": clause["label"],
            "posco_title": clause["title"],
            "posco_content": clause["content"],
            "candidates": clause["candidates"][:3],
            "coverage_analysis": clause.get("coverage_analysis"),
        }

    def save_coverage_analysis(
        self,
        project_id: str,
        clause_id: str,
        candidate_ids: list[str],
        analysis: dict[str, Any],
        model: str,
        *,
        expected_posco_text: str | None = None,
        expected_candidate_texts: list[str] | None = None,
    ) -> dict[str, Any] | None:
        coverage_status = str(analysis.get("coverage_status", ""))
        if coverage_status not in COVERAGE_STATUSES:
            raise ValueError("지원하지 않는 GPT 전체 포괄 판정입니다.")
        try:
            confidence = float(analysis.get("confidence", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("GPT 전체 포괄 신뢰도 값이 올바르지 않습니다.") from exc
        if not 0 <= confidence <= 1:
            raise ValueError("GPT 전체 포괄 신뢰도는 0과 1 사이여야 합니다.")
        rationale = str(analysis.get("rationale", "")).strip()
        residual_content = str(analysis.get("residual_content", "")).strip()
        raw_requirements = analysis.get("requirements")
        if not rationale or not isinstance(raw_requirements, list) or not raw_requirements:
            raise ValueError("GPT 전체 포괄 근거 또는 요구사항 분석이 비어 있습니다.")

        normalized_requirements: list[dict[str, Any]] = []
        evidence_ids: list[str] = []
        assigned_source_ids: list[str] = []
        allowed_candidate_ids = set(candidate_ids)
        for raw_requirement in raw_requirements:
            if not isinstance(raw_requirement, dict):
                raise ValueError("GPT 요구사항 분석 형식이 올바르지 않습니다.")
            requirement = str(raw_requirement.get("requirement", "")).strip()
            raw_source_ids = raw_requirement.get("source_segment_ids")
            status = str(raw_requirement.get("status", ""))
            evidence = str(raw_requirement.get("evidence", "")).strip()
            raw_ids = raw_requirement.get("evidence_candidate_ids")
            if not isinstance(raw_source_ids, list) or not isinstance(raw_ids, list):
                raise ValueError("GPT 요구사항 근거 후보 형식이 올바르지 않습니다.")
            requirement_source_ids = [str(value) for value in raw_source_ids]
            requirement_ids = list(dict.fromkeys(str(value) for value in raw_ids))
            if (
                not requirement
                or not requirement_source_ids
                or len(requirement_source_ids) != 1
                or status not in REQUIREMENT_STATUSES
                or not set(requirement_ids).issubset(allowed_candidate_ids)
                or (status == "covered" and (not requirement_ids or not evidence))
            ):
                raise ValueError("GPT 요구사항별 판정 값이 허용 범위를 벗어났습니다.")
            assigned_source_ids.extend(requirement_source_ids)
            evidence_ids.extend(requirement_ids)
            normalized_requirements.append(
                {
                    "requirement": requirement,
                    "source_segment_ids": requirement_source_ids,
                    "status": status,
                    "evidence_candidate_ids": requirement_ids,
                    "evidence": evidence,
                }
            )

        evidence_ids = list(dict.fromkeys(evidence_ids))
        all_covered = all(item["status"] == "covered" for item in normalized_requirements)
        if coverage_status == "fully_covered" and (not all_covered or residual_content):
            raise ValueError("GPT 전체 포괄 판정과 요구사항별 결과가 서로 모순됩니다.")
        if coverage_status != "fully_covered" and all_covered:
            raise ValueError("GPT 부분 포괄 판정과 요구사항별 결과가 서로 모순됩니다.")

        analyzed_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_review_editable(connection, project_id)
            clause = connection.execute(
                "SELECT * FROM clauses WHERE id = ? AND project_id = ?",
                (clause_id, project_id),
            ).fetchone()
            if not clause:
                return None
            if clause["source_type"] == "heading":
                raise ValueError("목차와 구조 제목은 전체 포괄 분석 대상이 아닙니다.")
            current_posco_text = (
                str(clause["content"] or "").strip()
                or str(clause["title"] or "").strip()
            )
            source_segments = coverage_source_segments(current_posco_text)
            if expected_posco_text is not None and source_segments != coverage_source_segments(
                expected_posco_text
            ):
                raise ValueError(
                    "포스코 원문이 GPT 분석 중 변경되었습니다. 현재 원문으로 다시 분석해 주세요."
                )
            expected_source_ids = [source_id for source_id, _ in source_segments]
            if (
                len(assigned_source_ids) != len(expected_source_ids)
                or len(set(assigned_source_ids)) != len(assigned_source_ids)
                or set(assigned_source_ids) != set(expected_source_ids)
            ):
                raise ValueError(
                    "GPT가 포스코 원문 구간 일부를 누락하거나 중복해 삭제 판정에 사용할 수 없습니다."
                )
            source_text_by_id = dict(source_segments)
            for requirement in normalized_requirements:
                requirement["requirement"] = source_text_by_id[
                    requirement["source_segment_ids"][0]
                ]
            source_order = {
                source_id: index for index, source_id in enumerate(expected_source_ids)
            }
            normalized_requirements.sort(
                key=lambda item: source_order[item["source_segment_ids"][0]]
            )
            candidate_rows = connection.execute(
                """
                SELECT k.id, k.kcs_code, k.document_name, k.kcs_clause,
                       k.title, k.content, k.warnings_json,
                       ai.relation_type, ai.confidence
                FROM candidates k
                LEFT JOIN candidate_ai_analysis ai ON ai.candidate_id = k.id
                WHERE k.clause_id = ?
                  AND NOT EXISTS(
                      SELECT 1 FROM candidate_ai_analysis excluded
                      WHERE excluded.candidate_id = k.id
                        AND excluded.relation_type = 'unrelated'
                        AND excluded.confidence >= 0.85
                  )
                ORDER BY k.rank
                LIMIT 3
                """,
                (clause_id,),
            ).fetchall()
            current_candidate_ids = [row["id"] for row in candidate_rows]
            if current_candidate_ids != candidate_ids:
                raise ValueError("KCS 후보가 변경되었습니다. 최신 후보로 다시 분석해 주세요.")
            if expected_candidate_texts is not None:
                current_candidate_texts = [
                    "\n".join(
                        part
                        for part in (
                            str(row["kcs_code"] or "").strip(),
                            str(row["document_name"] or "").strip(),
                            str(row["kcs_clause"] or "").strip(),
                            str(row["title"] or "").strip(),
                            str(row["content"] or "").strip(),
                        )
                        if part
                    )
                    for row in candidate_rows
                ]
                if current_candidate_texts != expected_candidate_texts:
                    raise ValueError(
                        "KCS 후보 내용이 GPT 분석 중 변경되었습니다. 최신 후보로 다시 분석해 주세요."
                    )
            rows_by_id = {row["id"]: row for row in candidate_rows}
            warning_free = True
            individually_safe = True
            for evidence_id in evidence_ids:
                row = rows_by_id.get(evidence_id)
                if not row:
                    warning_free = False
                    individually_safe = False
                    break
                try:
                    warnings = json.loads(row["warnings_json"] or "[]")
                except json.JSONDecodeError:
                    warnings = ["invalid"]
                if warnings:
                    warning_free = False
                if row["relation_type"] and (
                    row["relation_type"] not in SAFE_DELETE_RELATIONS
                    or float(row["confidence"] or 0) < 0.85
                ):
                    individually_safe = False
            deletion_safe = bool(
                coverage_status == "fully_covered"
                and confidence >= 0.85
                and all_covered
                and evidence_ids
                and warning_free
                and individually_safe
            )
            connection.execute(
                """
                INSERT INTO clause_coverage_analysis(
                    clause_id, candidate_ids_json, evidence_candidate_ids_json,
                    coverage_status, confidence, requirements_json,
                    residual_content, rationale, deletion_safe, model, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(clause_id) DO UPDATE SET
                    candidate_ids_json = excluded.candidate_ids_json,
                    evidence_candidate_ids_json = excluded.evidence_candidate_ids_json,
                    coverage_status = excluded.coverage_status,
                    confidence = excluded.confidence,
                    requirements_json = excluded.requirements_json,
                    residual_content = excluded.residual_content,
                    rationale = excluded.rationale,
                    deletion_safe = excluded.deletion_safe,
                    model = excluded.model,
                    analyzed_at = excluded.analyzed_at
                """,
                (
                    clause_id,
                    json.dumps(candidate_ids, ensure_ascii=False),
                    json.dumps(evidence_ids, ensure_ascii=False),
                    coverage_status,
                    confidence,
                    json.dumps(normalized_requirements, ensure_ascii=False),
                    residual_content,
                    rationale,
                    int(deletion_safe),
                    model,
                    analyzed_at,
                ),
            )
            if bool(clause["coverage_confirmed"]):
                connection.execute(
                    "UPDATE clauses SET coverage_confirmed = 0 WHERE id = ? AND project_id = ?",
                    (clause_id, project_id),
                )
                connection.execute(
                    """
                    INSERT INTO decision_history(
                        clause_id, previous_decision, new_decision, edited_content,
                        review_note, decision_reason, coverage_confirmed,
                        selected_candidate_id, coverage_analysis_json, changed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        clause_id,
                        clause["decision"],
                        clause["decision"],
                        clause["edited_content"],
                        clause["review_note"],
                        clause["decision_reason"],
                        clause["selected_candidate_id"],
                        self._coverage_analysis_snapshot(connection, clause_id),
                        analyzed_at,
                    ),
                )
        return self.get_clause(project_id, clause_id)

    def save_candidate_analysis(
        self,
        project_id: str,
        clause_id: str,
        candidate_id: str,
        analysis: dict[str, Any],
        model: str,
    ) -> dict[str, Any] | None:
        relation_type = str(analysis.get("relation_type", ""))
        valid_relations = {
            "equivalent",
            "kcs_covers",
            "partial_overlap",
            "posco_specific",
            "conflict",
            "unrelated",
        }
        if relation_type not in valid_relations:
            raise ValueError("지원하지 않는 GPT 관계 판정입니다.")
        try:
            confidence = float(analysis.get("confidence", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("GPT 신뢰도 값이 올바르지 않습니다.") from exc
        if not 0 <= confidence <= 1:
            raise ValueError("GPT 신뢰도는 0과 1 사이여야 합니다.")
        rationale = str(analysis.get("rationale", "")).strip()
        if not rationale:
            raise ValueError("GPT 판정 근거가 비어 있습니다.")

        analyzed_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_review_editable(connection, project_id)
            candidate = connection.execute(
                """
                SELECT (c.selected_candidate_id = k.id) AS business_selected,
                       EXISTS(
                           SELECT 1
                           FROM quality_evaluation_items qi
                           JOIN quality_evaluations qe ON qe.id = qi.evaluation_id
                           WHERE qe.project_id = c.project_id
                             AND qi.clause_id = c.id
                             AND qi.verdict = 'candidate_selected'
                             AND qi.relevant_candidate_id = k.id
                       ) AS quality_selected,
                       EXISTS(
                           SELECT 1
                           FROM quality_evaluation_items qi
                           JOIN quality_evaluations qe ON qe.id = qi.evaluation_id
                           WHERE qe.project_id = c.project_id AND qi.clause_id = c.id
                       ) AS quality_sample
                FROM candidates k
                JOIN clauses c ON c.id = k.clause_id
                WHERE c.project_id = ? AND c.id = ? AND k.id = ?
                """,
                (project_id, clause_id, candidate_id),
            ).fetchone()
            if not candidate:
                return None
            if (
                relation_type == "unrelated"
                and confidence >= 0.85
                and candidate["quality_sample"]
            ):
                raise ValueError(
                    "50개 품질평가 표본의 후보군은 고정되어 있어 자동 제외할 수 없습니다."
                )
            if relation_type == "unrelated" and (
                candidate["business_selected"] or candidate["quality_selected"]
            ):
                raise ValueError(
                    "담당자가 이미 선택한 후보는 GPT 판정만으로 제외할 수 없습니다. "
                    "선택을 먼저 해제한 뒤 다시 분석하세요."
                )
            confirmation_reset = self._invalidate_coverage_analysis(connection, clause_id)
            connection.execute(
                """
                INSERT INTO candidate_ai_analysis(
                    candidate_id, relation_type, confidence, rationale,
                    simplified_content, model, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    relation_type = excluded.relation_type,
                    confidence = excluded.confidence,
                    rationale = excluded.rationale,
                    simplified_content = excluded.simplified_content,
                    model = excluded.model,
                    analyzed_at = excluded.analyzed_at
                """,
                (
                    candidate_id,
                    relation_type,
                    confidence,
                    rationale,
                    str(analysis.get("simplified_content", "")).strip(),
                    model,
                    analyzed_at,
                ),
            )
            self._record_coverage_confirmation_reset(
                connection,
                confirmation_reset,
                analyzed_at,
            )
        return self.get_clause(project_id, clause_id)

    def clear_candidate_analysis(
        self,
        project_id: str,
        clause_id: str,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_review_editable(connection, project_id)
            candidate = connection.execute(
                """
                SELECT ai.relation_type, ai.confidence,
                       EXISTS(
                           SELECT 1
                           FROM quality_evaluation_items qi
                           JOIN quality_evaluations qe ON qe.id = qi.evaluation_id
                           WHERE qe.project_id = c.project_id AND qi.clause_id = c.id
                       ) AS quality_sample
                FROM candidates k
                JOIN clauses c ON c.id = k.clause_id
                LEFT JOIN candidate_ai_analysis ai ON ai.candidate_id = k.id
                WHERE c.project_id = ? AND c.id = ? AND k.id = ?
                """,
                (project_id, clause_id, candidate_id),
            ).fetchone()
            if not candidate:
                return None
            if candidate["quality_sample"]:
                raise ValueError(
                    "50개 품질평가 표본의 후보군은 고정되어 있어 제외 판정을 복원할 수 없습니다."
                )
            if (
                candidate["relation_type"] != "unrelated"
                or float(candidate["confidence"] or 0) < 0.85
            ):
                raise ValueError("GPT가 관련성 낮음으로 제외한 KCS 후보만 복원할 수 있습니다.")
            changed_at = datetime.now(timezone.utc).isoformat()
            confirmation_reset = self._invalidate_coverage_analysis(connection, clause_id)
            connection.execute(
                "DELETE FROM candidate_ai_analysis WHERE candidate_id = ?",
                (candidate_id,),
            )
            self._record_coverage_confirmation_reset(
                connection,
                confirmation_reset,
                changed_at,
            )
        return self.get_clause(project_id, clause_id)

    def ensure_quality_evaluation(
        self,
        project_id: str,
        reviewer_name: str = "담당자",
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM quality_evaluations WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if existing:
                evaluation_id = existing["id"]
            else:
                self._assert_review_editable(connection, project_id)
                project = connection.execute(
                    "SELECT source_sha256, kcs_snapshot FROM projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
                if not project:
                    return None
                rows = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT c.id, c.source_order
                        FROM clauses c
                        WHERE c.project_id = ? AND c.source_type != 'heading'
                        ORDER BY c.source_order
                        """,
                        (project_id,),
                    ).fetchall()
                ]
                for row in rows:
                    row["candidates_snapshot"] = self._quality_candidates_snapshot(
                        connection,
                        row["id"],
                    )
                    row["candidate_count"] = len(row["candidates_snapshot"])
                    row["top_score"] = max(
                        (
                            float(candidate.get("score") or 0)
                            for candidate in row["candidates_snapshot"]
                        ),
                        default=None,
                    )
                    if row["candidate_count"] == 0:
                        row["cohort"] = "no_candidate"
                    elif float(row["top_score"] or 0) >= 0.45:
                        row["cohort"] = "high"
                    else:
                        row["cohort"] = "review"

                selected = [
                    *_spaced_sample([row for row in rows if row["cohort"] == "high"], 12),
                    *_spaced_sample([row for row in rows if row["cohort"] == "review"], 23),
                    *_spaced_sample([row for row in rows if row["cohort"] == "no_candidate"], 15),
                ]
                selected_ids = {row["id"] for row in selected}
                if len(selected) < 50:
                    remaining = [row for row in rows if row["id"] not in selected_ids]
                    selected.extend(_spaced_sample(remaining, 50 - len(selected)))
                selected = sorted(selected[:50], key=lambda row: row["source_order"])

                sample_seed = f"{project['source_sha256']}:{project['kcs_snapshot']}:stratified-v1"
                evaluation_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"spec-quality:{project_id}:{sample_seed}")
                )
                connection.execute(
                    """
                    INSERT INTO quality_evaluations(
                        id, project_id, population_size, sample_seed,
                        reviewer_name, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evaluation_id,
                        project_id,
                        len(rows),
                        sample_seed,
                        reviewer_name.strip() or "담당자",
                        now,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO quality_evaluation_items(
                        evaluation_id, clause_id, sample_order, cohort,
                        candidates_snapshot_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            evaluation_id,
                            row["id"],
                            index,
                            row["cohort"],
                            json.dumps(row["candidates_snapshot"], ensure_ascii=False),
                        )
                        for index, row in enumerate(selected, start=1)
                    ],
                )
        return self.get_quality_evaluation(project_id)

    def get_quality_evaluation(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            evaluation = connection.execute(
                "SELECT * FROM quality_evaluations WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if not evaluation:
                return None
            items = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT qi.sample_order, qi.cohort, qi.verdict,
                           qi.relevant_candidate_id, qi.expected_kcs_code,
                           qi.expected_kcs_clause, qi.quality_note,
                           qi.evaluated_at, qi.updated_at,
                           qi.candidates_snapshot_json,
                           qi.relevant_candidate_key,
                           qi.relevant_rank_snapshot,
                           c.id AS clause_id, c.source_order, c.label, c.title,
                           c.source_type
                    FROM quality_evaluation_items qi
                    JOIN quality_evaluations qe ON qe.id = qi.evaluation_id
                    JOIN clauses c ON c.id = qi.clause_id
                    WHERE qe.project_id = ?
                    ORDER BY qi.sample_order
                    """,
                    (project_id,),
                ).fetchall()
            ]
            result = dict(evaluation)
        for item in items:
            candidates = _parse_quality_candidates_snapshot(
                item.pop("candidates_snapshot_json", "[]")
            )
            item["candidate_count"] = len(candidates)
            scores: list[float] = []
            for candidate in candidates:
                try:
                    scores.append(float(candidate.get("score") or 0))
                except (TypeError, ValueError):
                    continue
            item["top_score"] = max(scores, default=None)
            relevant_rank = item.pop("relevant_rank_snapshot", None)
            relevant_key = item.pop("relevant_candidate_key", "") or ""
            if relevant_rank is None and relevant_key:
                selected = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.get("candidate_key") == relevant_key
                    ),
                    None,
                )
                relevant_rank = selected.get("rank") if selected else None
            if relevant_rank is None and item.get("relevant_candidate_id"):
                selected = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.get("id") == item["relevant_candidate_id"]
                    ),
                    None,
                )
                relevant_rank = selected.get("rank") if selected else None
            item["relevant_rank"] = relevant_rank
        result["actual_sample_size"] = len(items)
        result["evaluated_count"] = sum(item["verdict"] is not None for item in items)
        result["items"] = items
        result["metrics"] = self._quality_metrics(items)
        result["insights"] = self._quality_insights(items, result["metrics"]["complete"])
        result.pop("sample_seed", None)
        return result

    @staticmethod
    def _quality_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
        verdicts = (
            "candidate_selected",
            "all_candidates_incorrect",
            "no_candidate_correct",
            "kcs_missing",
        )
        counts = {
            verdict: sum(item["verdict"] == verdict for item in items)
            for verdict in verdicts
        }
        rank_hits = {
            str(rank): sum(
                item["verdict"] == "candidate_selected" and item.get("relevant_rank") == rank
                for item in items
            )
            for rank in (1, 2, 3)
        }
        positive_count = counts["candidate_selected"] + counts["kcs_missing"]
        reciprocal_sum = sum(
            1 / int(item["relevant_rank"])
            for item in items
            if item["verdict"] == "candidate_selected" and item.get("relevant_rank")
        )
        evaluated_count = sum(value for value in counts.values())
        return {
            "sample_size": len(items),
            "evaluated_count": evaluated_count,
            "remaining_count": len(items) - evaluated_count,
            "complete": bool(items) and evaluated_count == len(items),
            "counts": counts,
            "rank_hits": rank_hits,
            "recall_at_3": round(counts["candidate_selected"] / positive_count, 4)
            if positive_count else None,
            "mrr": round(reciprocal_sum / positive_count, 4) if positive_count else None,
            "accuracy": round(
                (counts["candidate_selected"] + counts["no_candidate_correct"])
                / evaluated_count,
                4,
            ) if evaluated_count else None,
        }

    @staticmethod
    def _score_band(item: dict[str, Any]) -> str:
        if not item.get("candidate_count"):
            return "no_candidate"
        score = float(item.get("top_score") or 0)
        if score >= 0.45:
            return "gte_0_45"
        if score >= 0.35:
            return "0_35_to_0_45"
        return "0_25_to_0_35"

    @classmethod
    def _quality_insights(
        cls,
        items: list[dict[str, Any]],
        complete: bool,
    ) -> dict[str, Any]:
        evaluated = [item for item in items if item.get("verdict") is not None]
        verdicts = (
            "candidate_selected",
            "all_candidates_incorrect",
            "no_candidate_correct",
            "kcs_missing",
        )

        def grouped(keys: tuple[str, ...], key_for_item) -> dict[str, dict[str, Any]]:
            result: dict[str, dict[str, Any]] = {}
            for key in keys:
                sample_members = [item for item in items if key_for_item(item) == key]
                members = [item for item in evaluated if key_for_item(item) == key]
                counts = {
                    verdict: sum(item["verdict"] == verdict for item in members)
                    for verdict in verdicts
                }
                correct = counts["candidate_selected"] + counts["no_candidate_correct"]
                result[key] = {
                    "sample_count": len(sample_members),
                    "evaluated_count": len(members),
                    "correct_count": correct,
                    "sample_accuracy": round(correct / len(members), 4) if members else None,
                    "verdict_counts": counts,
                }
            return result

        rank_2 = sum(
            item["verdict"] == "candidate_selected" and item.get("relevant_rank") == 2
            for item in evaluated
        )
        rank_3 = sum(
            item["verdict"] == "candidate_selected" and item.get("relevant_rank") == 3
            for item in evaluated
        )
        all_incorrect = sum(
            item["verdict"] == "all_candidates_incorrect" for item in evaluated
        )
        kcs_missing = sum(item["verdict"] == "kcs_missing" for item in evaluated)
        kcs_missing_with_candidates = sum(
            item["verdict"] == "kcs_missing" and bool(item.get("candidate_count"))
            for item in evaluated
        )
        kcs_missing_without_candidates = kcs_missing - kcs_missing_with_candidates
        signal_total = rank_2 + rank_3 + all_incorrect + kcs_missing
        evaluated_count = len(evaluated)
        if not evaluated_count:
            status = "collecting"
            message = "아직 평가 결과가 없습니다. 표본 조항을 판정해 품질평가를 시작하세요."
        elif complete:
            status = "complete"
            message = "전체 표본 평가가 완료되었습니다. 집계값은 담당자 판정 결과입니다."
        else:
            status = "partial"
            message = "평가가 진행 중입니다. 현재 집계값은 판정이 끝난 표본만 반영합니다."
        return {
            "status": status,
            "message": message,
            "scope_notice": (
                "층화표본의 평가 완료 항목만 집계한 값으로 프로젝트 전체 정확도 추정치가 "
                "아니며, 매칭 임계값을 자동 변경하거나 권고하지 않습니다."
            ),
            "evaluated_count": evaluated_count,
            "cohorts": grouped(("high", "review", "no_candidate"), lambda item: item["cohort"]),
            "score_bands": grouped(
                ("gte_0_45", "0_35_to_0_45", "0_25_to_0_35", "no_candidate"),
                cls._score_band,
            ),
            "error_signals": {
                "candidate_selected_rank_2": rank_2,
                "candidate_selected_rank_3": rank_3,
                "candidate_selected_rank_2_or_3": rank_2 + rank_3,
                "all_candidates_incorrect": all_incorrect,
                "kcs_missing": kcs_missing,
                "kcs_missing_with_candidates": kcs_missing_with_candidates,
                "kcs_missing_without_candidates": kcs_missing_without_candidates,
                "total": signal_total,
                "rate": round(signal_total / evaluated_count, 4) if evaluated_count else None,
            },
        }

    def update_quality_item(
        self,
        project_id: str,
        clause_id: str,
        verdict: str | None,
        relevant_candidate_id: str | None,
        expected_kcs_code: str,
        expected_kcs_clause: str,
        quality_note: str,
    ) -> dict[str, Any] | None:
        evaluation = self.ensure_quality_evaluation(project_id)
        if not evaluation:
            return None
        evaluation_id = evaluation["id"]
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_review_editable(connection, project_id)
            item = connection.execute(
                """
                SELECT qi.*
                FROM quality_evaluation_items qi
                WHERE qi.evaluation_id = ? AND qi.clause_id = ?
                """,
                (evaluation_id, clause_id),
            ).fetchone()
            if not item:
                return None

            candidates = _parse_quality_candidates_snapshot(
                item["candidates_snapshot_json"]
            )
            candidate_count = len(candidates)
            relevant_candidate_key = ""
            relevant_rank_snapshot: int | None = None
            if verdict == "candidate_selected":
                if not relevant_candidate_id:
                    raise ValueError("적정한 KCS 후보를 선택하세요.")
                requested_candidate_id = relevant_candidate_id
                selected = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.get("id") == requested_candidate_id
                    ),
                    None,
                )
                if not selected:
                    raise ValueError("선택한 후보가 이 표본 조항에 속하지 않습니다.")
                relevant_candidate_key = str(selected.get("candidate_key") or "")
                try:
                    relevant_rank_snapshot = int(selected.get("rank"))
                except (TypeError, ValueError):
                    relevant_rank_snapshot = None
                live_candidate = connection.execute(
                    "SELECT 1 FROM candidates WHERE id = ? AND clause_id = ?",
                    (requested_candidate_id, clause_id),
                ).fetchone()
                relevant_candidate_id = requested_candidate_id if live_candidate else None
                expected_kcs_code = ""
                expected_kcs_clause = ""
            elif verdict == "all_candidates_incorrect":
                if candidate_count == 0:
                    raise ValueError("제시 후보가 없는 조항에서는 이 판정을 사용할 수 없습니다.")
                relevant_candidate_id = None
                expected_kcs_code = ""
                expected_kcs_clause = ""
            elif verdict == "no_candidate_correct":
                if candidate_count != 0:
                    raise ValueError("후보가 있는 조항에서는 '후보 없음이 적정'을 선택할 수 없습니다.")
                relevant_candidate_id = None
                expected_kcs_code = ""
                expected_kcs_clause = ""
            elif verdict == "kcs_missing":
                if not expected_kcs_code.strip():
                    raise ValueError("누락된 KCS 코드를 입력하세요.")
                relevant_candidate_id = None
            elif verdict is None:
                relevant_candidate_id = None
                expected_kcs_code = ""
                expected_kcs_clause = ""
                quality_note = ""
            else:
                raise ValueError("지원하지 않는 매칭 품질 판정입니다.")

            connection.execute(
                """
                UPDATE quality_evaluation_items
                SET verdict = ?, relevant_candidate_id = ?,
                    relevant_candidate_key = ?, relevant_rank_snapshot = ?,
                    expected_kcs_code = ?, expected_kcs_clause = ?,
                    quality_note = ?, evaluated_at = ?, updated_at = ?
                WHERE evaluation_id = ? AND clause_id = ?
                """,
                (
                    verdict,
                    relevant_candidate_id,
                    relevant_candidate_key,
                    relevant_rank_snapshot,
                    expected_kcs_code.strip(),
                    expected_kcs_clause.strip(),
                    quality_note.strip(),
                    now if verdict else None,
                    now,
                    evaluation_id,
                    clause_id,
                ),
            )
            remaining = connection.execute(
                """
                SELECT COUNT(*)
                FROM quality_evaluation_items
                WHERE evaluation_id = ? AND verdict IS NULL
                """,
                (evaluation_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE quality_evaluations SET completed_at = ? WHERE id = ?",
                (now if remaining == 0 else None, evaluation_id),
            )
        return self.get_quality_evaluation(project_id)

    def quality_export_rows(self, project_id: str) -> list[dict[str, Any]]:
        evaluation = self.get_quality_evaluation(project_id)
        if not evaluation:
            return []
        rows = {item["clause_id"]: dict(item) for item in evaluation["items"]}
        with self.connect() as connection:
            clause_rows = connection.execute(
                """
                SELECT c.id, c.content, qi.candidates_snapshot_json
                FROM quality_evaluation_items qi
                JOIN quality_evaluations qe ON qe.id = qi.evaluation_id
                JOIN clauses c ON c.id = qi.clause_id
                WHERE qe.project_id = ?
                """,
                (project_id,),
            ).fetchall()
            for clause in clause_rows:
                rows[clause["id"]]["content"] = clause["content"]
                rows[clause["id"]]["candidates"] = _parse_quality_candidates_snapshot(
                    clause["candidates_snapshot_json"]
                )

        result = sorted(rows.values(), key=lambda item: item["sample_order"])
        for item in result:
            item["score_band"] = self._score_band(item)
        return result

    def export_clauses(
        self,
        project_id: str,
        decisions: tuple[str, ...],
        *,
        include_unreviewed: bool = False,
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in decisions)
        decision_filter = f"c.decision IN ({placeholders})"
        if include_unreviewed:
            decision_filter = f"({decision_filter} OR c.decision IS NULL)"
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*,
                       k.kcs_code, k.document_name AS kcs_document_name,
                       k.kcs_clause, k.version AS kcs_version
                FROM clauses c
                LEFT JOIN candidates k ON k.id = c.selected_candidate_id
                WHERE c.project_id = ? AND c.source_type != 'heading'
                  AND {decision_filter}
                ORDER BY c.source_order
                """,
                (project_id, *decisions),
            ).fetchall()
            return [dict(row) for row in rows]

    def final_export_bundle(
        self,
        project_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """Validate readiness and fetch final rows from one SQLite snapshot."""
        with self.connect() as connection:
            connection.execute("BEGIN")
            row = self._project_summary_row(connection, project_id)
            if not row:
                return None
            project = self._add_export_readiness(connection, dict(row))
            if project["final_export_ready"]:
                approved_submission = connection.execute(
                    """
                    SELECT * FROM project_review_submissions
                    WHERE project_id = ? AND status = 'approved'
                    ORDER BY revision_no DESC
                    LIMIT 1
                    """,
                    (project_id,),
                ).fetchone()
                if not approved_submission:
                    project["final_export_ready"] = False
                    project["final_export_blockers"].append("승인 기록 확인 필요")
                else:
                    live_snapshot = self._review_snapshot(connection, project_id)
                    _, live_sha256 = self._canonical_review_snapshot(live_snapshot)
                    if live_sha256 != approved_submission["review_snapshot_sha256"]:
                        project["final_export_ready"] = False
                        project["final_export_blockers"].append(
                            "승인 후 검토 내용 변경 감지"
                        )
            clauses = connection.execute(
                """
                SELECT c.*,
                       k.kcs_code, k.document_name AS kcs_document_name,
                       k.kcs_clause, k.version AS kcs_version
                FROM clauses c
                LEFT JOIN candidates k ON k.id = c.selected_candidate_id
                WHERE c.project_id = ? AND c.source_type != 'heading'
                  AND c.decision = 'keep'
                ORDER BY c.source_order
                """,
                (project_id,),
            ).fetchall()
            return project, [dict(item) for item in clauses]

    def kcs_rematch_audit_rows(self, project_id: str) -> list[dict[str, Any]]:
        """Return immutable run and impact snapshots for the project audit workbook."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.id AS run_id, r.project_id, r.from_revision,
                       r.target_revision, r.target_snapshot, r.status,
                       r.expected_state_sha256, r.matcher_signature_json,
                       r.total_clauses, r.matched_count,
                       r.material_change_count, r.review_required_count,
                       r.error, r.queued_at, r.started_at, r.finished_at,
                       (SELECT COUNT(*) FROM kcs_clause_impacts pending
                        WHERE pending.run_id = r.id
                          AND pending.review_required = 1
                          AND pending.acknowledged_at IS NULL
                       ) AS unacknowledged_count,
                       i.clause_id, i.impact_type,
                       i.review_required AS impact_review_required,
                       i.reason_json, i.before_candidates_json,
                       i.after_candidates_json, i.before_decision_json,
                       i.acknowledged_at,
                       c.source_order, c.label, c.title, c.content
                FROM kcs_rematch_runs r
                LEFT JOIN kcs_clause_impacts i ON i.run_id = r.id
                LEFT JOIN clauses c ON c.id = i.clause_id
                WHERE r.project_id = ?
                ORDER BY r.queued_at, r.id, c.source_order, i.clause_id
                """,
                (project_id,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            run_keys = (
                "run_id",
                "project_id",
                "from_revision",
                "target_revision",
                "target_snapshot",
                "status",
                "expected_state_sha256",
                "matcher_signature_json",
                "total_clauses",
                "matched_count",
                "material_change_count",
                "review_required_count",
                "unacknowledged_count",
                "error",
                "queued_at",
                "started_at",
                "finished_at",
            )
            for row in rows:
                raw = dict(row)
                run = self._public_kcs_rematch_run(
                    {key: raw.get(key) for key in run_keys}
                )
                item = {
                    **run,
                    "clause_id": raw.get("clause_id"),
                    "source_order": raw.get("source_order"),
                    "label": raw.get("label"),
                    "title": raw.get("title"),
                    "content": raw.get("content"),
                }
                if raw.get("clause_id"):
                    impact = self._public_kcs_impact(
                        {
                            "run_id": raw.get("run_id"),
                            "clause_id": raw.get("clause_id"),
                            "impact_type": raw.get("impact_type"),
                            "review_required": raw.get("impact_review_required"),
                            "reason_json": raw.get("reason_json"),
                            "before_candidates_json": raw.get(
                                "before_candidates_json"
                            ),
                            "after_candidates_json": raw.get("after_candidates_json"),
                            "before_decision_json": raw.get("before_decision_json"),
                            "acknowledged_at": raw.get("acknowledged_at"),
                        },
                        full=True,
                    )
                    item.update(impact)
                else:
                    item.update(
                        {
                            "impact_type": None,
                            "review_required": False,
                            "reason": "",
                            "reasons": [],
                            "before_candidates": [],
                            "after_candidates": [],
                            "before_decision": {},
                            "acknowledged_at": None,
                        }
                    )
                result.append(item)
            return result

    def audit_rows(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.source_order, c.label, c.title, c.content, c.decision,
                       COALESCE(h.edited_content, c.edited_content) AS edited_content,
                       COALESCE(h.review_note, c.review_note) AS review_note,
                       COALESCE(h.decision_reason, c.decision_reason) AS decision_reason,
                       COALESCE(h.coverage_confirmed, c.coverage_confirmed) AS coverage_confirmed,
                       c.reviewed_at,
                       k.kcs_code, k.document_name AS kcs_document_name,
                       k.kcs_clause, k.score,
                       ai.relation_type AS ai_relation_type,
                       ai.confidence AS ai_confidence,
                       ai.rationale AS ai_rationale,
                       ai.simplified_content AS ai_simplified_content,
                       h.id AS history_id, h.previous_decision, h.new_decision, h.changed_at,
                       h.coverage_analysis_json
                FROM clauses c
                LEFT JOIN decision_history h ON h.clause_id = c.id
                LEFT JOIN candidates k ON k.id = CASE
                    WHEN h.id IS NULL THEN c.selected_candidate_id
                    ELSE h.selected_candidate_id
                END
                LEFT JOIN candidate_ai_analysis ai ON ai.candidate_id = k.id AND h.id IS NULL
                WHERE c.project_id = ?
                ORDER BY c.source_order, h.changed_at
                """,
                (project_id,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                history_id = item.pop("history_id", None)
                raw_snapshot = item.pop("coverage_analysis_json", "") or ""
                try:
                    parsed_snapshot = json.loads(raw_snapshot) if raw_snapshot else {}
                    coverage_snapshot = parsed_snapshot if isinstance(parsed_snapshot, dict) else {}
                except json.JSONDecodeError:
                    coverage_snapshot = {}
                selected_candidate = coverage_snapshot.get("selected_candidate")
                history_candidate = (
                    selected_candidate if isinstance(selected_candidate, dict) else None
                )
                if history_id is not None and history_candidate:
                    item.update(
                        {
                            "kcs_code": history_candidate.get("kcs_code"),
                            "kcs_document_name": history_candidate.get("document_name"),
                            "kcs_clause": history_candidate.get("kcs_clause"),
                            "score": history_candidate.get("score"),
                            "ai_relation_type": history_candidate.get("relation_type"),
                            "ai_confidence": history_candidate.get("confidence"),
                            "ai_rationale": history_candidate.get("ai_rationale"),
                            "ai_simplified_content": history_candidate.get(
                                "ai_simplified_content"
                            ),
                        }
                    )
                evidence_candidates = coverage_snapshot.get("evidence_candidates") or []
                if not isinstance(evidence_candidates, list):
                    evidence_candidates = []
                candidates_by_id = {
                    str(candidate.get("id")): candidate
                    for candidate in evidence_candidates
                    if isinstance(candidate, dict) and candidate.get("id")
                }
                requirements = coverage_snapshot.get("requirements") or []
                if not isinstance(requirements, list):
                    requirements = []
                requirement_lines: list[str] = []
                for index, requirement in enumerate(requirements, start=1):
                    if not isinstance(requirement, dict):
                        continue
                    evidence_labels = []
                    for evidence_id in requirement.get("evidence_candidate_ids") or []:
                        candidate = candidates_by_id.get(str(evidence_id))
                        if not candidate:
                            continue
                        evidence_labels.append(
                            " ".join(
                                part
                                for part in (
                                    str(candidate.get("kcs_code") or "").strip(),
                                    str(candidate.get("kcs_clause") or "").strip(),
                                )
                                if part
                            )
                        )
                    source_ids = ", ".join(
                        str(value) for value in requirement.get("source_segment_ids") or []
                    )
                    requirement_lines.append(
                        f"{index}. {source_ids or '원문'} [{requirement.get('status') or ''}] "
                        f"{requirement.get('requirement') or ''} | 근거 KCS: "
                        f"{'; '.join(evidence_labels) or '없음'} | "
                        f"{requirement.get('evidence') or ''}"
                    )
                item.update(
                    {
                        "coverage_status": coverage_snapshot.get("coverage_status"),
                        "coverage_confidence": coverage_snapshot.get("confidence"),
                        "coverage_deletion_safe": coverage_snapshot.get("deletion_safe"),
                        "coverage_evidence_kcs": ", ".join(
                            " · ".join(
                                part
                                for part in (
                                    str(candidate.get("kcs_code") or "").strip(),
                                    str(candidate.get("kcs_clause") or "").strip(),
                                    str(candidate.get("document_name") or "").strip(),
                                    str(candidate.get("version") or "").strip(),
                                )
                                if part
                            )
                            for candidate in evidence_candidates
                            if isinstance(candidate, dict)
                        ),
                        "coverage_requirements": "\n".join(requirement_lines),
                        "coverage_requirements_json": json.dumps(
                            requirements, ensure_ascii=False
                        ) if coverage_snapshot else "",
                        "coverage_residual_content": coverage_snapshot.get(
                            "residual_content"
                        ),
                        "coverage_rationale": coverage_snapshot.get("rationale"),
                        "coverage_model": coverage_snapshot.get("model"),
                        "coverage_analyzed_at": coverage_snapshot.get("analyzed_at"),
                    }
                )
                result.append(item)
            return result
