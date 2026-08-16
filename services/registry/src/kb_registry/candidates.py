"""Gate 1: which other clauses might be about the same rule (ADR-0033).

Two channels, fused rather than chosen between, because they fail differently.

**Not cosine over the whole chunk alone.** A chunk embedding mixes the subject with the rule,
so two clauses about lending rates that state *different* rates are maximally similar — which
is exactly what this gate wants — but so are two unrelated clauses sharing applicability
boilerplate, which is exactly what it does not. The **subject key** is the correction: the
heading chain with instrument boilerplate stripped, so it carries what a clause is *about* and
not how it is worded.

**Not the subject key alone either.** It misses a subject phrased in different words, which is
the common case across departments and decades — and that case is precisely a supersession
nobody has noticed. The vector channel misses nothing and admits boilerplate; the lexical
channel admits nothing and misses rewordings. Taking the union of both is the same instinct as
the retrieval funnel one level down, where keyword and vector are fused rather than ranked
against each other.

Gate 2 runs inside the query, because it is free: two clauses whose effectivity windows never
intersected cannot supersede one another, and filtering them in SQL costs nothing over
filtering them in Python after fetching them.

Nothing here decides anything. It produces pairs for gates 3 and 4 to reject cheaply and for
gate 5 to adjudicate expensively, and the number it produces is the one that says whether gate
5 is affordable at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from kb_registry.gates import OVERLAP_SQL

Channel = Literal["subject", "vector", "both"]

#: Candidates pulled from each channel before fusion. Wide enough that a reworded subject can
#: still surface, narrow enough that a corpus-wide scan stays a scan rather than a cross join.
PER_CHANNEL = 25
#: Shared subject-key tokens required on the corpus-wide path. One token in common is "lãi" and
#: means nothing; the key is already boilerplate-stripped, so two is a real subject overlap.
MIN_SUBJECT_OVERLAP = 2
#: Cosine distance beyond which two clauses are not plausibly about one rule. Deliberately
#: generous: this gate is allowed to over-produce, because three cheap gates and a model stand
#: behind it, and a candidate never generated is a supersession never found.
MAX_VECTOR_DISTANCE = 0.45
#: Reciprocal-rank fusion's damping constant, as in `kb_retrieval_api.fusion`. Re-stated rather
#: than imported: the registry does not depend on retrieval-api, and this is five lines.
RRF_K = 60


@dataclass(frozen=True, slots=True)
class Candidate:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    section_path: str | None
    anchor: str | None
    subject_key: str | None
    text: str
    effective_from: date | None
    effective_to: date | None
    #: Which channel found it. `both` is the strongest signal available here — the subject
    #: matches in words *and* the clause reads alike — and it is what the fusion rewards.
    channel: Channel
    score: float


_SUBJECT_CHANNEL = """
    SELECT c.id, cardinality(
        ARRAY(
            SELECT unnest(string_to_array(c.subject_key, ' '))
            INTERSECT
            SELECT unnest(string_to_array(:subject_key, ' '))
        )
    ) AS shared
    FROM chunks c
    WHERE c.document_id <> :document_id
      AND NOT c.tombstoned
      AND c.subject_key IS NOT NULL
      AND string_to_array(c.subject_key, ' ') && string_to_array(:subject_key, ' ')
      {scope}
    ORDER BY shared DESC, c.id
    LIMIT :per_channel
"""

_VECTOR_CHANNEL = """
    SELECT c.id, (c.embedding <=> CAST(:embedding AS vector)) AS distance
    FROM chunks c
    WHERE c.document_id <> :document_id
      AND NOT c.tombstoned
      AND c.embedding IS NOT NULL
      {scope}
    ORDER BY distance
    LIMIT :per_channel
