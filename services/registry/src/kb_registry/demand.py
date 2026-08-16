"""Which clauses are actually being served, and how often.

`audit_log.object_ref` has recorded every chunk id returned by every query since M2 (INV-11), so
"which stale clauses are we serving, and how often" is answerable *before* any detection is
built — and it is the honest way to size the problem (ADR-0033).

Two jobs:

* **Ranking the queue.** A detection backfill over 3,000 documents produces more proposals than
  anyone can work. Ordering them by how often the older clause has actually been retrieved puts
  the ones doing real harm first, and a proposal about a clause nobody has ever been served is
  worth exactly what its retrieval count says.
* **Sizing the problem at all.** If the log says almost nothing stale is being retrieved, the
  inference funnel's false-positive budget matters far less than the plan assumes, and that is
  worth knowing before building it rather than after.

**Chunk ids do not survive a rechunk**, which is the same rule that keeps them off edges and
supersession rows. A chunk retrieved before the chunker last changed has an id that resolves to
nothing today, so the counts below are necessarily a lower bound and every result says how much
of the history it could not resolve. A ranking that quietly dropped that would look precise and
be wrong in the direction of "nothing is being served", which is the comfortable direction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

#: How far back to look by default. Long enough to cover a quarter's usage patterns, short
#: enough that a clause retired six months ago does not outrank one being served today.
DEFAULT_WINDOW = timedelta(days=90)


@dataclass(frozen=True, slots=True)
class ClauseDemand:
    document_id: uuid.UUID
    document_title: str
    section_path: str | None
    anchor: str | None
    retrievals: int


@dataclass(frozen=True, slots=True)
class DemandReport:
    since: datetime
    clauses: list[ClauseDemand]
    #: Retrievals whose chunk ids no longer resolve, because the chunker has run since. Reported
    #: rather than dropped: it is the measure of how stale this ranking is, and a large number
    #: means the counts below understate demand rather than that demand is low.
    unresolved_retrievals: int = 0
    #: Retrieval records read, so a report of "nothing is stale" can be told from "nothing has
    #: been retrieved" — which look identical in the output and mean opposite things.
    records: int = 0

    @property
    def resolvable_share(self) -> float:
        total = sum(item.retrievals for item in self.clauses) + self.unresolved_retrievals
        return (total - self.unresolved_retrievals) / total if total else 1.0


_DEMAND = """
    WITH served AS (
        SELECT CAST(chunk_id AS uuid) AS chunk_id
        FROM audit_log,
             LATERAL jsonb_array_elements_text(object_ref -> 'chunk_ids') AS chunk_id
        WHERE action = 'retrieve'
          AND ts >= :since
          AND jsonb_typeof(object_ref -> 'chunk_ids') = 'array'
    )
    SELECT c.document_id, d.title, c.section_path, c.anchor, count(*) AS retrievals
    FROM served s
    JOIN chunks c ON c.id = s.chunk_id
    JOIN documents d ON d.id = c.document_id
    GROUP BY c.document_id, d.title, c.section_path, c.anchor
    ORDER BY retrievals DESC, c.section_path
    LIMIT :limit
"""

_UNRESOLVED = """
    SELECT count(*)
    FROM audit_log,
         LATERAL jsonb_array_elements_text(object_ref -> 'chunk_ids') AS chunk_id
    WHERE action = 'retrieve'
      AND ts >= :since
      AND jsonb_typeof(object_ref -> 'chunk_ids') = 'array'
      AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.id = CAST(chunk_id AS uuid))
"""


def clause_demand(
    session: Session,
    *,
    since: datetime | None = None,
    limit: int = 200,
    document_ids: set[uuid.UUID] | None = None,
) -> DemandReport:
    """How often each clause has been returned to a reader.

    Scoped to a document set when ranking one backfill batch; unscoped when sizing the corpus.

    Counts *retrievals*, not distinct askers or answers: a clause returned ten times to one
    person is being served ten times, and for "is a stale rule reaching readers" that is the
    number that matters.
    """
    window_start = since or datetime.now(UTC) - DEFAULT_WINDOW
    params: dict[str, object] = {"since": window_start, "limit": limit}

    sql = _DEMAND
    if document_ids:
        sql = sql.replace(
            "GROUP BY", "WHERE c.document_id = ANY(CAST(:documents AS uuid[]))\n    GROUP BY"
        )
        params["documents"] = [str(item) for item in document_ids]

    rows = session.execute(text(sql), params).all()
    unresolved = session.execute(text(_UNRESOLVED), {"since": window_start}).scalar() or 0
    records = (
        session.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'retrieve' AND ts >= :since"),
            {"since": window_start},
        ).scalar()
        or 0
    )

    return DemandReport(
        since=window_start,
        clauses=[
            ClauseDemand(
                document_id=row.document_id,
                document_title=str(row.title),
                section_path=row.section_path,
                anchor=row.anchor,
                retrievals=int(row.retrievals),
            )
            for row in rows
        ],
        unresolved_retrievals=int(unresolved),
        records=int(records),
    )


def rank(report: DemandReport, pairs: list[tuple[uuid.UUID, str]]) -> list[tuple[uuid.UUID, str]]:
    """Order candidate clauses by how often they have actually been served.

    Clauses nobody has been served keep their relative order at the end rather than being
    dropped: a stale rule that has not been asked for yet is still stale, and the queue is a
    priority order, not a filter.
    """
    counts = {
        (item.document_id, item.section_path or ""): item.retrievals for item in report.clauses
    }
    return sorted(pairs, key=lambda pair: -counts.get((pair[0], pair[1]), 0))


__all__ = ["DEFAULT_WINDOW", "ClauseDemand", "DemandReport", "clause_demand", "rank"]
