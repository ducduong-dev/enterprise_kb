"""Document structure: Phần → Chương → Mục → Điều → Khoản → Điểm.

A chunk's worth in this corpus comes from knowing *where* it sits: "Điều 12 Khoản 2 of
Circular 41" is a citation a compliance officer can act on, while the same sentence with no
location is unusable. So structure detection runs at parse time, is preserved through OCR
(M3), and drives both `section_path` and the citation label.

English headings are recognized too — the corpus is bilingual, and translated internal
policies use `Article`/`Section` while carrying the same legal weight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum


class Level(IntEnum):
    """Structural depth. Lower numbers contain higher ones."""

    PART = 1
    CHAPTER = 2
    SECTION = 3
    ARTICLE = 4
    CLAUSE = 5
    POINT = 6


@dataclass(frozen=True, slots=True)
class Heading:
    level: Level
    #: Canonical label used in `section_path`, e.g. "Điều 12", "Khoản 2".
    label: str
    #: The number/letter on its own, e.g. "12", "II", "a".
    number: str
    #: Text following the heading on the same line, e.g. the article's title.
    title: str = ""
    language: str = "vi"


_PATTERNS: tuple[tuple[Level, str, re.Pattern[str]], ...] = (
    (Level.PART, "Phần", re.compile(r"^\s*PHẦN\s+(THỨ\s+\S+|[IVXLC]+|\d+)\b\.?", re.IGNORECASE)),
    (Level.PART, "Part", re.compile(r"^\s*PART\s+([IVXLC]+|\d+)\b\.?", re.IGNORECASE)),
    (Level.CHAPTER, "Chương", re.compile(r"^\s*CHƯƠNG\s+([IVXLC]+|\d+)\b\.?", re.IGNORECASE)),
    (Level.CHAPTER, "Chapter", re.compile(r"^\s*CHAPTER\s+([IVXLC]+|\d+)\b\.?", re.IGNORECASE)),
    (Level.SECTION, "Mục", re.compile(r"^\s*MỤC\s+([IVXLC]+|\d+)\b\.?", re.IGNORECASE)),
    (
        Level.SECTION,
        "Section",
        re.compile(r"^\s*SECTION\s+([IVXLC]+|\d+(?:\.\d+)*)\b\.?", re.IGNORECASE),
    ),
    (Level.ARTICLE, "Điều", re.compile(r"^\s*ĐIỀU\s+(\d+[a-zđ]?)\b\.?", re.IGNORECASE)),
    (Level.ARTICLE, "Article", re.compile(r"^\s*ARTICLE\s+(\d+[a-z]?)\b\.?", re.IGNORECASE)),
)

#: Khoản — a bare "1." opening a paragraph. Only a clause when we are inside an article,
#: otherwise it is an ordinary numbered list and must not create a structural level.
_CLAUSE = re.compile(r"^\s*(\d{1,2})[\.\)]\s+(?=\S)")
#: Điểm — "a)" / "đ)". Vietnamese ordering includes đ, which is why this is not [a-z].
_POINT = re.compile(r"^\s*([a-hjklmnopqrstuvxyzđ])[\.\)]\s+(?=\S)")

_ENGLISH_LABELS = {"Part", "Chapter", "Section", "Article"}

#: Longest a named heading can plausibly be. Beyond this it is prose.
_MAX_HEADING_CHARS = 300


def parse_heading(line: str, *, inside_article: bool = False) -> Heading | None:
    """Recognize a structural heading at the start of `line`, or return None."""
    text = line.strip()
    if not text:
        return None

    # A named heading ("Chương II", "Điều 12") longer than a line of prose is prose that
    # happens to start with a heading word; treating it as structure would fragment the
    # document. Clause and point markers get no such guard: "1. <two thousand characters>"
    # is exactly what a substantial Khoản looks like.
    for level, label_word, pattern in _PATTERNS:
        if len(text) > _MAX_HEADING_CHARS:
            break
        match = pattern.match(text)
        if match:
            number = match.group(1).strip()
            return Heading(
                level=level,
                label=f"{label_word} {number}",
                number=number,
                title=_clean_title(text[match.end() :]),
                language="en" if label_word in _ENGLISH_LABELS else "vi",
            )

    if inside_article:
        clause = _CLAUSE.match(text)
        if clause:
            return Heading(
                level=Level.CLAUSE,
                label=f"Khoản {clause.group(1)}",
                number=clause.group(1),
                title=_clean_title(text[clause.end() :]),
            )
        point = _POINT.match(text)
        if point:
            return Heading(
                level=Level.POINT,
                label=f"Điểm {point.group(1)}",
                number=point.group(1),
                title=_clean_title(text[point.end() :]),
            )
    return None


def _clean_title(text: str) -> str:
    # En and em dashes are common heading separators in Vietnamese documents.
    return re.sub("^[\\.\\-\u2013\u2014:\\s]+", "", text).strip()


@dataclass
class SectionTracker:
    """Maintains the current structural path while walking a document in order.

    Feed it every block of text; ask it for `path` when emitting a block. Opening a heading at
    some level closes everything at or below it, which is what makes a flat stream of
    paragraphs reconstruct a tree.
    """

    _stack: list[Heading] = field(default_factory=list)

    @property
    def path(self) -> list[str]:
        return [heading.label for heading in self._stack]

    @property
    def inside_article(self) -> bool:
        return any(heading.level >= Level.ARTICLE for heading in self._stack)

    @property
    def current_article(self) -> Heading | None:
        return next((h for h in reversed(self._stack) if h.level is Level.ARTICLE), None)

    def feed(self, line: str) -> Heading | None:
        """Update the path from a line. Returns the heading if the line was one."""
        heading = parse_heading(line, inside_article=self.inside_article)
        if heading is not None:
            self.push(heading)
        return heading

    def push(self, heading: Heading) -> None:
        while self._stack and self._stack[-1].level >= heading.level:
            self._stack.pop()
        self._stack.append(heading)

    def reset(self) -> None:
        self._stack.clear()


def build_citation_label(path: list[str], legal_number: str | None = None) -> str:
    """Human-quotable citation, e.g. "Điều 12.2, TT 41/2016/TT-NHNN".

    Article and clause collapse into the dotted form lawyers actually write; anything above
    the article is context the citation does not need.
    """
    article = next((p for p in path if p.startswith(("Điều", "Article"))), None)
    clause = next((p for p in path if p.startswith("Khoản")), None)
    point = next((p for p in path if p.startswith("Điểm")), None)

    if article:
        location = article
        if clause:
            location += f".{clause.split()[-1]}"
        if point:
            location += f"{point.split()[-1]}"
    else:
        location = path[-1] if path else ""

    if legal_number:
        return f"{location}, {legal_number}" if location else legal_number
    return location
