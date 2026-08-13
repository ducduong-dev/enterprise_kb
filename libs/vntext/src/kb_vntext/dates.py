"""Dates as Vietnamese legal instruments write them.

Two questions, one module:

* **When was this issued?** — "Hà Nội, ngày 15 tháng 3 năm 2026", usually under the heading.
* **When does it take effect?** — "Nghị định này có hiệu lực thi hành kể từ ngày 01/01/2026",
  usually in the final article.

The second is the one the platform cannot work without. `effective_from` decides what a
point-in-time query returns (INV-6), how an amendment chain is ordered on the lineage strip,
and when a document expires. A NULL there is not "no opinion" — it silently means *always in
effect*, which for a regulation is the wrong default to hold quietly.

Deliberately conservative: this reads dates, it does not guess them. A document whose
effectivity is phrased in a way not matched here comes back `None` and is a question for the
reviewer, which is better than a plausible date nobody checked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

#: "ngày 01 tháng 01 năm 2026", tolerating one- or two-digit day and month.
_SPELLED = re.compile(
    r"ngày\s+(?P<day>\d{1,2})\s*tháng\s+(?P<month>\d{1,2})\s*năm\s+(?P<year>\d{4})",
    re.IGNORECASE,
)
#: "01/01/2026" and "01-01-2026".
_NUMERIC = re.compile(r"\b(?P<day>\d{1,2})[/-](?P<month>\d{1,2})[/-](?P<year>\d{4})\b")

#: The sentence that carries effectivity. `hiệu lực` alone is not enough — an instrument also
#: says which *other* documents cease to have effect, and matching those would date this
#: document by the one it repeals.
_EFFECTIVITY = re.compile(
    r"(?P<subject>[^.\n]{0,120}?)"
    r"(có\s+hiệu\s+lực|hiệu\s+lực\s+thi\s+hành)"
    r"(?P<tail>[^.\n]{0,160})",
    re.IGNORECASE,
)
#: Phrases that mark the sentence as being about a *different* instrument's effect ending.
_ABOUT_OTHERS = ("hết hiệu lực", "ngừng hiệu lực", "bãi bỏ", "thay thế")
#: "kể từ ngày ký" — effective on signature, i.e. the issue date.
_ON_SIGNING = re.compile(r"kể\s+từ\s+ngày\s+ký|từ\s+ngày\s+ký", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DetectedDates:
    issued: date | None = None
    effective_from: date | None = None
    #: The sentence the effective date was read from, so a reviewer can check it without
    #: opening the document. Empty when nothing matched.
    effective_evidence: str = ""


def _to_date(match: re.Match[str]) -> date | None:
    try:
        return date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    except ValueError:  # 31/02, or a year the regex allowed and the calendar does not
        return None


def find_dates(text: str) -> list[date]:
    """Every date in the text, in order of appearance, spelled or numeric."""
    found: list[tuple[int, date]] = []
    for pattern in (_SPELLED, _NUMERIC):
        for match in pattern.finditer(text):
            value = _to_date(match)
            if value is not None:
                found.append((match.start(), value))
    return [value for _position, value in sorted(found, key=lambda item: item[0])]


def find_issued_date(text: str) -> date | None:
    """The issue date: the first date in the document's opening, where the place-and-date line
    sits. Later dates belong to the body — deadlines, referenced instruments, effectivity."""
    head = text[:2000]
    match = _SPELLED.search(head)
    if match is not None:
        return _to_date(match)
    numeric = _NUMERIC.search(head)
    return _to_date(numeric) if numeric else None


def find_effective_from(text: str, *, issued: date | None = None) -> tuple[date | None, str]:
    """The date this instrument takes effect, and the sentence that says so.

    Returns `(None, "")` when the text does not state it plainly. That is a normal outcome —
    the reviewer supplies it — and much better than dating a regulation by a number that
    happened to be nearby.
    """
    for match in _EFFECTIVITY.finditer(text):
        sentence = match.group(0)
        if any(phrase in sentence.lower() for phrase in _ABOUT_OTHERS):
            continue  # this clause ends another instrument's effect, not this one's

        tail = match.group("tail")
        dates = find_dates(tail)
        if dates:
            return dates[0], sentence.strip()
        if _ON_SIGNING.search(tail) and issued is not None:
            return issued, sentence.strip()
    return None, ""


def detect(text: str) -> DetectedDates:
    """Both dates, read from the whole document text."""
    issued = find_issued_date(text)
    effective, evidence = find_effective_from(text, issued=issued)
    return DetectedDates(issued=issued, effective_from=effective, effective_evidence=evidence)


__all__ = [
    "DetectedDates",
    "detect",
    "find_dates",
    "find_effective_from",
    "find_issued_date",
]
