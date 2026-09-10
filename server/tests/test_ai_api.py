from __future__ import annotations

from fastapi.testclient import TestClient

import server.app as app_module
from server.openai_ai import CoverageAnalysis, MatchAnalysis, OpenAIClient
from server.storage import Store


def _seed(store: Store, tmp_path) -> tuple[str, str, str]:
    project_id = "ai-api-project"
    clause_id = "ai-api-clause"
    candidate_id = "ai-api-candidate"
    store.create_project(
        {
            "id": project_id,
            "title": "GPT API 시험",
            "source_filename": "source.docx",
            "source_path": str(tmp_path / "source.docx"),
            "source_sha256": "ai-api-sha",
            "uploaded_at": "2026-09-05T00:00:00+00:00",
            "kcs_snapshot": "2026-09-05T00:00:00+09:00",
            "kcs_revision": "test-revision",
            "kcs_scope": "KCS 41 31",
        },
        [
            {
                "id": clause_id,
                "source_order": 1,
                "label": "1.1",
                "title": "검사",
                "content": "포스코 검사를 실시하여야 한다.",
                "source_type": "paragraph",
                "outline_level": None,
                "candidates": [
                    {
                        "id": candidate_id,
                        "rank": 1,
                        "kcs_code": "KCS 41 31 05",
                        "document_name": "강구조공사 일반사항",
                        "version": "2026",
                        "update_date": "2026-08-01",
                        "kcs_clause": "3.2.1",
                        "title": "검사",
                        "content": "검사를 실시한다.",
                        "score": 0.72,
                        "classification": "관련성 높음",
                        "reasons": [],
                        "warnings": [],
                    }
                ],
            }
        ],
    )
    return project_id, clause_id, candidate_id


class _FakeAI:
    available = True

    def __init__(self, relation_type: str):
        self.calls = 0
        self.relation_type = relation_type

    def analyze_match(self, posco_text: str, kcs_text: str) -> MatchAnalysis:
        self.calls += 1
        assert "포스코 검사를" in posco_text
        assert "KCS 41 31 05" in kcs_text
        return MatchAnalysis(
            relation_type=self.relation_type,
            confidence=0.94,
            rationale="기술적 의무를 비교했습니다.",
            simplified_content="포스코 추가 검사를 실시한다.",
            model="gpt-test",
        )


class _CoverageFakeAI:
    available = True

    def __init__(self):
        self.calls = 0

    def analyze_coverage(self, posco_text, candidates, posco_context=""):
        self.calls += 1
        assert "포스코 검사를" in posco_text
        assert "1.1 검사" in posco_context
        assert candidates[0][0] == "C1"
        assert "KCS 41 31 05" in candidates[0][1]
        return CoverageAnalysis(
            coverage_status="fully_covered",
            confidence=0.95,
            requirements=(
                {
                    "requirement": "검사를 실시한다.",
                    "source_segment_ids": ["S1"],
                    "status": "covered",
                    "evidence_candidate_ids": ["C1"],
                    "evidence": "검사를 실시한다고 규정한다.",
                },
            ),
            residual_content="",
            rationale="KCS 후보가 포스코 요구사항을 모두 포함합니다.",
            model="gpt-test",
        )


class _MutatingCoverageFakeAI(_CoverageFakeAI):
    def __init__(self, store: Store, clause_id: str):
        super().__init__()
        self.store = store
        self.clause_id = clause_id

    def analyze_coverage(self, posco_text, candidates, posco_context=""):
        result = super().analyze_coverage(posco_text, candidates, posco_context)
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE clauses SET content = ? WHERE id = ?",
                ("GPT 호출 중 변경된 포스코 검사를 실시한다.", self.clause_id),
            )
        return result


def test_ai_analysis_requires_key(tmp_path, monkeypatch):
    store = Store(tmp_path / "no-key.sqlite3")
    project_id, clause_id, candidate_id = _seed(store, tmp_path)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "ai_client", OpenAIClient(None))

    with TestClient(app_module.app) as client:
        response = client.post(
            f"/api/projects/{project_id}/clauses/{clause_id}/candidates/{candidate_id}/ai-analysis"
        )

    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["detail"]


def test_unrelated_analysis_is_cached_and_candidate_disappears(tmp_path, monkeypatch):
    store = Store(tmp_path / "unrelated.sqlite3")
    project_id, clause_id, candidate_id = _seed(store, tmp_path)
    fake = _FakeAI("unrelated")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "ai_client", fake)

    url = f"/api/projects/{project_id}/clauses/{clause_id}/candidates/{candidate_id}/ai-analysis"
    with TestClient(app_module.app) as client:
        first = client.post(url)
        second = client.post(url)
        restored = client.delete(url)

    assert first.status_code == 200
    assert first.json()["excluded"] is True
    assert first.json()["clause"]["candidates"] == []
    assert second.status_code == 200
    assert fake.calls == 1
    assert restored.status_code == 200
    assert restored.json()["clause"]["excluded_candidates"] == []
    assert restored.json()["clause"]["candidates"][0]["id"] == candidate_id
    assert store.get_project(project_id)["candidate_clauses"] == 1


