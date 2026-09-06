from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
import server.kcs_sync as kcs_sync_module
from server.config import Settings
from server.kcs_sync import catalog_revision, sync_kcs
from server.matcher import (
    _rank_matches,
    activate_matcher_revision,
    clear_matcher_caches,
    read_snapshot,
)
from server.storage import Store


def _catalog_item(*, name: str = "강구조공사", parent_name: str = "건축공사") -> dict:
    return {
        "codeType": "KCS",
        "code": "413105",
        "fullCode": "KCS 41 31 05",
        "name": name,
        "version": "2026",
        "updateDate": "2026-09-01",
        "listParentCodes": [
            {"code": "41", "fullCode": "KCS 41", "name": parent_name}
        ],
    }


def _codeviewer_record(
    item: dict,
    *,
    sections: list[dict] | None = None,
    **overrides,
) -> dict:
    record = {
        field: item[field]
        for field in ("codeType", "code", "fullCode", "name", "version", "updateDate")
    }
    record["list"] = sections if sections is not None else [
        {"label": "3.2.1", "title": "정상 문서", "contents": "정상 본문"}
    ]
    record.update(overrides)
    return record


def _settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    kcs_dir = tmp_path / "kcs"
    raw_dir = kcs_dir / "raw"
    for path in (data_dir, data_dir / "uploads", data_dir / "exports", raw_dir):
        path.mkdir(parents=True, exist_ok=True)
    return Settings(
        project_root=tmp_path,
        data_dir=data_dir,
        uploads_dir=data_dir / "uploads",
        exports_dir=data_dir / "exports",
        database_path=data_dir / "test.sqlite3",
        kcs_data_dir=kcs_dir,
        kcs_raw_dir=raw_dir,
        kcs_manifest_path=kcs_dir / "manifest.json",
        openai_api_key=None,
        openai_base_url="https://example.test/v1",
        openai_embedding_model="test-embedding",
        openai_embedding_dimensions=8,
        openai_rerank_model="test-model",
        openai_timeout_seconds=1,
    )


def _ready_project(store: Store, tmp_path, revision: str = "revision-a") -> str:
    project_id = "revision-project"
    store.create_project(
        {
            "id": project_id,
            "title": "KCS 개정 시험",
            "source_filename": "source.docx",
            "source_path": str(tmp_path / "source.docx"),
            "source_sha256": "sha",
            "uploaded_at": "2026-09-05T00:00:00+00:00",
            "kcs_snapshot": "snapshot-a",
            "kcs_revision": revision,
            "kcs_scope": "KCS 41 31",
        },
        [
            {
                "id": "revision-clause",
                "source_order": 1,
                "label": "1.1",
                "title": "포스코 기준",
                "content": "포스코 강화 기준을 적용한다.",
                "source_type": "paragraph",
                "outline_level": None,
                "candidates": [],
            }
        ],
    )
    store.update_decision(
        project_id,
        "revision-clause",
        "keep",
        "",
        "",
        None,
        decision_reason="posco_stricter",
    )
    return project_id


def test_catalog_revision_is_stable_and_tracks_retrieval_metadata():
    original = [_catalog_item()]
    original[0]["listParentCodes"].append(
        {"code": "4100", "fullCode": "KCS 41 00", "name": "건축공사 일반"}
    )
    reordered_parents = json.loads(json.dumps(original, ensure_ascii=False))
    reordered_parents[0]["listParentCodes"] = list(
        reversed(reordered_parents[0]["listParentCodes"])
    )

    assert catalog_revision(original) == catalog_revision(reordered_parents)
    assert catalog_revision(original) != catalog_revision(
        [_catalog_item(parent_name="건축물 강구조공사")]
    )
    with pytest.raises(ValueError, match="비어"):
        catalog_revision([{"codeType": "KDS", "code": "413105"}])


def test_read_snapshot_rejects_manifest_catalog_mismatch(tmp_path):
    kcs_dir = tmp_path / "kcs"
    kcs_dir.mkdir()
    code_list = [_catalog_item()]
    (kcs_dir / "code_list.json").write_text(
        json.dumps(code_list, ensure_ascii=False), encoding="utf-8"
    )
    (kcs_dir / "manifest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "latestOnly": True,
                "completedAt": "snapshot-a",
                "revision": "incorrect-revision",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="일치하지 않습니다"):
        read_snapshot(kcs_dir / "manifest.json")


