"""The expiry ledger (M9a, ADR-0030/0031/0040).

Two halves are tested separately because they fail differently. *Deciding* is about the record
being honest — append-only, both clocks, only `confirmed` counts. *Applying* is about serving:
the chunk copies carry the date, so the moment that matters is confirmation, and nothing
scheduled is allowed to be load-bearing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from kb_common.audit import AuditRecord
from kb_common.errors import Conflict, NotFound, ValidationError
from kb_registry.expiry import ExpiryLedger, expiry_date_for_abrogation
from kb_registry.testing import chunk_dates, make_chunk, make_document, make_version
from kb_schemas.enums import ExpiryBasis, ExpiryState
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------- deciding


def test_a_proposal_changes_nothing_that_is_served(session: Session) -> None:
    """The whole point of the `proposed` state. A detector's opinion is not a withdrawal."""
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    make_chunk(session, doc_id, version_id)
    ledger = ExpiryLedger(session)

    decision = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )

    assert decision.state == ExpiryState.PROPOSED.value
    assert not decision.applied
    assert chunk_dates(session, doc_id) == [None]
    assert ledger.effective_to_for(doc_id) is None


def test_confirming_writes_a_new_row_and_closes_the_old_one(session: Session) -> None:
    """Append-only. If `state` mutated in place, "what did we believe on 3 May" would return
    today's belief wearing a past date."""
    doc_id = make_document(session)
    make_version(session, doc_id)
    ledger = ExpiryLedger(session)

    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    confirmed = ledger.confirm(proposed.row_id, actor="head@bank")

    history = ledger.history(doc_id)
    assert [row.state for row in history] == [
        ExpiryState.PROPOSED.value,
        ExpiryState.CONFIRMED.value,
    ]
    assert history[0].closed_at is not None, "the proposal is closed, not overwritten"
    assert history[1].closed_at is None, "the confirmation is the current belief"
    assert history[1].decided_by == "head@bank"
    assert confirmed.row_id != proposed.row_id


def test_the_second_clock_answers_what_we_believed_then(session: Session) -> None:
    """Distinct from `as_of`, which asks what was in force in the world. An auditor
    reconstructing a past answer is asking this one (INV-11)."""
    doc_id = make_document(session)
    make_version(session, doc_id)
    ledger = ExpiryLedger(session)

    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    before_confirmation = datetime.now(UTC)
    ledger.confirm(proposed.row_id, actor="head@bank")

    then = ledger.belief_at(doc_id, before_confirmation)
    assert [row.state for row in then] == [ExpiryState.PROPOSED.value], (
        "at that instant we had a proposal and had not decided"
    )
    now = ledger.belief_at(doc_id, datetime.now(UTC))
    assert [row.state for row in now] == [ExpiryState.CONFIRMED.value]


def test_a_replaced_row_cannot_be_acted_on_again(session: Session) -> None:
    doc_id = make_document(session)
    make_version(session, doc_id)
    ledger = ExpiryLedger(session)
    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    ledger.confirm(proposed.row_id, actor="head@bank")

    with pytest.raises(Conflict, match="replaced by a later decision"):
        ledger.confirm(proposed.row_id, actor="someone@bank")


def test_an_expiry_attributed_elsewhere_must_name_the_instrument(session: Session) -> None:
    doc_id = make_document(session)
    make_version(session, doc_id)
    ledger = ExpiryLedger(session)
    with pytest.raises(ValidationError, match="must name it"):
        ledger.propose(
            doc_id,
            effective_to=date(2026, 12, 31),
            basis=ExpiryBasis.ABROGATED_BY,
            detected_by="test",
            actor="steward@bank",
        )


def test_a_missing_document_is_not_expired_quietly(session: Session) -> None:
    ledger = ExpiryLedger(session)
    with pytest.raises(NotFound):
        ledger.propose(
            uuid.uuid4(),
            effective_to=date(2026, 12, 31),
            basis=ExpiryBasis.STEWARD,
            detected_by="test",
            actor="steward@bank",
        )


# ---------------------------------------------------------------------------- applying


