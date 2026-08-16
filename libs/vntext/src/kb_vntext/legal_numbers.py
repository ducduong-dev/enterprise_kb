"""Vietnamese legal instrument numbers.

Identity resolution (M5) decides whether two documents are the *same instrument*; this module
does the mechanical part both it and the IDP need: find the numbers in a text, normalize them
to a comparison key, and guess the relationship a mention implies.

Formats in scope:

    41/2016/TT-NHNN         Thông tư — number/year/type-issuer
    88/2019/NĐ-CP           Nghị định
    1627/2001/QĐ-NHNN       Quyết định
    32/2024/QH15            Luật, passed by the National Assembly
    05/VBHN-NHNN            Văn bản hợp nhất (consolidated text) — no year segment
    1234/NHNN-TTGSNH        Công văn — issuer/department, no type code
    114/2023/QĐ-HĐQT        Bank internal decision, national format
    QD-2023-114             Bank internal decision, legacy format
    QT-2024-007             Bank internal procedure

The normalized key folds diacritics in the *type code only* (QĐ ≡ QD, NĐ ≡ ND), because both
spellings appear in practice — in OCR output, in filenames, and in what people type into
search. It never folds diacritics in document text; that would destroy meaning.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

#: Instrument type codes we recognize, mapped to their Vietnamese name.
INSTRUMENT_TYPES: Final[dict[str, str]] = {
    "TT": "Thông tư",
    "TTLT": "Thông tư liên tịch",
    "ND": "Nghị định",
    "QD": "Quyết định",
    "CT": "Chỉ thị",
    "VBHN": "Văn bản hợp nhất",
    "QH": "Luật",
    "PL": "Pháp lệnh",
    "NQ": "Nghị quyết",
    "CV": "Công văn",
    "QT": "Quy trình",
    "QC": "Quy chế",
    "HD": "Hướng dẫn",
}

#: Words that introduce an instrument, used to widen a match into its human label.
INSTRUMENT_PREFIXES: Final[tuple[str, ...]] = (
    "Thông tư liên tịch",
    "Thông tư",
    "Nghị định",
    "Quyết định",
    "Chỉ thị",
    "Văn bản hợp nhất",
    "Nghị quyết",
    "Công văn",
    "Pháp lệnh",
    "Quy trình",
    "Quy chế",
    "Hướng dẫn",
    "Luật",
    "Circular",
    "Decree",
    "Decision",
    "Law",
)

#: A type code may carry a trailing number — `QH15` is the 15th National Assembly.
_TYPE = r"[A-ZĐ]{2,6}\d{0,3}"
#: Must not end on a separator, or a sentence-final period is swallowed into the issuer.
_ISSUER = r"[A-ZĐ0-9](?:[A-ZĐ0-9\-]{0,23}[A-ZĐ0-9])?"
_YEAR = r"(?:19|20)\d{2}"

#: 41/2016/TT-NHNN, 32/2024/QH15, 114/2023/QĐ-HĐQT
_FULL = re.compile(rf"\b(\d{{1,5}})/({_YEAR})/({_TYPE})(?:-({_ISSUER}))?")
#: 05/VBHN-NHNN, 1234/NHNN-TTGSNH, 01/CT-NHNN
_SHORT = re.compile(rf"\b(\d{{1,5}})/({_TYPE})-({_ISSUER})")
#: QD-2023-114, QT-2024-007
_LEGACY = re.compile(rf"\b({_TYPE})-({_YEAR})-(\d{{1,5}})\b")

_PREFIX_PATTERN = re.compile(
    r"(?:" + "|".join(re.escape(prefix) for prefix in INSTRUMENT_PREFIXES) + r")\s+(?:số\s+)?$",
    re.IGNORECASE,
)

#: Verbs that reveal what a mention *does* to the instrument it names. Order matters:
#: the first match wins, so the more specific relationships come first.
_REF_TYPE_CUES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("consolidates", ("hợp nhất", "văn bản hợp nhất", "consolidat")),
    (
        "abrogates",
        ("bãi bỏ", "hết hiệu lực", "thay thế", "hủy bỏ", "abrogat", "repeal", "supersed"),
    ),
    ("amends", ("sửa đổi", "bổ sung", "điều chỉnh", "amend", "modif")),
    (
        "implements",
        ("hướng dẫn thi hành", "triển khai thực hiện", "quy định chi tiết", "thi hành",
         "triển khai", "implement"),
    ),
)  # fmt: skip

#: How far back from a mention we look for a relationship cue.
_CUE_WINDOW = 160


@dataclass(frozen=True, slots=True)
class LegalNumber:
    """One instrument number found in a text."""

    #: Exactly as it appeared, including any "Thông tư" prefix.
    raw: str
    #: Canonical rendering of the number itself, e.g. "41/2016/TT-NHNN".
    value: str
    #: Diacritic-folded, uppercased comparison key. Two spellings of one instrument share it.
    key: str
    instrument_type: str | None
    issuer: str | None
    year: int | None
    start: int
    end: int
    confidence: float = 1.0

    @property
    def instrument_name(self) -> str | None:
        return INSTRUMENT_TYPES.get(self.instrument_type) if self.instrument_type else None


def fold_code(value: str) -> str:
    """Fold diacritics for comparison. Applies to instrument codes, never to document text."""
    decomposed = unicodedata.normalize("NFD", value)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.replace("Đ", "D").replace("đ", "d").upper()


def normalize_legal_number(value: str) -> str:
    """Comparison key: uppercase, diacritic-folded, whitespace and trailing dots removed."""
    return fold_code(re.sub(r"\s+", "", value.strip().rstrip(".")))


def extract_legal_numbers(text: str) -> list[LegalNumber]:
    """Every instrument number in `text`, in order, without duplicates by position.

    Overlapping matches are resolved in favour of the longer, more specific pattern: the full
    number/year/type form is tried before the short form, so `41/2016/TT-NHNN` is never split
    into a bogus short match.
    """
    found: list[LegalNumber] = []
    claimed: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < c_end and end > c_start for c_start, c_end in claimed)

    for match in _FULL.finditer(text):
        number, year, code, issuer = match.groups()
        value = f"{number}/{year}/{code}" + (f"-{issuer}" if issuer else "")
        found.append(_build(text, match.start(), match.end(), value, code, issuer, int(year)))
        claimed.append((match.start(), match.end()))

    for match in _SHORT.finditer(text):
        if overlaps(*match.span()):
            continue
        number, code, issuer = match.groups()
        value = f"{number}/{code}-{issuer}"
        found.append(_build(text, match.start(), match.end(), value, code, issuer, None))
        claimed.append(match.span())

    for match in _LEGACY.finditer(text):
        if overlaps(*match.span()):
            continue
        code, year, number = match.groups()
        value = f"{code}-{year}-{number}"
        found.append(_build(text, match.start(), match.end(), value, code, None, int(year)))
        claimed.append(match.span())

    return sorted(found, key=lambda item: item.start)


def _build(
    text: str,
    start: int,
    end: int,
    value: str,
    code: str,
    issuer: str | None,
    year: int | None,
) -> LegalNumber:
    prefix_match = _PREFIX_PATTERN.search(text[max(0, start - 40) : start])
    raw_start = start - len(prefix_match.group(0)) if prefix_match else start
    # Trailing digits on a code identify the issuing term (QH15), not the instrument kind.
    folded_code = fold_code(code.rstrip("0123456789"))
    return LegalNumber(
        raw=text[raw_start:end],
        value=value,
        key=normalize_legal_number(value),
        instrument_type=folded_code,
        issuer=issuer,
        year=year,
        start=raw_start,
        end=end,
        # A recognized instrument code is strong evidence; an unknown one still matches the
        # shape but is left for a human to confirm.
        confidence=0.99 if folded_code in INSTRUMENT_TYPES else 0.75,
    )


def guess_ref_type(text: str, mention_start: int) -> str:
    """Classify what the sentence around a mention says about it.

    Only ever a *guess*: `document_refs.detected_by` records that a machine proposed it, and
    the edge is not authoritative until a human sets `confirmed_by` (M5 review task).
    """
    window = text[max(0, mention_start - _CUE_WINDOW) : mention_start].lower()
    for ref_type, cues in _REF_TYPE_CUES:
        if any(cue in window for cue in cues):
            return ref_type
    return "cites"


#: How far back from a mention an anchor may sit. Shorter than the ref-type window on purpose:
#: "khoản 2 Điều 12 Thông tư 41/2016" is a tight phrase, and reaching further back starts
#: collecting the article numbers of *neighbouring* citations in a list.
_ANCHOR_WINDOW = 80

#: "Điều 12", "Điều 12a", "Article 12" — with an optional list tail: "Điều 5, 6 và 7".
_ANCHOR_ARTICLE = re.compile(
    r"(?:Điều|Dieu|Article)\s+(\d{1,3}[a-zđ]?)"
    r"((?:\s*(?:,|và|va|and)\s*\d{1,3}[a-zđ]?)*)",
    re.IGNORECASE,
)
_ANCHOR_LIST_TAIL = re.compile(r"(\d{1,3}[a-zđ]?)")
#: "khoản 2", "clause 2". Immediately before the article in Vietnamese citation order.
_ANCHOR_CLAUSE = re.compile(r"(?:khoản|khoan|clause)\s+(\d{1,2})", re.IGNORECASE)
#: "điểm a", "point a".
_ANCHOR_POINT = re.compile(r"(?:điểm|diem|point)\s+([a-hjklmnopqrstuvxyzđ])\b", re.IGNORECASE)


def find_anchors(text: str, mention_start: int) -> list[str]:
    """Which clauses of the cited instrument this reference names.

    Vietnamese citations run inside-out and sit *before* the instrument number: *"điểm a khoản
    3 Điều 8 Nghị định 88/2019/NĐ-CP"*. So the window before the mention is where the address
    is, exactly as it is for `guess_ref_type`.

    Returns the bare dotted form `build_anchor` emits — `"12"`, `"12.2"`, `"8.3a"` — so an
    anchor read from citing text is directly comparable to the anchor stored on the cited
    document's chunks. Empty when the citation names no article, which is the common case:
    *"theo quy định tại Thông tư 41/2016/TT-NHNN"* cites the whole instrument, and inventing an
    article for it would resolve a general reference to one arbitrary clause.

    A citation naming several articles — *"các Điều 5, 6 và 7"* — yields one anchor each. Only
    the last article in such a list can carry a clause, because that is the only one Vietnamese
    drafting attaches one to.
    """
    window = text[max(0, mention_start - _ANCHOR_WINDOW) : mention_start]
    # The article nearest the instrument number is the one it belongs to: in "Điều 5 của
    # Thông tư X và Điều 9 của Thông tư Y", each number takes the article on its left.
    found = list(_ANCHOR_ARTICLE.finditer(window))
    if not found:
        return []
    match = found[-1]

    articles = [match.group(1), *_ANCHOR_LIST_TAIL.findall(match.group(2) or "")]
    # The clause and point must sit *before* this article, or they belong to another citation.
    prefix = window[: match.start()]
    clause = _last(_ANCHOR_CLAUSE, prefix)
    point = _last(_ANCHOR_POINT, prefix) if clause else None

    anchors = [article.lower() for article in articles]
    if clause and len(anchors) == 1:
        anchors[0] = f"{anchors[0]}.{clause}{point or ''}"
    return anchors


def _last(pattern: re.Pattern[str], text: str) -> str | None:
    """The last match's first group, or None. "Last" because the nearest one to the article is
    the one that belongs to the citation being read."""
    found = pattern.findall(text)
    return str(found[-1]).lower() if found else None


def find_document_number(text: str, *, max_chars: int = 2000) -> LegalNumber | None:
    """The instrument number of the document *itself*, if it declares one.

    Vietnamese instruments carry their number in the header block, so only the opening of the
    document is considered — a citation on page 12 is not this document's own number.
    """
    numbers = extract_legal_numbers(text[:max_chars])
    return numbers[0] if numbers else None


#: Query terms that carry no signal and blow up an OR query.
_MIN_QUERY_TERM: Final[int] = 2
_MAX_QUERY_TERMS: Final[int] = 32
_QUERY_WORD = re.compile(r"[^\W_]+", re.UNICODE)
#: A letter run followed by a digit run, glued together the way people type instrument
#: shorthand: `TT41`, `QD114`, `ND88`.
_GLUED = re.compile(r"^([A-Za-zĐđ]{1,6})(\d{1,5})$")


def query_terms(query: str) -> list[str]:
    """Search terms for a keyword engine, with instrument shorthand pulled apart.

    "TT41" is one token to every tokenizer in existence — Tantivy's, Lucene's and Postgres's
    alike — and no document contains it, because the document says "TT 41/2016/TT-NHNN". A
    reader typing the shorthand is naming an instrument, and splitting the letters from the
    digits is what lets the citation label match it. This lives here rather than in an adapter
    because all three keyword backends need the identical rule; a per-engine version would be
    three chances for the same query to behave differently (bake-off gate 3).

    Order is preserved and duplicates are dropped, so the caller can OR the terms and let
    ranking sort out the rest.
    """
    terms: list[str] = []

    def add(term: str) -> None:
        if len(term) >= _MIN_QUERY_TERM and term not in terms:
            terms.append(term)

    for word in _QUERY_WORD.findall(query.lower()):
        add(word)
        glued = _GLUED.match(word)
        if glued:
            # Both halves *and* the original: "tt41" may yet appear verbatim in a filename or
            # an OCR artefact, and dropping it would trade one miss for another.
            add(glued.group(1))
            add(glued.group(2))
    return terms[:_MAX_QUERY_TERMS]
