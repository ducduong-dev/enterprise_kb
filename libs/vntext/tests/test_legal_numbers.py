"""Fixture list of real Vietnamese citation formats (plan section 10, unit tests)."""

from __future__ import annotations

import pytest
from kb_vntext.legal_numbers import (
    extract_legal_numbers,
    find_anchors,
    find_document_number,
    guess_ref_type,
    normalize_legal_number,
)
from kb_vntext.sections import build_anchor


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


# ---------------------------------------------------- reference anchors (M9b, ADR-0036)


def _anchors(sentence: str, number: str) -> list[str]:
    return find_anchors(sentence, sentence.index(number))


@pytest.mark.parametrize(
    ("sentence", "number", "expected"),
    [
        ("theo quy định tại Điều 12 Thông tư 41/2016/TT-NHNN", "41/2016", ["12"]),
        ("quy định tại khoản 2 Điều 12 Thông tư 41/2016/TT-NHNN", "41/2016", ["12.2"]),
        ("điểm a khoản 3 Điều 8 Nghị định 88/2019/NĐ-CP", "88/2019", ["8.3a"]),
        ("các Điều 5, 6 và 7 Thông tư 41/2016/TT-NHNN", "41/2016", ["5", "6", "7"]),
        ("as set out in Article 12 of Circular 41/2016/TT-NHNN", "41/2016", ["12"]),
        ("bãi bỏ khoản 2 Điều 12 Thông tư 10/2022/TT-NHNN", "10/2022", ["12.2"]),
        ("sửa đổi Điều 12a Thông tư 10/2022/TT-NHNN", "10/2022", ["12a"]),
    ],
)
def test_a_citation_yields_the_clauses_it_names(
    sentence: str, number: str, expected: list[str]
) -> None:
    assert _anchors(sentence, number) == expected


def test_a_reference_to_a_whole_instrument_names_no_clause() -> None:
    """ "theo quy định tại Thông tư 41/2016" cites the instrument. Inventing an article for it
    would resolve a general reference to one arbitrary clause."""
    assert _anchors("theo quy định tại Thông tư 41/2016/TT-NHNN", "41/2016") == []


def test_each_instrument_takes_the_article_on_its_left() -> None:
    """A sentence citing two instruments must not give the second one the first's article."""
    sentence = "Điều 5 của Thông tư 99/2020/TT-NHNN và khoản 2 Điều 9 của Thông tư 41/2016/TT-NHNN"
    assert _anchors(sentence, "99/2020") == ["5"]
    assert _anchors(sentence, "41/2016") == ["9.2"]


def test_an_anchor_reads_back_as_the_form_the_chunks_carry() -> None:
    """The extractor and the chunker must agree on what "12.2" means, or a reference resolves
    to text the citation does not point at (ADR-0036)."""
    sentence = "quy định tại khoản 2 Điều 12 Thông tư 41/2016/TT-NHNN"
    assert _anchors(sentence, "41/2016") == [build_anchor(["Chương II", "Điều 12", "Khoản 2"])]