def test_confirmation_projects_the_date_onto_the_chunks(session: Session) -> None:
    """This is what takes the document out of service: the predicate already reads this column
    on every query, so nothing else has to change."""
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    make_chunk(session, doc_id, version_id)
    make_chunk(session, doc_id, version_id)
    ledger = ExpiryLedger(session)

    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    decision = ledger.confirm(proposed.row_id, actor="head@bank")

    assert decision.applied
    assert decision.chunks_projected == 2
    assert chunk_dates(session, doc_id) == [date(2026, 12, 31), date(2026, 12, 31)]
    assert ledger.effective_to_for(doc_id) == date(2026, 12, 31)


def test_the_expired_chunk_is_unretrievable_with_no_sweep_ever_running(session: Session) -> None:
    """ADR-0031's central property. The failure mode of a scheduled job must be late banners,
    never wrong answers — so this asserts the serving predicate directly, on a clock past the
    expiry, with nothing scheduled having run."""
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    make_chunk(session, doc_id, version_id)
    ledger = ExpiryLedger(session)
    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    ledger.confirm(proposed.row_id, actor="head@bank")

    # The predicate `kb_authz.compile` emits on every read path, nothing more.
    def visible_on(when: date) -> int:
        return (
            session.execute(
                text(
                    "SELECT count(*) FROM chunks WHERE document_id = :d AND NOT tombstoned "
                    "AND (effective_to IS NULL OR effective_to >= :on)"
                ),
                {"d": doc_id, "on": when},
            ).scalar()
            or 0
        )

    assert visible_on(date(2026, 12, 31)) == 1, "the last day it applied"
    assert visible_on(date(2027, 1, 1)) == 0, "the day after, with no job having run"
    assert (
        session.execute(text("SELECT status FROM documents WHERE id = :d"), {"d": doc_id}).scalar()
        == "published"
    ), "the status flip is the sweep's bookkeeping, not a precondition for correctness"


def test_revoking_puts_the_document_back_and_leaves_the_trail(session: Session) -> None:
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    make_chunk(session, doc_id, version_id)
    ledger = ExpiryLedger(session)
    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    confirmed = ledger.confirm(proposed.row_id, actor="head@bank")
    assert chunk_dates(session, doc_id) == [date(2026, 12, 31)]

    ledger.revoke(confirmed.row_id, actor="legal@bank", reason="abrogation was misread")

    assert chunk_dates(session, doc_id) == [None]
    assert ledger.effective_to_for(doc_id) is None
    assert [row.state for row in ledger.history(doc_id)] == [
        ExpiryState.PROPOSED.value,
        ExpiryState.CONFIRMED.value,
        ExpiryState.REVOKED.value,
    ], "nothing was deleted; the sequence is the answer to 'why did this come back'"


def test_revoking_restores_a_self_stated_sunset_rather_than_clearing_it(
    session: Session,
) -> None:
    """The version's own date is a different fact from the ledger's. Withdrawing the ledger
    row must uncover it, not overwrite it with NULL."""
    doc_id = make_document(session)
    version_id = make_version(session, doc_id, effective_to=date(2030, 6, 30))
    make_chunk(session, doc_id, version_id, effective_to=date(2030, 6, 30))
    ledger = ExpiryLedger(session)
    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    confirmed = ledger.confirm(proposed.row_id, actor="head@bank")
    assert chunk_dates(session, doc_id) == [date(2026, 12, 31)], "the ledger wins while it stands"

    ledger.revoke(confirmed.row_id, actor="legal@bank", reason="withdrawn after review")
    assert chunk_dates(session, doc_id) == [date(2030, 6, 30)]


def test_a_tombstoned_chunk_keeps_its_own_versions_dates(session: Session) -> None:
    """Superseded versions are archive. Their dates are that version's history, and the default
    path cannot reach them anyway."""
    doc_id = make_document(session)
    old_version = make_version(session, doc_id, effective_to=date(2024, 12, 31), canonical=False)
    new_version = make_version(session, doc_id)
    make_chunk(session, doc_id, old_version, effective_to=date(2024, 12, 31), tombstoned=True)
    make_chunk(session, doc_id, new_version)
    ledger = ExpiryLedger(session)
    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    ledger.confirm(proposed.row_id, actor="head@bank")

    archived = session.execute(
        text("SELECT effective_to FROM chunks WHERE version_id = :v"), {"v": old_version}
    ).scalar()
    assert archived == date(2024, 12, 31)
    assert chunk_dates(session, doc_id) == [date(2026, 12, 31)]


