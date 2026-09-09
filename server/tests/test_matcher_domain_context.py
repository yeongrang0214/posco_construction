import json

from server.matcher import (
    _candidate_rows,
    _rank_matches,
    _stable_section_score,
    load_kcs_sections,
    match_clauses,
)


def _write_kcs(raw_dir, code, name, rows):
    (raw_dir / f"KCS_{code}.json").write_text(
        json.dumps(
            [{"code": code, "name": name, "version": "2026", "list": rows}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_shop_drawing_equivalents_prioritize_steel_fabrication(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    _write_kcs(
        raw_dir,
        "413115",
        "건축물 강구조공사 공장제작",
        [
            {
                "label": "3.2.1(1)",
                "title": "3.2.1 시공상세도의 내용",
                "contents": "<p>공장제작자는 공작도를 포함하는 시공상세도(shop drawing)를 작성한다.</p>",
            }
        ],
    )
    _write_kcs(
        raw_dir,
        "413001",
        "건축공사 일반사항",
        [
            {"label": "1", "title": "검사", "contents": "<p>담당원에게 보고한다.</p>"},
            {
                "label": "2",
                "title": "검사",
                "contents": "<p>상기 ①의 보고가 있는 경우 담당원의 검사를 받는다.</p>",
            },
        ],
    )

    ranked = _rank_matches("철골공사 공작도를 작성하여 승인을 받는다", raw_dir, ("4131",))

    rows = _candidate_rows(
        {"id": "00000000-0000-0000-0000-000000000002"},
        "철골공사 공작도를 작성하여 승인을 받는다",
        [(section, score, None) for section, score in ranked],
    )
    assert rows[0]["kcs_code"] == "KCS 41 31 15"
    assert "shop drawing" in rows[0]["content"]


def test_reference_candidate_includes_preceding_kcs_context(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    _write_kcs(
        raw_dir,
        "413001",
        "건축공사 일반사항",
        [
            {"label": "①", "title": "검사", "contents": "<p>공정 완료를 담당원에게 보고한다.</p>"},
            {
                "label": "②",
                "title": "검사",
                "contents": "<p>상기 ①의 보고가 있는 경우 담당원의 검사를 받는다.</p>",
            },
        ],
    )

    sections = load_kcs_sections(str(raw_dir), ("413001",))
    referenced = sections[1]
    clause = {"id": "00000000-0000-0000-0000-000000000001"}
    rows = _candidate_rows(clause, "검사 보고", [(referenced, 0.5, None)])

    assert referenced["context_resolved"] is True
    assert "[앞 조항] 공정 완료를 담당원에게 보고한다." in rows[0]["content"]
    assert "[현재 조항] 상기 ①의 보고가 있는 경우" in rows[0]["content"]
    assert "참조 표현의 앞 조항을 함께 비교했습니다." in rows[0]["reasons"]


def test_forward_reference_candidate_includes_following_kcs_context(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    _write_kcs(
        raw_dir,
        "413115",
        "건축물 강구조공사 공장제작",
        [
            {
                "label": "3.2.1(4)",
                "title": "3.2.1 시공상세도의 내용",
                "contents": "<p>시공상세도(공작도)는 아래의 내용을 명기한다.</p>",
            },
            {
                "label": "가",
                "title": "3.2.1 시공상세도의 내용",
                "contents": "<p>부재의 치수, 접합 상세와 조립 순서를 표시한다.</p>",
            },
        ],
    )

    section = load_kcs_sections(str(raw_dir), ("413115",))[0]
    rows = _candidate_rows(
        {"id": "00000000-0000-0000-0000-000000000003"},
        "공작도에 부재 치수와 조립 순서를 표시한다",
        [(section, 0.6, None)],
    )

    assert "[다음 조항] 부재의 치수, 접합 상세와 조립 순서를 표시한다." in rows[0]["content"]
    assert "참조 표현의 인접 조항을 함께 비교했습니다." in rows[0]["reasons"]


def test_short_source_fragment_uses_adjacent_requirements(tmp_path, monkeypatch):
    captured = []

    def fake_rank(text, *_args, **_kwargs):
        captured.append(text)
        return []

    monkeypatch.setattr("server.matcher._rank_matches", fake_rank)
    clauses = [
        {"id": "1", "source_type": "paragraph", "match_context": "10 용접", "title": "", "content": "맞댐용접을 한다."},
        {"id": "2", "source_type": "paragraph", "match_context": "10 용접", "title": "", "content": "아래 기준에 따른다."},
        {"id": "3", "source_type": "table", "match_context": "10 용접", "title": "", "content": "모재 두께 25 mm | 예열온도 80 ℃"},
    ]

    match_clauses(clauses, tmp_path, ("4131",))

    assert "맞댐용접을 한다" in captured[1]
    assert "모재 두께 25 mm" in captured[1]


def test_steel_scope_penalizes_unrelated_pipeline_candidate():
    steel = {
        "code": "KCS 41 31 15", "document_name": "강구조 공장제작", "title": "운반",
        "content": "철골 부재 운반 중 변형을 방지한다.", "search_content": "철골 부재 운반 중 변형을 방지한다.",
        "context_dependent": False,
    }
    pipeline = {
        **steel,
        "code": "KCS 61 20 10", "document_name": "배관공사", "content": "배관 운반 중 변형을 방지한다.",
        "search_content": "배관 운반 중 변형을 방지한다.",
    }
    source = "철골 부재를 운반할 때 변형을 방지한다"

    assert _stable_section_score(source, steel, ("4131", "1431")) > _stable_section_score(
        source, pipeline, ("4131", "1431")
    )
