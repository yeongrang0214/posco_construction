from __future__ import annotations

from server.config import get_settings


def test_get_settings_reads_openai_key_from_existing_spec_matcher_env(
    tmp_path, monkeypatch
):
    legacy_dir = tmp_path / "spec-matcher"
    legacy_dir.mkdir()
    (legacy_dir / ".env").write_text(
        "OPENAI_API_KEY=test-fallback-key\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("KCS_DATA_DIR", str(tmp_path / "kcs downloads"))
    monkeypatch.setenv("SPEC_MANAGER_DATA_DIR", str(tmp_path / "app-data"))

    settings = get_settings()

    assert settings.openai_api_key == "test-fallback-key"


def test_process_openai_key_takes_precedence_over_fallback(tmp_path, monkeypatch):
    legacy_dir = tmp_path / "spec-matcher"
    legacy_dir.mkdir()
    (legacy_dir / ".env").write_text(
        "OPENAI_API_KEY=test-fallback-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-process-key")
    monkeypatch.setenv("KCS_DATA_DIR", str(tmp_path / "kcs downloads"))
    monkeypatch.setenv("SPEC_MANAGER_DATA_DIR", str(tmp_path / "app-data"))

    settings = get_settings()

    assert settings.openai_api_key == "test-process-key"
