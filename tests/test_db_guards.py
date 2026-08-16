"""Database-level backstops for the invariants (see 0001_initial_schema.py).

Application code enforces these first. These tests prove that a bug in *any* service still
cannot corrupt the registry.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def _make_document(session: Session, **overrides: object) -> uuid.UUID:
    doc_id = uuid.uuid4()
    session.execute(
        text(
            """
            INSERT INTO documents (id, title, doc_class, category_path, visibility,
                                   allowed_groups, status, created_at, updated_at)
            VALUES (:id, 'Guard test', 'operational', 'internal.procedures', 'internal_all',
                    '{}', 'draft', now(), now())
            """
        ),
        {"id": doc_id, **overrides},
    )
    return doc_id


def _make_version(
    session: Session, doc_id: uuid.UUID, *, pii: str = "clear", canonical: bool = False
) -> uuid.UUID:
    version_id = uuid.uuid4()
    session.execute(
        text(
            """
            INSERT INTO document_versions (id, document_id, content_ref, content_hash,
                                           source_type, author, pii_status, is_canonical,
                                           retention_until, created_at)
            VALUES (:id, :doc, 'kb-originals/x', 'deadbeef', 'upload', 'tester', :pii,
                    :canonical, :retention, :created)
            """
        ),
        {
            "id": version_id,
            "doc": doc_id,
            "pii": pii,
            "canonical": canonical,
            "retention": date(2036, 12, 31),
            "created": datetime.now(UTC),
        },
    )
    return version_id


def test_version_content_cannot_be_rewritten(session: Session) -> None:
    """INV-9: versions are immutable."""
    doc_id = _make_document(session)
    version_id = _make_version(session, doc_id)
    with pytest.raises(DBAPIError, match="immutable"):
        session.execute(
            text("UPDATE document_versions SET content_hash = 'tampered' WHERE id = :id"),
            {"id": version_id},
        )


def test_canonical_flag_may_still_flip(session: Session) -> None:
    """Immutability must not block the publish transaction itself."""
    doc_id = _make_document(session)
    version_id = _make_version(session, doc_id)
    session.execute(
        text("UPDATE document_versions SET is_canonical = TRUE WHERE id = :id"),
        {"id": version_id},
    )
    canonical = session.execute(
        text("SELECT is_canonical FROM document_versions WHERE id = :id"), {"id": version_id}
    ).scalar()
    assert canonical is True


def test_only_one_canonical_version_per_document(session: Session) -> None:
    """INV-5/INV-6: exactly one canonical version, enforced by a partial unique index."""
    doc_id = _make_document(session)
    _make_version(session, doc_id, canonical=True)
    with pytest.raises(IntegrityError):
        _make_version(session, doc_id, canonical=True)
        session.flush()


def test_pending_pii_cannot_become_canonical(session: Session) -> None:
    """INV-7: the PII gate fails closed, including at the database."""
    doc_id = _make_document(session)
    with pytest.raises(DBAPIError, match="pii_status"):
        _make_version(session, doc_id, pii="pending", canonical=True)


def test_blocked_pii_cannot_become_canonical(session: Session) -> None:
    doc_id = _make_document(session)
    version_id = _make_version(session, doc_id, pii="blocked")
    with pytest.raises(DBAPIError, match="pii_status"):
        session.execute(
            text("UPDATE document_versions SET is_canonical = TRUE WHERE id = :id"),
            {"id": version_id},
        )


def test_overridden_pii_may_publish(session: Session) -> None:
    """An override is a human decision with an audit record — the DB allows it, the service
    is what demands the justification."""
    doc_id = _make_document(session)
    version_id = _make_version(session, doc_id, pii="overridden", canonical=True)
    assert version_id is not None


def test_versions_cannot_be_deleted_without_an_authorized_purge(session: Session) -> None:
    """INV-9: retention hold."""
    doc_id = _make_document(session)
    version_id = _make_version(session, doc_id)
    with pytest.raises(DBAPIError, match="authorized purge"):
        session.execute(text("DELETE FROM document_versions WHERE id = :id"), {"id": version_id})


def test_purge_still_refuses_while_under_retention(session: Session) -> None:
    doc_id = _make_document(session)
    version_id = _make_version(session, doc_id)
    session.execute(text("SET LOCAL kb.purge_authorized = 'on'"))
    with pytest.raises(DBAPIError, match="retention hold"):
        session.execute(text("DELETE FROM document_versions WHERE id = :id"), {"id": version_id})


def test_restricted_documents_require_a_group(session: Session) -> None:
    """A restricted document with no groups would be visible to nobody — or, if a filter bug
    ever treated an empty list as "no constraint", to everybody."""
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                """
                INSERT INTO documents (id, title, doc_class, category_path, visibility,
                                       allowed_groups, status, created_at, updated_at)
                VALUES (:id, 'Bad ACL', 'operational', 'internal.procedures', 'restricted',
                        '{}', 'draft', now(), now())
                """
            ),
            {"id": uuid.uuid4()},
        )
        session.flush()


def test_reference_edges_cannot_be_self_loops(session: Session) -> None:
    doc_id = _make_document(session)
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                """
                INSERT INTO document_refs (id, src_document_id, dst_document_id, ref_type,
                                           created_at)
                VALUES (:id, :doc, :doc, 'cites', now())
                """
            ),
            {"id": uuid.uuid4(), "doc": doc_id},
        )
        session.flush()


# ------------------------------------------------------------------ expiry ledger (ADR-0030)


def _expire(
    session: Session,
    doc_id: uuid.UUID,
    *,
    basis: str = "steward",
    state: str = "confirmed",
    anchors: list[str] | None = None,
    source: uuid.UUID | None = None,
    closed: bool = False,
) -> uuid.UUID:
    row_id = uuid.uuid4()
    session.execute(
        text(
            """
            INSERT INTO document_expiry (id, document_id, effective_to, basis,
                                         source_document_id, anchors, evidence, state,
                                         detected_by, created_at, closed_at)
            VALUES (:id, :doc, DATE '2026-12-31', :basis, :source, CAST(:anchors AS TEXT[]),
                    'Thông tư này hết hiệu lực kể từ ngày 01/01/2027.', :state,
                    'tester', now(), :closed)
            """
        ),
        {
            "id": row_id,
            "doc": doc_id,
            "basis": basis,
            "source": source,
            "anchors": anchors,
            "state": state,
            "closed": datetime.now(UTC) if closed else None,
        },
    )
    return row_id


def test_one_open_expiry_per_scope(session: Session) -> None:
    """The open row is the current belief — so two of them for the same scope is not a
    disagreement to resolve at read time, it is a bug. A new decision closes its predecessor
    in the same transaction."""
    doc_id = _make_document(session)
    _expire(session, doc_id)
    with pytest.raises(IntegrityError):
        _expire(session, doc_id)
        session.flush()


def test_a_closed_row_does_not_block_the_row_that_replaced_it(session: Session) -> None:
    """The sequence a document accumulates over its life: proposed, confirmed, revoked and
    re-proposed with a corrected date."""
    doc_id = _make_document(session)
    _expire(session, doc_id, state="proposed", closed=True)
    _expire(session, doc_id, state="confirmed")
    session.flush()


def test_two_clauses_of_one_document_expire_independently(session: Session) -> None:
    """The partially expired document, which is the ordinary shape in this corpus: Vietnamese
    instruments are abrogated in pieces, so a row over Điều 12 and a row over Điều 40 are both
    legitimately open."""
    doc_id = _make_document(session)
    _expire(session, doc_id, anchors=["12.2"])
    _expire(session, doc_id, anchors=["40"])
    session.flush()


def test_a_whole_document_expiry_collides_with_itself_however_it_is_written(
    session: Session,
) -> None:
    """NULL anchors and an empty array are the same scope — the whole document — and must not
    slip past each other into two open rows."""
    doc_id = _make_document(session)
    _expire(session, doc_id, anchors=None)
    with pytest.raises(IntegrityError):
        _expire(session, doc_id, anchors=[])
        session.flush()


def test_only_an_attributed_basis_may_name_a_source_instrument(session: Session) -> None:
    """A `self_stated` sunset that points at another document attributes the decision to the
    wrong place, and the inspection screen would show a reader the wrong reason."""
    doc_id = _make_document(session)
    other_id = _make_document(session)
    with pytest.raises(IntegrityError):
        _expire(session, doc_id, basis="self_stated", source=other_id)
        session.flush()


def test_a_document_cannot_be_abrogated_by_itself(session: Session) -> None:
    doc_id = _make_document(session)
    with pytest.raises(IntegrityError):
        _expire(session, doc_id, basis="abrogated_by", source=doc_id)
        session.flush()


def test_an_unknown_state_is_refused(session: Session) -> None:
    """Only `confirmed` is served, so a state nothing recognises must not reach the table —
    a typo that reads as "not confirmed" everywhere would silently keep a rule in service."""
    doc_id = _make_document(session)
    with pytest.raises(IntegrityError):
        _expire(session, doc_id, state="aproved")
        session.flush()