def test_read_snapshot_rejects_missing_completion_time(tmp_path):
    kcs_dir = tmp_path / "kcs"
    kcs_dir.mkdir()
    code_list = [_catalog_item()]
    (kcs_dir / "code_list.json").write_text(
        json.dumps(code_list, ensure_ascii=False), encoding="utf-8"
    )
    (kcs_dir / "manifest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "latestOnly": True,
                "revision": catalog_revision(code_list),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="완료시각"):
        read_snapshot(kcs_dir / "manifest.json")


def test_read_snapshot_upgrades_legacy_catalog_revision_to_body_bound_revision(
    tmp_path,
):
    kcs_dir = tmp_path / "kcs"
    raw_dir = kcs_dir / "raw"
    raw_dir.mkdir(parents=True)
    item = _catalog_item()
    code_list = [item]
    catalog_hash = catalog_revision(code_list)
    (kcs_dir / "code_list.json").write_text(
        json.dumps(code_list, ensure_ascii=False), encoding="utf-8"
    )
    (raw_dir / "KCS_413105.json").write_text(
        json.dumps([_codeviewer_record(item)], ensure_ascii=False), encoding="utf-8"
    )
    (kcs_dir / "manifest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "latestOnly": True,
                "completedAt": "snapshot-a",
                "documentCount": 1,
                "revision": catalog_hash,
            }
        ),
        encoding="utf-8",
    )

    snapshot, manifest = read_snapshot(kcs_dir / "manifest.json")

    assert snapshot == "snapshot-a"
    assert manifest["revision"] != catalog_hash
    assert manifest["usableDocumentCount"] == 1


def test_catalog_revision_change_marks_project_stale_and_blocks_final(tmp_path):
    store = Store(tmp_path / "revision.sqlite3")
    project_id = _ready_project(store, tmp_path)
    assert store.get_project(project_id)["review_submission_ready"] is True

    store.set_current_kcs_revision("snapshot-b", "revision-b")
    project = store.get_project(project_id)

    assert project["kcs_stale"] is True
    assert project["final_export_ready"] is False
    assert "최신 KCS 재매칭 대기 중" in project["final_export_blockers"]
    bundle = store.final_export_bundle(project_id)
    assert bundle is not None and bundle[0]["final_export_ready"] is False


def test_same_catalog_revision_with_new_snapshot_does_not_stale_project(tmp_path):
    store = Store(tmp_path / "same-revision.sqlite3")
    project_id = _ready_project(store, tmp_path)

    store.set_current_kcs_revision("snapshot-b", "revision-a")
    project = store.get_project(project_id)

    assert project["kcs_stale"] is False
    assert project["review_submission_ready"] is True


def test_final_endpoint_reconciles_external_catalog_change(tmp_path, monkeypatch):
    store = Store(tmp_path / "external-revision.sqlite3")
    project_id = _ready_project(store, tmp_path)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(
        app_module,
        "read_snapshot",
        lambda _path: ("snapshot-b", {"revision": "revision-b"}),
    )

    with TestClient(app_module.app) as client:
        response = client.get(f"/api/projects/{project_id}/export/final")
        review = client.get(f"/api/projects/{project_id}/export/review")

    assert response.status_code == 409
    assert "KCS 재매칭" in response.json()["detail"]
    assert review.status_code == 200


def test_create_project_rejects_revision_changed_during_matching(tmp_path):
    store = Store(tmp_path / "mid-match.sqlite3")
    store.set_current_kcs_revision("snapshot-b", "revision-b")

    with pytest.raises(ValueError, match="매칭 중 갱신"):
        _ready_project(store, tmp_path, revision="revision-a")

    assert store.list_projects() == []


