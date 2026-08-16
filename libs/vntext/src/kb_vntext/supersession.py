"""Declarations: the sentence in which an instrument says what it replaces.

In this corpus the replacing instrument nearly always **says so, in the text, in a sentence
written to be read**. Vietnamese drafting concentrates it in the closing article — *Điều khoản
thi hành* / *Hiệu lực thi hành* — in a small set of formulaic shapes:

* *"Thông tư này thay thế Thông tư 10/2022/TT-NHNN ngày 15/3/2022."* — whole instrument.
* *"Bãi bỏ khoản 2 Điều 12 Thông tư 10/2022/TT-NHNN."* — one clause ends, nothing replaces it.
* *"Điều 5 Thông tư 10/2022/TT-NHNN được sửa đổi, bổ sung như sau: …"* — one article replaced.
* *"Điều 7 Thông tư này thay thế Điều 5 Thông tư 10/2022/TT-NHNN."* — both ends, with direction.

Reading those is the cheap and certain path, and it is most of the problem (ADR-0039). What it
buys over the inference funnel is not speed: the sentence names **which side is which**, so
nothing has to be inferred from dates or publish order, and it names **the granularity**, so
*bãi bỏ khoản 2 Điều 12* ends one clause and leaves the other three standing.

Patterns only, on ADR-0013's terms: this is the floor and a model may add to it, never remove
from it. The forms above are formulaic — that is exactly what makes them extractable — so a
shape these regexes miss is a fixture to add, not a threshold to tune.

Nothing here decides anything. A `Declaration` is a reading of a sentence; it becomes a
proposal, and a person confirms it (ADR-0030's `proposed` state, INV-8 for the regulated
classes).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from kb_vntext.dates import find_dates
from kb_vntext.legal_numbers import LegalNumber, extract_legal_numbers, find_anchors

DeclarationKind = Literal["abrogates", "replaces", "amends"]

#: What the sentence does to the instrument it names. Ordered: a sentence carrying both
#: *"bãi bỏ"* and *"thay thế"* — "bãi bỏ Điều 5 và thay thế bằng Điều 7" — is a replacement,
#: because naming a successor is the more specific claim and the one a reader needs.
_KIND_CUES: tuple[tuple[DeclarationKind, tuple[str, ...]], ...] = (
    ("replaces", ("thay thế", "thay the", "replaced by", "supersede")),
    (
        "abrogates",
        ("bãi bỏ", "bai bo", "hủy bỏ", "huy bo", "hết hiệu lực", "het hieu luc",
         "ngừng hiệu lực", "chấm dứt hiệu lực", "abrogat", "repeal"),
    ),
    ("amends", ("sửa đổi", "sua doi", "bổ sung", "bo sung", "điều chỉnh", "amend")),
)  # fmt: skip

#: "Thông tư này", "Nghị định này", "Quyết định này" — the instrument talking about itself, and
#: therefore where a *replacement* anchor is attached: "Điều 7 Thông tư này thay thế …".
_SELF_REFERENCE = re.compile(
    r"(?:thông\s+tư|nghị\s+định|quyết\s+định|quy\s+chế|quy\s+trình|văn\s+bản|circular|decree)"
    r"\s+này",
    re.IGNORECASE,
)

#: A date only counts as the declaration's own when an effectivity cue introduces it. Without
#: this, *"thay thế Thông tư 10/2022/TT-NHNN ngày 15/3/2022"* would date the declaration by the
#: *target's* issue date — off by years, and in the direction that resurrects a repealed rule.
_EFFECTIVE_CUE = re.compile(
    r"(?:kể\s+từ\s+ngày|ke\s+tu\s+ngay|có\s+hiệu\s+lực\s+(?:thi\s+hành\s+)?(?:kể\s+)?từ"
    r"|hiệu\s+lực\s+từ|từ\s+ngày|effective\s+from|with\s+effect\s+from)",
    re.IGNORECASE,
)

#: Sentences end at a full stop or a line break. Deliberately not a smarter splitter: legal
#: numbers and dates are full of "/" and ".", and every clever boundary rule this corpus was
#: tried against split "41/2016/TT-NHNN." in the wrong place.
_SENTENCE = re.compile(r"[^\n.;]+(?:[.;]|$|\n)", re.MULTILINE)

#: How much of the sentence before a mention can carry its cue. The whole sentence, capped:
#: a declaration is one clause of prose, and reaching further starts borrowing the verb of the
#: sentence before it.
_CUE_WINDOW = 200
#: And how much after. Much shorter, because a verb *after* the instrument is only its own
#: under the conditions in `_acts_on`.
_PASSIVE_WINDOW = 60
#: What makes a following verb belong to the instrument before it rather than to a later
#: clause: Vietnamese marks the passive explicitly. "Điều 5 Thông tư X **được** sửa đổi".
_PASSIVE_MARKER = re.compile(r"\b(?:được|duoc|bị|bi|shall\s+be|is|are)\b", re.IGNORECASE)
#: Endings that need no marker because they take no object: an instrument does not "hết hiệu
#: lực" something, it simply ceases.
_INTRANSITIVE = ("hết hiệu lực", "het hieu luc", "ngừng hiệu lực", "chấm dứt hiệu lực")


@dataclass(frozen=True, slots=True)
class Declaration:
    """One instrument saying what it does to another."""

    kind: DeclarationKind
    #: The instrument acted upon. `None` when the sentence names no other instrument, which
    #: means the declaration is about the declaring document itself — a renumbering, and the
    #: merge flow's business rather than this one's.
    target: LegalNumber | None
    #: Clauses of the *target* this touches, in the dotted form the target's chunks carry.
    #: Empty means the whole instrument.
    target_anchors: list[str] = field(default_factory=list)
    #: Clauses of the *declaring* document that take their place. Empty for a pure abrogation,
    #: and also for a replacement whose new text simply follows the sentence unaddressed.
    replacement_anchors: list[str] = field(default_factory=list)
    #: Only when the sentence states it behind an effectivity cue. Never the date that happens
    #: to sit next to the target's number.
    effective_from: date | None = None
    #: The sentence itself, stored with the proposal so confirming it is one glance rather
    #: than a document read (ADR-0029's discipline).
    evidence: str = ""
    span: tuple[int, int] = (0, 0)
    confidence: float = 0.0

    @property
    def is_self_referential(self) -> bool:
        return self.target is None

    @property
    def ends_without_replacement(self) -> bool:
        """A clause stops applying and nothing takes its place."""
        return self.kind == "abrogates" and not self.replacement_anchors


def find_declarations(text: str, *, own_number: str | None = None) -> list[Declaration]:
    """Every sentence in which this document says what it does to another.

    `own_number` is the declaring document's own instrument number, so a sentence that merely
    repeats it — Vietnamese instruments name themselves constantly — is not read as a document
    superseding itself.

    Returns nothing for a document that declares nothing, which is the common case: most
    instruments are not amendments, and inventing a declaration for one would put a proposal in
    a steward's queue with no sentence behind it.
    """
    found: list[Declaration] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()

    for sentence, offset in _sentences(text):
        kind = _kind_of(sentence)
        if kind is None:
            continue

        replacement = _replacement_anchors(sentence)
        for number in extract_legal_numbers(sentence):
            if own_number and number.value == own_number:
                continue
            if not _acts_on(sentence, kind, number):
                continue

            anchors = find_anchors(sentence, number.start)
            key = (kind, number.key, tuple(anchors))
            if key in seen:
                continue
            seen.add(key)
            found.append(
                Declaration(
                    kind=kind,
                    target=number,
                    target_anchors=anchors,
                    # A replacement anchor is only meaningful when the sentence names a
                    # location in *this* document; "Thông tư này thay thế Thông tư X" replaces
                    # the whole instrument and has nothing clause-shaped to point at.
                    replacement_anchors=list(replacement),
                    effective_from=_stated_date(sentence),
                    evidence=sentence.strip(),
                    span=(offset, offset + len(sentence)),
                    # A recognised instrument code and a recognised cue is as sure as a pattern
                    # gets; an unrecognised code still matches the shape and wants a closer
                    # look, exactly as `extract_legal_numbers` grades it.
                    confidence=round(0.95 * number.confidence, 3),
                )
            )
    return found


def _sentences(text: str) -> list[tuple[str, int]]:
    return [(match.group(0), match.start()) for match in _SENTENCE.finditer(text)]


def _cues_for(kind: DeclarationKind) -> tuple[str, ...]:
    return next(cues for name, cues in _KIND_CUES if name == kind)


def _acts_on(sentence: str, kind: DeclarationKind, number: LegalNumber) -> bool:
    """Whether this sentence's verb is about *this* instrument.

    Vietnamese puts it on either side. Active, and the verb leads:
    *"bãi bỏ khoản 2 Điều 12 Thông tư 10/2022"*. Passive, and the instrument leads:
    *"Điều 5 Thông tư 10/2022 được sửa đổi, bổ sung như sau"* — which is the shape a
    consolidating amendment uses for every article it touches, so missing it would miss most
    of the corpus's amendments.

    The forward window is short and needs a passive marker precisely because the alternative
    over-reads: *"Căn cứ Thông tư 10/2022, nay bãi bỏ Điều 5 Thông tư 15/2020"* would otherwise
    declare the instrument it was merely enacted under.
    """
    cues = _cues_for(kind)
    before = sentence[max(0, number.start - _CUE_WINDOW) : number.start].lower()
    if any(cue in before for cue in cues):
        return True

    after = sentence[number.end : number.end + _PASSIVE_WINDOW].lower()
    for cue in cues:
        at = after.find(cue)
        if at < 0:
            continue
        if cue in _INTRANSITIVE or _PASSIVE_MARKER.search(after[:at]):
            return True
    return False


def _kind_of(sentence: str) -> DeclarationKind | None:
    lowered = sentence.lower()
    for kind, cues in _KIND_CUES:
        if any(cue in lowered for cue in cues):
            return kind
    return None


def _replacement_anchors(sentence: str) -> list[str]:
    """Clauses of the declaring document named beside a "… này".

    *"Điều 7 Thông tư này thay thế Điều 5 Thông tư 10/2022"* — the anchor before the
    self-reference is the replacement, the one before the instrument number is the target.
    That is the whole reason this path needs no inference about direction.
    """
    match = _SELF_REFERENCE.search(sentence)
    if match is None:
        return []
    return find_anchors(sentence, match.start())


def _stated_date(sentence: str) -> date | None:
    """The date the declaration gives itself, or nothing.

    Read only from behind an effectivity cue. The alternative — taking the first date in the
    sentence — reads *"thay thế Thông tư 10/2022/TT-NHNN ngày 15/3/2022"* as taking effect in
    2022, which is the target's issue date and years wrong in the direction that keeps a
    repealed rule in service.
    """
    cue = _EFFECTIVE_CUE.search(sentence)
    if cue is None:
        return None
    dates = find_dates(sentence[cue.end() :])
    return dates[0] if dates else None


__all__ = ["Declaration", "DeclarationKind", "find_declarations"]
