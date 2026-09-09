from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.header import decode_header, make_header
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .backups import BackupError, BackupManager, BackupNotFoundError
from .config import get_settings
from .documents import (
    build_audit_xlsx,
    build_quality_evaluation_xlsx,
    build_review_docx,
    convert_legacy_doc,
    legacy_doc_conversion_engine,
    parse_docx,
    safe_filename,
    sha256_bytes,
)
from .kcs_sync import kcs_read_lock, sync_kcs
from .matcher import (
    activate_matcher_revision,
    build_scope_sample,
    clear_matcher_caches,
    infer_scope,
    match_clauses,
    read_snapshot,
)
from .openai_ai import OpenAIAPIError, OpenAIClient
from .storage import (
    CURRENT_PARSER_VERSION,
    DATABASE_SCHEMA_VERSION,
    ClauseStructureConflictError,
    ClauseStructureNotFoundError,
    KCSRematchConflictError,
    KCSRematchNotFoundError,
    ReviewWorkflowConflictError,
    ReviewWorkflowNotFoundError,
    Store,
    UploadJobConflictError,
    UploadJobNotFoundError,
)


MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_BATCH_FILES = 200
MAX_BATCH_BYTES = 250 * 1024 * 1024
MAX_BACKUP_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
logger = logging.getLogger(__name__)
settings = get_settings()
store = Store(settings.database_path)
ai_client = OpenAIClient.from_settings(settings)
_rematch_worker_task: asyncio.Task[None] | None = None
_rematch_wakeup: asyncio.Event | None = None
_rematch_shutdown_requested = False
_upload_worker_task: asyncio.Task[None] | None = None
_upload_wakeup: asyncio.Event | None = None
_upload_shutdown_requested = False
_mutation_request_lock = asyncio.Lock()


def _read_current_kcs_target() -> tuple[str, str, dict]:
    snapshot, manifest = read_snapshot(settings.kcs_manifest_path)
    revision = str(manifest.get("revision") or "")
    if not snapshot or not revision:
        raise RuntimeError("현재 KCS 개정 정보를 확인할 수 없습니다.")
    return snapshot, revision, manifest


def _reconcile_current_kcs_revision() -> tuple[str, dict]:
    snapshot, revision, manifest = _read_current_kcs_target()
    activate_matcher_revision(revision)
    store.set_current_kcs_revision(snapshot, revision)
    return snapshot, manifest


try:
    _reconcile_current_kcs_revision()
except (RuntimeError, ValueError):
    pass


@asynccontextmanager
async def _app_lifespan(_application: FastAPI):
    await _resume_kcs_rematches()
    await _resume_upload_jobs()
    try:
        yield
    finally:
        await _stop_upload_jobs()
        await _stop_kcs_rematches()

app = FastAPI(
    title="포스코 시방서 정합성 검토 API",
    version="0.1.0",
    description="포스코 시방서와 최신 KCS의 검토 후보 및 담당자 판정을 관리합니다.",
    lifespan=_app_lifespan,
)

DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://posco-spec-manager.yeongrangrang.chatgpt.site",
    "https://poscoconstruction.vercel.app",
]


def _cors_origins_from_env() -> list[str]:
    raw = os.getenv("SPEC_MANAGER_CORS_ORIGINS", "")
    configured = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return [*DEFAULT_CORS_ORIGINS, *configured]


def _upload_filename(upload: UploadFile, fallback: str) -> str:
    filename = upload.filename or fallback
    if filename.startswith("=?") and "?=" in filename:
        try:
            return str(make_header(decode_header(filename)))
        except (LookupError, UnicodeDecodeError, ValueError):
            return filename
    return filename


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins_from_env(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def serialize_mutating_requests(request: Request, call_next):
    path = request.url.path
    mutating_get = request.method == "GET" and (
        path == "/api/config"
        or path.endswith("/quality-evaluation")
        or path.endswith("/export/final")
    )
    if request.method in {"GET", "HEAD", "OPTIONS"} and not mutating_get:
        return await call_next(request)
    # Local mode uses one backend process. Serializing state-changing requests
    # prevents an in-flight review write from landing after a restore swap.
    async with _mutation_request_lock:
        return await call_next(request)


class DecisionUpdate(BaseModel):
    decision: Literal["keep", "delete", "hold"] | None = None
    edited_content: str = Field(default="", max_length=20000)
    review_note: str = Field(default="", max_length=5000)
    decision_reason: str = Field(default="", max_length=100)
    coverage_confirmed: bool = False
    selected_candidate_id: str | None = None
    expected_kcs_revision: str = Field(default="", max_length=128)
    impact_run_id: str = Field(default="", max_length=128)
    acknowledge_kcs_impact: bool = False


class QuickReviewUpdate(BaseModel):
    decision: Literal["keep", "delete", "hold"] | None = None
    decision_reason: str = Field(default="", max_length=100)
    coverage_confirmed: bool = False
    selected_candidate_id: str | None = Field(default=None, min_length=1, max_length=200)
    expected_kcs_revision: str = Field(default="", max_length=128)
    impact_run_id: str = Field(default="", max_length=128)
    acknowledge_kcs_impact: bool = False


class QualityEvaluationStart(BaseModel):
    reviewer_name: str = Field(default="담당자", max_length=100)


class QualityItemUpdate(BaseModel):
    verdict: Literal[
        "candidate_selected",
        "all_candidates_incorrect",
        "no_candidate_correct",
        "kcs_missing",
    ] | None = None
    relevant_candidate_id: str | None = None
    expected_kcs_code: str = Field(default="", max_length=100)
    expected_kcs_clause: str = Field(default="", max_length=500)
    quality_note: str = Field(default="", max_length=5000)


class CandidateAnalysisRequest(BaseModel):
    refresh: bool = False


class ClauseMergeRequest(BaseModel):
    clause_ids: list[str] = Field(min_length=2, max_length=2)


class ClauseSplitRequest(BaseModel):
    parts: list[str] = Field(min_length=2, max_length=2)


class ProjectArchiveUpdate(BaseModel):
    archived: bool


class ReviewSubmitRequest(BaseModel):
    author_name: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=5000)


class ReviewApprovalRequest(BaseModel):
    approver_name: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=5000)


class ReviewChangesRequest(BaseModel):
    approver_name: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=5000)


class BackupRestoreRequest(BaseModel):
    confirmation: Literal["복구"]


def _project_or_404(project_id: str):
    project = store.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="검토 프로젝트를 찾을 수 없습니다.")
    return project


def _public_project(project: dict):
    hidden = {"source_path", "source_sha256", "_review_technical_blockers"}
    return {key: value for key, value in project.items() if key not in hidden}


