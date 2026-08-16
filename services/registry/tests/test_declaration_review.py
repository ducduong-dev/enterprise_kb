"""Confirming a declaration (M9c, ADR-0039/0040).

This is the only step in the declared path that changes what a reader sees, so the tests are
about exactly that: which clauses stop being served, what the answer can say instead, and what
happens on the days either side of the boundary.

The negative half matters as much. A declaration is a reading until somebody confirms it; a
reading that cannot be dated is refused rather than guessed; and a pointer that fails to resolve
must not take the expiry down with it, because the expiry is the half that keeps a repealed rule
out of an answer.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from kb_common.errors import Conflict, NotFound, ValidationError
from kb_registry.declarations import DeclarationReview
from kb_registry.expiry import ExpiryLedger
from kb_registry.schemas import DeclarationIn
from kb_registry.service import RegistryService
from kb_registry.supersession import ClauseSupersessions
from kb_registry.testing import chunk_dates, make_chunk, make_document, make_version
from kb_schemas.enums import DeclarationState
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

TAKES_EFFECT = date(2027, 1, 1)
LAST_DAY = date(2026, 12, 31)


def _clause(
    session: Session, document_id: uuid.UUID, version_id: uuid.UUID, anchor: str, ordinal: int
) -> None:
    make_chunk(session, document_id, version_id, ordinal=ordinal)
    session.execute(
        text(
            "UPDATE chunks SET anchor = :a, article = :n, section_path = :p "
            "WHERE document_id = :d AND ordinal = :o"
        ),
        {
            "a": anchor,
            "n": int(anchor.split(".")[0]),
            "p": f"Điều {anchor.split('.')[0]}"
            + (f" > Khoản {anchor.split('.')[1]}" if "." in anchor else ""),
            "d": document_id,
            "o": ordinal,
        },
    )


@pytest.fixture
def corpus(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    """An old circular with three clauses, and the instrument that supersedes part of it."""
    old = make_document(session, title="Thông tư 10/2022")
    old_version = make_version(session, old, effective_from=date(2022, 3, 15))
    for ordinal, anchor in enumerate(("12.1", "12.2", "40")):
        _clause(session, old, old_version, anchor, ordinal)

    new = make_document(session, title="Thông tư 15/2026")
    new_version = make_version(session, new, effective_from=TAKES_EFFECT)
    _clause(session, new, new_version, "7", 0)
    session.flush()
    return old, new


def _declare(session: Session, src: uuid.UUID, target: uuid.UUID, **kwargs: Any) -> uuid.UUID:
    registry = RegistryService(session)
    number = session.execute(
        text("SELECT coalesce(legal_number, 'X') FROM documents WHERE id = :d"), {"d": target}
    ).scalar()
    if number == "X":
        number = f"{uuid.uuid4().hex[:4]}/2022/TT-DECL"
        session.execute(
            text("UPDATE documents SET legal_number = :n WHERE id = :d"),
            {"n": number, "d": target},
        )
        session.flush()
    registry.record_declarations(
        src,
        [
            DeclarationIn(
                kind=kwargs.pop("kind", "replaces"),
                target_legal_number=str(number),
                evidence="Điều 7 Thông tư này thay thế khoản 2 Điều 12 Thông tư 10/2022.",
                **kwargs,
            )
        ],
    )
    from kb_registry import repository as repo

    return repo.declarations_from(session, src)[0].id


# ----------------------------------------------------------------------- what it applies


def test_confirming_ends_the_clause_and_names_its_replacement(
    session: Session, corpus: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Both halves of ADR-0040 in one act: the clause stops being served, and the answer can
    say what applies instead."""
    old, new = corpus
    declaration = _declare(
        session,
        new,
        old,
        target_anchors=["12.2"],
        replacement_anchors=["7"],
        effective_from=TAKES_EFFECT,
    )

    outcome = DeclarationReview(session).confirm(declaration, actor="steward@bank")

    assert outcome.state == DeclarationState.APPLIED.value
    assert outcome.pointers == 1
    assert chunk_dates(session, old) == [None, LAST_DAY, None], (
        "only khoản 2 ended; khoản 1 and Điều 40 are untouched"
    )
    pointer = ClauseSupersessions(session).served({old}, on=TAKES_EFFECT)
    row = pointer[(old, "Điều 12 > Khoản 2")]
    assert row.new_document_id == new
    assert row.new_section_path == "Điều 7"


