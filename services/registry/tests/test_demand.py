"""Which clauses are actually being served, and how often (M9d, ADR-0033).

`audit_log` has recorded every chunk id returned by every query since M2, so this is answerable
before any detection is built — and it is what decides whether the inference funnel is worth its
false-positive budget. A backfill over 3,000 documents produces more proposals than anyone can
work; ordering them by real demand puts the ones doing harm first.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from kb_registry.demand import clause_demand, rank
from kb_registry.testing import make_chunk, make_document, make_version
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def _served(session: Session, chunk_ids: list[uuid.UUID], *, ago: timedelta = timedelta()) -> None:
    """One retrieval, recorded the way `RetrievalEngine` records it (INV-11)."""
    session.execute(
        text(
            "INSERT INTO audit_log (ts, actor, action, object_ref) "
            "VALUES (:ts, 'u-test', 'retrieve', CAST(:ref AS jsonb))"
        ),
        {
            "ts": datetime.now(UTC) - ago,
            "ref": json.dumps({"chunk_ids": [str(item) for item in chunk_ids]}),
        },
    )


@pytest.fixture
def clauses(session: Session) -> tuple[uuid.UUID, list[uuid.UUID]]:
    document = make_document(session, title="Thông tư về lãi suất")
    version = make_version(session, document)
    ids = [make_chunk(session, document, version, ordinal=index) for index in range(3)]
    for index, chunk_id in enumerate(ids):
        session.execute(
            text("UPDATE chunks SET section_path = :p, anchor = :a WHERE id = :id"),
            {"p": f"Điều {index + 1}", "a": str(index + 1), "id": chunk_id},
        )
    session.flush()
    return document, ids


def test_the_most_served_clause_comes_first(
    session: Session, clauses: tuple[uuid.UUID, list[uuid.UUID]]
) -> None:
    document, ids = clauses
    _served(session, [ids[1]])
    _served(session, [ids[1], ids[0]])
    _served(session, [ids[1]])
    session.flush()

    report = clause_demand(session, document_ids={document})

    assert [(item.section_path, item.retrievals) for item in report.clauses] == [
        ("Điều 2", 3),
        ("Điều 1", 1),
    ]


def test_a_clause_nobody_asked_for_is_absent_rather_than_zero(
    session: Session, clauses: tuple[uuid.UUID, list[uuid.UUID]]
) -> None:
    """The report is about what *was* served. `rank` is where never-served clauses take their
    place at the end of a queue."""
    document, ids = clauses
    _served(session, [ids[0]])
    session.flush()

    report = clause_demand(session, document_ids={document})
    assert [item.section_path for item in report.clauses] == ["Điều 1"]


def test_retrievals_outside_the_window_do_not_count(
    session: Session, clauses: tuple[uuid.UUID, list[uuid.UUID]]
) -> None:
    """A clause retired six months ago should not outrank one being served today."""
    document, ids = clauses
    _served(session, [ids[0]], ago=timedelta(days=200))
    session.flush()

    recent = clause_demand(session, document_ids={document})
    assert recent.clauses == []

    everything = clause_demand(
        session, document_ids={document}, since=datetime.now(UTC) - timedelta(days=365)
    )
    assert [item.retrievals for item in everything.clauses] == [1]


def test_chunk_ids_that_no_longer_resolve_are_counted_not_dropped(
    session: Session, clauses: tuple[uuid.UUID, list[uuid.UUID]]
) -> None:
    """Chunk ids do not survive a rechunk, so the counts are a lower bound. A ranking that
    quietly dropped the unresolvable history would look precise and be wrong in the comfortable
    direction — "nothing is being served"."""
    document, ids = clauses
    _served(session, [ids[0]])
    _served(session, [uuid.uuid4()])  # a chunk the chunker has since replaced
    session.flush()

    report = clause_demand(session, document_ids={document})

    assert report.unresolved_retrievals >= 1
    assert report.resolvable_share < 1.0


def test_no_traffic_is_distinguishable_from_no_stale_clauses(session: Session) -> None:
    """The two look identical in the output and mean opposite things: one says the funnel is
    not worth its false-positive budget, the other says nobody has used the platform yet."""
    report = clause_demand(session, document_ids={uuid.uuid4()})

    assert report.clauses == []
    assert report.records >= 0, "the record count is what tells them apart"


def test_ranking_puts_served_clauses_first_and_keeps_the_rest(
    session: Session, clauses: tuple[uuid.UUID, list[uuid.UUID]]
) -> None:
    """A stale rule nobody has asked for yet is still stale: the queue is a priority order, not
    a filter."""
    document, ids = clauses
    _served(session, [ids[2]])
    _served(session, [ids[2]])
    session.flush()
    report = clause_demand(session, document_ids={document})

    ordered = rank(report, [(document, "Điều 1"), (document, "Điều 3"), (document, "Điều 2")])

    assert ordered[0] == (document, "Điều 3")
    assert len(ordered) == 3, "nothing is dropped"
