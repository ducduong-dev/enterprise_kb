"""Reading the dates a Vietnamese instrument states about itself.

`effective_from` is not decoration: it decides what a point-in-time query returns, how an
amendment chain is ordered, and when a document expires. The tests below are about the two
ways to get it wrong — missing a date that is plainly stated, and inventing one that is not.
"""

from __future__ import annotations

from datetime import date

from kb_vntext.dates import detect, find_effective_from, find_issued_date

NGHI_DINH = """
CHÍNH PHỦ
Số: 309/2026/NĐ-CP
Hà Nội, ngày 15 tháng 3 năm 2026

NGHỊ ĐỊNH
Sửa đổi, bổ sung một số điều của Nghị định số 118/2025/NĐ-CP

Điều 4. Hiệu lực thi hành
Nghị định này có hiệu lực thi hành kể từ ngày 01 tháng 5 năm 2026.
Nghị định số 42/2022/NĐ-CP ngày 20 tháng 6 năm 2022 hết hiệu lực kể từ ngày Nghị định này
có hiệu lực.
"""


def test_the_issue_date_comes_from_the_place_and_date_line() -> None:
    assert find_issued_date(NGHI_DINH) == date(2026, 3, 15)


def test_the_effective_date_is_read_from_the_effectivity_article() -> None:
    effective, evidence = find_effective_from(NGHI_DINH)
    assert effective == date(2026, 5, 1)
    assert "có hiệu lực thi hành" in evidence


def test_a_clause_ending_another_instrument_is_not_this_document_s_date() -> None:
    """The same article usually says which decree stops applying. Dating this document by that
    sentence would put it in force years early."""
    text = "Nghị định số 42/2022/NĐ-CP ngày 20 tháng 6 năm 2022 hết hiệu lực thi hành."
    effective, _evidence = find_effective_from(text)
    assert effective is None


def test_effective_on_signature_resolves_to_the_issue_date() -> None:
    text = "Hà Nội, ngày 09 tháng 02 năm 2026\nThông tư này có hiệu lực kể từ ngày ký."
    detected = detect(text)
    assert detected.issued == date(2026, 2, 9)
    assert detected.effective_from == date(2026, 2, 9)


def test_numeric_dates_are_read_too() -> None:
    text = "Quyết định này có hiệu lực thi hành từ ngày 01/01/2027."
    effective, _evidence = find_effective_from(text)
    assert effective == date(2027, 1, 1)


def test_an_impossible_date_is_not_a_date() -> None:
    text = "Nghị định này có hiệu lực thi hành từ ngày 31 tháng 2 năm 2026."
    effective, _evidence = find_effective_from(text)
    assert effective is None


def test_silence_stays_silence() -> None:
    """A document that does not state its effectivity must come back empty, so the reviewer is
    asked. A guess here is a regulation applied on the wrong day."""
    detected = detect("Điều 1. Phạm vi điều chỉnh\nNghị định này quy định về ...")
    assert detected.effective_from is None
    assert detected.effective_evidence == ""
