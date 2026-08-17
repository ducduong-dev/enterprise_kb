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

import re
import uuid
from dataclasses import dataclass, field

from kb_schemas.kbdoc import Block, KBDoc
from kb_vntext.language import fold_diacritics
from kb_vntext.sections import build_anchor, build_citation_label

_ARTICLE = re.compile(r"(?:Điều|Article)\s+(\d+)", re.IGNORECASE)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
#: Structural labels and instrument boilerplate. Diacritic-folded, because that is the form
#: `subject_key` compares in. Shares its purpose with `matching._TITLE_STOPWORDS`, which does
#: the same job for document titles.
_SUBJECT_STOPWORDS = frozenset(
    {
        # Structural labels: they say what the clause *is*, never what it is about.
        "chuong", "muc", "dieu", "khoan", "diem", "phu", "luc", "phan",
        "chapter", "section", "article", "clause", "point", "part", "annex",
        # Instrument boilerplate. Every Vietnamese instrument opens with these.
        "thong", "tu", "nghi", "dinh", "quyet", "quy", "che", "trinh", "hanh",
        # Function words. "cho vay" keeps `vay`, which is the half that carries the subject.
        "ve", "viec", "cua", "va", "cac", "ban", "mot", "so", "doi", "voi",
        "cho", "tai", "trong", "theo", "den", "cung", "khi", "la", "co",
        "the", "of", "and", "on", "for", "to", "in", "with", "by", "at",
        # Chapter numerals, which are structure wearing letters.
        "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii",
    }
)  # fmt: skip

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
    #: The article this chunk belongs to, so the supersession flag can be narrowed to the
    #: articles an amendment actually touches and a reference can resolve by equality join
    #: (ADR-0032, ADR-0036). NULL for front matter, an appendix, or a table before Điều 1.
    article: int | None = None
    #: The full dotted address of this clause — "12", "12.2", "12.2a" — which is what a
    #: reference resolves against. The citation label is the same address dressed for a
    #: reader; this one is a join key (ADR-0036).
    anchor: str | None = None
    #: The heading chain with instrument boilerplate stripped — what two clauses stating the
    #: same rule share even when they are worded differently (ADR-0033).
    subject_key: str | None = None
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
    titles = _heading_titles(kbdoc.blocks)

    chunks: list[Chunk] = []
    ordinal = 0
    pending: _Group | None = None

    for group in groups:
        # Tables stand alone: merging one into prose would bury the grid, and splitting it
        # would strand rows from their header. The exception is a heading immediately above
        # one — that is the table's caption, and a table without it is unattributable.
        if group.is_table:
            if pending is not None and pending.is_heading_only:
                ordinal = _emit(chunks, pending.merge(group), number, ordinal, titles)
            else:
                if pending is not None:
                    ordinal = _emit(chunks, pending, number, ordinal, titles)
                ordinal = _emit(chunks, group, number, ordinal, titles)
            pending = None
            continue

        if pending is None:
            pending = group
        elif pending.can_merge_with(group):
            pending = pending.merge(group)
        else:
            ordinal = _emit(chunks, pending, number, ordinal, titles)
            pending = group

        if pending is not None and len(pending.text) >= TARGET_CHARS:
            ordinal = _emit(chunks, pending, number, ordinal, titles)
            pending = None

    if pending is not None:
        _emit(chunks, pending, number, ordinal, titles)

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


def _heading_titles(blocks: list[Block]) -> dict[tuple[str, ...], str]:
    """Each structural path mapped to the heading line the document actually printed.

    `SectionTracker.path` carries labels — "Điều 6" — because a label is a stable join key and
    a title is prose that a consolidation can reword. That is right for `section_path`, and it
    is why `subject_key` computed from `section_path` alone was **always None**: a path of pure
    structure says what a clause *is* and never what it is about, so the one field that exists
    to say what a clause is about had nothing to read.

    The title was never lost, only unused: a heading block keeps its whole line, and its own
    `section_path` ends at itself. So the raw text is recoverable here, from data every stored
    KBDoc already holds — which is what makes this fixable by a rechunk rather than by
    re-running OCR over the corpus.
    """
    return {
        tuple(block.section_path): block.text
        for block in blocks
        if block.type == "heading" and block.section_path
    }


def _titled_path(section_path: list[str], titles: dict[tuple[str, ...], str]) -> list[str]:
    """The path with each level's printed heading in place of its bare label.

    Falls back to the label where a document printed no title for that level, which is common
    for `Khoản 2` and is exactly the case that should contribute nothing to a subject key.
    """
    return [
        titles.get(tuple(section_path[: index + 1]), part)
        for index, part in enumerate(section_path)
    ]


