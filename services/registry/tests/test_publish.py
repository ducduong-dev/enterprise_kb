"""Publish transaction: atomicity, guards, and what a reader can observe (INV-5/6/7/8)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from kb_common.audit import AuditAction, InMemoryAuditSink
from kb_common.errors import Conflict, GateBlocked
from kb_idp.builder import KBDocBuilder
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_registry import repository as repo
from kb_registry.publish import Approval, PublishService, is_superseded
from kb_registry.schemas import DocumentCreate, VersionCreate
from kb_registry.service import RegistryService
from kb_schemas.enums import DocClass, RefType, Visibility
from kb_schemas.kbdoc import KBDoc
from kb_schemas.orm import CategoryRow, DocumentRefRow
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

CATEGORY = "t_pub"
EMBEDDER = HashedEmbeddingAdapter()


@pytest.fixture
def audit() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def registry(session: Session, audit: InMemoryAuditSink) -> RegistryService:
    if repo.get_category(session, CATEGORY) is None:
        repo.add_category(
            session,
            CategoryRow(
                path=CATEGORY,
                label="Publish tests",
                default_visibility=Visibility.INTERNAL_ALL.value,
                default_allowed_groups=[],
                steward_group="dept/legal",
                existence_disclosure=False,
            ),
        )
    session.flush()
    return RegistryService(session, audit=audit)


@pytest.fixture
def publisher(session: Session, audit: InMemoryAuditSink) -> PublishService:
    return PublishService(session, embedder=EMBEDDER, audit=audit)


def kbdoc(text_body: str = "Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 8%.") -> KBDoc:
    builder = KBDocBuilder(source_format="docx")
    builder.add("Điều 6. Tỷ lệ an toàn vốn", block_type="heading")
    builder.add(f"1. {text_body}")
    builder.add("2. Cách xác định thực hiện theo hướng dẫn của Ngân hàng Nhà nước.")
    return builder.build(page_count=1)


def make_version(
    registry: RegistryService,
    *,
    doc_class: DocClass = DocClass.OPERATIONAL,
    pii: str = "clear",
    author: str = "u-author",
    document_id: uuid.UUID | None = None,
    content_hash: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    session = registry._session
    if document_id is None:
        document = registry.create_document(
            DocumentCreate(
                title="Tài liệu thử nghiệm",
                doc_class=doc_class,
                category_path=CATEGORY,
            ),
            actor=author,
        )
        document_id = document.id
    version = registry.create_version(
        VersionCreate(
            document_id=document_id,
            content_ref="kb-originals/x",
            content_hash=content_hash or uuid.uuid4().hex,
            author=author,
        ),
        actor=author,
    )
    session.execute(
        text("UPDATE document_versions SET pii_status = :pii WHERE id = :id"),
        {"pii": pii, "id": version.id},
    )
    session.flush()
    return document_id, version.id


# ----------------------------------------------------------------------------- guards


def test_a_pending_pii_state_blocks_publication(
    registry: RegistryService, publisher: PublishService
) -> None:
    """INV-7 fails closed: pending is not clear."""
    _, version_id = make_version(registry, pii="pending")
    prepared = publisher.prepare(kbdoc())
    with pytest.raises(GateBlocked) as exc:
        publisher.publish(version_id, prepared, actor="u-steward")
    assert exc.value.detail["invariant"] == "INV-7"


def test_a_blocked_pii_state_blocks_publication(
    registry: RegistryService, publisher: PublishService
) -> None:
    _, version_id = make_version(registry, pii="blocked")
    with pytest.raises(GateBlocked):
        publisher.publish(version_id, publisher.prepare(kbdoc()), actor="u-steward")


def test_an_audited_override_may_publish(
    registry: RegistryService, publisher: PublishService
) -> None:
    """Reaching `overridden` already required a human, a justification and an audit record."""
    _, version_id = make_version(registry, pii="overridden")
    result = publisher.publish(version_id, publisher.prepare(kbdoc()), actor="u-steward")
    assert result.chunks_written > 0


@pytest.mark.parametrize("doc_class", [DocClass.REGULATORY, DocClass.CUSTOMER_FACING])
def test_regulated_classes_never_auto_publish(
    registry: RegistryService, publisher: PublishService, doc_class: DocClass
) -> None:
    """INV-8, enforced in code rather than configuration."""
    _, version_id = make_version(registry, doc_class=doc_class)
    with pytest.raises(GateBlocked) as exc:
        publisher.publish(version_id, publisher.prepare(kbdoc()), actor="u-robot")
    assert exc.value.detail["invariant"] == "INV-8"


def test_the_approver_may_not_be_the_author(
    registry: RegistryService, publisher: PublishService
) -> None:
    _, version_id = make_version(registry, doc_class=DocClass.REGULATORY, author="u-author")
    with pytest.raises(GateBlocked, match="four-eyes"):
        publisher.publish(
            version_id,
            publisher.prepare(kbdoc()),
            actor="u-author",
            approval=Approval(approver="u-author"),
        )


def test_a_second_pair_of_eyes_unblocks_a_regulated_document(
    registry: RegistryService, publisher: PublishService
) -> None:
    _, version_id = make_version(registry, doc_class=DocClass.REGULATORY, author="u-author")
    result = publisher.publish(
        version_id,
        publisher.prepare(kbdoc()),
        actor="u-approver",
        approval=Approval(approver="u-approver", note="Legal cell sign-off"),
    )
    assert result.chunks_written > 0


def test_publishing_a_version_twice_is_refused(
    registry: RegistryService, publisher: PublishService
) -> None:
    _, version_id = make_version(registry)
    publisher.publish(version_id, publisher.prepare(kbdoc()), actor="u-steward")
    with pytest.raises(Conflict):
        publisher.publish(version_id, publisher.prepare(kbdoc()), actor="u-steward")


def test_a_document_with_no_chunks_cannot_be_published(publisher: PublishService) -> None:
    empty = KBDocBuilder(source_format="txt").build(page_count=1)
    with pytest.raises(Conflict):
        publisher.prepare(empty)


# -------------------------------------------------------------------------- atomicity


def test_publish_flips_canonical_and_writes_chunks_together(
    registry: RegistryService, publisher: PublishService, session: Session
) -> None:
    document_id, version_id = make_version(registry)
    result = publisher.publish(version_id, publisher.prepare(kbdoc()), actor="u-steward")

    document = repo.get_document(session, document_id)
    version = repo.get_version(session, version_id)
    assert document is not None and version is not None
    assert document.status == "published"
    assert document.canonical_version_id == version_id
    assert version.is_canonical is True
    assert version.published_at is not None

    rows = (
        session.execute(
            text(
                "SELECT visibility, doc_status, tombstoned, embedding IS NOT NULL AS has_vector "
                "FROM chunks WHERE version_id = :id"
            ),
            {"id": version_id},
        )
        .mappings()
        .all()
    )
    assert len(rows) == result.chunks_written
    assert all(row["doc_status"] == "published" for row in rows)
    assert all(not row["tombstoned"] for row in rows)
    assert all(row["has_vector"] for row in rows)
    # The ACL travels with the chunk so the filter can live in the query (ADR-0003).
    assert all(row["visibility"] == document.visibility for row in rows)


def test_a_new_version_supersedes_the_old_one_atomically(
    registry: RegistryService, publisher: PublishService, session: Session
) -> None:
    """INV-6: after the flip, only the new version's chunks are retrievable."""
    document_id, first_version = make_version(registry)
    publisher.publish(first_version, publisher.prepare(kbdoc()), actor="u-steward")

    _, second_version = make_version(registry, document_id=document_id)
    result = publisher.publish(
        second_version,
        publisher.prepare(kbdoc("Tỷ lệ an toàn vốn tối thiểu được nâng lên 10%.")),
        actor="u-steward",
    )

    assert result.previous_version_id == first_version
    assert result.chunks_tombstoned > 0

    live = (
        session.execute(
            text("SELECT version_id FROM chunks WHERE NOT tombstoned AND document_id = :id"),
            {"id": document_id},
        )
        .scalars()
        .all()
    )
    assert set(live) == {second_version}

    old = repo.get_version(session, first_version)
    assert old is not None and old.is_canonical is False


