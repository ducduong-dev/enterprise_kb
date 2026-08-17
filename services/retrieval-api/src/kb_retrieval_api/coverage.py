"""Fact sets: the other passages that state the same rule as a seed (M10, ADR-0037).

Ranking answers "which passage best matches this question". INV-13 asks a different question —
"which documents state this rule" — and no amount of depth in a likeness ranking answers it,
because the passage worth adding ranks low *precisely* because it words the rule differently.
So after rerank, the top passages become seeds and this module finds their company.

Three channels, in descending order of confidence, and the order is the point: a member found
by two channels keeps the stronger one, so a steward reading a source list can tell "the bank
said these are the same rule" from "they look alike".

**1 · reference** — the corpus said so. An equality join through ADR-0036's anchors, with
nothing to tune. This is the only channel whose membership the bank itself asserted.

**2 · subject** — the normalized subject key M9b writes onto every chunk: the heading chain with
instrument boilerplate stripped. Exact and indexed. It reaches a clause about the same subject
in a document that never cites the seed, which reference cannot.

**3 · vector** — cosine against the *seed's own* embedding rather than the question's. The only
channel that reaches a rule phrased in words the question never used, which is the case the
whole ADR exists for, and the only one with a threshold.

Two rules hold across all three and neither is negotiable:

**Every channel is a filtered query.** Each compiles `acl` into its own `WHERE` through
`compile_sql`, exactly as the seed's own retrieval did (INV-1/2). A fact set is never a reason
to widen, and this is precisely where somebody writes "we already checked the seed, just fetch
its neighbours".

**One member per document.** A fact set is about documents, not passages: the second clause of a
document already present adds nothing to "who else says this" and spends budget the fourth
document's first clause needed. `cap_per_document` exists one level up for the same reason.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from kb_authz.compile import compile_sql
from kb_authz.filters import ResolvedFilter
from kb_common.logging import get_logger
from kb_vntext.sections import anchor_families
from sqlalchemy import text
from sqlalchemy.orm import Session

log = get_logger(__name__)

Channel = Literal["reference", "subject", "vector"]

#: Strongest first. A member found twice keeps the stronger channel, so "the corpus says these
#: are the same rule" is never overwritten by "they look alike".
CHANNEL_RANK: Final[dict[Channel, int]] = {"reference": 0, "subject": 1, "vector": 2}

#: Cosine distance beyond which two passages are not plausibly the same rule. Tighter than the
#: detector's 0.45 (`kb_registry.candidates`) on purpose: that gate over-produces because four
#: more gates and a model stand behind it, and this one puts passages straight into an answer.
MAX_VECTOR_DISTANCE: Final[float] = 0.30

#: Members per seed, per channel, before the per-document cap. Bounds one pathological seed —
#: a boilerplate clause every instrument repeats — from filling the set on its own.
PER_CHANNEL: Final[int] = 12

#: `[OPEN]`-12: how many documents an answer may cite before nobody reads it is a product call.
#: Until it is made this is the number that keeps "every document that states the fact" from
#: meaning "all 3,000", and the response says when it truncated.
MAX_MEMBERS: Final[int] = 8


@dataclass(frozen=True, slots=True)
class Seed:
    """A ranked passage, and everything the three channels need to find its company."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    anchor: str | None = None
    subject_key: str | None = None


@dataclass(frozen=True, slots=True)
class Member:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    document_title: str | None
    citation_label: str | None
    section_path: str | None
    text: str
    channel: Channel
    #: Cosine distance for the vector channel, None for the exact ones. Never a "confidence":
    #: an equality join has no score and inventing one would make the two look comparable.
    distance: float | None = None


@dataclass(frozen=True, slots=True)
class Coverage:
    """One seed's fact set, plus what it could not show."""

    seed: Seed
    members: list[Member]
    #: Members the filter removed. Counted, never named — existence disclosure is the target
    #: category's decision (ADR-0023) — and carried into the audit record so a reviewer can
    #: tell "we did not have it" from "they could not see it" (INV-11).
    withheld: int = 0
    #: Members the cap removed. Spoken to the reader, unlike `withheld`, because a truncated
    #: answer that does not say so is an answer claiming completeness it does not have.
    truncated: int = 0


_REFERENCE = """
    WITH edges AS (
        -- Outbound: this seed's document cites a clause elsewhere, and named which one. The
        -- anchor list is what M9b made clause-precise, so this resolves into the target's
        -- individual clauses rather than its whole text.
        SELECT r.dst_document_id AS other_document_id, unnest(r.anchors) AS anchor
        FROM document_refs r
        WHERE r.src_document_id = CAST(:document_id AS uuid)
          AND r.anchors IS NOT NULL
          AND r.confirmed_by IS NOT NULL
    )
    SELECT c.id, c.document_id, c.version_id, c.citation_label, c.section_path, c.text,
           d.title
    FROM edges e
    JOIN chunks c ON c.document_id = e.other_document_id
    JOIN documents d ON d.id = c.document_id
    WHERE c.anchor IS NOT NULL
      AND (c.anchor = e.anchor OR c.anchor LIKE e.anchor || '.%')
      AND c.id <> CAST(:chunk_id AS uuid)
      AND {where}
    ORDER BY c.document_id, c.ordinal
    LIMIT :limit
"""