"""

_HYDRATE = """
    SELECT c.id, c.document_id, d.title, c.section_path, c.anchor, c.subject_key, c.text,
           c.effective_from, c.effective_to
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE c.id = ANY(CAST(:ids AS uuid[]))
"""


def find_candidates(
    session: Session,
    chunk_id: uuid.UUID,
    *,
    within_document: uuid.UUID | None = None,
    per_channel: int = PER_CHANNEL,
    min_subject_overlap: int = MIN_SUBJECT_OVERLAP,
    max_distance: float = MAX_VECTOR_DISTANCE,
) -> list[Candidate]:
    """Clauses elsewhere that might state the same rule as this one.

    `within_document` is ADR-0033's **Path B**: an edge exists between two documents but names
    no articles, so the search space is two documents rather than the corpus and the prior that
    a matching pair is real is far higher. On that path the subject gate is relaxed to any
    overlap, because the documents are already known to be related and the expensive part —
    finding the pair at all — has been done by whoever wrote the edge.

    Without it this is **Path C**: no edge, the corpus, and the gate that has to be strict.

    Never returns a clause from the source's own document. A clause superseding another in the
    same instrument is a renumbering, and that is the merge flow's question (ADR-0039).
    """
    source = (
        session.execute(
            text(
                "SELECT document_id, subject_key, embedding, effective_from, effective_to "
                "FROM chunks WHERE id = :id"
            ),
            {"id": chunk_id},
        )
        .mappings()
        .one_or_none()
    )
    if source is None:
        return []

    scope = "AND c.document_id = :within" if within_document else ""
    params: dict[str, object] = {
        "document_id": source["document_id"],
        "per_channel": per_channel,
    }
    if within_document:
        params["within"] = str(within_document)

    ranked: dict[uuid.UUID, dict[str, int]] = {}

    if source["subject_key"]:
        rows = session.execute(
            text(_SUBJECT_CHANNEL.format(scope=scope)),
            {**params, "subject_key": source["subject_key"]},
        ).all()
        threshold = 1 if within_document else min_subject_overlap
        for rank, row in enumerate([row for row in rows if int(row.shared) >= threshold], start=1):
            ranked.setdefault(row.id, {})["subject"] = rank

    if source["embedding"] is not None:
        rows = session.execute(
            text(_VECTOR_CHANNEL.format(scope=scope)),
            {**params, "embedding": str(source["embedding"])},
        ).all()
        for rank, row in enumerate(
            [row for row in rows if float(row.distance) <= max_distance], start=1
        ):
            ranked.setdefault(row.id, {})["vector"] = rank

    if not ranked:
        return []

    hydrated = {
        row.id: row
        for row in session.execute(text(_HYDRATE), {"ids": [str(item) for item in ranked]}).all()
    }

    source_window = (source["effective_from"], source["effective_to"])
    found: list[Candidate] = []
    for candidate_id, ranks in ranked.items():
        found_row = hydrated.get(candidate_id)
        if found_row is None:  # pragma: no cover - deleted between the two queries
            continue
        row = found_row
        if not _windows_overlap(source_window, (row.effective_from, row.effective_to)):
            # Gate 2, applied here rather than in each channel's query: a clause that was never
            # in force at the same time cannot have replaced this one.
            continue
        channel: Channel = (
            "both" if len(ranks) == 2 else ("subject" if "subject" in ranks else "vector")
        )
        found.append(
            Candidate(
                chunk_id=row.id,
                document_id=row.document_id,
                document_title=str(row.title),
                section_path=row.section_path,
                anchor=row.anchor,
                subject_key=row.subject_key,
                text=str(row.text),
                effective_from=row.effective_from,
                effective_to=row.effective_to,
                channel=channel,
                # RRF: a clause both channels found outranks one either found alone, which is
                # the property worth having — the subject matches in words *and* the clause
                # reads alike.
                score=round(sum(1.0 / (RRF_K + rank) for rank in ranks.values()), 6),
            )
        )
    return sorted(found, key=lambda item: (-item.score, str(item.chunk_id)))


def _windows_overlap(
    left: tuple[date | None, date | None], right: tuple[date | None, date | None]
) -> bool:
    from kb_registry.gates import Window, windows_overlap

    return windows_overlap(Window(*left), Window(*right))


__all__ = [
    "MAX_VECTOR_DISTANCE",
    "MIN_SUBJECT_OVERLAP",
    "OVERLAP_SQL",
    "PER_CHANNEL",
    "Candidate",
    "Channel",
    "find_candidates",
]