def test_a_partial_expiry_ends_only_the_clauses_it_names(session: Session) -> None:
    """The ordinary shape in this corpus: Vietnamese practice abrogates in pieces, so a
    sixty-article circular can carry four dead clauses and fifty-six live ones (ADR-0040)."""
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    for ordinal, anchor in enumerate(("12.1", "12.2", "40")):
        make_chunk(session, doc_id, version_id, ordinal=ordinal)
        session.execute(
            text(
                "UPDATE chunks SET anchor = :a, article = :n "
                "WHERE document_id = :d AND ordinal = :o"
            ),
            {"a": anchor, "n": int(anchor.split(".")[0]), "d": doc_id, "o": ordinal},
        )
    session.flush()
    ledger = ExpiryLedger(session)

    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
        anchors=["12.2"],
        evidence="Bãi bỏ khoản 2 Điều 12 theo Thông tư 15/2026.",
    )
    decision = ledger.confirm(proposed.row_id, actor="head@bank")

    assert decision.applied
    assert decision.chunks_projected == 1
    assert chunk_dates(session, doc_id) == [None, date(2026, 12, 31), None], (
        "only khoản 2 ended; khoản 1 and Điều 40 are untouched"
    )
    # And the document itself is still in service: a partial expiry is not a full one.
    assert ledger.effective_to_for(doc_id) is None
    assert (
        session.execute(text("SELECT status FROM documents WHERE id = :d"), {"d": doc_id}).scalar()
        == "published"
    )


def test_a_partial_expiry_whose_anchors_match_nothing_ends_nothing(session: Session) -> None:
    """An anchor that resolves to nothing is a steward's problem, never a licence to expire the
    whole instrument — the failure direction that would hurt most (ADR-0036)."""
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    make_chunk(session, doc_id, version_id)
    session.execute(
        text("UPDATE chunks SET anchor = '5', article = 5 WHERE document_id = :d"),
        {"d": doc_id},
    )
    session.flush()
    ledger = ExpiryLedger(session)

    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
        anchors=["99.1"],
    )
    decision = ledger.confirm(proposed.row_id, actor="head@bank")

    assert not decision.applied
    assert "không tìm thấy" in decision.note
    assert chunk_dates(session, doc_id) == [None], "the document is untouched"


def test_a_clause_expiry_falls_back_to_the_merged_article_chunk(session: Session) -> None:
    """The chunker merges two short clauses into one chunk anchored at their common article,
    so ending "khoản 2 Điều 12" has to reach the chunk that contains it."""
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    make_chunk(session, doc_id, version_id)
    session.execute(
        text("UPDATE chunks SET anchor = '12', article = 12 WHERE document_id = :d"),
        {"d": doc_id},
    )
    session.flush()
    ledger = ExpiryLedger(session)

    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
        anchors=["12.2"],
    )
    decision = ledger.confirm(proposed.row_id, actor="head@bank")

    assert decision.applied
    assert chunk_dates(session, doc_id) == [date(2026, 12, 31)]


def test_the_earliest_date_wins_when_a_clause_and_its_document_both_end(
    session: Session,
) -> None:
    """Order of application must not matter: a clause repealed in March inside a document that
    ends in December ends in March, and a document that ends first takes its clauses with it."""
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    for ordinal, anchor in enumerate(("12.1", "12.2")):
        make_chunk(session, doc_id, version_id, ordinal=ordinal)
        session.execute(
            text(
                "UPDATE chunks SET anchor = :a, article = 12 "
                "WHERE document_id = :d AND ordinal = :o"
            ),
            {"a": anchor, "d": doc_id, "o": ordinal},
        )
    session.flush()
    ledger = ExpiryLedger(session)

    clause = ledger.propose(
        doc_id,
        effective_to=date(2026, 3, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
        anchors=["12.2"],
    )
    ledger.confirm(clause.row_id, actor="head@bank")
    whole = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    ledger.confirm(whole.row_id, actor="head@bank")

    assert chunk_dates(session, doc_id) == [date(2026, 12, 31), date(2026, 3, 31)]
    assert ledger.project(doc_id) == 0, "and it is still idempotent with both rows open"


def test_a_partial_and_a_whole_document_expiry_are_different_scopes(session: Session) -> None:
    doc_id = make_document(session)
    make_version(session, doc_id)
    ledger = ExpiryLedger(session)
    ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
        anchors=["12.2"],
    )
    ledger.propose(
        doc_id,
        effective_to=date(2027, 6, 30),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )

    assert ledger.open_row(doc_id, anchors=["12.2"]) is not None
    assert ledger.open_row(doc_id) is not None
    assert len([row for row in ledger.history(doc_id) if row.closed_at is None]) == 2


