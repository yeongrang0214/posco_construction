"""Durable, single-worker GPT coverage queue using existing application metadata."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

PREFIX = "detailed_analysis:"
ACTIVE = {"queued", "running"}


def _read(connection, project_id):
    row = connection.execute("SELECT value FROM app_metadata WHERE key = ?", (PREFIX + project_id,)).fetchone()
    return json.loads(row["value"]) if row else None


def _write(connection, job):
    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    connection.execute(
        "INSERT INTO app_metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (PREFIX + job["project_id"], json.dumps(job, ensure_ascii=False)),
    )
    return job


def _counts(connection, project_id):
    return dict(connection.execute(
        """SELECT COUNT(*) AS total, COUNT(a.clause_id) AS completed
           FROM clauses c LEFT JOIN clause_coverage_analysis a ON a.clause_id = c.id
           WHERE c.project_id = ? AND c.source_type != 'heading'""", (project_id,),
    ).fetchone())


def status(store, project_id):
    with store.connect() as connection:
        return _read(connection, project_id)


def enqueue(store, project_id, *, retry=False):
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = _read(connection, project_id)
        if current and (current["status"] in ACTIVE or (current["status"] in {"failed", "paused"} and not retry)):
            return current
        counts = _counts(connection, project_id)
        return _write(connection, {
            "project_id": project_id,
            "status": "completed" if counts["total"] == counts["completed"] else "queued",
            **counts, "error": "", "current_clause_id": None,
        })


def pause(store, project_id):
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        job = _read(connection, project_id)
        if job and job["status"] in ACTIVE:
            job["status"] = "paused"
            _write(connection, job)
        return job


def active_count(store):
    with store.connect() as connection:
        return sum(json.loads(row["value"])["status"] in ACTIVE for row in connection.execute(
            "SELECT value FROM app_metadata WHERE key LIKE ?", (PREFIX + "%",),
        ))


class DetailedAnalysisWorker:
    def __init__(self, store, analyze, allowed):
        self.store, self.analyze, self.allowed = store, analyze, allowed
        self.stopping = False
        self.in_flight = False

    def recover(self):
        with self.store.connect() as connection:
            for row in connection.execute("SELECT value FROM app_metadata WHERE key LIKE ?", (PREFIX + "%",)).fetchall():
                job = json.loads(row["value"])
                if job["status"] == "running":
                    job.update(status="queued", current_clause_id=None)
                    _write(connection, job)

    def claim(self):
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in connection.execute("SELECT value FROM app_metadata WHERE key LIKE ? ORDER BY key", (PREFIX + "%",)).fetchall():
                job = json.loads(row["value"])
                if job["status"] == "queued":
                    job.update(status="running", **_counts(connection, job["project_id"]))
                    return _write(connection, job)
        return None

    def update(self, project_id, **changes):
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = _read(connection, project_id)
            if not job:
                return
            # Pause is authoritative, including while the last paid request finishes.
            if job["status"] == "paused":
                changes.pop("status", None)
            job.update(changes, **_counts(connection, project_id))
            _write(connection, job)

    async def run_one(self):
        job = self.claim()
        if not job:
            return False
        project_id = job["project_id"]
        try:
            self.allowed(project_id)
            clauses = self.store.list_clauses(project_id)
            for clause in clauses:
                if self.stopping:
                    self.update(project_id, status="queued", current_clause_id=None)
                    return True
                if status(self.store, project_id)["status"] == "paused":
                    return True
                if clause["source_type"] == "heading":
                    continue
                self.allowed(project_id)
                self.update(project_id, current_clause_id=clause["id"])
                self.in_flight = True
                try:
                    await self.analyze(project_id, clause["id"])
                finally:
                    self.in_flight = False
                self.update(project_id, current_clause_id=None)
            with self.store.connect() as connection:
                counts = _counts(connection, project_id)
            # Structure edits may add clauses while a document is being processed.
            self.update(project_id, status="completed" if counts["completed"] == counts["total"] else "queued", current_clause_id=None)
        except asyncio.CancelledError:
            self.update(project_id, status="queued", current_clause_id=None)
            raise
        except Exception as exc:
            # Quota/network/validation failures stop the run, never an unlimited paid retry loop.
            message = str(getattr(exc, "detail", "") or str(exc) or "GPT 상세비교를 완료하지 못했습니다.")
            self.update(project_id, status="failed", error=message, current_clause_id=None)
        return True

    async def run(self):
        self.recover()
        while not self.stopping:
            if not await self.run_one():
                await asyncio.sleep(1)