def test_combined_coverage_analysis_maps_evidence_ids_caches_and_enables_guarded_delete(tmp_path, monkeypatch):
    store = Store(tmp_path / "coverage.sqlite3")
    project_id, clause_id, candidate_id = _seed(store, tmp_path)
    fake = _CoverageFakeAI()
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "ai_client", fake)

    url = f"/api/projects/{project_id}/clauses/{clause_id}/coverage-analysis"
    with TestClient(app_module.app) as client:
        first = client.post(url)
        second = client.post(url)
        deleted = client.patch(
            f"/api/projects/{project_id}/clauses/{clause_id}",
            json={
                "decision": "delete",
                "decision_reason": "fully_covered_by_kcs",
                "coverage_confirmed": True,
                "selected_candidate_id": candidate_id,
            },
        )

    assert first.status_code == 200
    assert first.json()["analysis"]["deletion_safe"] is True
    assert first.json()["analysis"]["evidence_candidate_ids"] == [candidate_id]
    assert first.json()["analysis"]["requirements"][0]["evidence_candidate_ids"] == [candidate_id]
    assert second.status_code == 200
    assert fake.calls == 1
    assert deleted.status_code == 200
    assert deleted.json()["clause"]["decision"] == "delete"


def test_coverage_api_rejects_source_changed_while_gpt_is_running(tmp_path, monkeypatch):
    store = Store(tmp_path / "coverage-source-race.sqlite3")
    project_id, clause_id, _candidate_id = _seed(store, tmp_path)
    fake = _MutatingCoverageFakeAI(store, clause_id)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "ai_client", fake)

    with TestClient(app_module.app) as client:
        response = client.post(
            f"/api/projects/{project_id}/clauses/{clause_id}/coverage-analysis"
        )

    assert response.status_code == 400
    assert "분석 중 변경" in response.json()["detail"]
    with store.connect() as connection:
        saved_count = connection.execute(
            "SELECT COUNT(*) FROM clause_coverage_analysis WHERE clause_id = ?",
            (clause_id,),
        ).fetchone()[0]
    assert saved_count == 0


def test_coverage_checks_split_first_line_and_preserves_material_context(tmp_path, monkeypatch):
    store = Store(tmp_path / "split-source.sqlite3")
    project_id, clause_id, _ = _seed(store, tmp_path)
    with store.connect() as connection:
        connection.execute("UPDATE clauses SET title = ?, content = ?, match_context = ? WHERE id = ?",
                           ("내화 모르타르는 KS L 3202 규정에", "적합한 것을 사용한다.", "2.3 내화벽돌", clause_id))
    class CompleteSourceAI:
        available = True
        def analyze_coverage(self, posco_text, candidates, posco_context=""):
            assert posco_text == "내화 모르타르는 KS L 3202 규정에 적합한 것을 사용한다."
            assert "2.3 내화벽돌" in posco_context
            return CoverageAnalysis(coverage_status="posco_specific", confidence=.95,
                requirements=({"requirement": posco_text, "source_segment_ids": ["S1"], "status": "not_covered", "evidence_candidate_ids": [], "evidence": "시험 후보에 재료 규정이 없음"},),
                residual_content=posco_text, rationale="재료 규정이 다릅니다.", model="gpt-test")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "ai_client", CompleteSourceAI())
    with TestClient(app_module.app) as client:
        result = client.post(f"/api/projects/{project_id}/clauses/{clause_id}/coverage-analysis")
    assert result.status_code == 200, result.text
    assert "KS L 3202" in result.json()["analysis"]["requirements"][0]["requirement"]


def test_candidate_restore_api_cannot_remove_unsafe_coverage_gate(tmp_path, monkeypatch):
    store = Store(tmp_path / "unsafe-restore.sqlite3")
    project_id, clause_id, candidate_id = _seed(store, tmp_path)
    store.save_coverage_analysis(
        project_id,
        clause_id,
        [candidate_id],
        {
            "coverage_status": "partially_covered",
            "confidence": 0.96,
            "requirements": [
                {
                    "requirement": "포스코 추가 검사를 실시한다.",
                    "source_segment_ids": ["S1"],
                    "status": "not_covered",
                    "evidence_candidate_ids": [],
                    "evidence": "KCS에 추가 검사가 없다.",
                }
            ],
            "residual_content": "포스코 추가 검사를 실시하여야 한다.",
            "rationale": "포스코 고유 요구사항이 남습니다.",
        },
        "gpt-test",
    )
    fake = _FakeAI("unrelated")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "ai_client", fake)
    candidate_url = f"/api/projects/{project_id}/clauses/{clause_id}/candidates/{candidate_id}/ai-analysis"

    with TestClient(app_module.app) as client:
        invalid_restore = client.delete(candidate_url)
        excluded = client.post(candidate_url)
        restored = client.delete(candidate_url)
        deleted = client.patch(
            f"/api/projects/{project_id}/clauses/{clause_id}",
            json={
                "decision": "delete",
                "decision_reason": "fully_covered_by_kcs",
                "coverage_confirmed": True,
                "selected_candidate_id": candidate_id,
            },
        )

    assert invalid_restore.status_code == 400
    assert excluded.status_code == 200 and excluded.json()["excluded"] is True
    assert restored.status_code == 200
    assert restored.json()["clause"]["coverage_analysis"]["coverage_status"] == "uncertain"
    assert restored.json()["clause"]["coverage_analysis"]["deletion_safe"] is False
    assert deleted.status_code == 400
    assert "전체포괄" in deleted.json()["detail"]
