import json

import httpx
import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server import ks_test_comparison as ks
from server.openai_ai import OpenAIAPIError
from server.storage import Store
from server.tests.test_ai_api import _seed


SOURCE = "점토벽돌 | 형상및 치수 흡수율 압축강도시험 | KSL 4201 | 10,000매 또는 1LOT 당 5매씩 | 1급 흡수율 15% 이하 압축강도 100kg/㎠ 이상"
# Synthetic evidence only. No KS full text is bundled in test fixtures.
SECTIONS = [{"id": "KS L 4201:7", "standard": "KS L 4201", "section": "7", "text": "시험용 예제: 치수 및 흡수율 시험방법"},
            {"id": "KS L 4201:8", "standard": "KS L 4201", "section": "8", "text": "시험용 예제: 로트마다 시료를 채취한다."}]


def standard(code, _client):
    return {"standard": code, "name": "점토 벽돌", "status": "machine_verified", "checked_at": "2026-09-11",
            "edition_date": "2022-12-09", "revision": "20", "source_url": ks.official_url(code),
            "message": "시험", "sections": SECTIONS}


class AI:
    available = True
    rerank_model = "test-gpt"

    def __init__(self):
        self.calls = 0
        self.modify = lambda x: x

    def _post(self, _path, payload):
        self.calls += 1
        assert payload["store"] is False
        assert payload["text"]["format"]["strict"] is True
        data = json.loads(payload["input"])
        rows = [{"field_id": f["id"], "status": "corresponds" if f["id"] == "tests" else "differs",
                 "explanation": "시험용 근거와 비교", "evidence": [{"id": SECTIONS[0]["id"], "quote": SECTIONS[0]["text"]}]}
                for f in data["fields"]]
        return {"output_text": json.dumps({"fields": self.modify(rows)})}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path / "ks.sqlite3")
    pid, cid, _ = _seed(store, tmp_path)
    with store.connect() as c:
        c.execute("UPDATE clauses SET content=?, source_type='table',decision='hold',review_note='담당자 판단' WHERE id=?", (SOURCE, cid))
    ai = AI()
    monkeypatch.setattr(ks, "fetch_standard", standard)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "ai_client", ai)
    return store, store.get_clause(pid, cid), ai


def test_parse_metadata_uses_current_button_not_old_url_or_related_standards():
    page = """<th>표준명(한글)</th><td>점토 벽돌</td><th>최종개정확인일</th><td>2022-12-09</td>
      <button onclick="lookup('KSL4201','20')"></button><button onclick="open3('KSL4201','20','STD')"></button>"""
    meta = ks.parse_metadata("KS L 4201", page)
    assert meta["revision"] == "20" and meta["machine_available"]
    with pytest.raises(ValueError):
        ks.parse_metadata("KS F 4004", page)


def machine_data():
    return {"info": {"tmprKsNo": "KSL4201", "reformNo": "20", "formType": "STD", "activeYn": "Y", "operationDate": "2022년 12월 9일"},
            "content": [{"NUMBERING": str(i), "CONTENT": "<div>시험용 본문<div class='jb-serach-text'>AI추천문서 잘못된근거</div></div>"} for i in range(1, 4)]}


def test_machine_rejects_other_editions_and_strips_recommendations():
    meta = standard("KS L 4201", None)
    data = machine_data()
    assert all(x["text"] == "시험용 본문" for x in ks.parse_machine(meta, data))
    data["info"]["reformNo"] = "19"
    with pytest.raises(ValueError):
        ks.parse_machine(meta, data)
    data["info"]["reformNo"] = "20"
    data["info"]["operationDate"] = "2020년 1월 1일"
    with pytest.raises(ValueError):
        ks.parse_machine(meta, data)


def test_compare_caches_without_mutating_decisions_or_export(setup):
    store, clause, ai = setup
    before = store.get_clause(clause["project_id"], clause["id"])
    result = ks.compare(store, ai, clause)
    assert result["fields"][0]["status"] == "corresponds"
    assert "10,000매" in result["suggested_text"]
    assert "100kg/㎠" in result["suggested_text"]
    assert "KS L 4201에 따른다" in result["suggested_text"]
    assert "sections" not in result["standards"][0]
    ks.compare(store, ai, clause, refresh=True)
    assert ai.calls == 1
    assert store.get_clause(clause["project_id"], clause["id"]) == before
    with store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM decision_history").fetchone()[0] == 0


def test_get_is_read_only_and_returns_cached_comparison(setup):
    store, clause, ai = setup
    client = TestClient(app_module.app)
    path = f"/api/projects/{clause['project_id']}/clauses/{clause['id']}"
    assert client.get(path).json()["clause"]["ks_test_comparison"]["status"] == "pending"
    assert ai.calls == 0
    assert client.post(path + "/ks-test-comparison", json={"refresh": True}).status_code == 200
    assert client.get(path).json()["clause"]["ks_test_comparison"]["status"] == "completed"
    assert ai.calls == 1


def test_viewer_only_does_not_guess_or_call_gpt(setup, monkeypatch):
    store, clause, ai = setup
    monkeypatch.setattr(ks, "fetch_standard", lambda code, client: {**standard(code, client), "status": "viewer_only", "sections": []})
    result = ks.compare(store, ai, clause)
    assert ai.calls == 0
    assert not result["suggested_text"]
    assert all(f["status"] == "unconfirmed" for f in result["fields"])


