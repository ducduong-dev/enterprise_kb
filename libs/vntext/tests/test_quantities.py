"""Typed numbers, and the delta between two statements of one rule (M9d, ADR-0033).

The number *is* the claim in a bank's documents, so the tests here are mostly about the ways a
number can be read wrongly and still look plausible. Vietnamese separators are reversed from
English and both conventions appear in this bilingual corpus, so `1.000.000 đồng` read the
English way is one đồng — an error of six orders of magnitude, in the direction nobody notices.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from kb_vntext.quantities import compare, describe, find_quantities, parse_number


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Vietnamese: period groups thousands, comma is the decimal point.
        ("1.000.000", Decimal("1000000")),
        ("0,5", Decimal("0.5")),
        ("1.000,50", Decimal("1000.50")),
        # English, which the bilingual half of the corpus uses.
        ("1,000,000", Decimal("1000000")),
        ("8.25", Decimal("8.25")),
        ("1,000.50", Decimal("1000.50")),
        # Ambiguous and harmless: a thousand under either reading.
        ("1.000", Decimal("1000")),
        ("8", Decimal("8")),
    ],
)
def test_a_number_is_read_by_how_its_separators_are_arranged(raw: str, expected: Decimal) -> None:
    assert parse_number(raw) == expected


def test_an_unreadable_number_is_absent_rather_than_wrong() -> None:
    """Gate 4 compares these; a misread figure turns a restatement into a supersession."""
    assert parse_number("") is None
    assert parse_number("tám phần trăm") is None


def test_a_rate_keeps_its_period() -> None:
    """8%/năm and 8%/tháng are not the same rate, and a comparison that dropped the period
    would call a twelvefold change a restatement."""
    rate = find_quantities("Lãi suất 8%/năm áp dụng cho khoản vay.")[0]
    assert (rate.kind, str(rate.value), rate.unit) == ("percent", "8", "%/năm")

    assert find_quantities("0,5%/tháng")[0].unit == "%/tháng"
    assert find_quantities("tỷ lệ tối thiểu 8%")[0].unit == "%"


def test_money_carries_its_scale_word() -> None:
    assert find_quantities("Phí 1 tỷ đồng.")[0].value == Decimal("1000000000")
    assert find_quantities("Phí 500.000 VND.")[0].value == Decimal("500000")


def test_a_bare_number_is_not_money() -> None:
    """Something has to name the currency, or every article number becomes an amount."""
    assert [q.kind for q in find_quantities("Điều 5 nêu rõ như sau.")] == []


def test_quy_dinh_is_not_twelve_quarters() -> None:
    """*quy định* — "regulation" — is one of the commonest words in this corpus, and its first
    syllable is the unaccented form of *quý*. Tolerating the unaccented spelling for OCR's sake
    turned "Điều 12 quy định…" into a duration, which would then be compared against a real
    deadline in a different clause."""
    assert find_quantities("Điều 12 quy định như sau.") == []
    assert [str(q) for q in find_quantities("Trong 2 quý đầu năm.")] == ["2 quý"]


def test_durations_are_canonicalised() -> None:
    found = find_quantities("Trong 30 ngày làm việc, tối đa 03 tháng.")
    assert [(str(q.value), q.unit) for q in found] == [("30", "ngày làm việc"), ("3", "tháng")]


def test_a_percent_is_not_also_a_duration() -> None:
    """Overlaps resolve to the more specific kind: 8%/năm is a rate, not eight years."""
    assert [q.kind for q in find_quantities("Lãi suất 8%/năm")] == ["percent"]


# ------------------------------------------------------------------- the supersession signal


def test_a_changed_rate_is_the_supersession_signature() -> None:
    assert describe(compare("Lãi suất 8%/năm.", "Lãi suất 10%/năm.")) == ["8%/năm → 10%/năm"]


def test_the_same_rule_reworded_shows_no_delta() -> None:
    """Same subject, same numbers: whatever changed was wording, which is a restatement and
    not a supersession (ADR-0033, gate 4)."""
    assert compare("Lãi suất 8%/năm.", "Lãi suất là 8%/năm theo quy định hiện hành.") == []


def test_the_same_amount_written_two_ways_shows_no_delta() -> None:
    """ "1.000.000 đồng" and "1 triệu đồng" are one figure. A string comparison calls this a
    change and sends a restatement to a steward's queue."""
    assert compare("Phí 1.000.000 đồng.", "Phí 1 triệu đồng.") == []


def test_a_rate_whose_period_changed_is_a_change() -> None:
    """The failure this exists to prevent: monthly read as annual."""
    assert compare("Lãi suất 8%/năm.", "Lãi suất 8%/tháng.")


def test_an_added_quantity_is_a_change() -> None:
    """An added fee and a removed one are both changes; dropping them reports an amendment as
    a restatement."""
    delta = compare("Phí 100.000 đồng.", "Phí 100.000 đồng và phụ phí 50.000 đồng.")
    assert delta
    assert describe(delta) == ["— → 50000 đồng"]


def test_quantities_of_different_units_do_not_pair() -> None:
    """A rate is compared against a rate and a deadline against a deadline, or the delta is
    noise a steward has to un-read."""
    delta = compare("Lãi suất 8%/năm trong 30 ngày.", "Lãi suất 8%/năm trong 60 ngày.")
    assert describe(delta) == ["30 ngày → 60 ngày"]