def _ensure_project_review_unlocked(project: dict) -> None:
    if project.get("review_locked") or project.get("status") in {"submitted", "approved"}:
        raise HTTPException(
            status_code=409,
            detail="승인 요청 중이거나 승인 완료된 프로젝트는 검토 내용을 변경할 수 없습니다.",
        )
    if project.get("requires_source_reupload"):
        raise HTTPException(
            status_code=409,
            detail="새 문서 해석기를 적용하려면 같은 원본 시방서를 한 번 다시 업로드해 주세요.",
        )
    run = project.get("kcs_rematch")
    if project.get("kcs_stale") or (
        run and run.get("status") in {"pending", "running", "failed", "superseded"}
    ):
        raise HTTPException(
            status_code=409,
            detail="최신 KCS 영향 분석과 자동 재매칭을 완료한 뒤 판정을 저장해 주세요.",
        )


def _download_response(data: bytes, filename: str, media_type: str) -> StreamingResponse:
    encoded = quote(filename, safe="")
    return StreamingResponse(
        io.BytesIO(data),
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
            "Cache-Control": "no-store",
        },
    )


def _backup_manager() -> BackupManager:
    raw_retention = os.getenv("SPEC_MANAGER_BACKUP_RETENTION", "20")
    try:
        retention = int(raw_retention)
    except ValueError:
        retention = 20
    return BackupManager(
        store,
        settings.data_dir / "backups",
        settings.uploads_dir,
        settings.kcs_data_dir,
        retention=retention,
    )


def _ensure_backup_maintenance_idle() -> None:
    active_uploads = store.active_upload_item_count()
    active_rematches = store.active_kcs_rematch_count()
    if active_uploads or active_rematches:
        parts = []
        if active_uploads:
            parts.append(f"업로드 분석 {active_uploads}건")
        if active_rematches:
            parts.append(f"KCS 재매칭 {active_rematches}건")
        raise HTTPException(
            status_code=409,
            detail=f"{' · '.join(parts)}이 진행 중입니다. 작업이 끝난 뒤 다시 시도해 주세요.",
        )


def _create_backup_consistent(reason: str = "manual") -> dict:
    with kcs_read_lock():
        with store.maintenance():
            _ensure_backup_maintenance_idle()
            return _backup_manager().create(reason)


def _restore_backup_consistent(
    *,
    backup_id: str = "",
    archive_path: Path | None = None,
) -> dict:
    with kcs_read_lock():
        try:
            snapshot, revision, _manifest = _read_current_kcs_target()
        except (RuntimeError, ValueError) as exc:
            raise BackupError(
                "최신 KCS 상태를 확인할 수 없어 복구를 시작하지 않았습니다. KCS를 먼저 갱신해 주세요."
            ) from exc

        def prepare_database(database_path: Path) -> None:
            restored_store = Store(database_path)
            restored_store.set_current_kcs_revision(snapshot, revision)
            restored_store.recover_interrupted_upload_jobs()
            restored_store.recover_interrupted_kcs_rematches()
            with restored_store.connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            Path(f"{database_path}-wal").unlink(missing_ok=True)
            Path(f"{database_path}-shm").unlink(missing_ok=True)

        with store.maintenance():
            _ensure_backup_maintenance_idle()
            manager = _backup_manager()
            result = (
                manager.restore_archive(
                    archive_path,
                    prepare_database=prepare_database,
                )
                if archive_path is not None
                else manager.restore(
                    backup_id,
                    prepare_database=prepare_database,
                )
            )
        activate_matcher_revision(revision)
        return result


async def _stage_uploaded_backup(
    file: UploadFile,
    manager: BackupManager,
    *,
    prefix: str,
) -> Path:
    filename = file.filename or "backup.zip"
    if Path(filename).suffix.lower() != ".zip":
        raise HTTPException(status_code=400, detail="ZIP 백업 파일만 사용할 수 있습니다.")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=prefix,
            suffix=".zip",
            dir=manager.backups_dir,
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            total = 0
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BACKUP_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="백업 파일 크기는 2GB 이하여야 합니다.",
                    )
                output.write(chunk)
        if not total:
            raise HTTPException(status_code=400, detail="빈 백업 파일은 사용할 수 없습니다.")
        return temporary_path
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


async def _rematch_clause_structure(project: dict, prepared: dict) -> dict:
    replacements = prepared["replacements"]
    for replacement in replacements:
        replacement["id"] = str(uuid.uuid4())

    sample_text = build_scope_sample(replacements)
    prefixes, _ = infer_scope(
        str(project.get("source_filename") or ""),
        str(project.get("title") or ""),
        sample_text,
        settings.kcs_raw_dir,
    )

    def match_consistent_snapshot() -> str:
        with kcs_read_lock():
            _, manifest = read_snapshot(settings.kcs_manifest_path)
            revision = str(manifest.get("revision") or "")
            if revision != prepared["expected_kcs_revision"]:
                raise ClauseStructureConflictError(
                    "KCS 개정이 변경되어 구조 편집을 중단했습니다. 문서를 다시 업로드해 주세요."
                )
            activate_matcher_revision(revision)
            match_clauses(
                replacements,
                settings.kcs_raw_dir,
                prefixes,
                ai_client,
            )
            return revision

    matched_revision = await asyncio.to_thread(match_consistent_snapshot)
    return await asyncio.to_thread(
        store.apply_clause_structure_edit,
        prepared["project_id"],
        prepared["operation"],
        prepared["source_clause_ids"],
        prepared["expected_fingerprint"],
        matched_revision,
        replacements,
    )


