from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _openai_api_key(kcs_data_dir: Path) -> str | None:
    configured = (os.getenv("OPENAI_API_KEY") or "").strip()
    if configured:
        return configured
    for path in (
        kcs_data_dir.parent / ".env",
        kcs_data_dir.parent / "spec-matcher" / ".env",
    ):
        if not path.is_file():
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
            if name.removeprefix("export ").strip() != "OPENAI_API_KEY":
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if value:
                return value
    return None


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    uploads_dir: Path
    exports_dir: Path
    database_path: Path
    kcs_data_dir: Path
    kcs_raw_dir: Path
    kcs_manifest_path: Path
    openai_api_key: str | None = field(repr=False)
    openai_base_url: str
    openai_embedding_model: str
    openai_embedding_dimensions: int
    openai_rerank_model: str
    openai_timeout_seconds: float


def get_settings() -> Settings:
    data_dir = Path(os.getenv("SPEC_MANAGER_DATA_DIR", PROJECT_ROOT / "data"))
    kcs_data_dir = Path(
        os.getenv(
            "KCS_DATA_DIR",
            r"C:\Users\00byr\OneDrive\Desktop\260819\kcs downloads",
        )
    )
    settings = Settings(
        project_root=PROJECT_ROOT,
        data_dir=data_dir,
        uploads_dir=data_dir / "uploads",
        exports_dir=data_dir / "exports",
        database_path=data_dir / "spec_manager.sqlite3",
        kcs_data_dir=kcs_data_dir,
        kcs_raw_dir=kcs_data_dir / "raw",
        kcs_manifest_path=kcs_data_dir / "manifest.json",
        openai_api_key=_openai_api_key(kcs_data_dir),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        openai_embedding_model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        openai_embedding_dimensions=int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "256")),
        openai_rerank_model=os.getenv("OPENAI_RERANK_MODEL", "gpt-5.4-mini"),
        openai_timeout_seconds=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30")),
    )
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    return settings