#: Inbound is document-granular and narrowed by subject, because `document_refs` records which
#: document cited the seed's clause but not which *clause* of it did. See `_inbound` for why
#: that is left honest rather than guessed at.
_INBOUND = """
    WITH citing AS (
        SELECT r.src_document_id AS other_document_id
        FROM document_refs r
        CROSS JOIN unnest(CAST(:families AS TEXT[])) AS f(family)
        WHERE r.dst_document_id = CAST(:document_id AS uuid)
          AND r.anchors IS NOT NULL
          AND r.confirmed_by IS NOT NULL
          AND EXISTS (
                SELECT 1 FROM unnest(r.anchors) AS a(anchor)
                WHERE a.anchor = f.family OR a.anchor LIKE f.family || '.%'
          )
    )
    SELECT c.id, c.document_id, c.version_id, c.citation_label, c.section_path, c.text,
           d.title
    FROM citing e
    JOIN chunks c ON c.document_id = e.other_document_id
    JOIN documents d ON d.id = c.document_id
    WHERE c.subject_key IS NOT NULL
      AND c.subject_key = :subject_key
      AND c.id <> CAST(:chunk_id AS uuid)
      AND {where}
    ORDER BY c.document_id, c.ordinal
    LIMIT :limit
"""

_SUBJECT = """
    SELECT c.id, c.document_id, c.version_id, c.citation_label, c.section_path, c.text,
           d.title
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE c.subject_key = :subject_key
      AND c.document_id <> CAST(:document_id AS uuid)
      AND {where}
    ORDER BY c.document_id, c.ordinal
    LIMIT :limit
"""

_VECTOR = """
    SELECT c.id, c.document_id, c.version_id, c.citation_label, c.section_path, c.text,
           d.title, (c.embedding <=> CAST(:embedding AS vector)) AS distance
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE c.embedding IS NOT NULL
      AND c.document_id <> CAST(:document_id AS uuid)
      AND {where}
    ORDER BY distance
    LIMIT :limit
"""


def cover(
    session: Session,
    acl: ResolvedFilter,
    seed: Seed,
    *,
    max_members: int = MAX_MEMBERS,
    per_channel: int = PER_CHANNEL,
    max_distance: float = MAX_VECTOR_DISTANCE,
) -> Coverage:
    """Every document that states this seed's rule, up to `max_members`.

    Channels run strongest-first and their results are merged rather than concatenated: a
    document reached by both reference and vector is one member, recorded as `reference`.
    """
    found: dict[uuid.UUID, Member] = {}
    for member in (
        *_reference(session, acl, seed, per_channel),
        *_inbound(session, acl, seed, per_channel),
        *_subject(session, acl, seed, per_channel),
        *_vector(session, acl, seed, per_channel, max_distance),
    ):
        # One member per document, keeping the strongest channel that reached it.
        existing = found.get(member.document_id)
        if existing is None or CHANNEL_RANK[member.channel] < CHANNEL_RANK[existing.channel]:
            found[member.document_id] = member

    ordered = sorted(
        found.values(),
        key=lambda m: (CHANNEL_RANK[m.channel], m.distance if m.distance is not None else 0.0),
    )
    kept = ordered[:max_members]
    return Coverage(seed=seed, members=kept, truncated=len(ordered) - len(kept))


