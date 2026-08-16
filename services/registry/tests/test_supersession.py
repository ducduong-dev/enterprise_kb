"""Clause-level supersession records (M9c, ADR-0033/0040).

The tests are about the two things this table has to get right. It must be *honest* — the same
append-only, two-clock discipline as the expiry ledger, so "what did we believe on 3 May" is a
query. And it must be *narrower than expiry*: recording that a clause was replaced flags it and
points at what replaced it, and never takes it out of the document.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from kb_common.audit import AuditRecord
from kb_common.errors import Conflict, GateBlocked, NotFound, ValidationError
from kb_registry.supersession import ClauseRef, ClauseSupersessions
from kb_registry.testing import make_document
from kb_schemas.enums import ExpiryState, SupersessionBasis, SupersessionVerdict
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

FROM = date(2026, 1, 1)


def _pair(session: Session) -> tuple[ClauseRef, ClauseRef]:
    old = ClauseRef(
        make_document(session, title="Thông tư 10/2022"), "Chương II > Điều 12 > Khoản 2"
    )
    new = ClauseRef(make_document(session, title="Thông tư 15/2025"), "Điều 7")
    return old, new


def _propose(
    ledger: ClauseSupersessions,
    old: ClauseRef,
    new: ClauseRef | None,
    *,
    detected_by: str = "detector",
) -> uuid.UUID:
    return ledger.propose(
        old,
        new=new,
        supersedes_from=FROM,
        basis=SupersessionBasis.DECLARED,
        detected_by=detected_by,
        actor="steward@bank",
        evidence="Điều 7 Thông tư này thay thế khoản 2 Điều 12 Thông tư 10/2022.",
    ).row_id


# --------------------------------------------------------------------------- the record


def test_a_proposal_is_not_served(session: Session) -> None:
    old, new = _pair(session)
    ledger = ClauseSupersessions(session)
    _propose(ledger, old, new)

    assert ledger.served({old.document_id}) == {}, "only confirmed rows reach a reader"


def test_confirming_writes_a_new_row_and_closes_the_old_one(session: Session) -> None:
    """Append-only, exactly as the expiry ledger is. If `state` mutated in place, "what did we
    believe on 3 May" would return today's belief wearing a past date."""
    old, new = _pair(session)
    ledger = ClauseSupersessions(session)
    row_id = _propose(ledger, old, new)
    ledger.confirm(row_id, actor="head@bank")

    history = ledger.history(old.document_id)
    assert [row.state for row in history] == [
        ExpiryState.PROPOSED.value,
        ExpiryState.CONFIRMED.value,
    ]
    assert history[0].closed_at is not None
    assert history[1].closed_at is None
    assert history[1].confirmed_by == "head@bank"


def test_a_confirmed_supersession_names_what_replaced_the_clause(session: Session) -> None:
    """The whole difference from an expiry: "hết hiệu lực" is a dead end, "quy định hiện hành
    là Điều 7" is an answer."""
    old, new = _pair(session)
    ledger = ClauseSupersessions(session)
    ledger.confirm(_propose(ledger, old, new), actor="head@bank")

    served = ledger.served({old.document_id})
    row = served[(old.document_id, old.section_path)]
    assert row.new_document_id == new.document_id
    assert row.new_section_path == "Điều 7"
    assert "thay thế" in (row.evidence or "")


def test_an_abrogation_records_that_nothing_replaced_it(session: Session) -> None:
    """A clause can end with nothing in its place. Inventing a replacement would be a lie the
    answer then repeats."""
    old, _ = _pair(session)
    ledger = ClauseSupersessions(session)
    decision = ledger.propose(
        old,
        supersedes_from=FROM,
        basis=SupersessionBasis.DECLARED,
        detected_by="detector",
        actor="steward@bank",
        evidence="Bãi bỏ khoản 2 Điều 12 Thông tư 10/2022.",
    )

    assert decision.is_abrogation
    assert decision.new is None


def test_a_supersession_before_its_date_is_not_in_force(session: Session) -> None:
    """A confirmed supersession is true from the newer clause's own effective date, not from
    now — so an `as_of` query before it still shows the older clause unflagged (ADR-0033)."""
    old, new = _pair(session)
    ledger = ClauseSupersessions(session)
    ledger.confirm(_propose(ledger, old, new), actor="head@bank")

    assert ledger.served({old.document_id}, on=date(2025, 12, 31)) == {}
    assert ledger.served({old.document_id}, on=FROM)


def test_revoking_unflags_the_clause_and_leaves_the_trail(session: Session) -> None:
    old, new = _pair(session)
    ledger = ClauseSupersessions(session)
    confirmed = ledger.confirm(_propose(ledger, old, new), actor="head@bank")
    ledger.revoke(
        confirmed.row_id, actor="legal@bank", reason="lex specialis; the older rule stands"
    )

    assert ledger.served({old.document_id}) == {}
    assert [row.state for row in ledger.history(old.document_id)] == [
        ExpiryState.PROPOSED.value,
        ExpiryState.CONFIRMED.value,
        ExpiryState.REVOKED.value,
    ]