# --------------------------------------------------------------------- the abrogation path


def test_the_abrogated_instrument_ends_the_day_before_its_successor_starts(
    session: Session,
) -> None:
    """`[OPEN]`-7 in one assertion: one day either way is a day of a repealed rule served, or a
    day of a live rule hidden."""
    assert expiry_date_for_abrogation(date(2026, 5, 1)) == date(2026, 4, 30)


def test_an_abrogates_edge_proposes_an_expiry_with_the_reason_attached(
    session: Session,
) -> None:
    old_id = make_document(session, title="Thông tư 10/2022")
    new_id = make_document(session, title="Thông tư 15/2025")
    make_version(session, old_id)
    make_version(session, new_id, effective_from=date(2026, 5, 1))
    session.execute(
        text(
            """
            INSERT INTO document_refs (id, src_document_id, dst_document_id, ref_type,
                                       detected_by, created_at)
            VALUES (:id, :src, :dst, 'abrogates', 'detector', now())
            """
        ),
        {"id": uuid.uuid4(), "src": new_id, "dst": old_id},
    )
    ledger = ExpiryLedger(session)

    decisions = ledger.propose_from_abrogations(new_id, actor="system")

    assert len(decisions) == 1
    assert decisions[0].document_id == old_id
    assert decisions[0].effective_to == date(2026, 4, 30)
    assert decisions[0].state == ExpiryState.PROPOSED.value
    row = ledger.open_row(old_id)
    assert row is not None
    assert row.source_document_id == new_id
    assert "Thông tư 15/2025" in (row.evidence or ""), "the reason travels with the decision"


def test_proposing_from_abrogations_twice_does_not_queue_it_twice(session: Session) -> None:
    """It runs on every publish, and a steward's queue that regrows on each republish is a
    queue nobody works."""
    old_id = make_document(session)
    new_id = make_document(session)
    make_version(session, old_id)
    make_version(session, new_id, effective_from=date(2026, 5, 1))
    session.execute(
        text(
            """
            INSERT INTO document_refs (id, src_document_id, dst_document_id, ref_type,
                                       detected_by, created_at)
            VALUES (:id, :src, :dst, 'abrogates', 'detector', now())
            """
        ),
        {"id": uuid.uuid4(), "src": new_id, "dst": old_id},
    )
    ledger = ExpiryLedger(session)

    assert len(ledger.propose_from_abrogations(new_id, actor="system")) == 1
    assert ledger.propose_from_abrogations(new_id, actor="system") == []
    assert len(ledger.history(old_id)) == 1


def test_a_revoked_abrogation_is_not_re_proposed_on_the_next_publish(session: Session) -> None:
    """A steward turned it down. Putting it straight back is how a review queue loses its
    meaning."""
    old_id = make_document(session)
    new_id = make_document(session)
    make_version(session, old_id)
    make_version(session, new_id, effective_from=date(2026, 5, 1))
    session.execute(
        text(
            """
            INSERT INTO document_refs (id, src_document_id, dst_document_id, ref_type,
                                       detected_by, created_at)
            VALUES (:id, :src, :dst, 'abrogates', 'detector', now())
            """
        ),
        {"id": uuid.uuid4(), "src": new_id, "dst": old_id},
    )
    ledger = ExpiryLedger(session)
    proposed = ledger.propose_from_abrogations(new_id, actor="system")[0]
    ledger.revoke(proposed.row_id, actor="legal@bank", reason="lex specialis; the old rule stands")

    assert ledger.propose_from_abrogations(new_id, actor="system") == []


def test_an_abrogating_instrument_with_no_effective_date_proposes_nothing(
    session: Session,
) -> None:
    """A date nobody read is exactly what ADR-0029 refuses to invent. The reviewer supplies the
    effective date, and the next publish proposes."""
    old_id = make_document(session)
    new_id = make_document(session)
    make_version(session, old_id)
    make_version(session, new_id, effective_from=None)
    session.execute(
        text(
            """
            INSERT INTO document_refs (id, src_document_id, dst_document_id, ref_type,
                                       detected_by, created_at)
            VALUES (:id, :src, :dst, 'abrogates', 'detector', now())
            """
        ),
        {"id": uuid.uuid4(), "src": new_id, "dst": old_id},
    )

    assert ExpiryLedger(session).propose_from_abrogations(new_id, actor="system") == []
    assert ExpiryLedger(session).open_row(old_id) is None


