"""The daily expiry sweep (M9a, ADR-0031).

The property under test is mostly a negative one: the sweep must never be load-bearing. Its
failure mode is late banners and stale queues, never a wrong answer — so the first test here
runs no sweep at all and asserts serving is already correct, and everything after it is about
bookkeeping being idempotent enough to run unattended every night.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from kb_registry.expiry import ExpiryLedger
from kb_registry.sweep import WARNING_WINDOW, ExpirySweep
from kb_registry.testing import make_chunk, make_document, make_version
from kb_schemas.enums import DocStatus, ExpiryBasis, ReviewTaskState, ReviewTaskType
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

EXPIRED_ON = date(2026, 12, 31)
DAY_AFTER = date(2027, 1, 1)


def _confirmed_expiry(
    session: Session,
    doc_id: uuid.UUID,
    *,
    effective_to: date = EXPIRED_ON,
    anchors: list[str] | None = None,
    evidence: str | None = None,
) -> ExpiryLedger:
    ledger = ExpiryLedger(session)
    proposed = ledger.propose(
        doc_id,
        effective_to=effective_to,
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
        anchors=anchors,
        evidence=evidence,
    )
    ledger.confirm(proposed.row_id, actor="head@bank")
    return ledger


def _status(session: Session, doc_id: uuid.UUID) -> str:
    return str(
        session.execute(text("SELECT status FROM documents WHERE id = :d"), {"d": doc_id}).scalar()
    )


def _tasks(
    session: Session, doc_id: uuid.UUID, task_type: ReviewTaskType
) -> list[dict[str, object]]:
    rows = session.execute(
        text(
            "SELECT t.payload FROM review_tasks t "
            "JOIN document_versions v ON v.id = t.version_id "
            "WHERE v.document_id = :d AND t.task_type = :type"
        ),
        {"d": doc_id, "type": task_type.value},
    )
    return [row[0] for row in rows]


def test_the_answer_is_already_right_before_the_sweep_ever_runs(session: Session) -> None:
    """ADR-0031's central claim, stated as a test that deliberately runs nothing.

    If this ever fails, the design is wrong rather than the operations: "why was a withdrawn
    fee served to a customer yesterday" must never be answerable with "the nightly job did not
    run".
    """
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    make_chunk(session, doc_id, version_id)
    _confirmed_expiry(session, doc_id)

    served = session.execute(
        text(
            "SELECT count(*) FROM chunks WHERE document_id = :d AND NOT tombstoned "
            "AND (effective_to IS NULL OR effective_to >= :on)"
        ),
        {"d": doc_id, "on": DAY_AFTER},
    ).scalar()

    assert served == 0, "the chunk left the default path on its date, with no job having run"
    assert _status(session, doc_id) == DocStatus.PUBLISHED.value, (
        "and the label has not caught up yet, which is exactly the intended order"
    )


def test_the_sweep_flips_a_passed_expiry_to_expired(session: Session) -> None:
    doc_id = make_document(session)
    make_version(session, doc_id)
    _confirmed_expiry(session, doc_id)

    flipped = ExpirySweep(session).flip_expired(today=DAY_AFTER)

    assert flipped == [doc_id]
    assert _status(session, doc_id) == DocStatus.EXPIRED.value


def test_running_the_sweep_twice_produces_the_same_state(session: Session) -> None:
    """Idempotence is what makes it safe to run unattended, and to run after an outage."""
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    session.execute(
        text("UPDATE documents SET review_by = :when WHERE id = :d"),
        {"when": date(2026, 1, 1), "d": doc_id},
    )
    _confirmed_expiry(session, doc_id, effective_to=DAY_AFTER + timedelta(days=10))

    first = ExpirySweep(session).run(today=DAY_AFTER)
    second = ExpirySweep(session).run(today=DAY_AFTER)

    # Containment, not equality: the sweep is corpus-wide, and the seeded corpus has its own
    # documents due for re-attestation. What matters is this one being handled exactly once.
    assert doc_id in first.warned
    assert doc_id in first.attestations
    assert second.warned == [], "the task is already open; a queue that regrows nightly is unusable"
    assert second.attestations == []
    assert len(_tasks(session, doc_id, ReviewTaskType.EXPIRY_REVIEW)) == 1
    assert len(_tasks(session, doc_id, ReviewTaskType.PERIODIC_REVIEW)) == 1
    assert version_id is not None


def test_a_run_after_a_three_day_outage_catches_up_in_one_go(session: Session) -> None:
    """It reads a clock, not a cursor, so there is nothing to replay."""
    doc_id = make_document(session)
    make_version(session, doc_id)
    _confirmed_expiry(session, doc_id)

    missed = ExpirySweep(session).run(today=EXPIRED_ON + timedelta(days=3))

    assert missed.expired == [doc_id]
    assert _status(session, doc_id) == DocStatus.EXPIRED.value


def test_a_future_expiry_is_not_flipped_early(session: Session) -> None:
    doc_id = make_document(session)
    make_version(session, doc_id)
    _confirmed_expiry(session, doc_id)

    assert ExpirySweep(session).flip_expired(today=EXPIRED_ON) == []
    assert _status(session, doc_id) == DocStatus.PUBLISHED.value


def test_a_proposed_expiry_flips_nothing(session: Session) -> None:
    """Only `confirmed` is served, and only `confirmed` is relabelled."""
    doc_id = make_document(session)
    make_version(session, doc_id)
    ExpiryLedger(session).propose(
        doc_id,
        effective_to=EXPIRED_ON,
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )

    assert ExpirySweep(session).flip_expired(today=DAY_AFTER) == []
    assert _status(session, doc_id) == DocStatus.PUBLISHED.value


def test_a_revoked_expiry_flips_nothing(session: Session) -> None:
    doc_id = make_document(session)
    make_version(session, doc_id)
    ledger = _confirmed_expiry(session, doc_id)
    open_row = ledger.open_row(doc_id)
    assert open_row is not None
    ledger.revoke(open_row.id, actor="legal@bank", reason="the abrogation was misread")

    assert ExpirySweep(session).flip_expired(today=DAY_AFTER) == []
    assert _status(session, doc_id) == DocStatus.PUBLISHED.value


def test_a_partially_expired_document_is_never_relabelled(session: Session) -> None:
    """A row with anchors takes clauses out of service, not the instrument. Marking the whole
    document expired because four of its sixty articles were repealed would be a lie about the
    other fifty-six (ADR-0040)."""
    doc_id = make_document(session)
    make_version(session, doc_id)
    _confirmed_expiry(session, doc_id, anchors=["12.2"])

    assert ExpirySweep(session).flip_expired(today=DAY_AFTER) == []
    assert _status(session, doc_id) == DocStatus.PUBLISHED.value


def test_the_warning_lands_thirty_days_out_with_its_evidence(session: Session) -> None:
    doc_id = make_document(session)
    make_version(session, doc_id)
    _confirmed_expiry(
        session,
        doc_id,
        evidence="Thông tư này hết hiệu lực kể từ ngày 01/01/2027.",
    )

    warned = ExpirySweep(session).warn_approaching(today=EXPIRED_ON - timedelta(days=20))

    assert warned == [doc_id]
    payload = _tasks(session, doc_id, ReviewTaskType.EXPIRY_REVIEW)[0]
    assert payload["days_remaining"] == 20
    assert payload["expires_on"] == EXPIRED_ON.isoformat()
    assert "hết hiệu lực" in str(payload["evidence"]), "the steward can object without opening it"


def test_no_warning_before_the_window_opens(session: Session) -> None:
    doc_id = make_document(session)
    make_version(session, doc_id)
    _confirmed_expiry(session, doc_id)

    too_early = EXPIRED_ON - WARNING_WINDOW - timedelta(days=1)
    assert ExpirySweep(session).warn_approaching(today=too_early) == []
    assert _tasks(session, doc_id, ReviewTaskType.EXPIRY_REVIEW) == []


def test_review_by_finally_does_something(session: Session) -> None:
    """The column has been settable and indexed since the initial schema and read by nothing."""
    doc_id = make_document(session)
    make_version(session, doc_id)
    session.execute(
        text("UPDATE documents SET review_by = :when WHERE id = :d"),
        {"when": date(2026, 6, 30), "d": doc_id},
    )

    opened = ExpirySweep(session).open_attestations(today=date(2026, 7, 15))

    assert doc_id in opened
    payload = _tasks(session, doc_id, ReviewTaskType.PERIODIC_REVIEW)[0]
    assert payload["overdue_days"] == 15


def test_an_attestation_is_not_due_before_its_date(session: Session) -> None:
    doc_id = make_document(session)
    make_version(session, doc_id)
    session.execute(
        text("UPDATE documents SET review_by = :when WHERE id = :d"),
        {"when": date(2026, 6, 30), "d": doc_id},
    )

    assert doc_id not in ExpirySweep(session).open_attestations(today=date(2026, 6, 29))


def test_a_decided_task_lets_the_next_one_open(session: Session) -> None:
    """Idempotence must not become permanent silence: once the steward has dealt with a
    re-attestation, a later `review_by` has to be able to ask again."""
    doc_id = make_document(session)
    make_version(session, doc_id)
    session.execute(
        text("UPDATE documents SET review_by = :when WHERE id = :d"),
        {"when": date(2026, 6, 30), "d": doc_id},
    )
    sweep = ExpirySweep(session)
    sweep.open_attestations(today=date(2026, 7, 15))
    session.execute(
        text(
            "UPDATE review_tasks SET state = :decided, decided_by = 'steward@bank', "
            "decided_at = now() WHERE task_type = :type"
        ),
        {"decided": ReviewTaskState.DECIDED.value, "type": ReviewTaskType.PERIODIC_REVIEW.value},
    )

    assert doc_id in sweep.open_attestations(today=date(2027, 7, 15))
    assert len(_tasks(session, doc_id, ReviewTaskType.PERIODIC_REVIEW)) == 2


def test_the_flip_emits_the_event_an_external_index_needs(session: Session) -> None:
    """With `pg_search` the chunk dates are already the index. A keyword backend living outside
    Postgres has no other way to learn the status moved."""
    doc_id = make_document(session)
    make_version(session, doc_id)
    _confirmed_expiry(session, doc_id)

    ExpirySweep(session).flip_expired(today=DAY_AFTER)

    payload = session.execute(
        text(
            "SELECT payload FROM outbox WHERE payload->>'document_id' = :d "
            "AND payload->>'status' = 'expired'"
        ),
        {"d": str(doc_id)},
    ).scalar()
    assert payload is not None
    assert payload["expired_on"] == EXPIRED_ON.isoformat()
