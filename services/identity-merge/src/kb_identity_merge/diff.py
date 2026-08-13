"""Section diff: what changed between two versions of an instrument.

Aligned by **structural path**, not by position. A consolidation that inserts a new Điều 12a
shifts every following article down the page; a positional diff reports the whole second half
of the document as changed, and the reviewer stops reading. Aligning on "Điều 13" being
"Điều 13" in both documents reports one insertion.

Three consumers, one diff:

* the **merge screen**, which shows a reviewer what the amendment actually does;
* the **LLM merge draft**, which is given the changed sections rather than both whole
  documents — cheaper, and far less likely to rewrite text nobody asked it to touch;
* the **impact traversal**, which needs to know *which articles* moved, because a policy
  implementing Điều 12 does not care about a change to Điều 40.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from enum import StrEnum

from kb_schemas.kbdoc import Block, KBDoc

#: Below this similarity, an article is treated as rewritten rather than edited — a reviewer
#: reading a word-level diff of two unrelated texts learns nothing.
REWRITE_THRESHOLD = 0.35
_WORD = re.compile(r"\S+")


class ChangeKind(StrEnum):
    UNCHANGED = "unchanged"
    #: Text edited in place. The diff shows what moved.
    AMENDED = "amended"
    #: Present only in the new version.
    ADDED = "added"
    #: Present only in the old version. In a consolidation this is an abrogation.
    REMOVED = "removed"
    #: Same location, entirely different text.
    REWRITTEN = "rewritten"


@dataclass(frozen=True, slots=True)
class WordSpan:
    """One run of the word-level diff, for rendering the middle pane."""

    op: str  # equal | insert | delete
    text: str


@dataclass(frozen=True, slots=True)
class SectionChange:
    """One structural location, and what happened to it."""

    section_path: str
    kind: ChangeKind
    old_text: str = ""
    new_text: str = ""
    similarity: float = 0.0
    spans: tuple[WordSpan, ...] = ()
    #: The article number, when the path names one. What the impact traversal filters on.
    article: int | None = None

    @property
    def is_change(self) -> bool:
        return self.kind is not ChangeKind.UNCHANGED

    def as_dict(self) -> dict[str, object]:
        return {
            "section_path": self.section_path,
            "kind": self.kind.value,
            "similarity": round(self.similarity, 3),
            "article": self.article,
            "old_text": self.old_text,
            "new_text": self.new_text,
        }


@dataclass
class DocumentDiff:
    changes: list[SectionChange] = field(default_factory=list)

    @property
    def changed(self) -> list[SectionChange]:
        return [change for change in self.changes if change.is_change]

    @property
    def touched_articles(self) -> list[int]:
        """Article numbers the amendment actually touches (INV-10's `articles` column)."""
        return sorted({c.article for c in self.changed if c.article is not None})

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for change in self.changes:
            counts[change.kind.value] = counts.get(change.kind.value, 0) + 1
        return counts

    def as_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary(),
            "touched_articles": self.touched_articles,
            "changes": [change.as_dict() for change in self.changed],
        }


def diff_documents(old: KBDoc, new: KBDoc) -> DocumentDiff:
    """Compare two parsed documents section by section."""
    old_sections = _sections(old)
    new_sections = _sections(new)

    changes: list[SectionChange] = []
    # Ordered by the *new* document, then anything only the old one had — which is how a
    # reviewer reads it: down the new text, with removals shown where they used to be.
    for path, new_text in new_sections.items():
        old_text = old_sections.get(path)
        if old_text is None:
            changes.append(
                SectionChange(
                    section_path=path,
                    kind=ChangeKind.ADDED,
                    new_text=new_text,
                    article=_article_number(path),
                )
            )
            continue
        changes.append(_compare(path, old_text, new_text))

    for path, old_text in old_sections.items():
        if path not in new_sections:
            changes.append(
                SectionChange(
                    section_path=path,
                    kind=ChangeKind.REMOVED,
                    old_text=old_text,
                    article=_article_number(path),
                )
            )

    return DocumentDiff(changes=changes)