def test_a_cites_edge_expires_nothing(session: Session) -> None:
    old_id = make_document(session)
    new_id = make_document(session)
    make_version(session, old_id)
    make_version(session, new_id, effective_from=date(2026, 5, 1))
    session.execute(
        text(
            """
            INSERT INTO document_refs (id, src_document_id, dst_document_id, ref_type,
                                       detected_by, created_at)
            VALUES (:id, :src, :dst, 'cites', 'detector', now())
            """
        ),
        {"id": uuid.uuid4(), "src": new_id, "dst": old_id},
    )

    assert ExpiryLedger(session).propose_from_abrogations(new_id, actor="system") == []


def test_an_unpublished_draft_expires_nothing(session: Session) -> None:
    """A draft that says it repeals something has not repealed anything."""
    old_id = make_document(session)
    new_id = make_document(session)
    make_version(session, old_id)
    make_version(session, new_id, effective_from=date(2026, 5, 1))
    session.execute(text("UPDATE documents SET status = 'draft' WHERE id = :d"), {"d": new_id})
    session.execute(
        text(
            """
            INSERT INTO document_refs (id, src_document_id, dst_document_id, ref_type,
                                       detected_by, created_at)
            VALUES (:id, :src, :dst, 'abrogates', 'detector', now())
            """
        ),
        {"id": uuid.uuid4(), "src": new_id, "dst": old_id},
    )

    assert ExpiryLedger(session).propose_from_abrogations(new_id, actor="system") == []


def test_reprojection_survives_a_rechunk(session: Session) -> None:
    """Chunk ids do not survive a rechunk and neither do their dates — the new rows carry the
    *version's* `effective_to`. Without re-reading the ledger, a rechunk resurrects an expired
    document silently."""
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    make_chunk(session, doc_id, version_id)
    ledger = ExpiryLedger(session)
    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    ledger.confirm(proposed.row_id, actor="head@bank")

    # What `_insert_chunks` does: delete and re-insert, copying the version's dates.
    session.execute(text("DELETE FROM chunks WHERE version_id = :v"), {"v": version_id})
    make_chunk(session, doc_id, version_id)
    assert chunk_dates(session, doc_id) == [None], "the rechunked rows start from the version"

    ledger.project(doc_id)
    assert chunk_dates(session, doc_id) == [date(2026, 12, 31)]


def test_projection_is_idempotent(session: Session) -> None:
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    make_chunk(session, doc_id, version_id)
    ledger = ExpiryLedger(session)
    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    ledger.confirm(proposed.row_id, actor="head@bank")

    assert ledger.project(doc_id) == 0, "nothing to change the second time"
    assert chunk_dates(session, doc_id) == [date(2026, 12, 31)]


def test_the_ledger_beats_the_version_but_does_not_overwrite_it(session: Session) -> None:
    """Both have to stay visible: a steward who typed a date into a form needs to see why a
    different one is in force (ADR-0030), and INV-9 forbids editing a published version."""
    doc_id = make_document(session)
    version_id = make_version(session, doc_id, effective_to=date(2030, 6, 30))
    make_chunk(session, doc_id, version_id, effective_to=date(2030, 6, 30))
    ledger = ExpiryLedger(session)
    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    ledger.confirm(proposed.row_id, actor="head@bank")

    assert chunk_dates(session, doc_id) == [date(2026, 12, 31)]
    version_date = session.execute(
        text("SELECT effective_to FROM document_versions WHERE id = :v"), {"v": version_id}
    ).scalar()
    assert version_date == date(2030, 6, 30), "the version's own sunset is untouched"


def test_an_archive_query_before_the_expiry_still_finds_the_document(session: Session) -> None:
    """Point-in-time is the whole reason expiry hides rather than deletes."""
    doc_id = make_document(session)
    version_id = make_version(session, doc_id, effective_from=date(2022, 1, 1))
    make_chunk(session, doc_id, version_id)
    ledger = ExpiryLedger(session)
    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    ledger.confirm(proposed.row_id, actor="head@bank")

    as_of = date(2024, 3, 1)
    found = session.execute(
        text(
            "SELECT count(*) FROM chunks WHERE document_id = :d "
            "AND (effective_to IS NULL OR effective_to >= :on) "
            "AND (effective_from IS NULL OR effective_from <= :on)"
        ),
        {"d": doc_id, "on": as_of},
    ).scalar()
    assert found == 1


