from __future__ import annotations

import subprocess
import zipfile

import pytest

from server import documents


def _write_minimal_docx(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")


def test_linux_legacy_doc_conversion_uses_libreoffice(tmp_path, monkeypatch):
    source = tmp_path / "source.doc"
    output = tmp_path / "source.docx"
    source.write_bytes(b"legacy-doc")
    monkeypatch.setattr(documents, "IS_WINDOWS", False)
    monkeypatch.setattr(documents.shutil, "which", lambda name: "/usr/bin/libreoffice")

    def fake_run(command, **_kwargs):
        assert command == [
            "/usr/bin/libreoffice",
            "--headless",
            "--convert-to",
            "docx",
            "--outdir",
            str(tmp_path),
            str(source),
        ]
        _write_minimal_docx(output)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(documents.subprocess, "run", fake_run)

    assert documents.convert_legacy_doc(source, output) == output.read_bytes()


def test_linux_legacy_doc_conversion_requires_libreoffice(tmp_path, monkeypatch):
    source = tmp_path / "source.doc"
    output = tmp_path / "source.docx"
    source.write_bytes(b"legacy-doc")
    monkeypatch.setattr(documents, "IS_WINDOWS", False)
    monkeypatch.setattr(documents.shutil, "which", lambda _name: None)

    with pytest.raises(ValueError, match="LibreOffice"):
        documents.convert_legacy_doc(source, output)
