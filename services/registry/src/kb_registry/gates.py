"""The cheap gates, run before any model is asked anything (ADR-0033).

Each answers a question the model would otherwise be asked to guess at, and each is exact. The
funnel's shape is the point: on a 208-chunk instrument, five candidates each would be 1,040
pairs, and what reaches adjudication should be a small fraction of that. How small is a number
the eval reports, not one to assume.

This module holds the two gates that are pure logic:

* **effectivity** — two clauses whose windows never intersected cannot supersede one another;
* **direction** — which clause is the older one, decided by the legal date and never by the
  order the archive happened to be digitised in.

Gate 3 (scope) is `kb_vntext.scope` and gate 4 (quantities) is `kb_vntext.quantities`, because
both read text and belong with the other readers of Vietnamese legal prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

#: Instrument rank, for the one case where two clauses took effect on the same day. Higher
#: outranks lower. A Nghị định outranks a Quyết định because it is a superior instrument, not
#: because it is longer or newer.
_RANK: Final[dict[str, int]] = {
    "QH": 5,  # Luật / Nghị quyết Quốc hội
    "ND": 4,  # Nghị định
    "QD": 3,  # Quyết định
    "TT": 2,  # Thông tư
    "CT": 1,  # Chỉ thị
}

Direction = Literal["left_older", "right_older", "undecidable"]


@dataclass(frozen=True, slots=True)
class Window:
    """One clause's effectivity, as the chunk carries it."""

    effective_from: date | None = None
    effective_to: date | None = None


def windows_overlap(left: Window, right: Window) -> bool:
    """Whether these two clauses were ever in force at the same time.

    Two clauses whose windows never intersected cannot supersede one another — the second was
    not in force to replace anything while the first applied. Exact and free, so it runs before
    anything expensive.

    Open ends are open: `effective_from = None` means "as far back as we know" and
    `effective_to = None` means "still in force", which is what the serving predicate already
    reads them as. Treating an unknown start as *today* would make every undated clause
    disjoint from every dated one and quietly empty the funnel.

    Borrowed from Graphiti's `resolve_edge_contradictions`, which opens with the same guard.

    The boundary is the interesting part and it is inclusive on both sides: a clause ending on
    31/12 and one starting on 31/12 were both in force that day, which is one day of genuine
    overlap and exactly when a supersession is most likely to be real.
    """
    if (
        left.effective_to is not None
        and right.effective_from is not None
        and left.effective_to < right.effective_from
    ):
        return False
    return not (
        right.effective_to is not None
        and left.effective_from is not None
        and right.effective_to < left.effective_from
    )


#: The same guard as SQL, for the candidate query. Two windows overlap unless one ends strictly
#: before the other starts; NULLs are open ends and `<` is never true against them, so the
#: predicate lets them through without a COALESCE that would have to invent a date.
OVERLAP_SQL: Final[str] = """
    NOT (
        ({left}.effective_to IS NOT NULL AND {right}.effective_from IS NOT NULL
         AND {left}.effective_to < {right}.effective_from)
        OR
        ({right}.effective_to IS NOT NULL AND {left}.effective_from IS NOT NULL
         AND {right}.effective_to < {left}.effective_from)
    )
"""


def older_first(
    left_from: date | None,
    right_from: date | None,
    *,
    left_instrument: str | None = None,
    right_instrument: str | None = None,
) -> Direction:
    """Which clause came first, by the legal date.

    **Never by `created_at`, `published_at` or registry insertion order.** The corpus is being
    digitised in archive order, so publish order says nothing about which rule came first — a
    2023 circular loaded last week is not newer than a 2026 one loaded last year. Getting this
    backwards records the superseded clause as the survivor, which is worse than not detecting
    the pair at all.

    Where the effective dates are equal, the instrument's rank decides: a Nghị định outranks a
    Quyết định. Where rank is equal too, nothing is proposed — that is a
    `conflicting_unresolved` for a person, and it is a genuine finding rather than a failure
    (ADR-0033).
    """
    if left_from is not None and right_from is not None and left_from != right_from:
        return "left_older" if left_from < right_from else "right_older"
    if left_from is None or right_from is None:
        # One side undated. Direction is what the whole record turns on, so an undated pair is
        # a person's call and not a coin toss.
        return "undecidable"

    left_rank = _RANK.get((left_instrument or "").upper(), 0)
    right_rank = _RANK.get((right_instrument or "").upper(), 0)
    if left_rank == right_rank:
        return "undecidable"
    # Same day, so the superior instrument is the one that displaced the other.
    return "right_older" if left_rank > right_rank else "left_older"


__all__ = ["OVERLAP_SQL", "Direction", "Window", "older_first", "windows_overlap"]