def test_publish_emits_exactly_one_outbox_event(
    registry: RegistryService, publisher: PublishService, session: Session
) -> None:
    """The event and the state change commit together — that is what INV-5 buys."""
    _, version_id = make_version(registry)
    result = publisher.publish(version_id, publisher.prepare(kbdoc()), actor="u-steward")

    events = (
        session.execute(
            text(
                "SELECT topic, payload, processed_at FROM outbox "
                "WHERE payload->>'version_id' = :version_id"
            ),
            {"version_id": str(version_id)},
        )
        .mappings()
        .all()
    )
    assert len(events) == 1
    assert events[0]["topic"] == "registry.published"
    assert events[0]["processed_at"] is None
    assert events[0]["payload"]["chunk_count"] == result.chunks_written


def test_a_failed_publish_leaves_nothing_behind(
    registry: RegistryService, publisher: PublishService, session: Session
) -> None:
    """Rolling back must take the chunks, the flip and the outbox event with it."""
    document_id, version_id = make_version(registry)
    prepared = publisher.prepare(kbdoc())
    publisher.publish(version_id, prepared, actor="u-steward")
    session.rollback()

    assert (
        session.execute(
            text("SELECT count(*) FROM chunks WHERE version_id = :id"), {"id": version_id}
        ).scalar()
        == 0
    )
    assert (
        session.execute(
            text("SELECT count(*) FROM outbox WHERE payload->>'version_id' = :id"),
            {"id": str(version_id)},
        ).scalar()
        == 0
    )
    assert repo.get_document(session, document_id) is None