def _raise_structure_error(exc: ValueError) -> None:
    if isinstance(exc, ClauseStructureNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, (ClauseStructureConflictError, ReviewWorkflowConflictError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def _raise_kcs_rematch_error(exc: ValueError) -> None:
    if isinstance(exc, KCSRematchNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, KCSRematchConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def _pending_kcs_rematch_run_ids() -> list[str]:
    return store.pending_kcs_rematch_run_ids()


def _ensure_kcs_refresh_has_no_active_uploads() -> None:
    active_count = store.active_upload_item_count()
    if active_count:
        raise RuntimeError(
            f"시방서 {active_count}개가 분석 대기 또는 처리 중입니다. "
            "업로드 작업이 끝난 뒤 KCS를 갱신해 주세요."
        )


def _execute_kcs_rematch(run_id: str) -> None:
    """Match against one immutable KCS snapshot, then atomically publish results."""
    worker_client: OpenAIClient | None = None
    expected_state_sha256 = ""
    try:
        worker_client = OpenAIClient.from_settings(settings)
        matcher_signature = {
            "matcher": "hybrid-kcs-v3-domain-context",
            "requested_mode": "strict_openai_embeddings",
            "require_embeddings": True,
            "embedding_model": worker_client.embedding_model,
            "embedding_dimensions": worker_client.embedding_dimensions,
            "candidate_limit": 3,
        }
        prepared = store.claim_kcs_rematch(run_id, matcher_signature)
        expected_state_sha256 = prepared["expected_state_sha256"]
        run = prepared["run"]
        project = prepared["project"]
        clauses = prepared["clauses"]
        sample_text = build_scope_sample(clauses)
        prefixes, _ = infer_scope(
            str(project.get("source_filename") or ""),
            str(project.get("title") or ""),
            sample_text,
            settings.kcs_raw_dir,
        )

        with kcs_read_lock():
            snapshot, manifest = read_snapshot(settings.kcs_manifest_path)
            revision = str(manifest.get("revision") or "")
            if (
                revision != str(run.get("target_revision") or "")
                or snapshot != str(run.get("target_snapshot") or "")
            ):
                raise KCSRematchConflictError(
                    "재매칭 중 KCS 개정이 변경되었습니다. 최신 작업으로 다시 시도해 주세요."
                )
            activate_matcher_revision(revision)
            match_clauses(
                clauses,
                settings.kcs_raw_dir,
                prefixes,
                worker_client,
                require_embeddings=True,
            )

        store.apply_kcs_rematch(
            run_id,
            expected_state_sha256,
            clauses,
            matcher_signature,
        )
    except Exception as exc:
        if expected_state_sha256:
            try:
                store.fail_kcs_rematch(
                    run_id,
                    str(exc) or "KCS 자동 재매칭 중 오류가 발생했습니다.",
                    expected_state_sha256,
                )
            except (KCSRematchConflictError, KCSRematchNotFoundError):
                pass
    finally:
        if worker_client is not None:
            try:
                worker_client.close()
            except Exception:
                pass


async def _run_kcs_rematch_worker() -> None:
    global _rematch_wakeup
    while True:
        wakeup = _rematch_wakeup
        if wakeup is None:
            return
        await wakeup.wait()
        wakeup.clear()
        if _rematch_shutdown_requested:
            return
        attempted: set[str] = set()
        while True:
            run_ids = [
                run_id
                for run_id in await asyncio.to_thread(_pending_kcs_rematch_run_ids)
                if run_id not in attempted
            ]
            if not run_ids:
                break
            for run_id in run_ids:
                attempted.add(run_id)
                try:
                    await asyncio.to_thread(_execute_kcs_rematch, run_id)
                except Exception:
                    # A per-run failure must not strand other queued projects.
                    continue


def _kick_kcs_rematch_worker() -> None:
    global _rematch_worker_task, _rematch_wakeup, _rematch_shutdown_requested
    _rematch_shutdown_requested = False
    if _rematch_wakeup is None:
        _rematch_wakeup = asyncio.Event()
    if _rematch_worker_task is None or _rematch_worker_task.done():
        _rematch_worker_task = asyncio.create_task(_run_kcs_rematch_worker())
    _rematch_wakeup.set()


async def _resume_kcs_rematches() -> None:
    await asyncio.to_thread(store.recover_interrupted_kcs_rematches)
    _kick_kcs_rematch_worker()


async def _stop_kcs_rematches() -> None:
    global _rematch_worker_task, _rematch_wakeup, _rematch_shutdown_requested
    _rematch_shutdown_requested = True
    task = _rematch_worker_task
    if _rematch_wakeup is not None:
        _rematch_wakeup.set()
    if task and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=15)
        except TimeoutError:
            task.cancel()
        except asyncio.CancelledError:
            pass
    _rematch_worker_task = None
    _rematch_wakeup = None


def _cleanup_upload_artifacts(source_path: Path, project_id: str) -> None:
    upload_root = settings.uploads_dir.resolve()
    candidates = {source_path, source_path.with_suffix(".docx")}
    for candidate in candidates:
        resolved = candidate.resolve()
        if (
            resolved.parent == upload_root
            and resolved.stem == project_id
            and resolved.suffix.lower() in {".doc", ".docx"}
        ):
            resolved.unlink(missing_ok=True)


def _process_uploaded_source(
    filename: str,
    source_path: Path,
    project_id: str,
    client: OpenAIClient,
    *,
    expected_sha256: str = "",
    expected_kcs_snapshot: str = "",
    expected_kcs_revision: str = "",
    require_embeddings: bool = False,
    progress: Callable[[int, str], None] | None = None,
) -> dict:
    report = progress or (lambda _value, _phase: None)
    data = source_path.read_bytes()
    if expected_sha256 and sha256_bytes(data) != expected_sha256:
        raise ValueError("대기 중인 업로드 원본이 변경되어 처리를 중단했습니다.")
    suffix = source_path.suffix.lower()
    converted_path = source_path.with_suffix(".docx") if suffix == ".doc" else None
    try:
        if converted_path:
            report(10, "converting")
            docx_data = convert_legacy_doc(source_path, converted_path)
        else:
            docx_data = data
        report(20, "parsing")
        title, clauses, warnings = parse_docx(docx_data, filename, project_id)
        if converted_path:
            warnings.insert(0, "구형 DOC 원본을 분석 서버에서 DOCX로 변환해 분석했습니다.")
        sample_text = build_scope_sample(clauses)
        prefixes, scope_display = infer_scope(
            filename,
            title,
            sample_text,
            settings.kcs_raw_dir,
        )
        report(40, "matching")
        with kcs_read_lock():
            snapshot, manifest = read_snapshot(settings.kcs_manifest_path)
            revision = str(manifest.get("revision") or "")
            if expected_kcs_snapshot and (
                snapshot != expected_kcs_snapshot
                or revision != expected_kcs_revision
            ):
                raise RuntimeError(
                    "배치 접수 후 KCS 개정이 변경되어 다른 기준과 섞이지 않도록 처리를 중단했습니다."
                )
            activate_matcher_revision(revision)
            match_clauses(
                clauses,
                settings.kcs_raw_dir,
                prefixes,
                client,
                require_embeddings=require_embeddings,
            )
            if not require_embeddings and client.available and not client.embeddings_available:
                warnings.append(
                    "OpenAI 임베딩 호출에 실패하여 이번 파일은 로컬 TF-IDF + BM25 검색으로 자동 전환했습니다."
                )
            report(90, "saving")
            project = {
                "id": project_id,
                "title": title,
                "source_filename": filename,
                "source_path": str(source_path),
                "source_sha256": sha256_bytes(data),
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "kcs_snapshot": snapshot,
                "kcs_revision": revision,
                "kcs_scope": scope_display,
                "warning": " ".join(warnings),
                "parser_version": CURRENT_PARSER_VERSION,
            }
            store.create_project(project, clauses)
        return project
    finally:
        if converted_path:
            converted_path.unlink(missing_ok=True)


def _execute_upload_item(item_id: str) -> None:
    item: dict | None = None
    worker_client: OpenAIClient | None = None
    try:
        item = store.claim_upload_item(item_id)
        project_id = str(item["project_id"])
        if store.get_project(project_id):
            store.complete_upload_item(item_id)
            return
        worker_client = OpenAIClient.from_settings(settings)
        _process_uploaded_source(
            str(item["original_filename"]),
            Path(item["source_path"]),
            project_id,
            worker_client,
            expected_sha256=str(item["source_sha256"]),
            expected_kcs_snapshot=str(item["kcs_snapshot"]),
            expected_kcs_revision=str(item["kcs_revision"]),
            require_embeddings=True,
            progress=lambda value, phase: store.update_upload_item_progress(
                item_id, value, phase
            ),
        )
        store.complete_upload_item(item_id)
    except (UploadJobConflictError, UploadJobNotFoundError):
        return
    except Exception as exc:
        if item is not None:
            project_id = str(item["project_id"])
            if store.get_project(project_id):
                try:
                    store.complete_upload_item(item_id)
                except (UploadJobConflictError, UploadJobNotFoundError):
                    pass
            else:
                retryable = isinstance(exc, OpenAIAPIError)
                if not retryable:
                    _cleanup_upload_artifacts(Path(item["source_path"]), project_id)
                try:
                    store.fail_upload_item(
                        item_id,
                        str(exc) or "업로드 파일 처리 중 오류가 발생했습니다.",
                        retryable=retryable,
                    )
                except (UploadJobConflictError, UploadJobNotFoundError):
                    pass
    finally:
        if worker_client is not None:
            try:
                worker_client.close()
            except Exception:
                pass


async def _run_upload_worker() -> None:
    global _upload_wakeup
    while True:
        wakeup = _upload_wakeup
        if wakeup is None:
            return
        await wakeup.wait()
        wakeup.clear()
        if _upload_shutdown_requested:
            return
        attempted: set[str] = set()
        while True:
            item_ids = [
                item_id
                for item_id in await asyncio.to_thread(store.pending_upload_item_ids)
                if item_id not in attempted
            ]
            if not item_ids:
                break
            for item_id in item_ids:
                attempted.add(item_id)
                try:
                    await asyncio.to_thread(_execute_upload_item, item_id)
                except Exception as exc:
                    logger.exception(
                        "업로드 작업 파일 처리기가 예기치 않게 중단되었습니다: %s",
                        item_id,
                    )
                    try:
                        await asyncio.to_thread(
                            store.fail_upload_item,
                            item_id,
                            str(exc) or "내부 저장 오류로 처리가 중단되었습니다.",
                            retryable=True,
                        )
                    except (UploadJobConflictError, UploadJobNotFoundError):
                        pass
                    except Exception:
                        logger.exception(
                            "중단된 업로드 작업 상태를 기록하지 못했습니다: %s",
                            item_id,
                        )


def _kick_upload_worker() -> None:
    global _upload_worker_task, _upload_wakeup, _upload_shutdown_requested
    _upload_shutdown_requested = False
    if _upload_wakeup is None:
        _upload_wakeup = asyncio.Event()
    if _upload_worker_task is None or _upload_worker_task.done():
        _upload_worker_task = asyncio.create_task(_run_upload_worker())
    _upload_wakeup.set()


async def _resume_upload_jobs() -> None:
    await asyncio.to_thread(store.recover_interrupted_upload_jobs)
    _kick_upload_worker()


async def _stop_upload_jobs() -> None:
    global _upload_worker_task, _upload_wakeup, _upload_shutdown_requested
    _upload_shutdown_requested = True
    task = _upload_worker_task
    if _upload_wakeup is not None:
        _upload_wakeup.set()
    if task and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=15)
        except TimeoutError:
            task.cancel()
        except asyncio.CancelledError:
            pass
    _upload_worker_task = None
    _upload_wakeup = None


@app.get("/api/health")
def health():
    doc_conversion_engine = legacy_doc_conversion_engine()
    return {
        "status": "ok",
        "kcs_available": settings.kcs_raw_dir.is_dir(),
        "openai_available": ai_client.available,
        "doc_upload_available": bool(doc_conversion_engine),
        "doc_conversion_engine": doc_conversion_engine,
    }


@app.get("/api/config")
async def config():
    doc_conversion_engine = legacy_doc_conversion_engine()
    ai_config = {
        "openai_available": ai_client.available,
        "openai_embeddings_available": ai_client.embeddings_available,
        "openai_embedding_model": settings.openai_embedding_model,
        "openai_embedding_dimensions": settings.openai_embedding_dimensions,
        "openai_rerank_model": settings.openai_rerank_model,
        "doc_upload_available": bool(doc_conversion_engine),
        "doc_conversion_engine": doc_conversion_engine,
    }
    try:
        snapshot, manifest = _reconcile_current_kcs_revision()
        _kick_kcs_rematch_worker()
        return {
            "kcs_available": settings.kcs_raw_dir.is_dir(),
            "kcs_snapshot": snapshot,
            "kcs_revision": manifest.get("revision", ""),
            "kcs_document_count": manifest.get("documentCount", 0),
            "kcs_usable_document_count": manifest.get(
                "usableDocumentCount", manifest.get("documentCount", 0)
            ),
            "kcs_unavailable_document_count": manifest.get(
                "unavailableDocumentCount", 0
            ),
            "latest_only": bool(manifest.get("latestOnly")),
            **ai_config,
        }
    except RuntimeError as exc:
        return {"kcs_available": False, "error": str(exc), **ai_config}


@app.get("/api/system/backups")
def list_system_backups():
    return {"backups": _backup_manager().list()}


@app.post("/api/system/backups", status_code=201)
async def create_system_backup():
    _ensure_backup_maintenance_idle()
    try:
        backup = await asyncio.to_thread(_create_backup_consistent)
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"backup": backup}