def test_legacy_revision_backfills_only_exact_snapshot(tmp_path):
    store = Store(tmp_path / "legacy-revisions.sqlite3")
    with store.connect() as connection:
        for project_id, snapshot in (
            ("same-snapshot", "snapshot-current"),
            ("old-snapshot", "snapshot-old"),
        ):
            connection.execute(
                """
                INSERT INTO projects(
                    id, title, source_filename, source_path, source_sha256,
                    uploaded_at, kcs_snapshot, kcs_revision, kcs_scope
                ) VALUES (?, ?, 'source.docx', 'source.docx', 'sha',
                          '2026-01-01', ?, '', 'KCS 41')
                """,
                (project_id, project_id, snapshot),
            )

    store.set_current_kcs_revision("snapshot-current", "revision-current")
    same = store.get_project("same-snapshot")
    old = store.get_project("old-snapshot")

    assert same["kcs_revision"] == "revision-current"
    assert same["kcs_stale"] is False
    assert old["kcs_revision"] == ""
    assert old["kcs_stale"] is True


def test_sync_revision_change_fields_follow_effective_snapshot(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    state = {"item": _catalog_item()}

    def fake_request(path: str, _api_key: str):
        item = state["item"]
        return [item] if path == "CodeList" else [_codeviewer_record(item)]

    monkeypatch.setattr(kcs_sync_module, "_load_api_key", lambda _settings: "key")
    monkeypatch.setattr(kcs_sync_module, "_request_json", fake_request)

    first = sync_kcs(settings)
    identical = sync_kcs(settings)
    state["item"] = _catalog_item(name="개정 명칭")
    metadata_changed = sync_kcs(settings)

    assert first["previous_revision"] == ""
    assert first["revision_changed"] is True
    assert identical["changed_count"] == 0
    assert identical["previous_revision"] == first["kcs_revision"]
    assert identical["kcs_revision"] == first["kcs_revision"]
    assert identical["revision_changed"] is False
    assert metadata_changed["changed_count"] == 1
    assert metadata_changed["previous_revision"] == identical["kcs_revision"]
    assert metadata_changed["kcs_revision"] != identical["kcs_revision"]
    assert metadata_changed["revision_changed"] is True


def test_sync_normalizes_legacy_catalog_revision_before_comparison(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    item = _catalog_item()
    monkeypatch.setattr(kcs_sync_module, "_load_api_key", lambda _settings: "key")
    monkeypatch.setattr(
        kcs_sync_module,
        "_request_json",
        lambda path, _key: [item]
        if path == "CodeList"
        else [_codeviewer_record(item)],
    )

    first = sync_kcs(settings)
    legacy_manifest = json.loads(
        settings.kcs_manifest_path.read_text(encoding="utf-8")
    )
    legacy_manifest.pop("rawIntegrity")
    legacy_manifest["revision"] = catalog_revision([item])
    settings.kcs_manifest_path.write_text(
        json.dumps(legacy_manifest, ensure_ascii=False), encoding="utf-8"
    )

    identical = sync_kcs(settings)

    assert legacy_manifest["revision"] != first["kcs_revision"]
    assert identical["changed_count"] == 0
    assert identical["previous_revision"] == first["kcs_revision"]
    assert identical["kcs_revision"] == first["kcs_revision"]
    assert identical["revision_changed"] is False


def test_sync_redownloads_when_document_name_changes(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    old_item = _catalog_item(name="기존 명칭")
    new_item = _catalog_item(name="변경 명칭")
    settings.kcs_data_dir.joinpath("code_list.json").write_text(
        json.dumps([old_item], ensure_ascii=False), encoding="utf-8"
    )
    settings.kcs_manifest_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "latestOnly": True,
                "completedAt": "snapshot-a",
                "revision": catalog_revision([old_item]),
            }
        ),
        encoding="utf-8",
    )
    settings.kcs_raw_dir.joinpath("KCS_413105.json").write_text(
        json.dumps([_codeviewer_record(old_item)], ensure_ascii=False),
        encoding="utf-8",
    )
    requested: list[str] = []

    def fake_request(path: str, _api_key: str):
        requested.append(path)
        if path == "CodeList":
            return [new_item]
        return [_codeviewer_record(new_item)]

    monkeypatch.setattr(kcs_sync_module, "_load_api_key", lambda _settings: "key")
    monkeypatch.setattr(kcs_sync_module, "_request_json", fake_request)

    callback_lock_states: list[bool] = []
    result = sync_kcs(
        settings,
        lambda _result: callback_lock_states.append(kcs_sync_module.SYNC_LOCK.locked()),
    )

    assert result["changed_count"] == 1
    assert callback_lock_states == [True]
    assert "CodeViewer/KCS/413105" in requested
    assert read_snapshot(settings.kcs_manifest_path)[1]["revision"] == result["kcs_revision"]