def test_fabricated_quotes_and_ditto_are_not_replacements(setup):
    _, clause, ai = setup
    ai.modify = lambda rows: [{**r, "evidence": [{"id": SECTIONS[0]["id"], "quote": "없는 인용문을 지어낸 응답"}]} for r in rows]
    result = ks.analyze_fields(ai, ks.fields_for(clause), [standard("KS L 4201", None)])
    assert all(f["status"] == "unconfirmed" for f in result)
    assert all(not f["evidence"] for f in result)
    ai.modify = lambda x: x
    result = ks.analyze_fields(ai, [{"id": "tests", "label": "시험", "source": "상 동"}], [standard("KS L 4201", None)])
    assert result[0]["status"] == "unconfirmed"


def test_missing_fields_are_rejected(setup):
    _, clause, ai = setup
    ai.modify = lambda rows: rows[:-1]
    with pytest.raises(OpenAIAPIError):
        ks.analyze_fields(ai, ks.fields_for(clause), [standard("KS L 4201", None)])


def test_changed_source_invalidates_saved_result(setup):
    store, clause, ai = setup
    ks.compare(store, ai, clause)
    with store.connect() as c:
        c.execute("UPDATE clauses SET content=? WHERE id=?", (SOURCE.replace("10,000", "20,000"), clause["id"]))
    changed = store.get_clause(clause["project_id"], clause["id"])
    assert ks.read_saved(store, changed)["status"] == "pending"
    ks.compare(store, ai, changed)
    assert ai.calls == 2


def test_network_failure_is_unavailable_not_missing():
    def fail(_request):
        raise httpx.ConnectError("offline")
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        result = ks.fetch_standard("KS F 4004", client)
    assert result["status"] == "unavailable" and not result["sections"]


def test_unknown_layout_not_assumed_test_table():
    assert not ks.fields_for({"source_type": "paragraph", "content": SOURCE})
    assert not ks.fields_for({"source_type": "table", "content": "KS L 4201 | 치수 | 값"})


def test_abbreviated_ks_range_blocks_replacement(setup):
    store, clause, ai = setup
    source = SOURCE.replace("KSL 4201", "KSL 4201 -5 3111")
    with store.connect() as c:
        c.execute("UPDATE clauses SET content=? WHERE id=?", (source, clause["id"]))
    clause = store.get_clause(clause["project_id"], clause["id"])
    result = ks.compare(store, ai, clause)
    assert not result["suggested_text"]
    assert result["fields"][0]["status"] == "unconfirmed"


def test_new_edition_recompares_and_stale_date_is_visible(setup, monkeypatch):
    store, clause, ai = setup
    ks.compare(store, ai, clause)
    with store.connect() as c:
        row = json.loads(c.execute("SELECT value FROM app_metadata WHERE key=?", ("ks_test:" + clause["id"],)).fetchone()[0])
        row["checked_at"] = "2000-01-01T00:00:00Z"
        c.execute("UPDATE app_metadata SET value=? WHERE key=?", (json.dumps(row), "ks_test:" + clause["id"]))
    assert ks.read_saved(store, clause)["stale"]
    monkeypatch.setattr(ks, "fetch_standard", lambda code, client: {**standard(code, client), "revision": "21"})
    ks.compare(store, ai, clause, refresh=True)
    assert ai.calls == 2


def test_original_change_during_gpt_does_not_save_stale_result(setup):
    store, clause, ai = setup
    def change(rows):
        with store.connect() as c:
            c.execute("UPDATE clauses SET content=? WHERE id=?", (SOURCE + " 변경", clause["id"]))
        return rows
    ai.modify = change
    with pytest.raises(ValueError, match="분석 중 원문"):
        ks.compare(store, ai, clause)
    assert ks.read_saved(store, clause)["status"] == "pending"


def test_archived_document_cannot_start_paid_comparison(setup):
    store, clause, ai = setup
    with store.connect() as c:
        c.execute("UPDATE projects SET archived_at='2026-09-11' WHERE id=?", (clause["project_id"],))
    response = TestClient(app_module.app).post(f"/api/projects/{clause['project_id']}/clauses/{clause['id']}/ks-test-comparison")
    assert response.status_code == 409
    assert ai.calls == 0


@pytest.mark.parametrize("source", ["시험소요일 10일", "공사개시전 1회", "10,000매당 5매", "1급 압축강도 100kg/㎠ 이상"])
def test_generic_ks_quality_evidence_cannot_cover_specific_conditions(setup, source):
    _, _, ai = setup
    # Simulate a false-positive GPT label using a real but irrelevant quote.
    ai.modify = lambda rows: [{**r, "status": "corresponds"} for r in rows]
    result = ks.analyze_fields(ai, [{"id": "conditions", "label": "등급·합격기준", "source": source}], [standard("KS L 4201", None)])
    assert result[0]["status"] == "unconfirmed"


def test_contradictory_explanation_cannot_mark_condition_covered(setup):
    _, _, ai = setup
    ai.modify = lambda rows: [{**r, "status": "corresponds", "explanation": "품질증명서 제출은 KS 본문에 없으나 시험 기준은 있다."} for r in rows]
    result = ks.analyze_fields(ai, [{"id": "frequency", "label": "빈도", "source": "품질증명서 제출"}], [standard("KS L 4201", None)])
    assert result[0]["status"] == "unconfirmed"


def test_parent_test_title_is_context_not_standalone_evidence():
    data = machine_data()
    data["content"].extend([
        {"NUMBERING": "7.4", "CONTENT": "", "TITLE_NUMBERING": "<h3>흡수율</h3>"},
        {"NUMBERING": "7.4.1", "CONTENT": "<p>시험용 조작 본문</p>", "TITLE_NUMBERING": "<h4>조작</h4>"},
    ])
    sections = ks.parse_machine(standard("KS L 4201", None), data)
    assert not any(s["section"] == "7.4" for s in sections)
    assert sections[-1]["title_path"] == "흡수율 > 조작"