@app.get("/api/system/backups/{backup_id}/download")
def download_system_backup(backup_id: str):
    try:
        path = _backup_manager().download_path(backup_id)
    except BackupNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"포스코시방서_백업_{backup_id}.zip",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/system/backups/{backup_id}/restore")
async def restore_system_backup(
    backup_id: str,
    _payload: BackupRestoreRequest,
):
    _ensure_backup_maintenance_idle()
    try:
        result = await asyncio.to_thread(
            _restore_backup_consistent,
            backup_id=backup_id,
        )
    except BackupNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    _kick_upload_worker()
    _kick_kcs_rematch_worker()
    return result


@app.post("/api/system/backups/inspect-upload")
async def inspect_uploaded_system_backup(file: UploadFile = File(...)):
    manager = _backup_manager()
    temporary_path: Path | None = None
    try:
        temporary_path = await _stage_uploaded_backup(
            file,
            manager,
            prefix=".inspected-backup-",
        )
        backup = await asyncio.to_thread(manager.inspect_archive, temporary_path)
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return {"backup": backup}


@app.post("/api/system/backups/restore-upload")
async def restore_uploaded_system_backup(
    file: UploadFile = File(...),
    confirmation: str = Form(...),
):
    if confirmation != "복구":
        raise HTTPException(status_code=400, detail="복구 확인 문구가 일치하지 않습니다.")
    _ensure_backup_maintenance_idle()
    manager = _backup_manager()
    temporary_path: Path | None = None
    try:
        temporary_path = await _stage_uploaded_backup(
            file,
            manager,
            prefix=".uploaded-backup-",
        )
        result = await asyncio.to_thread(
            _restore_backup_consistent,
            archive_path=temporary_path,
        )
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    _kick_upload_worker()
    _kick_kcs_rematch_worker()
    return result


