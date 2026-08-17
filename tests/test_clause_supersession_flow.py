"""M9d end to end: two schedules, no edge between them, and what a reader is shown.

The acceptance criteria for the inferred path, run against a real database and the real
retrieval funnel. The fixture is the one ADR-0033 opens with — the same rate item stated in a
2023 document and again in a 2026 document, with no reference edge connecting them, which is
the case nothing in the platform used to notice.

What is asserted here, in the order a reader meets it:

* a *proposal* changes nothing at all, which is the property that makes the funnel safe to run
  over 3,000 documents before anybody has checked its output;
* a *confirmation* drops the older clause from the answer, but only because the replacement is
  in the same answer, and points at what replaced it;
* an `as_of` query dated before the replacement took effect still returns the older clause, and
  returns it **unflagged** — a pointer rendered on that query would be as wrong as hiding it;
* the document's own page is untouched by all of the above, because the clause is still in the
  document and a page that omitted it would lie about what the document says.

The last test is the drift guard: `RetrievalEngine._clause_supersessions` is hand-written SQL
because retrieval-api does not depend on the registry, so it is asserted to agree with
`ClauseSupersessions.served` rather than assumed to.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest
from kb_authz.fixtures import ALL_PRINCIPALS, USER_COMPLIANCE_OFFICER, USER_RETAIL_STAFF
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
from kb_ports.adapters.rerank import LexicalRerankAdapter
from kb_registry.supersession import ClauseRef, ClauseSupersessions
from kb_registry.testing import make_chunk, make_document, make_version
from kb_retrieval_api.engine import RetrievalEngine
from kb_schemas.api import RetrieveRequest, RetrieveResponse
from kb_schemas.enums import RetrievalMode, SupersessionBasis
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

TOOK_EFFECT = date(2026, 1, 1)
OLD_RATE = "Biểu phí chuyển tiền trong nước qua kênh quầy là 11.000 đồng mỗi giao dịch."
NEW_RATE = "Biểu phí chuyển tiền trong nước qua kênh quầy là 15.000 đồng mỗi giao dịch."
QUERY = "biểu phí chuyển tiền trong nước qua kênh quầy"

OLD_CLAUSE = "Điều 5"
NEW_CLAUSE = "Điều 7"
#: Shared by both clauses, so gate 1's lexical channel pairs them when the funnel runs.
SUBJECT = "bieu phi chuyen tien trong nuoc"


@pytest.fixture
def schedules(pristine_corpus: Engine, session: Session) -> tuple[ClauseRef, ClauseRef]:
    """The two-schedule fixture: same rate item, two instruments, no edge between them."""
    old_doc = make_document(session, title="Biểu phí dịch vụ 2023", legal_number="01/2023/QĐ-NHNN")
    old_version = make_version(session, old_doc, effective_from=date(2023, 1, 1))
    make_chunk(
        session,
        old_doc,
        old_version,
        effective_from=date(2023, 1, 1),
        section_path=OLD_CLAUSE,
        body=OLD_RATE,
        subject_key=SUBJECT,
    )

    new_doc = make_document(session, title="Biểu phí dịch vụ 2026", legal_number="09/2026/QĐ-NHNN")
    new_version = make_version(session, new_doc, effective_from=TOOK_EFFECT)
    make_chunk(
        session,
        new_doc,
        new_version,
        effective_from=TOOK_EFFECT,
        section_path=NEW_CLAUSE,
        body=NEW_RATE,
        subject_key=SUBJECT,
    )
    session.flush()
    return ClauseRef(old_doc, OLD_CLAUSE), ClauseRef(new_doc, NEW_CLAUSE)


@pytest.fixture
def retrieval(pristine_corpus: Engine, session: Session) -> RetrievalEngine:
    return RetrievalEngine(
        session,
        keyword_index=PostgresFtsIndexAdapter(session),
        vector_index=PgVectorIndexAdapter(session),
        embedder=HashedEmbeddingAdapter(),
        reranker=LexicalRerankAdapter(),
    )


def propose(session: Session, old: ClauseRef, new: ClauseRef) -> uuid.UUID:
    """What the funnel writes: `detected`, `proposed`, nothing visible."""
    return (
        ClauseSupersessions(session)
        .propose(
            old,
            new=new,
            supersedes_from=TOOK_EFFECT,
            basis=SupersessionBasis.DETECTED,
            detected_by="m9d_funnel",
            actor="detector@bank",
            evidence="mức phí đã thay đổi",
        )
        .row_id
    )


def ask(engine: RetrievalEngine, **kwargs: object) -> RetrieveResponse:
    return engine.retrieve(USER_RETAIL_STAFF, RetrieveRequest(query=QUERY, **kwargs)).response


def rates_in(response: RetrieveResponse) -> set[str]:
    return {
        value for value in ("11.000", "15.000") if any(value in c.text for c in response.chunks)
    }


# --------------------------------------------------------------------------- before anybody acts


def test_both_rates_are_served_before_anything_is_detected(
    retrieval: RetrievalEngine, schedules: tuple[ClauseRef, ClauseRef]
) -> None:
    """The failure this milestone exists to fix, demonstrated rather than described: two rates
    reach the reader and nothing says which one applies."""
    assert rates_in(ask(retrieval, top_k=10)) == {"11.000", "15.000"}


def test_a_proposal_changes_nothing(
    retrieval: RetrievalEngine, session: Session, schedules: tuple[ClauseRef, ClauseRef]
) -> None:
    """`proposed` is a row in a steward's queue and nothing else. If this ever fails, the
    backfill stops being safe to run before anybody has read its output."""
    old, new = schedules
    propose(session, old, new)
    session.flush()

    response = ask(retrieval, top_k=10)

    assert rates_in(response) == {"11.000", "15.000"}
    assert all(chunk.superseded_by is None for chunk in response.chunks)


# ------------------------------------------------------------------------- after a person acts


def test_a_confirmed_supersession_drops_the_old_rate_and_names_its_replacement(
    retrieval: RetrievalEngine, session: Session, schedules: tuple[ClauseRef, ClauseRef]
) -> None:
    old, new = schedules
    row_id = propose(session, old, new)
    ClauseSupersessions(session).confirm(row_id, actor="steward@bank")
    session.flush()

    response = ask(retrieval, top_k=10)

    # Only the current rate. Quoting both and leaving the customer to choose is the harm.
    assert rates_in(response) == {"15.000"}
    assert all(chunk.document_id != old.document_id for chunk in response.chunks)


def test_a_clause_is_still_flagged_when_its_replacement_can_no_longer_be_named(
    retrieval: RetrievalEngine, session: Session, schedules: tuple[ClauseRef, ClauseRef]
) -> None:
    """A rechunk retires the replacement's chunk. The stale clause must not quietly un-flag.

    It is left *unnamed* rather than named from `documents`, and that is a deliberate trade
    rather than an oversight: recovering the title would mean hand-writing a second ACL
    predicate over `documents`, which is how expansion ends up leaking what search does not
    (`compile_sql_graph`). One audited predicate that occasionally says less beats two that can
    disagree.

    Note the flag survives here only because the tombstoned replacement also leaves the
    candidate set — `drop_superseded` needs the replacement *present* to remove its
    predecessor, which is the condition that keeps a reader from being left with nothing.
    """
    old, new = schedules
    row_id = propose(session, old, new)
    ClauseSupersessions(session).confirm(row_id, actor="steward@bank")
    session.execute(
        text("UPDATE chunks SET tombstoned = true WHERE document_id = :d"),
        {"d": new.document_id},
    )
    session.flush()

    response = ask(retrieval, top_k=10)

    stale = [c for c in response.chunks if c.document_id == old.document_id]
    assert stale, "the old clause must still be served when nothing replaced it in the answer"
    pointer = stale[0].superseded_by
    assert pointer is not None
    assert pointer.supersedes_from == TOOK_EFFECT
    assert not pointer.names_replacement


def test_a_readable_replacement_is_named_well_enough_to_quote(
    retrieval: RetrievalEngine, session: Session, schedules: tuple[ClauseRef, ClauseRef]
) -> None:
    """The pointer an answer actually speaks, exercised on its own.

    Not reachable through `retrieve` in a corpus this small: a named pointer only surfaces when
    the replacement is *readable but not retrieved*, and with a handful of documents every
    chunk is a candidate, so the older clause is always dropped instead. In production — 3,000
    documents against 50 candidates per retriever — it is the ordinary case, which is why the
    label construction is worth a test of its own rather than being left to a ranking accident.
    """
    old, new = schedules
    acl = retrieval._filters.build(USER_RETAIL_STAFF)

    pointers = retrieval._replacement_labels(
        {(old.document_id, old.section_path): ((new.document_id, new.section_path), TOOK_EFFECT)},
        acl,
    )

    pointer = pointers[(old.document_id, old.section_path)]
    assert pointer.names_replacement
    assert pointer.document_id == new.document_id
    assert pointer.section_path == NEW_CLAUSE
    assert pointer.document_title == "Biểu phí dịch vụ 2026"
    assert pointer.citation_label == NEW_CLAUSE
    assert pointer.supersedes_from == TOOK_EFFECT


def test_a_replacement_the_caller_may_not_read_is_warned_about_but_never_named(
    retrieval: RetrievalEngine, session: Session, schedules: tuple[ClauseRef, ClauseRef]
) -> None:
    """The half of the pointer that is an access decision.

    Naming the replacing instrument would disclose the existence of a document the filter
    excluded — what `compile_sql_graph` refuses to do for an edge, on the same INV-10 reasoning.
    Withholding the *warning* would be the opposite failure, leaving a reader acting on a stale
    figure with no reason to doubt it. So the warning is unconditional and the name is not.
    """
    old, new = schedules
    row_id = propose(session, old, new)
    ClauseSupersessions(session).confirm(row_id, actor="steward@bank")
    session.execute(
        text(
            "UPDATE chunks SET visibility = 'restricted', allowed_groups = ARRAY['grp-board'] "
            "WHERE document_id = :d"
        ),
        {"d": new.document_id},
    )
    session.execute(
        text(
            "UPDATE documents SET visibility = 'restricted', allowed_groups = ARRAY['grp-board'] "
            "WHERE id = :d"
        ),
        {"d": new.document_id},
    )
    session.flush()

    response = retrieval.retrieve(
        USER_RETAIL_STAFF, RetrieveRequest(query="11.000 đồng mỗi giao dịch", top_k=5)
    ).response

    stale = [c for c in response.chunks if c.document_id == old.document_id]
    assert stale, "a restricted replacement must not take the old clause down with it"
    pointer = stale[0].superseded_by
    assert pointer is not None
    # Warned.
    assert pointer.supersedes_from == TOOK_EFFECT
    # But not named, in any field.
    assert not pointer.names_replacement
    assert pointer.document_id is None
    assert pointer.section_path is None
    assert pointer.document_title is None
    assert pointer.citation_label is None


def test_the_dropped_clause_is_named_in_the_audit_record(
    pristine_corpus: Engine, session: Session, schedules: tuple[ClauseRef, ClauseRef]
) -> None:
    """A passage removed from an answer is invisible by design, so "why was this not cited"
    must still be answerable months later (INV-11)."""
    from kb_common.audit import AuditAction, InMemoryAuditSink

    old, new = schedules
    row_id = propose(session, old, new)
    ClauseSupersessions(session).confirm(row_id, actor="steward@bank")
    session.flush()

    audit = InMemoryAuditSink()
    engine = RetrievalEngine(
        session,
        keyword_index=PostgresFtsIndexAdapter(session),
        vector_index=PgVectorIndexAdapter(session),
        embedder=HashedEmbeddingAdapter(),
        reranker=LexicalRerankAdapter(),
        audit=audit,
    )
    ask(engine, top_k=10)

    record = next(r for r in audit.records if r.action is AuditAction.RETRIEVE)
    assert record.object_ref["superseded_dropped"]


# ------------------------------------------------------------------------------ time travel


def test_an_as_of_query_before_the_change_returns_the_old_rate_unflagged(
    retrieval: RetrievalEngine, session: Session, schedules: tuple[ClauseRef, ClauseRef]
) -> None:
    """The property the whole expiry programme exists to establish. On 2024-06-01 the 2023 rate
    *was* the rate — presenting it with a "replaced by" pointer would be as wrong as hiding it,
    because on that date nothing had replaced it."""
    old, new = schedules
    row_id = propose(session, old, new)
    ClauseSupersessions(session).confirm(row_id, actor="steward@bank")
    session.flush()

    response = retrieval.retrieve(
        USER_COMPLIANCE_OFFICER,
        RetrieveRequest(
            query=QUERY, top_k=10, mode=RetrievalMode.AS_OF, as_of_date=date(2024, 6, 1)
        ),
    ).response

    stale = [c for c in response.chunks if c.document_id == old.document_id]
    assert stale, "the 2023 rate was current on that date"
    assert stale[0].superseded_by is None


# ------------------------------------------------------------------------- the document page


def test_the_document_page_still_shows_a_superseded_clause(
    session: Session, schedules: tuple[ClauseRef, ClauseRef], tmp_path: Path
) -> None:
    """Where a *declared* expiry would have ended this clause, an inference only flags it. The
    text is still in the document, and a page that omitted it would lie about what the document
    says (ADR-0015/0040)."""
    from kb_portal_api.inspect import InspectService
    from kb_ports.adapters.storage_local import LocalStorageAdapter

    old, new = schedules
    row_id = propose(session, old, new)
    ClauseSupersessions(session).confirm(row_id, actor="steward@bank")
    session.flush()

    inspector = InspectService(
        session, storage=LocalStorageAdapter(tmp_path), embedder=HashedEmbeddingAdapter()
    )
    chunks = inspector.chunks(old.document_id, ALL_PRINCIPALS["user_operations_steward"])

    assert [c.section_path for c in chunks] == [OLD_CLAUSE]
    assert any("11.000" in c.text for c in chunks)


# ------------------------------------------------------------------------------ drift guard


def test_the_engines_query_agrees_with_the_registrys(
    retrieval: RetrievalEngine, session: Session, schedules: tuple[ClauseRef, ClauseRef]
) -> None:
    """Retrieval-api hand-writes this query because it does not depend on the registry — the
    same arrangement `_superseded_articles` has with `kb_registry.publish`. Two copies of a
    predicate drift, so they are asserted to agree rather than assumed to.

    Checked on both sides of the boundary date, because that is where a `<` and a `<=` differ
    and where a silent disagreement would be invisible.
    """
    old, new = schedules
    row_id = propose(session, old, new)
    ledger = ClauseSupersessions(session)

    for when in (date(2025, 12, 31), TOOK_EFFECT, date(2026, 6, 1)):
        # While proposed, both must say nothing.
        assert retrieval._clause_supersessions({old.document_id}, when) == {}
        assert ledger.served({old.document_id}, on=when) == {}

    ledger.confirm(row_id, actor="steward@bank")
    session.flush()

    for when in (date(2025, 12, 31), TOOK_EFFECT, date(2026, 6, 1)):
        engine_view = retrieval._clause_supersessions({old.document_id}, when)
        registry_view = ledger.served({old.document_id}, on=when)
        assert set(engine_view) == set(registry_view), f"disagreement on {when}"
        for key, (replacement, supersedes_from) in engine_view.items():
            row = registry_view[key]
            assert replacement == (row.new_document_id, row.new_section_path)
            assert supersedes_from == row.supersedes_from


# ------------------------------------------------------------ funnel → queue → screen


def test_the_review_screen_reads_what_the_funnel_wrote(
    session: Session, schedules: tuple[ClauseRef, ClauseRef]
) -> None:
    """The payload contract, asserted across the two services that share it.

    `ClauseFunnel` builds the `clause_review` task's payload in registry; `ClauseReviewService`
    reads it in portal-api. Nothing else checks that the two agree — the funnel's own tests
    assert what it writes and the screen's assert what it reads, and both would keep passing if
    a key were renamed on one side. This is the test that fails.
    """
    import json

    from kb_portal_api.clauses import ClauseReviewService
    from kb_ports.adapters.generation import ScriptedGeneration
    from kb_registry.adjudicate import ClauseAdjudicator
    from kb_registry.funnel import ClauseFunnel
    from kb_schemas.enums import ReviewTaskType

    _, new = schedules
    scripted = ScriptedGeneration(
        responses=[
            json.dumps(
                {
                    "verdict": "superseded",
                    "replacement_idx": 0,
                    "confidence": 0.9,
                    "rationale": "mức phí đã thay đổi",
                }
            )
        ]
    )
    report = ClauseFunnel(session, ClauseAdjudicator(scripted)).run(new.document_id)
    assert report.proposals == 1, "fixture did not produce a proposal to review"
    session.flush()

    task_id = session.execute(
        text("SELECT id, assignee_group FROM review_tasks WHERE task_type = :t"),
        {"t": ReviewTaskType.CLAUSE_REVIEW.value},
    ).one()

    screen = ClauseReviewService(session).screen(
        task_id[0], reviewer_groups={task_id[1]} if task_id[1] else set()
    )

    # Every field the screen leads with has to have survived the hand-off.
    assert screen["old"]["text"] == OLD_RATE
    assert screen["new"]["text"] == NEW_RATE
    assert screen["quantity_delta"] == ["11000 đồng → 15000 đồng"]
    assert screen["state"] == "proposed"
    assert screen["supersedes_from"] == TOOK_EFFECT.isoformat()
    assert screen["settled_by_label"], "settled_by did not survive as something readable"
