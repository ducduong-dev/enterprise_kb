"""KBDoc → retrievable chunks.

The chunk is the unit a compliance officer gets back and has to act on, so the boundary is
structural, not a fixed token window: **one clause (Khoản), or one article (Điều) when it has
no clauses**. That is the granularity Vietnamese instruments are written and cited at, and it
is what makes `citation_label` a real citation ("Điều 12.2, TT 41/2016/TT-NHNN") rather than
"chunk 47".

Consequences of that choice, handled here:

* A clause too long to embed well is split, and every part keeps the same citation — a split
  is an embedding concern, never a citation concern.
* A clause too short to retrieve on its own ("2. Áp dụng từ ngày 01/01/2026.") is merged with
  its siblings until it carries enough context to match a query.
* A table is never split. Half a fee schedule is worse than no fee schedule.
* Every chunk carries its ancestors as a heading prefix, so a query about "tỷ lệ an toàn vốn"
  matches a clause that only says "tỷ lệ này" — the article title is in the chunk text.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from kb_schemas.kbdoc import Block, KBDoc
from kb_vntext.sections import build_citation_label

#: Target size in characters. BGE-M3 handles 8k tokens, but retrieval quality falls off long
#: before that: a chunk answering two questions ranks well for neither.
TARGET_CHARS = 1200
#: Hard ceiling for one chunk's text before it is split.
MAX_CHARS = 2000
#: Below this, a chunk is merged with the next sibling instead of being emitted alone.
MIN_CHARS = 200
#: Overlap when a long clause has to be split, so a sentence spanning the cut is still findable.
SPLIT_OVERLAP_CHARS = 150


@dataclass(slots=True)
class Chunk:
    """A chunk before it acquires ACL columns and an embedding (those come from the document
    and the EmbeddingPort at publish time)."""

    id: uuid.UUID
    section_path: list[str]
    citation_label: str | None
    text: str
    #: Ordinal within the document, so a reviewer can read chunks back in document order.
    ordinal: int
    page: int
    block_ids: list[str] = field(default_factory=list)
    #: Set when a single clause was too long and had to be divided.
    part: int = 1
    part_count: int = 1
    is_table: bool = False

    @property
    def section_path_text(self) -> str:
        return " > ".join(self.section_path)


def chunk_document(kbdoc: KBDoc, *, legal_number: str | None = None) -> list[Chunk]:
    """Split a parsed document into chunks at clause/article granularity."""
    number = legal_number or kbdoc.doc_meta.legal_number
    groups = _group_by_section(kbdoc.blocks)

    chunks: list[Chunk] = []
    ordinal = 0
    pending: _Group | None = None

    for group in groups:
        # Tables stand alone: merging one into prose would bury the grid, and splitting it
        # would strand rows from their header. The exception is a heading immediately above
        # one — that is the table's caption, and a table without it is unattributable.
        if group.is_table:
            if pending is not None and pending.is_heading_only:
                ordinal = _emit(chunks, pending.merge(group), number, ordinal)
            else:
                if pending is not None:
                    ordinal = _emit(chunks, pending, number, ordinal)
                ordinal = _emit(chunks, group, number, ordinal)
            pending = None
            continue

        if pending is None:
            pending = group
        elif pending.can_merge_with(group):
            pending = pending.merge(group)
        else:
            ordinal = _emit(chunks, pending, number, ordinal)
            pending = group

        if pending is not None and len(pending.text) >= TARGET_CHARS:
            ordinal = _emit(chunks, pending, number, ordinal)
            pending = None

    if pending is not None:
        _emit(chunks, pending, number, ordinal)

    return chunks


@dataclass(slots=True)
class _Group:
    """Blocks sharing one structural location."""

    section_path: list[str]
    blocks: list[Block]
    is_table: bool = False

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.blocks if block.text)

    @property
    def page(self) -> int:
        return self.blocks[0].page if self.blocks else 1

    @property
    def is_heading_only(self) -> bool:
        """A bare structural label with nothing under it yet — "Điều 9. Quy định chi tiết".

        Never a chunk on its own: it matches every query about that phrase and answers none.
        Deliberately a *single short* heading block: clause openers are also typed as headings
        once the tracker recognizes them, and treating an accumulated group as "just a label"
        would let it absorb the entire document.
        """
        return (
            not self.is_table
            and len(self.blocks) == 1
            and self.blocks[0].type == "heading"
            and len(self.text) < MIN_CHARS
        )

    def can_merge_with(self, other: _Group) -> bool:
        """Merge a bare heading into what follows, or short neighbours under one article.

        Merging across articles would produce a chunk citing one article while containing
        another's text — the failure mode that makes an answer unverifiable.
        """
        if other.is_table or self.is_table:
            return False
        if self.is_heading_only:
            # Always: a heading emitted alone is a chunk that matches queries about its topic
            # and answers none of them. Oversized merges are split afterwards, and every part
            # keeps the section path in its text.
            return True
        if len(self.text) >= MIN_CHARS and len(other.text) >= MIN_CHARS:
            return False
        if len(self.text) + len(other.text) > TARGET_CHARS:
            return False
        return _article_of(self.section_path) == _article_of(other.section_path)

    def merge(self, other: _Group) -> _Group:
        if self.is_heading_only:
            # The heading is context for what follows, so the chunk is cited where the
            # *content* sits: heading "Điều 9" + clause "Khoản 1" cites Điều 9.1.
            path = other.section_path
        else:
            # Spanning two clauses, the citation backs off to their common ancestor —
            # otherwise the chunk would cite Khoản 1 while also containing Khoản 2.
            path = _common_prefix(self.section_path, other.section_path) or min(
                (self.section_path, other.section_path), key=len
            )
        return _Group(
            section_path=list(path),
            blocks=[*self.blocks, *other.blocks],
            is_table=self.is_table or other.is_table,
        )


def _group_by_section(blocks: list[Block]) -> list[_Group]:
    groups: list[_Group] = []
    for block in blocks:
        if block.table is not None:
            groups.append(
                _Group(section_path=list(block.section_path), blocks=[block], is_table=True)
            )
            continue
        if block.type == "heading":
            # A heading opens a new group; its text becomes that group's first line, which is
            # what puts the article title into the chunk a clause query has to match.
            groups.append(_Group(section_path=list(block.section_path), blocks=[block]))
            continue
        if (
            groups
            and not groups[-1].is_table
            and groups[-1].section_path == list(block.section_path)
        ):
            groups[-1].blocks.append(block)
        else:
            groups.append(_Group(section_path=list(block.section_path), blocks=[block]))
    return [group for group in groups if group.text.strip()]


def _emit(chunks: list[Chunk], group: _Group, legal_number: str | None, ordinal: int) -> int:
    citation = build_citation_label(group.section_path, legal_number) or None
    heading = _heading_prefix(group.section_path)
    parts = [group.text] if group.is_table else _split(group.text)

    for index, part in enumerate(parts, start=1):
        body = part if part.startswith(heading) or not heading else f"{heading}\n{part}"
        chunks.append(
            Chunk(
                id=uuid.uuid4(),
                section_path=list(group.section_path),
                citation_label=citation,
                text=body.strip(),
                ordinal=ordinal,
                page=group.page,
                block_ids=[block.id for block in group.blocks],
                part=index,
                part_count=len(parts),
                is_table=group.is_table,
            )
        )
        ordinal += 1
    return ordinal


def _heading_prefix(section_path: list[str]) -> str:
    """Ancestors as a single line. Without it, "2. Hệ số rủi ro..." is unmatchable by a query
    naming the article it belongs to."""
    return " > ".join(section_path)


def _split(text: str) -> list[str]:
    """Divide over-long text on paragraph, then sentence, then hard boundaries."""
    if len(text) <= MAX_CHARS:
        return [text]

    parts: list[str] = []
    remaining = text
    while len(remaining) > MAX_CHARS:
        cut = _best_cut(remaining, MAX_CHARS)
        parts.append(remaining[:cut].strip())
        # Overlap keeps a sentence that straddles the cut retrievable from either side.
        remaining = remaining[max(0, cut - SPLIT_OVERLAP_CHARS) :].strip()
    if remaining:
        parts.append(remaining)
    return parts


def _best_cut(text: str, limit: int) -> int:
    window = text[:limit]
    for separator in ("\n\n", "\n", ". ", "; ", " "):
        position = window.rfind(separator)
        # Refuse a cut that would leave a stub: better an oversized piece than a meaningless one.
        if position > limit // 2:
            return position + len(separator)
    return limit


def _article_of(section_path: list[str]) -> str | None:
    return next((part for part in section_path if part.startswith(("Điều", "Article"))), None)


def _common_prefix(left: list[str], right: list[str]) -> list[str]:
    prefix: list[str] = []
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        prefix.append(a)
    return prefix