@app.post("/api/kcs/sync")
async def refresh_kcs():
    queued_runs: list[dict] = []

    def publish_revision(result: dict) -> None:
        if result["revision_changed"]:
            clear_matcher_caches()
        activate_matcher_revision(result["kcs_revision"])
        queued_runs.extend(
            store.publish_kcs_revision(
                result["kcs_snapshot"],
                result["kcs_revision"],
            )
        )

    try:
        result = await asyncio.to_thread(
            sync_kcs,
            settings,
            publish_revision,
            _ensure_kcs_refresh_has_no_active_uploads,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result["queued_project_count"] = len(queued_runs)
    result["rematch_run_ids"] = [run["id"] for run in queued_runs]
    _kick_kcs_rematch_worker()
    return result


@app.get("/api/projects")
def list_projects(
    search: str = Query(default="", max_length=200),
    parser_status: Literal["current", "legacy"] | None = Query(default=None),
    include_archived: bool = Query(default=False),
    archive_status: Literal["active", "archived"] | None = Query(default=None),
    kcs_impact_only: bool = Query(default=False),
    review_status: Literal[
        "reviewing", "submitted", "changes_requested", "approved"
    ] | None = Query(default=None),
    discipline: Literal["architecture", "mechanical", "electrical"] | None = Query(
        default=None
    ),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str = Query(default="", max_length=2048),
):
    try:
        page = store.list_projects_page(
            search=search,
            parser_status=parser_status or "",
            include_archived=include_archived,
            archive_status=archive_status or "",
            kcs_impact_only=kcs_impact_only,
            review_status=review_status or "",
            discipline=discipline or "",
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    page["projects"] = [_public_project(project) for project in page["projects"]]
    return page


@app.post("/api/projects/upload", status_code=201)
async def upload_project(file: UploadFile = File(...)):
    filename = _upload_filename(file, "시방서.docx")
    suffix = Path(filename).suffix.lower()
    if suffix not in {".doc", ".docx"}:
        raise HTTPException(status_code=400, detail=".doc 또는 .docx 파일만 지원합니다.")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="빈 파일은 업로드할 수 없습니다.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="파일 크기는 25MB 이하여야 합니다.")

    project_id = str(uuid.uuid4())
    source_path = settings.uploads_dir / f"{project_id}{suffix}"
    source_path.write_bytes(data)
    try:
        await asyncio.to_thread(
            _process_uploaded_source,
            filename,
            source_path,
            project_id,
            ai_client,
            expected_sha256=sha256_bytes(data),
        )
    except (ValueError, RuntimeError) as exc:
        _cleanup_upload_artifacts(source_path, project_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        _cleanup_upload_artifacts(source_path, project_id)
        raise

    return {
        "project": _public_project(store.get_project(project_id)),
        "clauses": store.list_clauses(project_id),
    }


@app.post("/api/upload-jobs", status_code=202)
async def create_upload_job(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="업로드할 파일이 없습니다.")
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"한 작업에는 최대 {MAX_BATCH_FILES}개 파일을 업로드할 수 있습니다.",
        )

    job_id = str(uuid.uuid4())
    queued_at = datetime.now(timezone.utc).isoformat()
    items: list[dict] = []
    saved: list[tuple[Path, str]] = []
    total_bytes = 0
    try:
        for position, upload in enumerate(files, start=1):
            filename = _upload_filename(upload, f"시방서-{position}.docx")
            suffix = Path(filename).suffix.lower()
            if suffix not in {".doc", ".docx"}:
                raise HTTPException(
                    status_code=400,
                    detail=f"{filename}: .doc 또는 .docx 파일만 지원합니다.",
                )
            data = await upload.read(MAX_UPLOAD_BYTES + 1)
            if not data:
                raise HTTPException(
                    status_code=400,
                    detail=f"{filename}: 빈 파일은 업로드할 수 없습니다.",
                )
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"{filename}: 파일 크기는 25MB 이하여야 합니다.",
                )
            total_bytes += len(data)
            if total_bytes > MAX_BATCH_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="한 업로드 작업의 전체 파일 크기는 250MB 이하여야 합니다.",
                )
            project_id = str(uuid.uuid4())
            source_path = settings.uploads_dir / f"{project_id}{suffix}"
            source_path.write_bytes(data)
            saved.append((source_path, project_id))
            items.append(
                {
                    "id": str(uuid.uuid4()),
                    "position": position,
                    "filename": filename,
                    "source_path": str(source_path),
                    "source_sha256": sha256_bytes(data),
                    "source_size": len(data),
                    "suffix": suffix,
                    "project_id": project_id,
                    "queued_at": queued_at,
                }
            )

        def persist_job() -> dict:
            with kcs_read_lock():
                snapshot, manifest = read_snapshot(settings.kcs_manifest_path)
                revision = str(manifest.get("revision") or "")
                if not snapshot or not revision:
                    raise RuntimeError("현재 KCS 개정 정보가 없어 업로드 작업을 시작할 수 없습니다.")
                activate_matcher_revision(revision)
                store.set_current_kcs_revision(snapshot, revision)
                return store.create_upload_job(
                    {
                        "id": job_id,
                        "kcs_snapshot": snapshot,
                        "kcs_revision": revision,
                        "created_at": queued_at,
                    },
                    items,
                )

        job = await asyncio.to_thread(persist_job)
    except HTTPException:
        for source_path, project_id in saved:
            _cleanup_upload_artifacts(source_path, project_id)
        raise
    except (ValueError, RuntimeError) as exc:
        for source_path, project_id in saved:
            _cleanup_upload_artifacts(source_path, project_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        for source_path, project_id in saved:
            _cleanup_upload_artifacts(source_path, project_id)
        raise

    _kick_upload_worker()
    _kick_kcs_rematch_worker()
    return {"job": job}


@app.get("/api/upload-jobs")
def list_upload_jobs(limit: int = Query(default=20, ge=1, le=100)):
    return {"jobs": store.list_upload_jobs(limit)}


@app.get("/api/upload-jobs/{job_id}")
def get_upload_job(job_id: str):
    job = store.get_upload_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="업로드 작업을 찾을 수 없습니다.")
    return {"job": job}


@app.post("/api/upload-jobs/{job_id}/items/{item_id}/retry", status_code=202)
async def retry_upload_job_item(job_id: str, item_id: str):
    try:
        job = await asyncio.to_thread(store.retry_upload_item, job_id, item_id)
    except UploadJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UploadJobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _kick_upload_worker()
    return {"job": job}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    project = _project_or_404(project_id)
    return {"project": _public_project(project), "clauses": store.list_clauses(project_id)}