def test_sync_failure_after_publish_begins_leaves_fail_closed_manifest(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    item = _catalog_item()
    monkeypatch.setattr(kcs_sync_module, "_load_api_key", lambda _settings: "key")
    monkeypatch.setattr(
        kcs_sync_module,
        "_request_json",
        lambda path, _key: [item]
        if path == "CodeList"
        else [_codeviewer_record(item)],
    )
    original_write = kcs_sync_module._write_json_atomic

    def fail_code_list(path, payload):
        if path.name == "code_list.json":
            raise OSError("simulated publish failure")
        original_write(path, payload)

    monkeypatch.setattr(kcs_sync_module, "_write_json_atomic", fail_code_list)

    with pytest.raises(OSError, match="simulated"):
        sync_kcs(settings)

    manifest = json.loads(settings.kcs_manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "updating"
    with pytest.raises(RuntimeError, match="최신본"):
        read_snapshot(settings.kcs_manifest_path)


def test_sync_quarantines_empty_codeviewer_as_unavailable(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    item = _catalog_item()
    monkeypatch.setattr(kcs_sync_module, "_load_api_key", lambda _settings: "key")
    monkeypatch.setattr(
        kcs_sync_module,
        "_request_json",
        lambda path, _key: [item] if path == "CodeList" else [],
    )

    result = sync_kcs(settings)

    manifest = json.loads(settings.kcs_manifest_path.read_text(encoding="utf-8"))
    raw = json.loads(
        settings.kcs_raw_dir.joinpath("KCS_413105.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "completed"
    assert result["usable_document_count"] == 0
    assert result["unavailable_document_count"] == 1
    assert raw[0]["code"] == "413105"
    assert raw[0]["bodyUnavailableReason"] == "empty_response"


def test_sync_rejects_malformed_codeviewer(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    item = _catalog_item()
    monkeypatch.setattr(kcs_sync_module, "_load_api_key", lambda _settings: "key")
    monkeypatch.setattr(
        kcs_sync_module,
        "_request_json",
        lambda path, _key: [item] if path == "CodeList" else [{}],
    )

    with pytest.raises(RuntimeError, match="내려받지 못했습니다"):
        sync_kcs(settings)

    manifest = json.loads(settings.kcs_manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "updating"


@pytest.mark.parametrize(
    "section",
    (
        {"label": "1.1"},
        {"label": "1.2", "title": "  ", "contents": "<p>&nbsp;</p>"},
    ),
    ids=("label-only", "empty-html"),
)
def test_sync_rejects_codeviewer_sections_without_searchable_text(
    tmp_path, monkeypatch, section
):
    settings = _settings(tmp_path)
    item = _catalog_item()
    payload = [_codeviewer_record(item, sections=[section])]
    monkeypatch.setattr(kcs_sync_module, "_load_api_key", lambda _settings: "key")
    monkeypatch.setattr(
        kcs_sync_module,
        "_request_json",
        lambda path, _key: [item] if path == "CodeList" else payload,
    )

    with pytest.raises(RuntimeError, match="내려받지 못했습니다"):
        sync_kcs(settings)

    manifest = json.loads(settings.kcs_manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "updating"
    assert not settings.kcs_raw_dir.joinpath("KCS_413105.json").exists()


def test_sync_quarantines_provider_wrong_code_without_indexing_it(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    item = _catalog_item()
    provider_fixed = False

    def fake_request(path: str, _key: str):
        if path == "CodeList":
            return [item]
        if provider_fixed:
            return [_codeviewer_record(item)]
        return [
            {
                "code": "143140",
                "codeType": "KCS",
                "fullCode": "KCS 14 31 40",
                "name": "잘못된 문서",
                "version": item["version"],
                "updateDate": item["updateDate"],
                "list": [{"title": "잘못된 문서"}],
            }
        ]

    monkeypatch.setattr(kcs_sync_module, "_load_api_key", lambda _settings: "key")
    monkeypatch.setattr(kcs_sync_module, "_request_json", fake_request)

    unavailable_result = sync_kcs(settings)
    raw = json.loads(
        settings.kcs_raw_dir.joinpath("KCS_413105.json").read_text(encoding="utf-8")
    )

    assert unavailable_result["usable_document_count"] == 0
    assert unavailable_result["unavailable_document_count"] == 1
    assert unavailable_result["previous_revision"] == ""
    assert unavailable_result["revision_changed"] is True
    assert raw[0]["code"] == "413105"
    assert raw[0]["bodyUnavailableReason"] == "provider_code_mismatch"
    assert raw[0]["providerResponseCodes"] == ["143140"]

    provider_fixed = True
    usable_result = sync_kcs(settings)
    repaired = json.loads(
        settings.kcs_raw_dir.joinpath("KCS_413105.json").read_text(encoding="utf-8")
    )

    assert usable_result["changed_count"] == 1
    assert usable_result["usable_document_count"] == 1
    assert usable_result["unavailable_document_count"] == 0
    assert usable_result["previous_revision"] == unavailable_result["kcs_revision"]
    assert usable_result["kcs_revision"] != unavailable_result["kcs_revision"]
    assert usable_result["revision_changed"] is True
    assert repaired[0]["code"] == "413105"


@pytest.mark.parametrize(
    ("field", "stale_value"),
    (("version", "2025"), ("name", "이전 문서 명칭")),
    ids=("stale-version", "metadata-mismatch"),
)
def test_sync_quarantines_matching_code_with_stale_catalog_metadata(
    tmp_path, monkeypatch, field, stale_value
):
    settings = _settings(tmp_path)
    item = _catalog_item()
    stale_record = _codeviewer_record(item, **{field: stale_value})
    monkeypatch.setattr(kcs_sync_module, "_load_api_key", lambda _settings: "key")
    monkeypatch.setattr(
        kcs_sync_module,
        "_request_json",
        lambda path, _key: [item] if path == "CodeList" else [stale_record],
    )

    result = sync_kcs(settings)
    raw = json.loads(
        settings.kcs_raw_dir.joinpath("KCS_413105.json").read_text(encoding="utf-8")
    )

    assert result["usable_document_count"] == 0
    assert result["unavailable_document_count"] == 1
    assert raw[0]["bodyUnavailableReason"] == "provider_metadata_mismatch"
    assert raw[0]["providerResponseCodes"] == ["413105"]
    assert raw[0]["version"] == item["version"]
    clear_matcher_caches()
    assert _rank_matches("정상 본문", settings.kcs_raw_dir, ("4131",)) == []


def test_sync_uses_exact_current_record_when_stale_record_is_also_returned(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    item = _catalog_item()
    stale_record = _codeviewer_record(
        item,
        sections=[{"label": "1.1"}],
        version="2025",
        updateDate="2025-01-01",
    )
    current_record = _codeviewer_record(item)
    monkeypatch.setattr(kcs_sync_module, "_load_api_key", lambda _settings: "key")
    monkeypatch.setattr(
        kcs_sync_module,
        "_request_json",
        lambda path, _key: [item]
        if path == "CodeList"
        else [stale_record, current_record],
    )

    result = sync_kcs(settings)
    raw = json.loads(
        settings.kcs_raw_dir.joinpath("KCS_413105.json").read_text(encoding="utf-8")
    )

    assert result["usable_document_count"] == 1
    assert result["unavailable_document_count"] == 0
    assert raw == [current_record]


def test_sync_repairs_corrupt_raw_even_when_catalog_revision_is_same(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    item = _catalog_item()
    settings.kcs_data_dir.joinpath("code_list.json").write_text(
        json.dumps([item], ensure_ascii=False), encoding="utf-8"
    )
    settings.kcs_manifest_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "latestOnly": True,
                "completedAt": "snapshot-a",
                "revision": catalog_revision([item]),
            }
        ),
        encoding="utf-8",
    )
    raw_path = settings.kcs_raw_dir / "KCS_413105.json"
    raw_path.write_text("{", encoding="utf-8")
    requested: list[str] = []

    def fake_request(path: str, _key: str):
        requested.append(path)
        if path == "CodeList":
            return [item]
        return [_codeviewer_record(item)]

    monkeypatch.setattr(kcs_sync_module, "_load_api_key", lambda _settings: "key")
    monkeypatch.setattr(kcs_sync_module, "_request_json", fake_request)

    result = sync_kcs(settings)

    assert result["changed_count"] == 1
    assert "CodeViewer/KCS/413105" in requested
    assert isinstance(json.loads(raw_path.read_text(encoding="utf-8")), list)


def test_sync_redownloads_and_repairs_structurally_valid_tampered_raw(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    item = _catalog_item()
    provider_record = _codeviewer_record(
        item,
        sections=[
            {"label": "3.2.1", "title": "표면 처리", "contents": "공급자 원문"}
        ],
    )
    requested: list[str] = []

    def fake_request(path: str, _key: str):
        requested.append(path)
        return [item] if path == "CodeList" else [provider_record]

    monkeypatch.setattr(kcs_sync_module, "_load_api_key", lambda _settings: "key")
    monkeypatch.setattr(kcs_sync_module, "_request_json", fake_request)

    first = sync_kcs(settings)
    first_manifest = json.loads(settings.kcs_manifest_path.read_text(encoding="utf-8"))
    raw_path = settings.kcs_raw_dir / "KCS_413105.json"
    tampered = json.loads(raw_path.read_text(encoding="utf-8"))
    tampered[0]["list"][0]["contents"] = "구조적으로 유효한 로컬 변조"
    raw_path.write_text(
        json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    requested.clear()

    second = sync_kcs(settings)
    repaired = json.loads(raw_path.read_text(encoding="utf-8"))
    second_manifest = json.loads(settings.kcs_manifest_path.read_text(encoding="utf-8"))

    assert requested == ["CodeList", "CodeViewer/KCS/413105"]
    assert second["changed_count"] == 1
    assert repaired == [provider_record]
    assert second["previous_revision"] == first["kcs_revision"]
    assert second["kcs_revision"] == first["kcs_revision"]
    assert second["revision_changed"] is False
    assert second_manifest["rawIntegrity"] == first_manifest["rawIntegrity"]


def test_revision_activation_reloads_warm_matcher_cache(tmp_path):
    kcs_dir = tmp_path / "kcs"
    raw_dir = kcs_dir / "raw"
    raw_dir.mkdir(parents=True)
    item_v1 = _catalog_item(name="기존 문서")
    content_v1 = "강재 표면의 기존 이물질을 제거한다."
    content_v2 = "강재 표면의 최신 염분을 측정한다."

    def write_version(item: dict, content: str):
        kcs_dir.joinpath("code_list.json").write_text(
            json.dumps([item], ensure_ascii=False), encoding="utf-8"
        )
        raw_dir.joinpath("KCS_413105.json").write_text(
            json.dumps(
                [
                    _codeviewer_record(
                        item,
                        sections=[
                            {"label": "3.2.1", "title": "표면", "contents": content}
                        ],
                    )
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    clear_matcher_caches()
    write_version(item_v1, content_v1)
    activate_matcher_revision(catalog_revision([item_v1]))
    assert _rank_matches(content_v1, raw_dir, ("4131",))[0][0]["content"] == content_v1

    item_v2 = _catalog_item(name="최신 문서")
    item_v2["version"] = "2027"
    item_v2["updateDate"] = "2027-01-01"
    write_version(item_v2, content_v2)
    activate_matcher_revision(catalog_revision([item_v2]))

    latest = _rank_matches(content_v2, raw_dir, ("4131",))[0][0]
    assert latest["content"] == content_v2
    assert latest["version"] == "2027"
