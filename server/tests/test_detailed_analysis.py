from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import server.app as app_module
from server import detailed_analysis as queue
from server.openai_ai import CoverageAnalysis, OpenAIAPIError, OpenAIClient, coverage_source_segments
from server.storage import Store
from server.tests.test_ai_api import _seed


class CoverageAI:
    available = True

    def __init__(self):
        self.calls = []
        self.fail_at = None

    def analyze_coverage(self, source, candidates, posco_context=""):
        self.calls.append((source, candidates))
        if len(self.calls) == self.fail_at:
            raise OpenAIAPIError("API 연결 실패")
        return CoverageAnalysis(
            coverage_status="uncertain", confidence=.6,
            requirements=tuple({"requirement": text, "source_segment_ids": [key],
                                "status": "uncertain", "evidence_candidate_ids": [],
                                "evidence": "제시된 근거가 충분하지 않습니다."}
                               for key, text in coverage_source_segments(source)),
            residual_content=source, rationale="담당자 확인 필요", model="test-gpt",
        )


def add_clause(store, project_id, clause_id, order, source_type="paragraph"):
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO clauses(id, project_id, source_order, label, title, content, source_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (clause_id, project_id, order, str(order), "검사", "표면을 청소한다.", source_type),
        )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path / "detail.sqlite3")
    project_id, clause_id, candidate_id = _seed(store, tmp_path)
    ai = CoverageAI()
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "ai_client", ai)
    monkeypatch.setattr(app_module, "_coverage_request_lock", asyncio.Lock())
    worker = queue.DetailedAnalysisWorker(store, app_module._automatic_coverage, app_module._allow_detailed_analysis)
    return store, project_id, clause_id, candidate_id, ai, worker


def test_all_body_and_tables_analyzed_once_preserving_decisions(setup):
    store, project_id, clause_id, candidate_id, ai, worker = setup
    add_clause(store, project_id, "heading", 2, "heading")
    add_clause(store, project_id, "table", 3, "table")
    add_clause(store, project_id, "unmatched", 4)
    with store.connect() as connection:
        connection.execute("UPDATE clauses SET decision='keep', review_note='유지 의견', coverage_confirmed=1, selected_candidate_id=? WHERE id=?", (candidate_id, clause_id))
    job = queue.enqueue(store, project_id)
    assert job["total"] == 3
    assert queue.enqueue(store, project_id) == job
    asyncio.run(worker.run_one())
    assert len(ai.calls) == 3
    assert queue.status(store, project_id)["status"] == "completed"
    assert queue.status(store, project_id)["completed"] == 3
    result = store.get_clause(project_id, clause_id)
    assert (result["decision"], result["review_note"], result["coverage_confirmed"], result["selected_candidate_id"]) == ("keep", "유지 의견", True, candidate_id)
    assert store.get_clause(project_id, "heading")["coverage_analysis"] is None
    for missing in ("table", "unmatched"):
        assert store.get_clause(project_id, missing)["coverage_analysis"]["deletion_safe"] is False
    # Both queue restarts and concurrent manual GET-like analysis reuse stored results.
    assert queue.enqueue(store, project_id, retry=True)["status"] == "completed"
    asyncio.run(app_module._automatic_coverage(project_id, clause_id))
    assert len(ai.calls) == 3
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM decision_history").fetchone()[0] == 0


def test_failure_stops_until_explicit_retry_and_reuses_completed(setup):
    store, project_id, _, _, ai, worker = setup
    add_clause(store, project_id, "second", 2)
    ai.fail_at = 2
    queue.enqueue(store, project_id)
    asyncio.run(worker.run_one())
    assert queue.status(store, project_id)["status"] == "failed"
    assert queue.status(store, project_id)["completed"] == 1
    assert queue.enqueue(store, project_id)["status"] == "failed"
    assert asyncio.run(worker.run_one()) is False
    assert len(ai.calls) == 2
    queue.enqueue(store, project_id, retry=True)
    asyncio.run(worker.run_one())
    assert len(ai.calls) == 3
    assert queue.status(store, project_id)["completed"] == 2


def test_new_analysis_version_refreshes_once_without_touching_human_decision(setup):
    store, project_id, clause_id, _, ai, worker = setup
    with store.connect() as connection:
        connection.execute("UPDATE clauses SET decision='delete', decision_reason='management_decision', review_note='보존' WHERE id=?", (clause_id,))
    queue.enqueue(store, project_id)
    asyncio.run(worker.run_one())
    with store.connect() as connection:
        connection.execute("UPDATE app_metadata SET value='older-version' WHERE key=?", ("coverage_version:" + clause_id,))
    assert queue.enqueue(store, project_id, retry=True)["status"] == "queued"
    asyncio.run(worker.run_one())
    assert len(ai.calls) == 2
    result = store.get_clause(project_id, clause_id)
    assert (result["decision"], result["decision_reason"], result["review_note"]) == ("delete", "management_decision", "보존")
    assert queue.enqueue(store, project_id, retry=True)["status"] == "completed"
    asyncio.run(worker.run_one())
    assert len(ai.calls) == 2