def _compare(path: str, old_text: str, new_text: str) -> SectionChange:
    if _normalize(old_text) == _normalize(new_text):
        return SectionChange(
            section_path=path,
            kind=ChangeKind.UNCHANGED,
            old_text=old_text,
            new_text=new_text,
            similarity=1.0,
            article=_article_number(path),
        )

    similarity = _body_similarity(old_text, new_text)
    kind = ChangeKind.AMENDED if similarity >= REWRITE_THRESHOLD else ChangeKind.REWRITTEN
    return SectionChange(
        section_path=path,
        kind=kind,
        old_text=old_text,
        new_text=new_text,
        similarity=similarity,
        spans=word_diff(old_text, new_text) if kind is ChangeKind.AMENDED else (),
        article=_article_number(path),
    )


def _body_similarity(old_text: str, new_text: str) -> float:
    """How similar two versions of a section are, ignoring a heading they share.

    A section's text begins with its heading ("Điều 6. Tỷ lệ an toàn vốn"), which an amendment
    almost always keeps. Counting it makes two completely rewritten articles look half the
    same, and the rewrite would be shown as a word-level edit — which is unreadable.
    """
    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    if old_lines and new_lines and old_lines[0] == new_lines[0]:
        old_lines, new_lines = old_lines[1:], new_lines[1:]
    old_body = "\n".join(old_lines)
    new_body = "\n".join(new_lines)
    if not old_body and not new_body:
        return 1.0
    return difflib.SequenceMatcher(None, old_body, new_body).ratio()


def word_diff(old_text: str, new_text: str) -> tuple[WordSpan, ...]:
    """Word-level diff.

    Words rather than characters: Vietnamese is written in syllables, and a character diff of
    "tối thiểu 8%" → "tối thiểu 10%" highlights fragments of syllables, which is unreadable.
    """
    old_words = _WORD.findall(old_text)
    new_words = _WORD.findall(new_text)
    matcher = difflib.SequenceMatcher(None, old_words, new_words)

    spans: list[WordSpan] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            spans.append(WordSpan(op="equal", text=" ".join(old_words[i1:i2])))
        elif op == "delete":
            spans.append(WordSpan(op="delete", text=" ".join(old_words[i1:i2])))
        elif op == "insert":
            spans.append(WordSpan(op="insert", text=" ".join(new_words[j1:j2])))
        else:  # replace
            spans.append(WordSpan(op="delete", text=" ".join(old_words[i1:i2])))
            spans.append(WordSpan(op="insert", text=" ".join(new_words[j1:j2])))
    return tuple(spans)


def _sections(kbdoc: KBDoc) -> dict[str, str]:
    """Collapse a document to `{section path: text}`.

    Blocks under one article are joined: the unit a reviewer decides about is the article, not
    the paragraph, and an amendment that re-flows paragraphs within an article has not changed
    the article.
    """
    sections: dict[str, list[str]] = {}
    for block in kbdoc.blocks:
        if not block.text.strip():
            continue
        path = " > ".join(block.section_path) if block.section_path else _FRONT_MATTER
        sections.setdefault(path, []).append(_block_text(block))
    return {path: "\n".join(parts) for path, parts in sections.items()}


_FRONT_MATTER = "(front matter)"


def _block_text(block: Block) -> str:
    if block.table is not None:
        return "\n".join(" | ".join(row) for row in block.table.rows)
    return block.text


def _normalize(text: str) -> str:
    """Whitespace-insensitive comparison. Re-flowed text is not amended text."""
    return " ".join(text.split())


def _article_number(path: str) -> int | None:
    match = re.search(r"(?:Điều|Article)\s+(\d+)", path)
    return int(match.group(1)) if match else None