def load_seed(session: Session, chunk_id: uuid.UUID) -> Seed | None:
    """The seed's own anchor and subject key, which no index hit carries.

    Read without the ACL predicate on purpose: this chunk already came back through the funnel,
    so the filter admitted it. Re-applying it here would be a second decision about a passage
    the caller demonstrably holds, and a divergence between the two would be silent.
    """
    row = (
        session.execute(
            text("SELECT document_id, anchor, subject_key FROM chunks WHERE id = :id"),
            {"id": str(chunk_id)},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return Seed(
        chunk_id=chunk_id,
        document_id=row["document_id"],
        anchor=row["anchor"],
        subject_key=row["subject_key"],
    )


def _reference(session: Session, acl: ResolvedFilter, seed: Seed, limit: int) -> list[Member]:
    """Clauses this seed's document cites by name.

    Confirmed edges only. An unconfirmed reference is a detection nobody has checked, and
    putting its target into an answer as "the bank says these are the same rule" would claim
    the bank's authority for our own parser (ADR-0028).
    """
    where, params = compile_sql(acl)
    params.update(
        {"document_id": str(seed.document_id), "chunk_id": str(seed.chunk_id), "limit": limit}
    )
    return _members(session, _REFERENCE.format(where=where), params, "reference")


def _inbound(session: Session, acl: ResolvedFilter, seed: Seed, limit: int) -> list[Member]:
    """Clauses elsewhere that cite *this* seed by name.

    Narrowed by the seed's subject key, and that is a compromise rather than a design.
    `document_refs` records which document cited the seed's clause but not which clause of it
    did the citing — the anchors are clause-precise at the *target* end only (ADR-0036). So the
    citing document's own passage cannot be identified from the edge, and the honest options
    were to pick its best-matching chunk by subject or to return nothing.

    Guessing the citing clause from the reference's position in the text was the third option
    and is worse than both: it would put a passage into an answer on a heuristic, wearing the
    "the corpus said so" label that is this channel's entire value. Making this symmetric needs
    a chunk-level source on `document_refs`, which is a migration and not a query.
    """
    if not seed.anchor or not seed.subject_key:
        return []
    where, params = compile_sql(acl)
    params.update(
        {
            "document_id": str(seed.document_id),
            "chunk_id": str(seed.chunk_id),
            "families": anchor_families([seed.anchor]),
            "subject_key": seed.subject_key,
            "limit": limit,
        }
    )
    return _members(session, _INBOUND.format(where=where), params, "reference")


def _subject(session: Session, acl: ResolvedFilter, seed: Seed, limit: int) -> list[Member]:
    """Clauses elsewhere about the same subject, by exact key.

    Exact equality and not a similarity: the key is already normalized and boilerplate-stripped
    by the chunker, so two clauses sharing it are about the same thing by construction. A fuzzy
    match here would reintroduce the threshold this channel exists to avoid.
    """
    if not seed.subject_key:
        return []
    where, params = compile_sql(acl)
    params.update(
        {"document_id": str(seed.document_id), "subject_key": seed.subject_key, "limit": limit}
    )
    return _members(session, _SUBJECT.format(where=where), params, "subject")


def _vector(
    session: Session, acl: ResolvedFilter, seed: Seed, limit: int, max_distance: float
) -> list[Member]:
    """Passages that read like the seed, by the seed's own vector.

    Seeded by the *passage* and never by the question — that difference is the whole channel.
    A question-seeded round returns more of what ranking already returned; a passage-seeded one
    reaches the clause that states the same rule in words the asker never used.
    """
    embedding = session.execute(
        text("SELECT embedding FROM chunks WHERE id = :id"), {"id": str(seed.chunk_id)}
    ).scalar()
    if embedding is None:
        return []
    where, params = compile_sql(acl)
    params.update(
        {"document_id": str(seed.document_id), "embedding": str(embedding), "limit": limit}
    )
    rows = session.execute(text(_VECTOR.format(where=where)), params).mappings().all()
    return [
        _member(row, "vector", distance=float(row["distance"]))
        for row in rows
        if float(row["distance"]) <= max_distance
    ]


def _members(session: Session, sql: str, params: dict[str, Any], channel: Channel) -> list[Member]:
    return [_member(row, channel) for row in session.execute(text(sql), params).mappings().all()]


def _member(row: Any, channel: Channel, *, distance: float | None = None) -> Member:
    return Member(
        chunk_id=row["id"],
        document_id=row["document_id"],
        version_id=row["version_id"],
        document_title=row["title"],
        citation_label=row["citation_label"],
        section_path=row["section_path"],
        text=row["text"] or "",
        channel=channel,
        distance=distance,
    )


def withheld_count(
    session: Session, seed: Seed, visible: Sequence[Member], *, per_channel: int = PER_CHANNEL
) -> int:
    """How many documents the *filter* removed from this set, for the audit record only.

    Runs the subject channel with no ACL predicate and subtracts what the filtered run returned.
    Deliberately the exact channel rather than all three: it is the one whose unfiltered result
    is a fact about the corpus rather than about a threshold, so the number means "documents
    stating this subject that this caller may not read" and not "documents that scored above
    0.30 for somebody".

    Never returned to a caller and never spoken in an answer. Its only reader is a reviewer
    asking why an answer missed something, which is the question INV-11 exists for; disclosing
    it would let a count stand in for the documents the filter hid (ADR-0023).
    """
    if not seed.subject_key:
        return 0
    total = session.execute(
        text(
            "SELECT count(DISTINCT document_id) FROM chunks "
            "WHERE subject_key = :subject_key AND document_id <> CAST(:document_id AS uuid) "
            "AND NOT tombstoned"
        ),
        {"subject_key": seed.subject_key, "document_id": str(seed.document_id)},
    ).scalar()
    seen = {member.document_id for member in visible if member.channel in ("reference", "subject")}
    return max(0, int(total or 0) - len(seen))


__all__ = [
    "CHANNEL_RANK",
    "MAX_MEMBERS",
    "MAX_VECTOR_DISTANCE",
    "PER_CHANNEL",
    "Channel",
    "Coverage",
    "Member",
    "Seed",
    "cover",
    "load_seed",
    "withheld_count",
]
