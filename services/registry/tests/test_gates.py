"""The gates that are pure logic (M9d, ADR-0033).

Effectivity and direction, both exact, both run before anything expensive. The interesting
cases are boundaries and missing dates, which is why these live as functions rather than only
as SQL — a boundary you cannot test without a database is a boundary nobody tests.
"""

from __future__ import annotations

from datetime import date

import pytest
from kb_registry.gates import Window, older_first, windows_overlap

JAN = date(2026, 1, 1)
JUN = date(2026, 6, 30)
JUL = date(2026, 7, 1)
DEC = date(2026, 12, 31)


def test_windows_that_never_met_cannot_supersede() -> None:
    """The second was not in force to replace anything while the first applied."""
    assert not windows_overlap(Window(JAN, JUN), Window(JUL, DEC))
    assert not windows_overlap(Window(JUL, DEC), Window(JAN, JUN))


def test_windows_that_touch_on_one_day_overlap() -> None:
    """Inclusive on both sides: a clause ending on 30/6 and one starting on 30/6 were both in
    force that day — one day of genuine overlap, and exactly when a supersession is most likely
    to be real."""
    assert windows_overlap(Window(JAN, JUN), Window(JUN, DEC))


def test_an_open_end_stays_open() -> None:
    """`effective_to = None` means still in force and `effective_from = None` means as far back
    as we know, which is what the serving predicate already reads them as. Treating an unknown
    start as *today* would make every undated clause disjoint from every dated one and quietly
    empty the funnel."""
    assert windows_overlap(Window(JAN, None), Window(DEC, None))
    assert windows_overlap(Window(None, None), Window(JAN, JUN))
    assert windows_overlap(Window(None, JUN), Window(JAN, None))


def test_a_closed_window_before_an_open_one_still_does_not_overlap() -> None:
    assert not windows_overlap(Window(JAN, JUN), Window(JUL, None))


# ------------------------------------------------------------------------------- direction


def test_the_older_clause_is_the_one_that_took_effect_first() -> None:
    assert older_first(JAN, JUL) == "left_older"
    assert older_first(JUL, JAN) == "right_older"


def test_direction_never_comes_from_publish_order() -> None:
    """The corpus is digitised in archive order, so a 2023 circular loaded last week is not
    newer than a 2026 one loaded last year. Getting this backwards records the superseded
    clause as the survivor, which is worse than not detecting the pair."""
    old_but_loaded_late = date(2023, 1, 1)
    new_but_loaded_early = date(2026, 1, 1)
    assert older_first(old_but_loaded_late, new_but_loaded_early) == "left_older"


def test_a_tie_is_broken_by_the_instrument_that_outranks() -> None:
    """Same day, so the superior instrument is the one that displaced the other."""
    assert older_first(JAN, JAN, left_instrument="TT", right_instrument="ND") == "left_older"
    assert older_first(JAN, JAN, left_instrument="ND", right_instrument="TT") == "right_older"


@pytest.mark.parametrize(
    ("left", "right", "left_kind", "right_kind"),
    [
        # Same day, same rank: a genuine finding for a person, not a coin toss.
        (JAN, JAN, "TT", "TT"),
        (JAN, JAN, None, None),
        # One side undated. Direction is what the whole record turns on.
        (None, JAN, "TT", "ND"),
        (JAN, None, "TT", "ND"),
    ],
)
def test_what_cannot_be_decided_is_left_to_a_person(
    left: date | None, right: date | None, left_kind: str | None, right_kind: str | None
) -> None:
    assert (
        older_first(left, right, left_instrument=left_kind, right_instrument=right_kind)
        == "undecidable"
    )