@app.get("/api/projects/{project_id}/document-map")
def get_document_map(project_id: str):
    _project_or_404(project_id)
    return {"items": store.document_map(project_id)}


@app.get("/api/projects/{project_id}/source.docx")
def get_project_source_docx(project_id: str):
    project = _project_or_404(project_id)
    source_path = Path(str(project.get("source_path") or ""))
    upload_root = settings.uploads_dir.resolve()
    try:
        resolved = source_path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise HTTPException(status_code=404, detail="업로드 원본 파일을 찾을 수 없습니다.")
    if resolved.parent != upload_root or resolved.suffix.lower() not in {".doc", ".docx"}:
        raise HTTPException(status_code=404, detail="업로드 원본 파일을 찾을 수 없습니다.")

    preview_path = resolved
    if resolved.suffix.lower() == ".doc":
        preview_path = resolved.with_suffix(".docx")
        if not preview_path.exists():
            try:
                convert_legacy_doc(resolved, preview_path)
            except (OSError, RuntimeError, ValueError) as exc:
                preview_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=409,
                    detail=f"원문 미리보기를 위한 DOCX 변환에 실패했습니다: {exc}",
                ) from exc
    filename = f"{Path(str(project.get('source_filename') or 'source')).stem}.docx"
    return FileResponse(
        preview_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/projects/{project_id}/review-workflow")
def get_review_workflow(project_id: str):
    _project_or_404(project_id)
    workflow = store.get_review_workflow(project_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="검토 프로젝트를 찾을 수 없습니다.")
    return {"workflow": workflow}


@app.post("/api/projects/{project_id}/review-workflow/submit")
def submit_review(project_id: str, payload: ReviewSubmitRequest):
    try:
        workflow = store.submit_review(
            project_id,
            payload.author_name,
            payload.note,
        )
    except ReviewWorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReviewWorkflowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    project = store.get_project(project_id)
    return {"project": _public_project(project), "workflow": workflow}


@app.post(
    "/api/projects/{project_id}/review-workflow/{submission_id}/approve"
)
def approve_review(
    project_id: str,
    submission_id: str,
    payload: ReviewApprovalRequest,
):
    try:
        workflow = store.approve_review(
            project_id,
            submission_id,
            payload.approver_name,
            payload.note,
        )
    except ReviewWorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReviewWorkflowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    project = store.get_project(project_id)
    return {"project": _public_project(project), "workflow": workflow}


@app.post(
    "/api/projects/{project_id}/review-workflow/{submission_id}/request-changes"
)
def request_review_changes(
    project_id: str,
    submission_id: str,
    payload: ReviewChangesRequest,
):
    try:
        workflow = store.request_review_changes(
            project_id,
            submission_id,
            payload.approver_name,
            payload.reason,
        )
    except ReviewWorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReviewWorkflowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    project = store.get_project(project_id)
    return {"project": _public_project(project), "workflow": workflow}


@app.put("/api/projects/{project_id}/archive")
async def update_project_archive(project_id: str, payload: ProjectArchiveUpdate):
    try:
        project = await asyncio.to_thread(
            store.set_project_archived,
            project_id,
            payload.archived,
        )
        if not payload.archived and not project.get("requires_source_reupload"):
            run = await asyncio.to_thread(store.ensure_kcs_rematch, project_id)
            if run and run["status"] in {"failed", "superseded"}:
                run = await asyncio.to_thread(store.retry_kcs_rematch, run["id"])
            if run and run["status"] == "pending":
                _kick_kcs_rematch_worker()
            refreshed = await asyncio.to_thread(store.get_project, project_id)
            if refreshed:
                project = refreshed
    except KCSRematchConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (KCSRematchNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"project": _public_project(project)}


@app.get("/api/projects/{project_id}/kcs-impact")
def get_kcs_impact(project_id: str):
    project = _project_or_404(project_id)
    latest = project.get("kcs_rematch")
    if not latest:
        return {"run": None, "impacts": []}
    run = store.get_kcs_rematch_run(str(latest["id"]))
    if not run:
        return {"run": None, "impacts": []}
    impacts = run.pop("impacts", [])
    return {"run": run, "impacts": impacts}


@app.post("/api/projects/{project_id}/kcs-rematch")
async def start_kcs_rematch(project_id: str):
    project = _project_or_404(project_id)
    try:
        run = await asyncio.to_thread(store.ensure_kcs_rematch, project_id)
        if run and run["status"] in {"failed", "superseded"}:
            run = await asyncio.to_thread(store.retry_kcs_rematch, run["id"])
    except ValueError as exc:
        _raise_kcs_rematch_error(exc)
    if run is None:
        latest = project.get("kcs_rematch")
        if latest and latest.get("status") == "completed":
            return {"run": latest}
        raise HTTPException(
            status_code=409,
            detail="이 프로젝트에는 이미 최신 KCS 기준이 반영되어 있습니다.",
        )
    _kick_kcs_rematch_worker()
    return {"run": run}


@app.get("/api/projects/{project_id}/bulk-review")
def get_bulk_review(
    project_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=60, ge=1, le=200),
    status: Literal["all", "kcs_impact", "unreviewed", "keep", "delete", "hold"] = "all",
    q: str = Query(default="", max_length=200),
):
    _project_or_404(project_id)
    items, total = store.bulk_review(project_id, offset, limit, status, q)
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < total,
    }


@app.get("/api/projects/{project_id}/clauses/{clause_id}")
def get_clause(project_id: str, clause_id: str):
    _project_or_404(project_id)
    clause = store.get_clause(project_id, clause_id)
    if not clause:
        raise HTTPException(status_code=404, detail="조항을 찾을 수 없습니다.")
    return {"clause": clause}


@app.post("/api/projects/{project_id}/clauses/merge")
async def merge_clauses(project_id: str, payload: ClauseMergeRequest):
    project = _project_or_404(project_id)
    _ensure_project_review_unlocked(project)
    try:
        prepared = await asyncio.to_thread(
            store.prepare_clause_merge,
            project_id,
            payload.clause_ids,
        )
        result = await _rematch_clause_structure(project, prepared)
    except ValueError as exc:
        _raise_structure_error(exc)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"최신 KCS 상태를 확인할 수 없어 구조 편집을 중단했습니다: {exc}",
        ) from exc
    return {
        "project": _public_project(store.get_project(project_id)),
        "clauses": result["clauses"],
        "active_clause_id": result["active_clause_id"],
    }


