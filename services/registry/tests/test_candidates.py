"""Gate 1: which clauses might be about the same rule (M9d, ADR-0033).

Two channels that fail differently, so the tests are mostly about the failure each covers for
the other: the subject key misses a reworded subject, and the embedding admits anything sharing
boilerplate. A pair either channel finds is a candidate; a pair both find is the strongest
signal this gate has.
"""

from __future__ import annotations

import uuid

import pytest
from kb_registry.candidates import find_candidates
from kb_registry.testing import make_chunk, make_document, make_version
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


#: Deterministic stand-in vectors. The hashed embedder is what dev and CI retrieve with, so a
#: literal vector here keeps the test about the gate rather than about a model.
def _vector(seed: float) -> str:
    return "[" + ",".join([f"{seed:.6f}"] * 1024) + "]"


def _clause(
    session: Session,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    subject: str | None,
    ordinal: int = 0,
    seed: float = 0.1,
    body: str = "Nội dung điều khoản.",
    effective_from: str | None = None,
    effective_to: str | None = None,
) -> uuid.UUID:
    chunk_id = make_chunk(session, document_id, version_id, ordinal=ordinal)
    session.execute(
        text(
            "UPDATE chunks SET subject_key = :s, text = :t, embedding = CAST(:e AS vector), "
            "section_path = :p, effective_from = CAST(:ef AS date), "
            "effective_to = CAST(:et AS date) WHERE id = :id"
        ),
        {
            "s": subject,
            "t": body,
            "e": _vector(seed),
            "p": f"Điều {ordinal + 1}",
            "ef": effective_from,
            "et": effective_to,
            "id": chunk_id,
        },
    )
    session.flush()
    return chunk_id


@pytest.fixture
def rate_clause(session: Session) -> uuid.UUID:
    document = make_document(session, title="Thông tư 2023")
    version = make_version(session, document)
    return _clause(session, document, version, subject="ca hang khach lai nhan suat vay", seed=0.1)


def test_a_clause_with_the_same_subject_elsewhere_is_a_candidate(
    session: Session, rate_clause: uuid.UUID
) -> None:
    """The pair the whole funnel exists for: same rule, two instruments, no edge between them."""
    other = make_document(session, title="Thông tư 2026")
    version = make_version(session, other)
    match = _clause(session, other, version, subject="ca hang khach lai nhan suat vay", seed=0.9)

    found = find_candidates(session, rate_clause)

    assert match in {item.chunk_id for item in found}


def test_the_subject_channel_catches_what_the_vector_channel_misses(
    session: Session, rate_clause: uuid.UUID
) -> None:
    """Different wording, different vector, same subject. Without the lexical channel this pair
    is a supersession nobody ever notices."""
    other = make_document(session, title="Quyết định khác")
    version = make_version(session, other)
    match = _clause(
        session,
        other,
        version,
        subject="ca hang khach lai nhan suat vay",
        seed=-0.9,  # deliberately far away in vector space
        body="Cách diễn đạt hoàn toàn khác về cùng một quy định.",
    )

    found = {item.chunk_id: item for item in find_candidates(session, rate_clause)}

    assert match in found
    assert found[match].channel == "subject"


def test_the_vector_channel_catches_what_the_subject_channel_misses(
    session: Session, rate_clause: uuid.UUID
) -> None:
    """A heading that shares no words but a clause that reads alike — the case a bare keyword
    gate drops."""
    other = make_document(session, title="Biểu phí")
    version = make_version(session, other)
    match = _clause(session, other, version, subject="phi bieu dich vu", seed=0.1)

    found = {item.chunk_id: item for item in find_candidates(session, rate_clause)}

    assert match in found
    assert found[match].channel == "vector"


def test_a_pair_both_channels_find_outranks_one_either_found_alone(
    session: Session, rate_clause: uuid.UUID
) -> None:
    """The subject matches in words *and* the clause reads alike, which is the strongest signal
    this gate has — so fusion has to reward it."""
    other = make_document(session, title="Thông tư 2026")
    version = make_version(session, other)
    both = _clause(
        session, other, version, subject="ca hang khach lai nhan suat vay", seed=0.1, ordinal=0
    )
    vector_only = _clause(session, other, version, subject="phi bieu dich vu", seed=0.1, ordinal=1)

    found = find_candidates(session, rate_clause)
    by_id = {item.chunk_id: item for item in found}

    assert by_id[both].channel == "both"
    assert by_id[both].score > by_id[vector_only].score
    assert found[0].chunk_id == both


def test_a_clause_in_the_same_document_is_never_a_candidate(
    session: Session, rate_clause: uuid.UUID
) -> None:
    """A clause superseding another in the same instrument is a renumbering, which is the merge
    flow's question."""
    same_document = session.execute(
        text("SELECT document_id FROM chunks WHERE id = :id"), {"id": rate_clause}
    ).scalar()
    assert same_document is not None
    version = make_version(session, same_document, canonical=False)
    sibling = _clause(
        session, same_document, version, subject="ca hang khach lai nhan suat vay", ordinal=5
    )

    assert sibling not in {item.chunk_id for item in find_candidates(session, rate_clause)}


def test_clauses_that_were_never_in_force_together_are_dropped(
    session: Session, rate_clause: uuid.UUID
) -> None:
    """Gate 2 inside the funnel: the second was not in force to replace anything while the
    first applied."""
    session.execute(
        text(
            "UPDATE chunks SET effective_from = DATE '2020-01-01', "
            "effective_to = DATE '2021-12-31' WHERE id = :id"
        ),
        {"id": rate_clause},
    )
    other = make_document(session, title="Thông tư sau này")
    version = make_version(session, other)
    later = _clause(
        session,
        other,
        version,
        subject="ca hang khach lai nhan suat vay",
        seed=0.1,
        effective_from="2022-01-01",
    )
    session.flush()

    assert later not in {item.chunk_id for item in find_candidates(session, rate_clause)}


def test_one_shared_token_is_not_a_subject_match(session: Session, rate_clause: uuid.UUID) -> None:
    """The key is already boilerplate-stripped, so a single token in common is coincidence. It
    may still arrive through the vector channel, which is the point of having two."""
    other = make_document(session, title="Không liên quan")
    version = make_version(session, other)
    thin = _clause(session, other, version, subject="vay bao lanh khac", seed=-0.9)

    found = {item.chunk_id: item for item in find_candidates(session, rate_clause)}
    assert thin not in found


def test_path_b_relaxes_the_subject_gate_within_one_document(
    session: Session, rate_clause: uuid.UUID
) -> None:
    """An edge already says the two documents are related, so the expensive part is done and a
    single shared token is enough to pair the clauses (ADR-0033, Path B)."""
    other = make_document(session, title="Thông tư sửa đổi")
    version = make_version(session, other)
    thin = _clause(session, other, version, subject="vay bao lanh khac", seed=-0.9)

    assert thin not in {item.chunk_id for item in find_candidates(session, rate_clause)}
    scoped = find_candidates(session, rate_clause, within_document=other)
    assert thin in {item.chunk_id for item in scoped}


def test_a_missing_chunk_yields_nothing(session: Session) -> None:
    assert find_candidates(session, uuid.uuid4()) == []
