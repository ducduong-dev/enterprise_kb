"""Fixture list of real Vietnamese citation formats (plan section 10, unit tests)."""

from __future__ import annotations

import pytest
from kb_vntext.legal_numbers import (
    extract_legal_numbers,
    find_document_number,
    guess_ref_type,
    normalize_legal_number,
)


@pytest.mark.parametrize(
    ("text", "value", "instrument", "issuer", "year"),
    [
        ("Thông tư 41/2016/TT-NHNN", "41/2016/TT-NHNN", "TT", "NHNN", 2016),
        ("Nghị định số 88/2019/NĐ-CP", "88/2019/NĐ-CP", "ND", "CP", 2019),
        ("Quyết định 1627/2001/QĐ-NHNN", "1627/2001/QĐ-NHNN", "QD", "NHNN", 2001),
        ("Luật số 32/2024/QH15", "32/2024/QH15", "QH", None, 2024),
        ("Văn bản hợp nhất 05/VBHN-NHNN", "05/VBHN-NHNN", "VBHN", "NHNN", None),
        ("Công văn 1234/NHNN-TTGSNH", "1234/NHNN-TTGSNH", "NHNN", "TTGSNH", None),
        ("Quyết định 114/2023/QĐ-HĐQT", "114/2023/QĐ-HĐQT", "QD", "HĐQT", 2023),
        ("theo QD-2023-114 của Tổng giám đốc", "QD-2023-114", "QD", None, 2023),
        ("Quy trình QT-2024-007", "QT-2024-007", "QT", None, 2024),
        ("Chỉ thị 01/CT-NHNN ngày 1/1/2025", "01/CT-NHNN", "CT", "NHNN", None),
    ],
)
def test_recognizes_real_citation_formats(
    text: str, value: str, instrument: str, issuer: str | None, year: int | None
) -> None:
    (found,) = extract_legal_numbers(text)
    assert found.value == value
    assert found.instrument_type == instrument
    assert found.issuer == issuer
    assert found.year == year


def test_raw_span_includes_the_instrument_label() -> None:
    (found,) = extract_legal_numbers("Căn cứ Thông tư số 41/2016/TT-NHNN ngày 30/12/2016")
    assert found.raw == "Thông tư số 41/2016/TT-NHNN"
    assert found.value == "41/2016/TT-NHNN"


def test_diacritic_spellings_share_a_key() -> None:
    """QĐ and QD are the same instrument typed two ways — search must not care."""
    assert normalize_legal_number("1627/2001/QĐ-NHNN") == normalize_legal_number(
        "1627/2001/QD-NHNN"
    )
    assert normalize_legal_number(" 41/2016/tt-nhnn. ") == "41/2016/TT-NHNN"


def test_full_form_is_not_split_into_a_short_match() -> None:
    found = extract_legal_numbers("Thông tư 41/2016/TT-NHNN quy định...")
    assert len(found) == 1
    assert found[0].value == "41/2016/TT-NHNN"


def test_multiple_mentions_are_returned_in_order() -> None:
    text = "Thông tư này sửa đổi Thông tư 41/2016/TT-NHNN và bãi bỏ Quyết định 1627/2001/QĐ-NHNN."
    found = extract_legal_numbers(text)
    assert [f.value for f in found] == ["41/2016/TT-NHNN", "1627/2001/QĐ-NHNN"]
    assert found[0].start < found[1].start


def test_unknown_instrument_codes_are_flagged_for_review() -> None:
    (found,) = extract_legal_numbers("theo 99/2020/XYZ-ABC")
    assert found.confidence < 0.9  # shape matches, meaning does not — a human confirms


def test_plain_numbers_are_not_citations() -> None:
    assert extract_legal_numbers("Tỷ lệ 8% áp dụng từ 01/01/2020 đến 31/12/2020") == []


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("Sửa đổi, bổ sung một số điều của Thông tư 41/2016/TT-NHNN", "amends"),
        ("Bãi bỏ Quyết định 1627/2001/QĐ-NHNN", "abrogates"),
        ("Quyết định này thay thế Quyết định 114/2023/QĐ-HĐQT", "abrogates"),
        ("Hướng dẫn thi hành Nghị định 88/2019/NĐ-CP", "implements"),
        ("Văn bản hợp nhất Thông tư 41/2016/TT-NHNN", "consolidates"),
        ("Căn cứ Thông tư 41/2016/TT-NHNN", "cites"),
    ],
)
def test_relationship_is_guessed_from_the_surrounding_verb(sentence: str, expected: str) -> None:
    (found,) = extract_legal_numbers(sentence)
    assert guess_ref_type(sentence, found.start) == expected


def test_document_number_comes_from_the_header_not_the_body() -> None:
    text = (
        "NGÂN HÀNG NHÀ NƯỚC VIỆT NAM\n"
        "Số: 41/2016/TT-NHNN\n\n"
        "THÔNG TƯ\n" + ("nội dung " * 400) + "\ncăn cứ Nghị định 88/2019/NĐ-CP"
    )
    number = find_document_number(text)
    assert number is not None
    assert number.value == "41/2016/TT-NHNN"


def test_document_without_a_number_returns_none() -> None:
    assert find_document_number("Biểu phí dịch vụ khách hàng cá nhân năm 2026") is None