def test_publish_is_audited_with_the_approval(
    registry: RegistryService, publisher: PublishService, audit: InMemoryAuditSink
) -> None:
    _, version_id = make_version(registry, doc_class=DocClass.REGULATORY, author="u-author")
    publisher.publish(
        version_id,
        publisher.prepare(kbdoc()),
        actor="u-approver",
        approval=Approval(approver="u-approver"),
    )
    (record,) = audit.by_action(AuditAction.PUBLISH)
    assert record.actor == "u-approver"
    assert record.detail["approver"] == "u-approver"
    assert record.detail["doc_class"] == DocClass.REGULATORY.value


# ------------------------------------------------------------------------ graph serving


def test_publish_rebuilds_the_serving_projection_with_the_targets_acl(
    registry: RegistryService, publisher: PublishService, session: Session
) -> None:
    target_id, target_version = make_version(registry)
    publisher.publish(target_version, publisher.prepare(kbdoc()), actor="u-steward")

    source_id, source_version = make_version(registry)
    session.add(
        DocumentRefRow(
            id=uuid.uuid4(),
            src_document_id=source_id,
            dst_document_id=target_id,
            ref_type=RefType.IMPLEMENTS.value,
            detected_by="idp",
            created_at=datetime.now(UTC),
        )
    )
    session.flush()

    result = publisher.publish(source_version, publisher.prepare(kbdoc()), actor="u-steward")
    assert result.edges_rebuilt == 1

    row = (
        session.execute(
            text(
                "SELECT dst_visibility, dst_allowed_groups, dst_canonical_version_id, dst_summary "
                "FROM graph_serving WHERE src_document_id = :src"
            ),
            {"src": source_id},
        )
        .mappings()
        .one()
    )
    # The target's ACL travels with the edge, so expansion filters identically (INV-10).
    assert row["dst_visibility"] == "internal_all"
    assert row["dst_canonical_version_id"] == target_version
    assert row["dst_summary"]


def test_an_unconsolidated_amendment_marks_the_target_superseded(
    registry: RegistryService, publisher: PublishService, session: Session
) -> None:
    target_id, target_version = make_version(registry)
    publisher.publish(target_version, publisher.prepare(kbdoc()), actor="u-steward")
    assert not is_superseded(session, target_id)

    amender_id, amender_version = make_version(registry)
    session.add(
        DocumentRefRow(
            id=uuid.uuid4(),
            src_document_id=amender_id,
            dst_document_id=target_id,
            ref_type=RefType.AMENDS.value,
            detected_by="idp",
            created_at=datetime.now(UTC),
        )
    )
    publisher.publish(amender_version, publisher.prepare(kbdoc()), actor="u-steward")

    # Until the consolidation is approved (M5), the target's text is still canonical but a
    # published instrument amends it — quoting it without a warning would mislead.
    assert is_superseded(session, target_id)


def test_a_concurrent_publish_of_the_same_document_is_serialized(
    registry: RegistryService, publisher: PublishService, session: Session
) -> None:
    """Two publishes racing on one document must not both replace the same canonical version.

    The lock is what makes the check-then-flip sequence safe; without it both transactions
    would read the same "previous canonical" and one would silently lose its tombstoning.
    """
    from kb_common.config import get_settings
    from kb_common.db import create_db_engine
    from sqlalchemy.exc import OperationalError

    document_id, version_id = make_version(registry)
    session.commit()

    publisher.publish(version_id, publisher.prepare(kbdoc()), actor="u-steward")

    other = create_db_engine(get_settings().db)
    with other.connect() as connection:
        connection.execute(text("SET lock_timeout = '250ms'"))
        with pytest.raises(OperationalError):
            connection.execute(
                text("SELECT id FROM documents WHERE id = :id FOR UPDATE"), {"id": document_id}
            )
    other.dispose()
    session.rollback()