def test_pause_finishes_inflight_without_starting_next_request(setup):
    store, project_id, _, _, ai, _ = setup
    add_clause(store, project_id, "second", 2)

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def analyze(pid, cid):
            started.set()
            await release.wait()
            return await app_module._automatic_coverage(pid, cid)

        worker = queue.DetailedAnalysisWorker(store, analyze, app_module._allow_detailed_analysis)
        queue.enqueue(store, project_id)
        task = asyncio.create_task(worker.run_one())
        await started.wait()
        assert worker.in_flight
        queue.pause(store, project_id)
        assert queue.active_count(store) == 0
        app_module._detail_worker = worker
        try:
            with pytest.raises(HTTPException, match="409"):
                app_module._ensure_backup_maintenance_idle()
        finally:
            app_module._detail_worker = None
        release.set()
        await task
        assert queue.status(store, project_id)["status"] == "paused"
        assert queue.status(store, project_id)["completed"] == 1
        assert queue.enqueue(store, project_id)["status"] == "paused"
        queue.enqueue(store, project_id, retry=True)
        await worker.run_one()

    asyncio.run(scenario())
    assert len(ai.calls) == 2
    assert queue.status(store, project_id)["status"] == "completed"


def test_restart_recovers_inflight_record_without_rebilling_saved_clauses(setup):
    store, project_id, clause_id, _, ai, worker = setup
    add_clause(store, project_id, "second", 2)
    queue.enqueue(store, project_id)
    worker.claim()
    asyncio.run(app_module._automatic_coverage(project_id, clause_id))
    recovered_store = Store(store.database_path)
    resumed = queue.DetailedAnalysisWorker(recovered_store, app_module._automatic_coverage, app_module._allow_detailed_analysis)
    resumed.recover()
    assert queue.status(store, project_id)["status"] == "queued"
    asyncio.run(resumed.run_one())
    assert len(ai.calls) == 2
    assert queue.status(store, project_id)["status"] == "completed"


def test_added_clause_during_run_is_not_reported_as_completed(setup):
    store, project_id, _, _, ai, worker = setup
    original = worker.analyze
    added = False

    async def analyze(pid, cid):
        nonlocal added
        result = await original(pid, cid)
        if not added:
            add_clause(store, pid, "added", 2)
            added = True
        return result

    worker.analyze = analyze
    queue.enqueue(store, project_id)
    asyncio.run(worker.run_one())
    assert queue.status(store, project_id)["status"] == "queued"
    asyncio.run(worker.run_one())
    assert queue.status(store, project_id)["completed"] == 2


def test_kcs_change_during_gpt_request_rejects_old_result(setup):
    store, project_id, clause_id, _, ai, worker = setup
    original = ai.analyze_coverage

    def changed(*args):
        result = original(*args)
        with store.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO app_metadata(key,value) VALUES ('current_kcs_revision','new-revision')")
        return result

    ai.analyze_coverage = changed
    queue.enqueue(store, project_id)
    asyncio.run(worker.run_one())
    assert queue.status(store, project_id)["status"] == "failed"
    assert "KCS" in queue.status(store, project_id)["error"]
    assert store.get_clause(project_id, clause_id)["coverage_analysis"] is None


def test_status_read_is_free_and_start_requires_key(setup, monkeypatch):
    store, project_id, _, _, ai, _ = setup
    monkeypatch.setattr(app_module, "ai_client", OpenAIClient(None))
    with TestClient(app_module.app) as client:
        response = client.get(f"/api/projects/{project_id}/detailed-analysis")
        assert response.status_code == 200 and response.json() == {"job": None}
        assert client.post(f"/api/projects/{project_id}/detailed-analysis").status_code == 503
        assert client.get("/api/projects/missing/detailed-analysis").status_code == 404
    assert not ai.calls


def test_queued_analysis_blocks_restore_and_locked_project_cannot_start(setup):
    store, project_id, _, _, _, _ = setup
    queue.enqueue(store, project_id)
    with pytest.raises(HTTPException) as error:
        app_module._ensure_backup_maintenance_idle()
    assert error.value.status_code == 409
    with store.connect() as connection:
        connection.execute("UPDATE projects SET status='submitted' WHERE id=?", (project_id,))
    with pytest.raises(HTTPException) as error:
        app_module.start_detailed_analysis(project_id)
    assert error.value.status_code == 409


def test_api_worker_runs_after_enqueue_and_result_is_readable(setup):
    store, project_id, clause_id, _, ai, _ = setup
    with TestClient(app_module.app) as client:
        response = client.post(f"/api/projects/{project_id}/detailed-analysis")
        assert response.status_code == 200
        # Shutdown cooperatively completes an already claimed clause; for a queued
        # job, run_one below also verifies persisted work survives server restart.
    if queue.status(store, project_id)["status"] != "completed":
        worker = queue.DetailedAnalysisWorker(store, app_module._automatic_coverage, app_module._allow_detailed_analysis)
        asyncio.run(worker.run_one())
    assert len(ai.calls) == 1
    assert store.get_clause(project_id, clause_id)["coverage_analysis"]["model"] == "test-gpt"


@pytest.mark.parametrize("model_status", ["uncertain", "covered", "not_covered"])
def test_no_candidate_gpt_schema_and_server_validation(model_status):
    requests = []
    source = "바탕면을 청소한다."

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        item = payload["text"]["format"]["schema"]["properties"]["requirements_by_source"]["properties"]["S1"]
        assert item["properties"]["status"]["enum"] == ["uncertain"]
        assert item["properties"]["evidence_candidate_ids"]["maxItems"] == 0
        return httpx.Response(200, json={"output_text": json.dumps({
            "confidence": .5, "requirements_by_source": {"S1": {"status": model_status,
            "evidence_candidate_ids": [], "evidence": "비교할 KCS 근거 부족"}},
            "residual_content": source, "rationale": "KCS가 없다고 단정할 수 없습니다."})})

    client = OpenAIClient("test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    if model_status == "uncertain":
        result = client.analyze_coverage(source, [])
        assert result.coverage_status == "uncertain"
        assert result.residual_content == source
    else:
        with pytest.raises(OpenAIAPIError):
            client.analyze_coverage(source, [])
    assert len(requests) == 1
