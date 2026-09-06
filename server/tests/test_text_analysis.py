from server.text_analysis import analyze_text_differences, starts_with_quantity


def test_recognises_only_leading_number_and_unit_quantities():
    assert starts_with_quantity("1.5 m 이상")
    assert starts_with_quantity("12.5 mm")
    assert starts_with_quantity("0.8 MPa")
    assert starts_with_quantity("1.5 개 이상")
    assert starts_with_quantity("1.5 개월 동안")
    assert not starts_with_quantity("1.5 제출물")
    assert not starts_with_quantity("3.1 개 요")
    assert not starts_with_quantity("13.1 일반사항")


def test_detects_quantity_and_limit_differences():
    result = analyze_text_differences(
        "강판 두께는 10 ㎜ 이상이어야 한다.",
        "강판 두께는 12 mm 초과이어야 한다.",
    )

    assert result == {
        "warnings": [
            "수치·단위가 다르므로 기술 검토가 필요합니다.",
            "이상·이하·초과·미만 표현이 다릅니다.",
        ],
        "differences": [
            {
                "category": "quantity",
                "posco": [{"number": "10", "unit": "mm"}],
                "kcs": [{"number": "12", "unit": "mm"}],
            },
            {"category": "limit", "posco": ["이상"], "kcs": ["초과"]},
        ],
    }


def test_normalises_equivalent_numbers_units_and_whitespace():
    result = analyze_text_differences(
        "간격은 1,000 ㎜이고 온도는 20 ℃ 이상이어야 한다.",
        "온도는 20.0 °C이고 간격은 1000.0 mm 이상이어야 한다.",
    )

    assert result == {"warnings": [], "differences": []}


def test_treats_equivalent_prohibition_phrases_as_same_modality():
    result = analyze_text_differences(
        "용접부의 임의 도장을 금지한다.",
        "용접부를 임의로 도장해서는 안 된다.",
    )

    assert result == {"warnings": [], "differences": []}


def test_detects_prohibition_and_obligation_presence_differences():
    result = analyze_text_differences(
        "임시 볼트 사용은 금지한다. 검사는 실시하여야 한다.",
        "임시 볼트를 사용할 수 있다. 검사는 실시할 수 있다.",
    )

    assert result["warnings"] == [
        "금지 표현의 유무가 다릅니다.",
        "의무 표현의 유무가 다릅니다.",
    ]
    assert result["differences"] == [
        {"category": "prohibition", "posco": ["금지"], "kcs": []},
        {"category": "obligation", "posco": ["하여야 한다"], "kcs": []},
    ]


def test_returns_empty_lists_for_empty_texts():
    assert analyze_text_differences("", "") == {"warnings": [], "differences": []}
