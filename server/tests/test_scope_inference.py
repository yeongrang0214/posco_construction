from __future__ import annotations

import json

import server.matcher as matcher_module
from server.matcher import build_scope_sample, infer_scope


def _write_catalog(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (tmp_path / "code_list.json").write_text(
        json.dumps(
            [
                {
                    "codeType": "KCS",
                    "code": "413105",
                    "name": "강구조공사 일반사항",
                    "listParentCodes": [{"name": "건축공사"}],
                },
                {
                    "codeType": "KCS",
                    "code": "573010",
                    "name": "상수도 관로공사",
                    "listParentCodes": [{"name": "상수도공사"}],
                },
                {
                    "codeType": "KCS",
                    "code": "316535",
                    "name": "산업설비 배관공사",
                    "listParentCodes": [{"name": "산업설비공사"}],
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return raw_dir


def test_catalog_scope_routes_an_unseen_document_discipline(tmp_path):
    raw_dir = _write_catalog(tmp_path)

    prefixes, display = infer_scope(
        "포스코_상수도_관로_시방서.docx",
        "상수도 관로 설치",
        "관로의 운반, 접합, 수압시험 및 매설 기준",
        raw_dir,
    )

    assert prefixes[0] == "573010"
    assert "KCS 57 30 10" in display
    assert "KCS 목록 기반 자동 추정" in display


def test_catalog_scope_prioritizes_an_explicit_kcs_reference(tmp_path):
    raw_dir = _write_catalog(tmp_path)

    prefixes, _ = infer_scope(
        "설비시방서.docx",
        "배관공사",
        "본 공사는 KCS 31 65 35를 적용한다.",
        raw_dir,
    )

    assert prefixes[0] == "316535"


def test_current_catalog_overrides_a_known_posco_chapter_hint(tmp_path):
    raw_dir = _write_catalog(tmp_path)

    prefixes, display = infer_scope(
        "건축_제13장_철골공사.docx",
        "철골공사",
        "강재 제작 및 설치",
        raw_dir,
    )

    assert prefixes[0] == "413105"
    assert all(len(prefix) == 6 for prefix in prefixes)
    assert "KCS 41 31 05" in display
    assert "KCS 목록 기반 자동 추정" in display


def test_catalog_routing_does_not_assume_the_current_posco_chapter_system(tmp_path):
    raw_dir = _write_catalog(tmp_path)

    prefixes, display = infer_scope(
        "새체계_제13장.docx",
        "상수도 관로 설치",
        "관로의 접합, 수압시험 및 매설 기준",
        raw_dir,
    )

    assert prefixes[0] == "573010"
    assert "KCS 57 30 10" in display


def test_static_chapter_scope_is_only_the_no_catalog_fallback():
    prefixes, display = infer_scope(
        "건축_제13장_철골공사.docx",
        "철골공사",
        "강재 제작 및 설치",
    )

    assert prefixes == ("4131", "1431")
    assert display == "KCS 41 31, KCS 14 31"


def test_scope_sample_represents_the_whole_document_with_a_fixed_bound():
    clauses = [
        {"title": f"조항 {index}", "content": f"본문 {index}"}
        for index in range(200)
    ]

    sample = build_scope_sample(clauses)

    assert "조항 0" in sample
    assert "조항 199" in sample
    assert len(sample) <= 12_000


def test_exact_catalog_routes_build_reusable_single_document_indexes(tmp_path, monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_rank(_text, _raw_dir, prefixes, _limit):
        calls.append(prefixes)
        code = prefixes[0]
        return [
            (
                {
                    "code": code,
                    "document_name": f"문서 {code}",
                    "clause": "1.1",
                    "title": "시험",
                    "content": "시험 내용",
                    "version": "2026",
                    "update_date": "2026-01-01",
                },
                0.5,
            )
        ]

    monkeypatch.setattr(matcher_module, "_rank_lexical", fake_rank)
    monkeypatch.setattr(matcher_module, "_catalog_prefixes", lambda *_args, **_kwargs: ())

    matcher_module._rank_matches(
        "새 분야 시험 내용",
        tmp_path,
        ("573010", "316535"),
    )

    assert calls == [("573010",), ("316535",)]
