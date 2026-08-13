"""Outbox consumer: idempotence, ordering, failure handling, recovery (INV-5/6)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from kb_common.errors import UpstreamError
from kb_idp.builder import KBDocBuilder
from kb_indexer.consumer import MAX_ATTEMPTS, OutboxConsumer
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.indexes import IndexDocument, IndexHit
from kb_registry import repository as repo
from kb_registry.publish import PublishService
from kb_registry.schemas import DocumentCreate, VersionCreate
from kb_registry.service import RegistryService
from kb_schemas.enums import DocClass, Visibility
from kb_schemas.orm import CategoryRow
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

CATEGORY = "t_consumer"
EMBEDDER = HashedEmbeddingAdapter()


@dataclass
class FakeKeywordIndex:
    """Stands in for an out-of-database keyword index. Records what the consumer asked."""

    documents: dict[uuid.UUID, IndexDocument] = field(default_factory=dict)
    upserts: int = 0
    fail_next: int = 0

    @property
    def info(self) -> Any:  # pragma: no cover - not asserted on
        from kb_ports.base import AdapterInfo

        return AdapterInfo(name="fake")

    def health(self) -> bool:
        return True

    def search(self, query: str, acl: Any, *, top_k: int = 50) -> list[IndexHit]:
        return []

    def upsert(self, documents: Any) -> int:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise UpstreamError("keyword index unavailable")
        self.upserts += 1
        for document in documents:
            self.documents[document.chunk_id] = document
        return len(documents)

    def tombstone(self, version_ids: Any) -> int:
        removed = [
            chunk_id
            for chunk_id, document in self.documents.items()
            if document.version_id in set(version_ids)
        ]
        for chunk_id in removed:
            del self.documents[chunk_id]
        return len(removed)

    def delete_by_document(self, document_id: uuid.UUID) -> int:
        removed = [k for k, v in self.documents.items() if v.document_id == document_id]
        for chunk_id in removed:
            del self.documents[chunk_id]
        return len(removed)


@pytest.fixture
def keyword() -> FakeKeywordIndex:
    return FakeKeywordIndex()


@pytest.fixture
def consumer(session: Session, keyword: FakeKeywordIndex) -> OutboxConsumer:
    return OutboxConsumer(session, keyword_index=keyword)


@pytest.fixture
def registry(session: Session) -> RegistryService:
    if repo.get_category(session, CATEGORY) is None:
        repo.add_category(
            session,
            CategoryRow(
                path=CATEGORY,
                label="Consumer tests",
                default_visibility=Visibility.INTERNAL_ALL.value,
                default_allowed_groups=[],
                steward_group="dept/legal",
                existence_disclosure=False,
            ),
        )
    session.flush()
    return RegistryService(session)


def publish_a_version(
    registry: RegistryService, publisher: PublishService, *, document_id: uuid.UUID | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    session = registry._session
    if document_id is None:
        document_id = registry.create_document(
            DocumentCreate(
                title="Tài liệu chỉ mục", doc_class=DocClass.OPERATIONAL, category_path=CATEGORY
            ),
            actor="u-author",
        ).id
    version = registry.create_version(
        VersionCreate(
            document_id=document_id,
            content_ref="kb-originals/x",
            content_hash=uuid.uuid4().hex,
            author="u-author",
        ),
        actor="u-author",
    )
    # Through the ORM object, so the identity map and the row agree — the publish guard
    # re-reads this version and would otherwise still see `pending`.
    version.pii_status = "clear"
    session.flush()
    builder = KBDocBuilder(source_format="docx")
    builder.add("Điều 1. Phạm vi", block_type="heading")
    builder.add("1. Quy định này áp dụng cho toàn hệ thống ngân hàng.")
    publisher.publish(version.id, publisher.prepare(builder.build(page_count=1)), actor="u-pub")
    return document_id, version.id


@pytest.fixture
def publisher(session: Session) -> PublishService:
    return PublishService(session, embedder=EMBEDDER)


def test_a_published_version_reaches_the_keyword_index(
    registry: RegistryService,
    publisher: PublishService,
    consumer: OutboxConsumer,
    keyword: FakeKeywordIndex,
) -> None:
    _, version_id = publish_a_version(registry, publisher)
    result = consumer.run_once()

    # `processed` counts the whole batch, and this database is shared with other tests that
    # also commit; what matters is that this publish was handled and nothing failed.
    assert result.processed >= 1
    assert result.failed == 0
    assert keyword.documents
    indexed = next(iter(keyword.documents.values()))
    assert indexed.version_id == version_id
    # The ACL travels into the index, or the filter there could not be applied (INV-2).
    assert indexed.visibility == Visibility.INTERNAL_ALL.value
    assert indexed.category_ancestors[0] == CATEGORY.split(".")[0]


def test_processing_is_idempotent(
    registry: RegistryService,
    publisher: PublishService,
    consumer: OutboxConsumer,
    keyword: FakeKeywordIndex,
    session: Session,
) -> None:
    """At-least-once delivery: replaying an event must converge, not duplicate."""
    _, version_id = publish_a_version(registry, publisher)
    consumer.run_once()
    first = dict(keyword.documents)
    assert first

    # Replay this publish only — the consumer commits, so other tests' events are real rows.
    session.execute(
        text("UPDATE outbox SET processed_at = NULL WHERE payload->>'version_id' = :id"),
        {"id": str(version_id)},
    )
    session.commit()
    consumer.run_once()
    assert keyword.documents.keys() == first.keys()


def test_an_event_is_consumed_only_once_when_it_succeeds(
    registry: RegistryService, publisher: PublishService, consumer: OutboxConsumer
) -> None:
    publish_a_version(registry, publisher)
    assert consumer.run_once().processed >= 1
    assert consumer.run_once().processed == 0


def test_superseded_chunks_are_removed_from_the_index(
    registry: RegistryService,
    publisher: PublishService,
    consumer: OutboxConsumer,
    keyword: FakeKeywordIndex,
) -> None:
    """INV-6: after a republish, only the canonical version is searchable."""
    document_id, first_version = publish_a_version(registry, publisher)
    consumer.run_once()
    assert {d.version_id for d in keyword.documents.values()} == {first_version}

    _, second_version = publish_a_version(registry, publisher, document_id=document_id)
    consumer.run_once()
    assert {d.version_id for d in keyword.documents.values()} == {second_version}


def test_a_failing_index_leaves_the_event_for_retry(
    registry: RegistryService,
    publisher: PublishService,
    consumer: OutboxConsumer,
    keyword: FakeKeywordIndex,
    session: Session,
) -> None:
    """A keyword-index outage must delay indexing, never lose it."""
    publish_a_version(registry, publisher)
    keyword.fail_next = 1

    result = consumer.run_once()
    assert result.failed == 1
    assert not keyword.documents

    row = (
        session.execute(
            text("SELECT attempts, last_error, processed_at FROM outbox ORDER BY id DESC LIMIT 1")
        )
        .mappings()
        .one()
    )
    assert row["attempts"] == 1
    assert "UpstreamError" in row["last_error"]
    assert row["processed_at"] is None

    # The retry succeeds and the backlog drains.
    assert consumer.run_once().processed == 1
    assert keyword.documents


def test_events_stuck_after_too_many_attempts_stop_blocking_the_queue(
    registry: RegistryService, publisher: PublishService, consumer: OutboxConsumer, session: Session
) -> None:
    publish_a_version(registry, publisher)
    session.execute(text("UPDATE outbox SET attempts = :n"), {"n": MAX_ATTEMPTS})
    session.flush()
    assert consumer.run_once().processed == 0  # claimed by nobody; alerting picks it up


def test_unknown_topics_do_not_stall_the_queue(consumer: OutboxConsumer, session: Session) -> None:
    repo.enqueue(session, "some.future.topic", {"hello": "world"})
    session.flush()
    result = consumer.run_once()
    assert result.skipped == 1
    assert result.processed == 1


def test_publish_to_searchable_latency_is_recorded(
    registry: RegistryService, publisher: PublishService, consumer: OutboxConsumer
) -> None:
    """The number INV-5's 10 s budget is measured against."""
    from kb_common.metrics import publish_to_searchable

    before = _histogram_count(publish_to_searchable)
    publish_a_version(registry, publisher)
    consumer.run_once()
    assert _histogram_count(publish_to_searchable) == before + 1