def test_the_decision_is_audited_with_its_evidence(session: Session) -> None:
    """An auditor asks who decided this instrument stopped applying, and on what evidence."""
    recorded: list[AuditRecord] = []

    class _Sink:
        def write(self, record: AuditRecord) -> None:
            recorded.append(record)

    doc_id = make_document(session)
    make_version(session, doc_id)
    ledger = ExpiryLedger(session, audit=_Sink())
    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
        evidence="Thông tư này hết hiệu lực kể từ ngày 01/01/2027.",
    )
    ledger.confirm(proposed.row_id, actor="head@bank")

    assert len(recorded) == 2
    assert {record.action for record in recorded} == {"expiry_decision"}
    assert recorded[0].detail["evidence"].startswith("Thông tư này hết hiệu lực")
    assert recorded[1].actor == "head@bank"
    assert recorded[1].detail["applied"] is True


def test_expiry_does_not_reach_a_document_it_was_not_about(session: Session) -> None:
    other_id = make_document(session)
    other_version = make_version(session, other_id)
    make_chunk(session, other_id, other_version)
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    make_chunk(session, doc_id, version_id)
    ledger = ExpiryLedger(session)
    proposed = ledger.propose(
        doc_id,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    ledger.confirm(proposed.row_id, actor="head@bank")

    assert chunk_dates(session, other_id) == [None]


def test_a_future_expiry_is_confirmed_now_and_bites_later(session: Session) -> None:
    """Confirmation and the date arriving are different moments, and the gap is where the
    projection earns its place: the date is written months before it matters."""
    doc_id = make_document(session)
    version_id = make_version(session, doc_id)
    make_chunk(session, doc_id, version_id)
    ledger = ExpiryLedger(session)
    far_off = date.today() + timedelta(days=180)
    proposed = ledger.propose(
        doc_id,
        effective_to=far_off,
        basis=ExpiryBasis.STEWARD,
        detected_by="test",
        actor="steward@bank",
    )
    ledger.confirm(proposed.row_id, actor="head@bank")

    still_served = session.execute(
        text(
            "SELECT count(*) FROM chunks WHERE document_id = :d "
            "AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)"
        ),
        {"d": doc_id},
    ).scalar()
    assert still_served == 1, "confirmed today, in force until the date arrives"
    assert chunk_dates(session, doc_id) == [far_off]


# ------------------------------------------------ the named refusal's probe (ADR-0030)


def test_the_expired_probe_finds_what_ceased_and_never_what_is_current(
    session: Session,
) -> None:
    """The query behind chat's named refusal, against a real database.

    Two properties in one test because they are the same property: it must see the expired
    instrument (otherwise the refusal is silence again) and it must be structurally unable to
    see a live one (otherwise it is a second retrieval path with a different predicate, which
    is what INV-1 exists to prevent).
    """
    from kb_authz.compile import compile_sql_expired
    from kb_authz.filters import FilterBuilder
    from kb_authz.fixtures import USER_RETAIL_STAFF
    from kb_vntext.language import fold_diacritics

    today = date(2027, 6, 1)
    gone = make_document(session, title="Biểu phí 2023")
    gone_version = make_version(session, gone)
    make_chunk(session, gone, gone_version, effective_to=date(2026, 12, 31))
    live = make_document(session, title="Biểu phí 2027")
    live_version = make_version(session, live)
    make_chunk(session, live, live_version)

    where, params = compile_sql_expired(FilterBuilder(today=today).base(USER_RETAIL_STAFF))
    params["probe_query"] = fold_diacritics("thử")
    rows = session.execute(
        text(
            f"""
            SELECT DISTINCT ON (c.document_id) c.document_id, c.effective_to
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE {where}
              AND to_tsvector('simple', kb_unaccent(c.text))
                  @@ to_tsquery('simple', :probe_query)
            ORDER BY c.document_id, c.ordinal
            """
        ),
        params,
    ).all()

    found = {row.document_id for row in rows}
    assert gone in found, "the instrument that ceased is what the refusal names"
    assert live not in found, "and a current one can never be named as expired"