@app.post("/api/projects/{project_id}/clauses/{clause_id}/split")
async def split_clause(
    project_id: str,
    clause_id: str,
    payload: ClauseSplitRequest,
):
    project = _project_or_404(project_id)
    _ensure_project_review_unlocked(project)
    if any(len(part) > 100_000 for part in payload.parts):
        raise HTTPException(status_code=400, detail="분할 조항은 각각 100,000자 이하여야 합니다.")
    try:
        prepared = await asyncio.to_thread(
            store.prepare_clause_split,
            project_id,
            clause_id,
            payload.parts,
        )
        result = await _rematch_clause_structure(project, prepared)
    except ValueError as exc:
        _raise_structure_error(exc)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"최신 KCS 상태를 확인할 수 없어 구조 편집을 중단했습니다: {exc}",
        ) from exc
    return {
        "project": _public_project(store.get_project(project_id)),
        "clauses": result["clauses"],
        "active_clause_id": result["active_clause_id"],
    }


@app.patch("/api/projects/{project_id}/clauses/{clause_id}")
def update_clause(project_id: str, clause_id: str, payload: DecisionUpdate):
    _ensure_project_review_unlocked(_project_or_404(project_id))
    try:
        clause = store.update_decision(
            project_id,
            clause_id,
            payload.decision,
            payload.edited_content.strip(),
            payload.review_note.strip(),
            payload.selected_candidate_id,
            decision_reason=payload.decision_reason.strip(),
            coverage_confirmed=payload.coverage_confirmed,
            expected_kcs_revision=payload.expected_kcs_revision.strip(),
            impact_run_id=payload.impact_run_id.strip(),
            acknowledge_kcs_impact=payload.acknowledge_kcs_impact,
        )
    except ReviewWorkflowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReviewWorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (KCSRematchConflictError, KCSRematchNotFoundError) as exc:
        _raise_kcs_rematch_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not clause:
        raise HTTPException(status_code=404, detail="조항을 찾을 수 없습니다.")
    return {"clause": clause, "project": _public_project(store.get_project(project_id))}


@app.patch("/api/projects/{project_id}/clauses/{clause_id}/quick-review")
def quick_review_clause(project_id: str, clause_id: str, payload: QuickReviewUpdate):
    _ensure_project_review_unlocked(_project_or_404(project_id))
    changes = {
        field: getattr(payload, field)
        for field in payload.model_fields_set
        if field in {
            "decision",
            "decision_reason",
            "coverage_confirmed",
            "selected_candidate_id",
        }
    }
    if not changes:
        raise HTTPException(status_code=400, detail="변경할 빠른 검토 항목이 없습니다.")
    try:
        clause = store.quick_update_decision(
            project_id,
            clause_id,
            changes,
            expected_kcs_revision=payload.expected_kcs_revision.strip(),
            impact_run_id=payload.impact_run_id.strip(),
            acknowledge_kcs_impact=payload.acknowledge_kcs_impact,
        )
    except ReviewWorkflowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReviewWorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (KCSRematchConflictError, KCSRematchNotFoundError) as exc:
        _raise_kcs_rematch_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not clause:
        raise HTTPException(status_code=404, detail="조항을 찾을 수 없습니다.")
    return {"clause": clause, "project": _public_project(store.get_project(project_id))}


@app.post("/api/projects/{project_id}/clauses/{clause_id}/coverage-analysis")
async def analyze_clause_coverage(
    project_id: str,
    clause_id: str,
    payload: CandidateAnalysisRequest | None = None,
):
    _ensure_project_review_unlocked(_project_or_404(project_id))
    context = store.get_coverage_context(project_id, clause_id)
    if not context:
        raise HTTPException(status_code=404, detail="분석할 포스코 조항을 찾을 수 없습니다.")
    if context["source_type"] == "heading":
        raise HTTPException(status_code=400, detail="목차와 구조 제목은 전체 포괄 분석 대상이 아닙니다.")
    candidates = context["candidates"]
    if not candidates:
        raise HTTPException(status_code=400, detail="전체 포괄 분석에 사용할 KCS 후보가 없습니다.")
    if context.get("coverage_analysis") and not (payload and payload.refresh):
        return {
            "analysis": context["coverage_analysis"],
            "clause": store.get_clause(project_id, clause_id),
            "project": _public_project(store.get_project(project_id)),
        }
    if not ai_client.available:
        raise HTTPException(
            status_code=503,
            detail="GPT 전체포괄 분석을 사용하려면 프로젝트 .env에 OPENAI_API_KEY를 설정하세요.",
        )

    posco_text = (
        str(context.get("posco_content") or "").strip()
        or str(context.get("posco_title") or "").strip()
    )
    posco_context = " ".join(
        part
        for part in (
            str(context.get("posco_label") or "").strip(),
            str(context.get("posco_title") or "").strip(),
        )
        if part
    )
    reference_to_candidate_id: dict[str, str] = {}
    candidate_inputs: list[tuple[str, str]] = []
    for index, candidate in enumerate(candidates, start=1):
        reference_id = f"C{index}"
        reference_to_candidate_id[reference_id] = candidate["id"]
        candidate_inputs.append(
            (
                reference_id,
                "\n".join(
                    part
                    for part in (
                        str(candidate.get("kcs_code") or "").strip(),
                        str(candidate.get("document_name") or "").strip(),
                        str(candidate.get("kcs_clause") or "").strip(),
                        str(candidate.get("title") or "").strip(),
                        str(candidate.get("content") or "").strip(),
                    )
                    if part
                ),
            )
        )
    try:
        result = await asyncio.to_thread(
            ai_client.analyze_coverage,
            posco_text,
            candidate_inputs,
            posco_context,
        )
        mapped_analysis = result.as_dict()
        mapped_analysis["requirements"] = [
            {
                **requirement,
                "evidence_candidate_ids": [
                    reference_to_candidate_id[reference_id]
                    for reference_id in requirement["evidence_candidate_ids"]
                ],
            }
            for requirement in mapped_analysis["requirements"]
        ]
        clause = store.save_coverage_analysis(
            project_id,
            clause_id,
            [candidate["id"] for candidate in candidates],
            mapped_analysis,
            result.model,
            expected_posco_text=posco_text,
            expected_candidate_texts=[text for _, text in candidate_inputs],
        )
    except ReviewWorkflowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReviewWorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OpenAIAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if clause is None:
        raise HTTPException(status_code=404, detail="분석할 포스코 조항을 찾을 수 없습니다.")
    return {
        "analysis": clause["coverage_analysis"],
        "clause": clause,
        "project": _public_project(store.get_project(project_id)),
    }