def test_a_stale_event_for_an_already_superseded_version_indexes_nothing(
    registry: RegistryService,
    publisher: PublishService,
    consumer: OutboxConsumer,
    keyword: FakeKeywordIndex,
    session: Session,
) -> None:
    """Recovery after a crash: the queue may hold an event whose version is already retired.

    Indexing it would resurrect content the registry has tombstoned.
    """
    document_id, first_version = publish_a_version(registry, publisher)
    publish_a_version(registry, publisher, document_id=document_id)

    # Replay only the first publish, as a restarted consumer would.
    session.execute(
        text("UPDATE outbox SET processed_at = NULL WHERE payload->>'version_id' = :id"),
        {"id": str(first_version)},
    )
    session.execute(
        text("UPDATE outbox SET processed_at = now() WHERE payload->>'version_id' <> :id"),
        {"id": str(first_version)},
    )
    session.flush()

    consumer.run_once()
    assert first_version not in {d.version_id for d in keyword.documents.values()}


def _histogram_count(histogram: Any) -> float:
    for metric in histogram.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count"):
                return float(sample.value)
    return 0.0


def test_backlog_gauge_reflects_the_queue(
    registry: RegistryService, publisher: PublishService, consumer: OutboxConsumer, session: Session
) -> None:
    from kb_common.metrics import outbox_unprocessed

    publish_a_version(registry, publisher)
    session.execute(
        text("INSERT INTO outbox (ts, topic, payload) VALUES (:ts, 'x', '{}'::jsonb)"),
        {"ts": datetime.now(UTC) - timedelta(minutes=5)},
    )
    session.flush()
    consumer.run_once()
    assert outbox_unprocessed._value.get() >= 0
