from concurrent.futures import Future
from pathlib import Path

import pytest

from server import source_preview as preview


def page(text, number=1):
    return {"page": number, "width": 595, "height": 842, "chars": [[char, i * 5, 100, i * 5 + 5, 110] for i, char in enumerate(preview.normalized(text))]}


def clause(identifier, order, text, title=""):
    return {"id": identifier, "source_order": order, "content": text, "title": title}


def test_multi_page_clause_has_all_highlight_boxes():
    index = {"pages": [page("공작도를 작성하여"), page("담당자의 승인을 받는다", 2)]}
    mapped = preview.map_clauses(index, [clause("a", 1, "공작도를 작성하여 담당자의 승인을 받는다")])
    assert {box["page"] for box in mapped["items"][0]["boxes"]} == {1, 2}
    assert "chars" not in mapped["pages"][0]


def test_duplicate_phrases_follow_document_order():
    index = {"pages": [page("승인을 받는다 승인을 받는다")]}
    mapped = preview.map_clauses(index, [clause("a", 1, "승인을 받는다"), clause("b", 2, "승인을 받는다")])
    assert mapped["mapped_count"] == 2
    assert mapped["items"][0]["boxes"][0]["left"] < mapped["items"][1]["boxes"][0]["left"]


def test_absent_text_is_not_mapped_to_similar_clause():
    mapped = preview.map_clauses({"pages": [page("강재 20 mm") ]}, [clause("a", 1, "강재 30 mm")])
    assert mapped["items"][0]["status"] == "unmapped"
    assert mapped["items"][0]["boxes"] == []


def test_table_between_sentences_is_not_highlighted_as_sentence():
    index = {"pages": [page("아래 기준에 따른다. 표 1 20mm 30mm 가붙임 간격은 15배로 한다.")]}
    mapped = preview.map_clauses(index, [clause("a", 1, "아래 기준에 따른다. 가붙임 간격은 15배로 한다.")])
    assert mapped["mapped_count"] == 1
    boxes = mapped["items"][0]["boxes"]
    assert len(boxes) == 2
    assert boxes[0]["right"] < boxes[1]["left"]


def test_missing_second_sentence_prevents_partial_false_mapping():
    index = {"pages": [page("아래 기준에 따른다. 다른 조항이다.")]}
    mapped = preview.map_clauses(index, [clause("a", 1, "아래 기준에 따른다. 가붙임 간격은 15배로 한다.")])
    assert mapped["mapped_count"] == 0


def test_repeated_poll_does_not_start_duplicate_conversion(tmp_path, monkeypatch):
    source = tmp_path / "source.docx"
    source.write_bytes(b"test")
    calls = []
    future = Future()
    class Executor:
        def submit(self, *args):
            calls.append(args)
            return future
    monkeypatch.setattr(preview, "_executor", Executor())
    monkeypatch.setattr(preview, "_jobs", {})
    for _ in range(3):
        assert preview.request_preview(source, tmp_path / "cache")[0] == "processing"
    assert len(calls) == 1
    future.set_exception(RuntimeError("conversion failed"))
    with pytest.raises(RuntimeError):
        preview.request_preview(source, tmp_path / "cache")
    assert len(calls) == 1


def test_source_change_invalidates_cache(tmp_path):
    source = tmp_path / "source.docx"
    source.write_bytes(b"first")
    first = preview.preview_path(source, tmp_path)
    source.write_bytes(b"second")
    assert first != preview.preview_path(source, tmp_path)


def test_material_row_uses_whole_row_not_merged_application_height():
    p = page("SM275A SM275B")
    p["material_rows"] = [
        {"grade": "sm275a", "left": 0, "right": 500, "top": 99, "bottom": 115},
        {"grade": "sm275b", "left": 0, "right": 500, "top": 120, "bottom": 145},
    ]
    for char in p["chars"][6:]:
        char[2], char[4] = 125, 135
    rows = [{**clause("a", 1, "1종 | SM275A | 두께200 mm 이하"), "source_type": "table"},
            {**clause("b", 2, "1종 | SM275B | 두께200 mm 이하"), "source_type": "table"}]
    mapped = preview.map_clauses({"pages": [p]}, rows)
    assert [m["status"] for m in mapped["items"]] == ["table_row", "table_row"]
    assert mapped["items"][0]["boxes"][0]["bottom"] < mapped["items"][1]["boxes"][0]["top"]
    assert mapped["items"][1]["boxes"][0]["right"] == 500


def test_grade_without_table_geometry_never_invents_row_box():
    row = {**clause("a", 1, "1종 | SM275A | 두께200 mm 이하"), "source_type": "table"}
    result = preview.map_clauses({"pages": [page("SM275A 다른 내용")]}, [row])
    assert result["items"][0]["status"] == "unmapped"