def _emit(
    chunks: list[Chunk],
    group: _Group,
    legal_number: str | None,
    ordinal: int,
    titles: dict[tuple[str, ...], str] | None = None,
) -> int:
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
                article=article_number(group.section_path),
                anchor=build_anchor(group.section_path),
                subject_key=subject_key(_titled_path(group.section_path, titles or {})),
                block_ids=[block.id for block in group.blocks],
                part=index,
                part_count=len(parts),
                is_table=group.is_table,
            )
        )
        ordinal += 1
    return ordinal


def split_section_path(section_path_text: str | None) -> list[str]:
    """The inverse of `Chunk.section_path_text`.

    Exact rather than a best effort: the parts are heading fragments and never contain the
    " > " separator. Lives here beside the join it undoes, so a reader of a stored
    `chunks.section_path` — the backfill, a migration, a report — recovers the same list the
    chunker started from instead of re-inventing the split.
    """
    if not section_path_text:
        return []
    return [part.strip() for part in section_path_text.split(">") if part.strip()]


def article_number(section_path: list[str]) -> int | None:
    """The article a section path sits under, as an integer.

    Derived once, here, and stored — the SQL layer and the retrieval hot path must not
    re-derive it with a regex over a text column, and ADR-0036's resolver needs to *join* on
    it. Mirrors `diff._article_number`, which recovers the same number for the impact
    traversal; the two are asserted to agree in the chunker's tests.
    """
    for part in section_path:
        match = _ARTICLE.search(part)
        if match:
            return int(match.group(1))
    return None


def _title_tokens(part: str) -> str:
    """One heading's subject words, folded, deduplicated and sorted into a comparable key.

    Diacritics folded because the same subject is typed both ways across a corpus this size,
    and tokens sorted so word order cannot split one subject into two keys.
    """
    tokens = {
        token
        for token in _WORD.findall(fold_diacritics(part).lower())
        if len(token) > 1 and token not in _SUBJECT_STOPWORDS and not token.isdigit()
    }
    return " ".join(sorted(tokens))


#: Headings every Vietnamese instrument carries. Their *words* are ordinary — "hiệu" is in
#: "hiệu quả" and "thi" in "thi công" — so these cannot be stopworded token by token without
#: losing real subjects. They are boilerplate as whole headings, which is the granularity at
#: which they are recognisable, so that is where they are matched.
_BOILERPLATE_TITLES = (
    "Hiệu lực thi hành",
    "Điều khoản thi hành",
    "Điều khoản chuyển tiếp",
    "Tổ chức thực hiện",
    "Trách nhiệm thi hành",
    "Phạm vi điều chỉnh",
    "Đối tượng áp dụng",
    "Giải thích từ ngữ",
    "Quy định chung",
    "Nguyên tắc chung",
)
#: Computed with the same tokenizer the keys are, so the list above stays readable prose and
#: cannot drift from what it is compared against.
_BOILERPLATE_KEYS = frozenset(_title_tokens(title) for title in _BOILERPLATE_TITLES) - {""}


def subject_key(section_path: list[str]) -> str | None:
    """What this clause is *about*, normalized so two documents can be compared on it.

    **The deepest titled heading, not the chain.** `Chương II. Tỷ lệ an toàn vốn > Điều 6. Tỷ lệ
    an toàn vốn tối thiểu > Khoản 1` keys on Điều 6's title alone. Unioning the whole chain was
    the original reading of "the heading chain with boilerplate stripped" and it defeated the
    only thing this key is for: an ancestor heading pollutes it, so the regulator's `Chương II.
    Tỷ lệ an toàn vốn > Điều 6. …` and a bank policy's `Phần 2. Quản lý vốn > Mục 3. …` state one
    rule under the same title and produced different keys. The channel then fired only between
    documents of the same structural shape — two versions of one instrument, the case that
    least needs it (ADR-0037, *Corrections*).

    Ancestors are context, and context is what makes a key too specific to join on.

    **A boilerplate heading yields None, and does not fall back to its parent.** Every
    instrument has a "Hiệu lực thi hành"; keying on it linked the closing article of one
    document to the closing article of every other. Falling back to the ancestor would be
    worse than nothing — it would file the effectivity article of a capital circular under
    capital adequacy, which is a confident wrong answer rather than an absent one.

    Returns None when nothing survives — a path of pure structure ("Điều 6") says what the
    clause *is* but not what it is about, and a key that says nothing would match everything.
    """
    for part in reversed(section_path):
        key = _title_tokens(part)
        if not key:
            continue
        return None if key in _BOILERPLATE_KEYS else key
    return None


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