@app.post(
    "/api/projects/{project_id}/clauses/{clause_id}/candidates/{candidate_id}/ai-analysis"
)
async def analyze_candidate(
    project_id: str,
    clause_id: str,
    candidate_id: str,
    payload: CandidateAnalysisRequest | None = None,
):
    _ensure_project_review_unlocked(_project_or_404(project_id))
    context = store.get_candidate_context(project_id, clause_id, candidate_id)
    if not context:
        raise HTTPException(status_code=404, detail="분석할 KCS 후보를 찾을 수 없습니다.")

    if context.get("relation_type") and not (payload and payload.refresh):
        analysis = {
            "relation_type": context["relation_type"],
            "confidence": context["confidence"],
            "rationale": context["rationale"],
            "simplified_content": context["simplified_content"],
            "model": context["model"],
            "analyzed_at": context["analyzed_at"],
        }
        return {
            "analysis": analysis,
            "excluded": context["relation_type"] == "unrelated"
            and float(context.get("confidence") or 0) >= 0.85,
            "clause": store.get_clause(project_id, clause_id),
            "project": _public_project(store.get_project(project_id)),
        }

    if not ai_client.available:
        raise HTTPException(
            status_code=503,
            detail="GPT 정밀분석을 사용하려면 프로젝트 .env에 OPENAI_API_KEY를 설정하세요.",
        )

    posco_text = "\n".join(
        part
        for part in (
            str(context.get("posco_label") or "").strip(),
            str(context.get("posco_title") or "").strip(),
            str(context.get("posco_content") or "").strip(),
        )
        if part
    )
    kcs_text = "\n".join(
        part
        for part in (
            str(context.get("kcs_code") or "").strip(),
            str(context.get("kcs_clause") or "").strip(),
            str(context.get("kcs_title") or "").strip(),
            str(context.get("kcs_content") or "").strip(),
        )
        if part
    )
    try:
        result = await asyncio.to_thread(ai_client.analyze_match, posco_text, kcs_text)
        clause = store.save_candidate_analysis(
            project_id,
            clause_id,
            candidate_id,
            result.as_dict(),
            result.model,
        )
    except ReviewWorkflowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReviewWorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OpenAIAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if clause is None:
        raise HTTPException(status_code=404, detail="분석할 KCS 후보를 찾을 수 없습니다.")
    return {
        "analysis": result.as_dict(),
        "excluded": result.relation_type == "unrelated" and result.confidence >= 0.85,
        "clause": clause,
        "project": _public_project(store.get_project(project_id)),
    }


@app.delete(
    "/api/projects/{project_id}/clauses/{clause_id}/candidates/{candidate_id}/ai-analysis"
)
def restore_candidate(
    project_id: str,
    clause_id: str,
    candidate_id: str,
):
    _ensure_project_review_unlocked(_project_or_404(project_id))
    try:
        clause = store.clear_candidate_analysis(project_id, clause_id, candidate_id)
    except ReviewWorkflowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReviewWorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if clause is None:
        raise HTTPException(status_code=404, detail="복원할 KCS 후보를 찾을 수 없습니다.")
    return {
        "clause": clause,
        "project": _public_project(store.get_project(project_id)),
    }


@app.post("/api/projects/{project_id}/quality-evaluation")
def start_quality_evaluation(
    project_id: str,
    payload: QualityEvaluationStart | None = None,
):
    _ensure_project_review_unlocked(_project_or_404(project_id))
    reviewer_name = payload.reviewer_name.strip() if payload else "담당자"
    try:
        evaluation = store.ensure_quality_evaluation(project_id, reviewer_name or "담당자")
    except ReviewWorkflowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReviewWorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not evaluation:
        raise HTTPException(status_code=404, detail="품질평가 프로젝트를 찾을 수 없습니다.")
    return {"evaluation": evaluation}


@app.get("/api/projects/{project_id}/quality-evaluation")
def get_quality_evaluation(project_id: str):
    _project_or_404(project_id)
    try:
        evaluation = store.ensure_quality_evaluation(project_id)
    except ReviewWorkflowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReviewWorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not evaluation:
        raise HTTPException(status_code=404, detail="품질평가 프로젝트를 찾을 수 없습니다.")
    return {"evaluation": evaluation}


@app.patch("/api/projects/{project_id}/quality-evaluation/items/{clause_id}")
def update_quality_evaluation_item(
    project_id: str,
    clause_id: str,
    payload: QualityItemUpdate,
):
    _ensure_project_review_unlocked(_project_or_404(project_id))
    try:
        evaluation = store.update_quality_item(
            project_id,
            clause_id,
            payload.verdict,
            payload.relevant_candidate_id,
            payload.expected_kcs_code,
            payload.expected_kcs_clause,
            payload.quality_note,
        )
    except ReviewWorkflowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReviewWorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not evaluation:
        raise HTTPException(status_code=404, detail="품질평가 표본 조항을 찾을 수 없습니다.")
    return {
        "evaluation": evaluation,
        "clause": store.get_clause(project_id, clause_id),
    }


@app.get("/api/projects/{project_id}/quality-evaluation.xlsx")
def export_quality_evaluation(project_id: str):
    project = _project_or_404(project_id)
    evaluation = store.ensure_quality_evaluation(project_id)
    if not evaluation:
        raise HTTPException(status_code=404, detail="품질평가 프로젝트를 찾을 수 없습니다.")
    data = build_quality_evaluation_xlsx(
        project,
        evaluation,
        store.quality_export_rows(project_id),
    )
    filename = f"{safe_filename(project['title'])}_KCS매칭_품질평가.xlsx"
    return _download_response(
        data,
        filename,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/projects/{project_id}/export/{kind}")
def export_docx(project_id: str, kind: Literal["review", "final"]):
    if kind == "final":
        with kcs_read_lock():
            try:
                _reconcile_current_kcs_revision()
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"최신 KCS 상태를 확인할 수 없어 최종 DOCX 생성을 차단했습니다: {exc}",
                ) from exc
            bundle = store.final_export_bundle(project_id)
        if not bundle:
            raise HTTPException(status_code=404, detail="검토 프로젝트를 찾을 수 없습니다.")
        project, clauses = bundle
    else:
        project = _project_or_404(project_id)
        clauses = store.export_clauses(
            project_id,
            ("keep", "hold"),
            include_unreviewed=True,
        )

    if kind == "final" and not project["final_export_ready"]:
        blockers = ", ".join(project["final_export_blockers"])
        raise HTTPException(
            status_code=409,
            detail=f"최종 DOCX 생성 조건을 충족하지 않았습니다: {blockers}",
        )
    if kind == "review" and not clauses:
        raise HTTPException(
            status_code=400,
            detail="간소화 시방서에 포함할 삭제 제외 조항이 없습니다.",
        )
    data = build_review_docx(project, clauses, kind)
    filename = f"{safe_filename(project['title'])}_간소화시방서.docx"
    return _download_response(
        data,
        filename,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@app.get("/api/projects/{project_id}/audit.xlsx")
def export_audit(project_id: str):
    project = _project_or_404(project_id)
    data = build_audit_xlsx(
        project,
        store.audit_rows(project_id),
        store.kcs_rematch_audit_rows(project_id),
        store.workflow_audit_rows(project_id),
    )
    filename = f"{safe_filename(project['title'])}_판정이력.xlsx"
    return _download_response(
        data,
        filename,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/debug/database")
def database_debug():
    """Local-only diagnostic without paths, source text, or secrets."""
    return {
        "project_count": len(store.list_projects()),
        "database_ready": settings.database_path.exists(),
        "schema_version": DATABASE_SCHEMA_VERSION,
    }