def test_the_old_rule_applies_through_the_day_before(
    session: Session, corpus: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """One day either way is a day of a repealed rule served, or a day of a live rule hidden.
    Asserted on the serving predicate itself, not on the stored date."""
    old, new = corpus
    declaration = _declare(session, new, old, target_anchors=["12.2"], effective_from=TAKES_EFFECT)
    DeclarationReview(session).confirm(declaration, actor="steward@bank")

    def live_on(when: date) -> int:
        return (
            session.execute(
                text(
                    "SELECT count(*) FROM chunks WHERE document_id = :d AND anchor = '12.2' "
                    "AND (effective_to IS NULL OR effective_to >= :on)"
                ),
                {"d": old, "on": when},
            ).scalar()
            or 0
        )

    assert live_on(LAST_DAY) == 1
    assert live_on(TAKES_EFFECT) == 0


def test_the_pointer_is_not_in_force_before_the_replacement_is(
    session: Session, corpus: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """An `as_of` query before the new rule started still sees the old one, unflagged."""
    old, new = corpus
    declaration = _declare(
        session,
        new,
        old,
        target_anchors=["12.2"],
        replacement_anchors=["7"],
        effective_from=TAKES_EFFECT,
    )
    DeclarationReview(session).confirm(declaration, actor="steward@bank")

    assert ClauseSupersessions(session).served({old}, on=date(2026, 6, 1)) == {}


def test_an_abrogation_ends_a_clause_with_no_pointer(
    session: Session, corpus: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Nothing replaced it, and inventing a successor would be a lie the answer repeats."""
    old, new = corpus
    declaration = _declare(
        session,
        new,
        old,
        kind="abrogates",
        target_anchors=["12.2"],
        effective_from=TAKES_EFFECT,
    )

    outcome = DeclarationReview(session).confirm(declaration, actor="steward@bank")

    assert outcome.pointers == 0
    assert chunk_dates(session, old) == [None, LAST_DAY, None]


def test_a_whole_instrument_declaration_ends_the_document(
    session: Session, corpus: tuple[uuid.UUID, uuid.UUID]
) -> None:
    old, new = corpus
    declaration = _declare(session, new, old, effective_from=TAKES_EFFECT)

    DeclarationReview(session).confirm(declaration, actor="steward@bank")

    assert chunk_dates(session, old) == [LAST_DAY, LAST_DAY, LAST_DAY]
    assert ExpiryLedger(session).effective_to_for(old) == LAST_DAY


def test_the_declaring_instruments_date_stands_in_when_the_sentence_gave_none(
    session: Session, corpus: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """It is that instrument coming into force that ends the old rule, so the two are one fact
    read from two places."""
    old, new = corpus
    declaration = _declare(session, new, old, target_anchors=["12.2"])

    DeclarationReview(session).confirm(declaration, actor="steward@bank")

    assert chunk_dates(session, old) == [None, LAST_DAY, None]


# ------------------------------------------------------------------------ what it refuses


def test_a_declaration_with_no_date_anywhere_is_refused(session: Session) -> None:
    """A date nobody read is exactly what ADR-0029 exists to prevent, and here it would decide
    when a rule stopped applying."""
    old = make_document(session, title="Thông tư cũ")
    make_version(session, old)
    new = make_document(session, title="Thông tư mới")
    make_version(session, new, effective_from=None)
    session.flush()
    declaration = _declare(session, new, old, target_anchors=["12.2"])

    with pytest.raises(ValidationError, match="states no date"):
        DeclarationReview(session).confirm(declaration, actor="steward@bank")


def test_a_waiting_declaration_cannot_be_confirmed(session: Session) -> None:
    src = make_document(session, title="Thông tư đến trước")
    make_version(session, src, effective_from=TAKES_EFFECT)
    RegistryService(session).record_declarations(
        src,
        [
            DeclarationIn(
                kind="abrogates",
                target_legal_number="99/1999/TT-ABSENT",
                evidence="Bãi bỏ Thông tư 99/1999.",
            )
        ],
    )
    from kb_registry import repository as repo

    row = repo.declarations_from(session, src)[0]

    with pytest.raises(Conflict, match="waiting"):
        DeclarationReview(session).confirm(row.id, actor="steward@bank")


def test_confirming_twice_is_refused(session: Session, corpus: tuple[uuid.UUID, uuid.UUID]) -> None:
    old, new = corpus
    declaration = _declare(session, new, old, target_anchors=["12.2"], effective_from=TAKES_EFFECT)
    review = DeclarationReview(session)
    review.confirm(declaration, actor="steward@bank")

    with pytest.raises(Conflict, match="not open"):
        review.confirm(declaration, actor="steward@bank")


def test_a_missing_declaration_is_not_found(session: Session) -> None:
    with pytest.raises(NotFound):
        DeclarationReview(session).confirm(uuid.uuid4(), actor="steward@bank")


def test_rejecting_records_the_reason_and_applies_nothing(
    session: Session, corpus: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Kept rather than deleted: the detector reads the same sentence on the next pass, and a
    rejection nobody stored is a proposal that comes back every time."""
    old, new = corpus
    declaration = _declare(session, new, old, target_anchors=["12.2"], effective_from=TAKES_EFFECT)

    outcome = DeclarationReview(session).reject(
        declaration, actor="legal@bank", reason="đọc nhầm, điều khoản này vẫn còn hiệu lực"
    )

    assert outcome.state == DeclarationState.REJECTED.value
    assert chunk_dates(session, old) == [None, None, None]
    assert ExpiryLedger(session).open_row(old) is None


def test_an_applied_declaration_cannot_be_rejected(
    session: Session, corpus: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The expiry is the record now; withdrawing it is the ledger's act, where it leaves a
    trail somebody can read."""
    old, new = corpus
    declaration = _declare(session, new, old, target_anchors=["12.2"], effective_from=TAKES_EFFECT)
    review = DeclarationReview(session)
    review.confirm(declaration, actor="steward@bank")

    with pytest.raises(Conflict, match="withdraw the expiry"):
        review.reject(declaration, actor="legal@bank", reason="thay đổi ý kiến")


def test_a_pointer_that_does_not_resolve_still_ends_the_clause(
    session: Session, corpus: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The expiry is the half that keeps a repealed rule out of an answer, so a replacement
    anchor naming a clause that is not there must not take it down with it."""
    old, new = corpus
    declaration = _declare(
        session,
        new,
        old,
        target_anchors=["12.2"],
        replacement_anchors=["99"],
        effective_from=TAKES_EFFECT,
    )

    outcome = DeclarationReview(session).confirm(declaration, actor="steward@bank")

    assert outcome.pointers == 0
    assert chunk_dates(session, old) == [None, LAST_DAY, None]


def test_the_queue_is_one_declaring_document_at_a_time(
    session: Session, corpus: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A closing article declares a dozen changes from one paragraph, so they are confirmed
    together with the evidence shown once (ADR-0039)."""
    old, new = corpus
    _declare(session, new, old, target_anchors=["12.2"], effective_from=TAKES_EFFECT)

    queue = DeclarationReview(session).queue(new)
    assert [row.state for row in queue] == [DeclarationState.OPEN.value]
    assert queue[0].evidence
