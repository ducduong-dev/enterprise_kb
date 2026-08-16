"""Typed numbers, as Vietnamese banking documents write them.

For the clauses that matter most — rates, fees, ratios, deadlines — the discriminating content
is a small set of typed values. Same subject and identical quantities is a *restatement*; same
subject and different quantities is the *supersession signature* (ADR-0033, gate 4).

Its second job is the review screen. "8%/năm → 10%/năm" is what makes a steward's queue
workable; a similarity score is not.

Patterns only, on ADR-0013's terms: the floor, which a model may add to and never remove from.

**The separators are reversed from English and this is the whole risk.** Vietnamese writes
`1.000.000` for a million and `0,5` for a half. Read with English conventions, `1.000.000 đồng`
becomes one đồng and `0,5%` becomes five percent — errors of six orders of magnitude and one,
silently, in a number that *is* the claim. Both forms appear in this corpus, because the
English half of the bilingual documents uses English separators, so the reading is decided per
number by how the separators are actually arranged rather than by a global setting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

QuantityKind = Literal["percent", "money", "duration"]

#: A number with either convention's separators: `8`, `8,5`, `0.5`, `1.000.000`, `1,000,000`.
_NUMBER = r"\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?"

#: "8%/năm", "0,5 % / tháng", "8%". The period is part of the value, so it is captured with it:
#: a rate without its period is not comparable to one with, and treating "8%" and "8%/năm" as
#: the same number is how a monthly rate gets read as an annual one.
_PERCENT = re.compile(
    rf"(?P<value>{_NUMBER})\s*%\s*"
    rf"(?:/\s*(?P<period>năm|nam|tháng|thang|ngày|ngay|quý|year|month|day))?",
    re.IGNORECASE,
)

#: "1.000.000 đồng", "500.000 VND", "1 tỷ đồng", "USD 1,000".
_MONEY = re.compile(
    rf"(?:(?P<pre>VND|USD|EUR|JPY)\s*)?(?P<value>{_NUMBER})\s*"
    rf"(?P<scale>nghìn|nghin|triệu|trieu|tỷ|ty|tỉ)?\s*"
    rf"(?P<unit>đồng|dong|VND|USD|EUR|JPY)?",
    re.IGNORECASE,
)

#: "30 ngày", "03 tháng", "02 năm", "15 ngày làm việc".
_DURATION = re.compile(
    rf"(?P<value>{_NUMBER})\s*(?P<unit>ngày làm việc|ngay lam viec|ngày|ngay|tuần|tuan|"
    rf"tháng|thang|quý|năm|nam|working days?|days?|months?|years?)\b",
    re.IGNORECASE,
)

_SCALES: dict[str, int] = {
    "nghìn": 1_000,
    "nghin": 1_000,
    "triệu": 1_000_000,
    "trieu": 1_000_000,
    "tỷ": 1_000_000_000,
    "ty": 1_000_000_000,
    "tỉ": 1_000_000_000,
}

#: Canonical unit names, so "ngay" and "ngày" and "days" compare equal.
_DURATION_UNITS: dict[str, str] = {
    "ngày": "ngày", "ngay": "ngày", "day": "ngày", "days": "ngày",
    "ngày làm việc": "ngày làm việc", "ngay lam viec": "ngày làm việc",
    "working day": "ngày làm việc", "working days": "ngày làm việc",
    "tuần": "tuần", "tuan": "tuần",
    "tháng": "tháng", "thang": "tháng", "month": "tháng", "months": "tháng",
    # `quý` only with its accent. The unaccented "quy" is the first syllable of *quy định* —
    # "regulation", one of the commonest words in this corpus — so "Điều 12 quy định…" reads
    # as twelve quarters. The unaccented forms exist for OCR that dropped the marks, and this
    # is the one where that tolerance costs more than it buys.
    "quý": "quý",
    "năm": "năm", "nam": "năm", "year": "năm", "years": "năm",
}  # fmt: skip

_PERIODS: dict[str, str] = {
    "năm": "năm", "nam": "năm", "year": "năm",
    "tháng": "tháng", "thang": "tháng", "month": "tháng",
    "ngày": "ngày", "ngay": "ngày", "day": "ngày",
    "quý": "quý",
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Quantity:
    kind: QuantityKind
    value: Decimal
    #: Canonical: "%/năm", "%", "đồng", "USD", "ngày", "tháng". Two quantities are comparable
    #: only when this matches — 8%/năm and 8%/tháng are not the same rate, and a comparison
    #: that ignored the period would call a twelvefold change a restatement.
    unit: str
    raw: str
    start: int
    end: int

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "value": str(self.value), "unit": self.unit, "raw": self.raw}

    def __str__(self) -> str:
        if self.unit.startswith("%"):
            return f"{self.value}{self.unit}"
        return f"{self.value} {self.unit}"


def parse_number(raw: str) -> Decimal | None:
    """A Vietnamese or English number, read by how its separators are arranged.

    The rule, in order:

    * one separator with exactly three digits after it and no other separator is ambiguous —
      `1.000` is a thousand either way, so it does not matter and it is read as a thousand;
    * repeated separators of one kind are thousands separators (`1.000.000`, `1,000,000`);
    * the *last* separator is the decimal point when what follows is not three digits
      (`0,5` → 0.5, `8.25` → 8.25);
    * both kinds present means the last one is the decimal point (`1.000,50`, `1,000.50`).

    Returns None rather than guessing when the string is not a number this understands. A
    quantity nobody could read is better absent than wrong: gate 4 compares these, and a
    misread figure turns a restatement into a supersession or the reverse.
    """
    text = raw.strip()
    if not text:
        return None

    has_dot = "." in text
    has_comma = "," in text
    try:
        if has_dot and has_comma:
            decimal_sep = "." if text.rfind(".") > text.rfind(",") else ","
            thousands = "," if decimal_sep == "." else "."
            normalized = text.replace(thousands, "").replace(decimal_sep, ".")
        elif has_dot or has_comma:
            sep = "." if has_dot else ","
            parts = text.split(sep)
            if len(parts) > 2 or len(parts[-1]) == 3:
                # Repeated, or a single group of exactly three: thousands.
                normalized = text.replace(sep, "")
            else:
                normalized = text.replace(sep, ".")
        else:
            normalized = text
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None


def find_quantities(text: str) -> list[Quantity]:
    """Every typed number in the text, in order of appearance.

    Overlaps are resolved by preferring the more specific kind: `8%/năm` is a percent and not
    also a duration of eight years, so a match that starts inside one already taken is dropped.
    """
    found: list[Quantity] = []
    taken: list[tuple[int, int]] = []

    def claim(start: int, end: int) -> bool:
        if any(start < other_end and end > other_start for other_start, other_end in taken):
            return False
        taken.append((start, end))
        return True

    for match in _PERCENT.finditer(text):
        value = parse_number(match.group("value"))
        if value is None or not claim(match.start(), match.end()):
            continue
        period = match.group("period")
        unit = f"%/{_PERIODS.get(period.lower(), period.lower())}" if period else "%"
        found.append(
            Quantity("percent", value, unit, match.group(0).strip(), match.start(), match.end())
        )

    for match in _MONEY.finditer(text):
        unit_raw = match.group("unit") or match.group("pre")
        if not unit_raw:
            continue  # a bare number is not money; something has to name the currency
        value = parse_number(match.group("value"))
        if value is None or not claim(match.start(), match.end()):
            continue
        scale = _SCALES.get((match.group("scale") or "").lower(), 1)
        unit = "đồng" if unit_raw.lower() in ("đồng", "dong", "vnd") else unit_raw.upper()
        found.append(
            Quantity(
                "money", value * scale, unit, match.group(0).strip(), match.start(), match.end()
            )
        )

    for match in _DURATION.finditer(text):
        value = parse_number(match.group("value"))
        if value is None or not claim(match.start(), match.end()):
            continue
        canonical = _DURATION_UNITS.get(match.group("unit").lower())
        if canonical is None:
            continue
        found.append(
            Quantity(
                "duration", value, canonical, match.group(0).strip(), match.start(), match.end()
            )
        )

    return sorted(found, key=lambda item: item.start)


def compare(old: str, new: str) -> list[tuple[Quantity | None, Quantity | None]]:
    """What changed between two statements of the same rule, paired by unit.

    Pairs on the canonical unit, in order, because that is the comparison that means something:
    a rate against a rate and a deadline against a deadline. A quantity present on one side
    only pairs with None — an added fee and a removed one are both changes, and dropping them
    would report an amendment as a restatement.

    Returns only the pairs that *differ*. An empty result is the restatement signature: same
    subject, same numbers, so whatever else changed was wording (ADR-0033, gate 4).
    """
    changed: list[tuple[Quantity | None, Quantity | None]] = []
    old_by_unit: dict[str, list[Quantity]] = {}
    new_by_unit: dict[str, list[Quantity]] = {}
    for item in find_quantities(old):
        old_by_unit.setdefault(item.unit, []).append(item)
    for item in find_quantities(new):
        new_by_unit.setdefault(item.unit, []).append(item)

    for unit in sorted(set(old_by_unit) | set(new_by_unit)):
        before = old_by_unit.get(unit, [])
        after = new_by_unit.get(unit, [])
        for index in range(max(len(before), len(after))):
            left = before[index] if index < len(before) else None
            right = after[index] if index < len(after) else None
            if left is None or right is None or left.value != right.value:
                changed.append((left, right))
    return changed


def describe(delta: list[tuple[Quantity | None, Quantity | None]]) -> list[str]:
    """The delta as a steward reads it: "8%/năm → 10%/năm"."""
    return [f"{left if left else '—'} → {right if right else '—'}" for left, right in delta]


__all__ = ["Quantity", "QuantityKind", "compare", "describe", "find_quantities", "parse_number"]