def test_a_replaced_row_cannot_be_acted_on_again(session: Session) -> None:
    old, new = _pair(session)
    ledger = ClauseSupersessions(session)
    row_id = _propose(ledger, old, new)
    ledger.confirm(row_id, actor="head@bank")

    with pytest.raises(Conflict, match="replaced by a later decision"):
        ledger.confirm(row_id, actor="someone@bank")


def test_one_open_belief_per_clause(session: Session) -> None:
    """A clause has one answer to "what replaced this" at a time; a second decision closes the
    first rather than sitting beside it."""
    old, new = _pair(session)
    ledger = ClauseSupersessions(session)
    _propose(ledger, old, new)
    _propose(ledger, old, new)

    open_rows = [row for row in ledger.history(old.document_id) if row.closed_at is None]
    assert len(open_rows) == 1


def test_two_clauses_of_one_document_are_separate_beliefs(session: Session) -> None:
    old, new = _pair(session)
    other = ClauseRef(old.document_id, "Điều 40")
    ledger = ClauseSupersessions(session)
    _propose(ledger, old, new)
    _propose(ledger, other, new)

    assert ledger.open_row(old) is not None
    assert ledger.open_row(other) is not None


# ------------------------------------------------------------------------- what it refuses


def test_a_clause_cannot_replace_itself(session: Session) -> None:
    old, _ = _pair(session)
    ledger = ClauseSupersessions(session)
    with pytest.raises(ValidationError, match="cannot replace itself"):
        _propose(ledger, old, old)


def test_a_missing_document_is_refused(session: Session) -> None:
    ledger = ClauseSupersessions(session)
    with pytest.raises(NotFound):
        _propose(ledger, ClauseRef(uuid.uuid4(), "Điều 1"), None)


def test_a_revocation_needs_a_reason(session: Session) -> None:
    old, new = _pair(session)
    ledger = ClauseSupersessions(session)
    confirmed = ledger.confirm(_propose(ledger, old, new), actor="head@bank")
    with pytest.raises(ValidationError, match="needs a reason"):
        ledger.revoke(confirmed.row_id, actor="legal@bank", reason="  ")


def test_a_regulated_clause_needs_a_second_pair_of_eyes(session: Session) -> None:
    """Withdrawing a rule from every answer changes what the bank tells people, whether it is
    done by ending the clause or by flagging it (INV-8)."""
    old, new = _pair(session)
    session.execute(
        text("UPDATE documents SET doc_class = 'regulatory' WHERE id = :d"),
        {"d": old.document_id},
    )
    session.flush()
    ledger = ClauseSupersessions(session)
    row_id = _propose(ledger, old, new, detected_by="steward@bank")

    with pytest.raises(GateBlocked, match="four-eyes"):
        ledger.confirm(row_id, actor="steward@bank")
    assert ledger.confirm(row_id, actor="legal@bank").state == ExpiryState.CONFIRMED.value


def test_a_machine_proposal_on_a_regulated_clause_needs_exactly_one_human(
    session: Session,
) -> None:
    """`detected_by` is a detector, never an actor, so it cannot collide with the confirmer."""
    old, new = _pair(session)
    session.execute(
        text("UPDATE documents SET doc_class = 'regulatory' WHERE id = :d"),
        {"d": old.document_id},
    )
    session.flush()
    ledger = ClauseSupersessions(session)
    row_id = _propose(ledger, old, new, detected_by="declaration_extractor")

    assert ledger.confirm(row_id, actor="steward@bank").state == ExpiryState.CONFIRMED.value


# ---------------------------------------------------------------------------- the record's


def test_the_inferred_path_carries_its_working(session: Session) -> None:
    """A steward's queue is workable on "8%/năm → 10%/năm" and not on a similarity score, so
    the delta and the verdict travel with the row (ADR-0033)."""
    old, new = _pair(session)
    ledger = ClauseSupersessions(session)
    decision = ledger.propose(
        old,
        new=new,
        supersedes_from=FROM,
        basis=SupersessionBasis.DETECTED,
        verdict=SupersessionVerdict.SUPERSEDED,
        detected_by="funnel",
        actor="system",
        quantity_delta={"rate": ["8%/năm", "10%/năm"]},
        scope_facets={"matched": ["cá nhân"], "mismatched": []},
        score=0.91,
        model="qwen3.5-9b",
        prompt_version="1",
    )

    row = ledger.open_row(old)
    assert row is not None
    assert row.verdict == SupersessionVerdict.SUPERSEDED.value
    assert row.quantity_delta == {"rate": ["8%/năm", "10%/năm"]}
    assert row.model == "qwen3.5-9b"
    assert decision.state == ExpiryState.PROPOSED.value


def test_the_decision_is_audited(session: Session) -> None:
    recorded: list[AuditRecord] = []

    class _Sink:
        def write(self, record: AuditRecord) -> None:
            recorded.append(record)

    old, new = _pair(session)
    ledger = ClauseSupersessions(session, audit=_Sink())
    ledger.confirm(_propose(ledger, old, new), actor="head@bank")

    assert {record.action for record in recorded} == {"clause_supersession"}
    assert recorded[-1].detail["replaced_by"].endswith("Điều 7")
    assert recorded[-1].object_ref["section_path"] == old.section_path
