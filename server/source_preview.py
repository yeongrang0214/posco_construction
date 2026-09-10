"""Cached source PDFs for display only. Matching/export continue to use Word text."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pdfplumber
from .table_evidence import grades

PREVIEW_VERSION = "pdf-v2-table-rows"
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="source-pdf")
_lock = threading.Lock()
_jobs: dict[str, Future] = {}


def normalized(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", unicodedata.normalize("NFKC", text).lower())


def preview_path(source: Path, cache_dir: Path) -> Path:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return cache_dir / f"{PREVIEW_VERSION}-{digest}.pdf"


def convert_pdf(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Every conversion uses its own temporary output and Office profile. No source edits.
    with tempfile.TemporaryDirectory(prefix="source-pdf-", dir=destination.parent) as temporary:
        temp = Path(temporary)
        result_path = temp / "source.pdf"
        if os.name == "nt":
            command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                       "-File", str(Path(__file__).with_name("convert_source_pdf.ps1")),
                       "-InputPath", str(source.resolve()), "-OutputPath", str(result_path.resolve())]
        else:
            office = shutil.which("libreoffice") or shutil.which("soffice")
            if not office:
                raise RuntimeError("서버에 PDF 변환 프로그램이 없습니다.")
            local_source = temp / f"source{source.suffix.lower()}"
            shutil.copyfile(source, local_source)
            command = [office, f"-env:UserInstallation={(temp / 'profile').resolve().as_uri()}",
                       "--headless", "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(temp), str(local_source)]
        try:
            result = subprocess.run(command, capture_output=True, timeout=150, check=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("원문 PDF 변환 시간이 초과되었거나 변환기를 실행하지 못했습니다.") from exc
        if result.returncode or not result_path.is_file() or result_path.stat().st_size < 20:
            raise RuntimeError("원문 PDF 변환에 실패했습니다. 원본을 내려받거나 텍스트 보기로 검토하세요.")
        index = extract_index(result_path)
        if not index["pages"]:
            raise RuntimeError("PDF 페이지가 비어 있습니다.")
        (temp / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        # Publish only complete pairs; a restart may safely retry an unfinished conversion.
        result_path.replace(destination)
        (temp / "index.json").replace(destination.with_suffix(".json"))
    return destination


def extract_index(path: Path) -> dict:
    pages = []
    with pdfplumber.open(path) as document:
        for page in document.pages:
            if page.page_number > 500:
                raise RuntimeError("원문 미리보기는 최대 500쪽까지 지원합니다.")
            chars = []
            # PDF reading order uses physical lines, including table rows.
            for word in page.extract_words(return_chars=True, x_tolerance=2, y_tolerance=3):
                for char in word["chars"]:
                    text = normalized(char["text"])
                    for letter in text:
                        chars.append([letter, round(char["x0"], 2), round(char["top"], 2),
                                      round(char["x1"], 2), round(char["bottom"], 2)])
            material_rows = []
            for table in page.find_tables():
                for cell in table.cells:
                    x0, top, x1, bottom = cell
                    # Match an entire grade cell; table geometry supplies the row
                    # band even when adjacent application cells span several rows.
                    cell_chars = [c for c in chars if x0 <= (c[1] + c[3]) / 2 <= x1 and top <= (c[2] + c[4]) / 2 <= bottom]
                    value = "".join(c[0] for c in cell_chars)
                    identifiers = grades(value)
                    if len(identifiers) == 1 and value == normalized(next(iter(identifiers))):
                        material_rows.append({"grade": value, "left": table.bbox[0], "top": top,
                                              "right": table.bbox[2], "bottom": bottom})
            pages.append({"page": page.page_number, "width": float(page.width), "height": float(page.height), "chars": chars, "material_rows": material_rows})
            page.close()
    return {"pages": pages}


def request_preview(source: Path, cache_dir: Path) -> tuple[str, Path]:
    path = preview_path(source, cache_dir)
    if path.is_file() and path.with_suffix(".json").is_file():
        return "ready", path
    key = str(path)
    with _lock:
        future = _jobs.get(key)
        if future is None:
            # Bound queued work and forget completed jobs; files retain successful results.
            for old_key in list(_jobs):
                if _jobs[old_key].done() and old_key != key:
                    del _jobs[old_key]
            if len(_jobs) >= 8:
                raise RuntimeError("다른 원문을 변환 중입니다. 잠시 후 다시 시도하세요.")
            future = _executor.submit(convert_pdf, source, path)
            _jobs[key] = future
    if not future.done():
        return "processing", path
    try:
        future.result()
    except Exception as exc:
        # Retain failure while polling this job; another job or restart can evict it.
        raise RuntimeError("원문 PDF를 준비하지 못했습니다. 텍스트 보기 또는 원본 내려받기를 이용하세요.") from exc
    return "ready", path


def map_clauses(index: dict, clauses: list[dict]) -> dict:
    characters = []
    for page in index["pages"]:
        characters.extend((page["page"], *char) for char in page["chars"])
    text = "".join(char[1] for char in characters)
    cursor = 0
    items = []
    for clause in sorted(clauses, key=lambda row: row["source_order"]):
        title = str(clause.get("title") or "").removesuffix("…")
        content = str(clause.get("content") or "")
        identifiers = grades(content) if clause.get("source_type") == "table" else set()
        if len(identifiers) == 1:
            target = normalized(next(iter(identifiers)))
            start = text.find(target, cursor)
            if start >= 0:
                selected_chars = characters[start:start + len(target)]
                page_number = selected_chars[0][0]
                page = next(p for p in index["pages"] if p["page"] == page_number)
                rows = [r for r in page.get("material_rows", []) if r["grade"] == target and all(
                    p == page_number and r["left"] <= (x0 + x1) / 2 <= r["right"] and r["top"] <= (top + bottom) / 2 <= r["bottom"]
                    for p, _, x0, top, x1, bottom in selected_chars)]
                if rows:
                    row = min(rows, key=lambda r: r["bottom"] - r["top"])
                    box = {k: v for k, v in row.items() if k != "grade"}
                    items.append({"id": clause["id"], "status": "table_row", "boxes": [{"page": page_number, **box}]})
                    cursor = start + len(target)
                    continue
        whole = content if normalized(content).startswith(normalized(title)) else f"{title} {content}"
        choices = list(dict.fromkeys(filter(None, (normalized(whole), normalized(content)))))
        spans = []
        for target in choices:
            if len(target) < 4:
                continue
            start = text.find(target, cursor)
            if start >= 0:
                spans = [(start, start + len(target))]
                break
        if not spans:
            # A table/header can interrupt a multi-sentence Word paragraph. Require
            # every sentence to match exactly; never highlight the intervening text.
            parts = [normalized(part) for part in re.split(r"(?<=[.!?])\s+", whole)]
            parts = [part for part in parts if part]
            if len(parts) > 1 and all(len(part) >= 4 for part in parts):
                next_cursor = cursor
                for part in parts:
                    start = text.find(part, next_cursor)
                    if start < 0 or (spans and start - next_cursor > 2500):
                        spans = []
                        break
                    spans.append((start, start + len(part)))
                    next_cursor = start + len(part)
        boxes = []
        for start, end in spans:
            for char_index, (page, _, x0, top, x1, bottom) in enumerate(characters[start:end]):
                previous = boxes[-1] if boxes and char_index else None
                if previous and previous["page"] == page and abs(previous["top"] - top) < 3 and x0 <= previous["right"] + 24:
                    previous["left"] = min(previous["left"], x0)
                    previous["right"] = max(previous["right"], x1)
                    previous["bottom"] = max(previous["bottom"], bottom)
                else:
                    boxes.append({"page": page, "left": x0, "top": top, "right": x1, "bottom": bottom})
            cursor = end
        items.append({"id": clause["id"], "status": "exact" if boxes else "unmapped", "boxes": boxes})
    return {
        "status": "ready",
        "pages": [{k: page[k] for k in ("page", "width", "height")} for page in index["pages"]],
        "items": items,
        "mapped_count": sum(bool(item["boxes"]) for item in items),
        "total_count": len(items),
    }
