"""Dates as Vietnamese legal instruments write them.

Three questions, one module:

* **When was this issued?** — "Hà Nội, ngày 15 tháng 3 năm 2026", usually under the heading.
* **When does it take effect?** — "Nghị định này có hiệu lực thi hành kể từ ngày 01/01/2026",
  usually in the final article.
* **When does it stop?** — "Thông tư này có hiệu lực đến hết ngày 31/12/2026", in the same
  article, and much rarer: most instruments never say.

The second is the one the platform cannot work without. `effective_from` decides what a
point-in-time query returns (INV-6), how an amendment chain is ordered on the lineage strip,
and when a document expires. A NULL there is not "no opinion" — it silently means *always in
effect*, which for a regulation is the wrong default to hold quietly.

The third reads only the **self-stated** sunset — the instrument setting its own end date,
which is a version fact known at publication and stays on the version (ADR-0030). Expiry that
arrives *later*, from a different instrument abrogating this one, is not read here: it is a
ledger decision with an evidence sentence and a human behind it, and the closing article that
carries it is the declaration extractor's (ADR-0039). The two are easy to confuse in the text,
because they sit in the same paragraph and share the same words, so the discrimination is
explicit below and tested both ways round.

Deliberately conservative: this reads dates, it does not guess them. A document whose
effectivity is phrased in a way not matched here comes back `None` and is a question for the
reviewer, which is better than a plausible date nobody checked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

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

#: A sunset stated as a *last day*: "có hiệu lực đến hết ngày 31/12/2026", "áp dụng đến ngày
#: 31/12/2026". The date is the last day the instrument applied, which is what the effectivity
#: predicate compares against (`effective_to >= :effective_on`), so it is stored as read.
_SUNSET_THROUGH = re.compile(
    r"(?P<subject>[^.;\n]{0,120}?)"
    r"(có\s+hiệu\s+lực|hiệu\s+lực\s+thi\s+hành|áp\s+dụng|thực\s+hiện)"
    r"[^.;\n]{0,40}?đến\s+(?:hết\s+)?(?P<tail>[^.;\n]{0,80})",
    re.IGNORECASE,
)
#: A sunset stated as a *ceasing day*: "hết hiệu lực kể từ ngày 01/01/2027". The instrument
#: applied through the day *before*, which is the one arithmetic difference between the two
#: forms and the reason they are separate patterns rather than one.
_SUNSET_FROM = re.compile(
    r"(?P<subject>[^.;\n]{0,120}?)"
    r"(hết\s+hiệu\s+lực|ngừng\s+hiệu\s+lực|chấm\s+dứt\s+hiệu\s+lực)"
    r"(?P<tail>[^.;\n]{0,160})",
    re.IGNORECASE,
)
#: "Thông tư này", "Quyết định này", "các quy định tại Nghị định này" — the instrument talking
#: about itself. Without this the sentence ending *another* instrument reads identically.
_SELF_REFERENCE = re.compile(r"\bnày\b", re.IGNORECASE)
#: An instrument number in the subject: "Nghị định số 42/2022/NĐ-CP hết hiệu lực…". Belt and
#: braces with the rule above, for a subject that names an instrument *and* says "này" later
#: in the same clause.
_NAMES_AN_INSTRUMENT = re.compile(r"\d{1,5}\s*/\s*\d{4}\s*/")


@dataclass(frozen=True, slots=True)
class DetectedDates:
    issued: date | None = None
    effective_from: date | None = None
    #: The sentence the effective date was read from, so a reviewer can check it without
    #: opening the document. Empty when nothing matched.
    effective_evidence: str = ""
    #: The last day the instrument applies, when it says so about itself. Almost always None:
    #: expiry usually arrives later, from another instrument, and is a ledger row (ADR-0030).
    effective_to: date | None = None
    #: The sentence `effective_to` was read from. Same discipline, same reason.
    expiry_evidence: str = ""


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


def _is_about_itself(subject: str) -> bool:
    return bool(_SELF_REFERENCE.search(subject)) and not _NAMES_AN_INSTRUMENT.search(subject)


def find_effective_to(text: str) -> tuple[date | None, str]:
    """The date this instrument stops applying *by its own terms*, and the sentence saying so.

    Only a self-stated sunset. A clause ending some *other* instrument — which is the far more
    common sentence, and sits in the same article — returns `(None, "")`: that is an expiry
    decision about a different document, it needs the abrogating instrument's identity and a
    steward, and it belongs in the ledger rather than on this version (ADR-0030).

    Returns the **last day the instrument applied**, so it can be compared directly against a
    query date. The two phrasings differ by exactly one day and the difference is the whole
    point of reading them separately:

    * "có hiệu lực đến hết ngày 31/12/2026" → 31/12/2026, the last day, as written;
    * "hết hiệu lực kể từ ngày 01/01/2027" → 31/12/2026, the day before it ceased.
    """
    for match in _SUNSET_THROUGH.finditer(text):
        if not _is_about_itself(match.group("subject")):
            continue
        dates = find_dates(match.group("tail"))
        if dates:
            return dates[0], match.group(0).strip()

    for match in _SUNSET_FROM.finditer(text):
        if not _is_about_itself(match.group("subject")):
            continue
        dates = find_dates(match.group("tail"))
        if dates:
            # The instrument ceases *on* that day, so the last day it applied is the one before.
            return dates[0] - timedelta(days=1), match.group(0).strip()
    return None, ""


def detect(text: str) -> DetectedDates:
    """Every date the document states about itself, read from the whole text."""
    issued = find_issued_date(text)
    effective, evidence = find_effective_from(text, issued=issued)
    expires, expiry_evidence = find_effective_to(text)
    return DetectedDates(
        issued=issued,
        effective_from=effective,
        effective_evidence=evidence,
        effective_to=expires,
        expiry_evidence=expiry_evidence,
    )


__all__ = [
    "DetectedDates",
    "detect",
    "find_dates",
    "find_effective_from",
    "find_effective_to",
    "find_issued_date",
]
