"""Resolving a clause anchor to the chunks it names, registry-side.

The read path resolves anchors under the caller's ACL (`RetrievalEngine._resolve`). This is the
same resolution without a caller: the expiry projection has to find the clauses a partial expiry
ends, and confirmation has to find the section paths a supersession row is anchored on, and
neither is answering a question on anyone's behalf — they are writing a decision that applies to
everybody.

The *matching* is shared with the read path through `anchor_families`/`anchor_matches`, and that
sharing is the point: a clause a reference resolves to and a clause an expiry ends must be the
same clause, or a reader follows a citation into text the platform has quietly withdrawn
(ADR-0036).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from kb_vntext.sections import anchor_families, anchor_matches
from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ResolvedClause:
    chunk_id: uuid.UUID
    #: What a supersession row is anchored on. The one address present on every chunk — the
    #: dotted anchor is NULL for front matter, an appendix or a rate-schedule item (ADR-0033).
    section_path: str
    #: The anchor as it was *asked for*, not as it matched. A reference to khoản 2 answered by
    #: the merged Điều 12 chunk is still a reference to khoản 2.
    anchor: str


def resolve_clauses(
    session: Session, document_id: uuid.UUID, anchors: Sequence[str]
) -> list[ResolvedClause]:
    """The serving chunks a set of anchors names, in document order.

    An anchor matches itself and anything beneath it, so `"12"` reaches every clause of Điều 12
    and never `"12a"` — a different article, which Vietnamese amendments insert routinely.

    A clause anchor that finds nothing falls back **only to a chunk anchored at the article
    exactly**: the signature of two short clauses the chunker merged into one. Never to the
    article's other clauses, because khoản 1 is not an answer to a question about khoản 2, and
    that fallback would silently widen an expiry from one clause to its neighbours.

    Returns empty when nothing matches. That is a steward's problem to look at, never a licence
    to widen the scope to the whole document (ADR-0036).
    """
    wanted = [anchor.strip() for anchor in anchors if anchor and anchor.strip()]
    families = anchor_families(wanted)
    if not families:
        return []

    rows = session.execute(
        text(
            """
            SELECT id, anchor, section_path
            FROM chunks
            WHERE document_id = :doc AND NOT tombstoned AND anchor IS NOT NULL
            ORDER BY ordinal
            """
        ),
        {"doc": document_id},
    ).all()

    under: dict[str, list[tuple[uuid.UUID, str]]] = {}
    exact: dict[str, list[tuple[uuid.UUID, str]]] = {}
    for row in rows:
        entry = (row.id, str(row.section_path or ""))
        for family in families:
            if anchor_matches(row.anchor, family):
                under.setdefault(family, []).append(entry)
        exact.setdefault(str(row.anchor), []).append(entry)

    found: list[ResolvedClause] = []
    seen: set[uuid.UUID] = set()
    for anchor in wanted:
        for chunk_id, section_path in under.get(anchor) or exact.get(anchor.split(".")[0]) or []:
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            found.append(
                ResolvedClause(chunk_id=chunk_id, section_path=section_path, anchor=anchor)
            )
    return found


__all__ = ["ResolvedClause", "resolve_clauses"]
