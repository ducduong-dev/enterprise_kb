"""Publish consistency and the publish-to-searchable budget (M2 acceptance, INV-5/6).

Two properties, both stated in the plan's testing strategy:

* publish a revision → the old version's text becomes unreachable and the new one is
  searchable within the budget;
* kill the indexer mid-publish → outbox recovery leaves no divergence.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from kb_authz.fixtures import USER_RETAIL_STAFF
from kb_common.config import get_settings
from kb_idp.builder import KBDocBuilder
from kb_indexer.consumer import OutboxConsumer
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
from kb_ports.adapters.rerank import LexicalRerankAdapter
from kb_registry import repository as repo
from kb_registry.publish import PublishService
from kb_registry.schemas import DocumentCreate, VersionCreate
from kb_registry.service import RegistryService
from kb_retrieval_api.engine import RetrievalEngine
from kb_schemas.api import RetrieveRequest
from kb_schemas.enums import DocClass, Visibility
from kb_schemas.orm import CategoryRow
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

CATEGORY = "t_consistency"
EMBEDDER = HashedEmbeddingAdapter()


@pytest.fixture
def marker() -> str:
    """A token unique to one test.

    These tests commit — that is the point, they are about what other connections observe —
    so they must not read each other's leftovers. Searching for a per-test marker makes each
    assertion about this test's documents only.
    """
    return f"mk{uuid.uuid4().hex[:8]}"


def old_text(marker: str) -> str:
    return f"Tỷ lệ an toàn vốn tối thiểu {marker} theo bản cũ là 8 phần trăm."


def new_text(marker: str) -> str:
    return f"Tỷ lệ an toàn vốn tối thiểu {marker} theo bản mới là 10 phần trăm."


@pytest.fixture
def registry(session: Session) -> RegistryService:
    if repo.get_category(session, CATEGORY) is None:
        repo.add_category(
            session,
            CategoryRow(
                path=CATEGORY,
                label="Consistency tests",
                default_visibility=Visibility.INTERNAL_ALL.value,
                default_allowed_groups=[],
                steward_group="dept/legal",
                existence_disclosure=False,
            ),
        )
    session.flush()
    return RegistryService(session)


@pytest.fixture
def publisher(session: Session) -> PublishService:
    return PublishService(session, embedder=EMBEDDER)


@pytest.fixture
def funnel(session: Session) -> RetrievalEngine:
    return RetrievalEngine(
        session,
        keyword_index=PostgresFtsIndexAdapter(session),
        vector_index=PgVectorIndexAdapter(session),
        embedder=EMBEDDER,
        reranker=LexicalRerankAdapter(),
    )


def make_and_publish(
    registry: RegistryService,
    publisher: PublishService,
    body: str,
    *,
    document_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID, float]:
    session = registry._session
    if document_id is None:
        document_id = registry.create_document(
            DocumentCreate(
                title="Quy định thử nghiệm", doc_class=DocClass.OPERATIONAL, category_path=CATEGORY
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
    version.pii_status = "clear"
    session.flush()

    builder = KBDocBuilder(source_format="docx")
    builder.add("Điều 1. Tỷ lệ an toàn vốn", block_type="heading")
    builder.add(f"1. {body}")
    prepared = publisher.prepare(builder.build(page_count=1))

    started = time.perf_counter()
    publisher.publish(version.id, prepared, actor="u-steward")
    return document_id, version.id, started


def search(funnel: RetrievalEngine, query: str, *, marker: str | None = None) -> list[str]:
    """Retrieve as an ordinary user.

    Vector search has no similarity floor, so a top-k always comes back full — including
    chunks that merely happen to be the nearest neighbours of an unusual query. Assertions
    about *this* test's documents therefore filter to its marker; assertions about what the
    corpus contains do not.
    """
    response = funnel.retrieve(USER_RETAIL_STAFF, RetrieveRequest(query=query, top_k=20)).response
    texts = [chunk.text for chunk in response.chunks]
    return [text for text in texts if marker in text] if marker else texts


def test_a_published_revision_replaces_the_old_text_within_the_budget(
    registry: RegistryService, publisher: PublishService, funnel: RetrievalEngine, marker: str
) -> None:
    """INV-5: publish-to-searchable ≤ 10 s; INV-6: only the canonical version answers."""
    budget = get_settings().index_visibility_budget_seconds

    document_id, _, _ = make_and_publish(registry, publisher, old_text(marker))
    assert any("bản cũ" in text for text in search(funnel, marker, marker=marker))

    _, _, started = make_and_publish(registry, publisher, new_text(marker), document_id=document_id)
    elapsed = time.perf_counter() - started

    results = search(funnel, marker, marker=marker)
    assert any("bản mới" in text for text in results)
    assert not any("bản cũ" in text for text in results)
    assert elapsed <= budget, f"publish-to-searchable took {elapsed:.2f}s (budget {budget}s)"


def test_the_superseded_version_is_unreachable_by_any_query(
    registry: RegistryService, publisher: PublishService, funnel: RetrievalEngine, marker: str
) -> None:
    document_id, old_version, _ = make_and_publish(registry, publisher, old_text(marker))
    make_and_publish(registry, publisher, new_text(marker), document_id=document_id)

    for query in (marker, f"{marker} bản cũ", f"tỷ lệ an toàn vốn {marker}"):
        for chunk_text in search(funnel, query, marker=marker):
            assert "bản cũ" not in chunk_text
    # The version itself is retained — immutable and auditable — just not served (INV-9).
    assert repo.get_version(registry._session, old_version) is not None


def test_killing_the_indexer_mid_publish_leaves_no_divergence(
    registry: RegistryService,
    publisher: PublishService,
    funnel: RetrievalEngine,
    session: Session,
    marker: str,
) -> None:
    """The outbox is what makes a crash a delay rather than a divergence.

    Postgres is already consistent at commit; the keyword index catches up when the consumer
    restarts. Neither state can show the old text as current.
    """

    class DyingIndex:
        """Fails the first time, as a process killed mid-write would."""

        def __init__(self) -> None:
            self.documents: dict[uuid.UUID, Any] = {}
            self.crashed = False

        @property
        def info(self) -> Any:
            from kb_ports.base import AdapterInfo

            return AdapterInfo(name="dying")

        def health(self) -> bool:
            return True

        def search(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

        def upsert(self, documents: Any) -> int:
            if not self.crashed:
                self.crashed = True
                raise RuntimeError("indexer killed mid-write")
            for document in documents:
                self.documents[document.chunk_id] = document
            return len(documents)

        def tombstone(self, version_ids: Any) -> int:
            for chunk_id in [
                k for k, v in self.documents.items() if v.version_id in set(version_ids)
            ]:
                del self.documents[chunk_id]
            return 0

        def delete_by_document(self, document_id: uuid.UUID) -> int:
            return 0

    document_id, _, _ = make_and_publish(registry, publisher, old_text(marker))
    index = DyingIndex()
    consumer = OutboxConsumer(session, keyword_index=index)
    consumer.run_once()  # crashes on the first event

    make_and_publish(registry, publisher, new_text(marker), document_id=document_id)

    # Postgres never showed the old text as canonical, crash or no crash.
    results = search(funnel, marker, marker=marker)
    assert any("bản mới" in text for text in results)
    assert not any("bản cũ" in text for text in results)

    # And the queue drains on restart, converging the external index.
    for _ in range(5):
        if consumer.run_once().processed == 0:
            break
    pending = session.execute(
        text("SELECT count(*) FROM outbox WHERE processed_at IS NULL AND attempts < 10")
    ).scalar()
    assert pending == 0
    assert index.documents, "the keyword index caught up after the restart"


def test_a_reader_never_sees_the_new_pointer_with_the_old_chunks(
    registry: RegistryService, publisher: PublishService, session: Session, marker: str
) -> None:
    """The canonical flip and the chunk swap commit together, or not at all (INV-5)."""
    document_id, first_version, _ = make_and_publish(registry, publisher, old_text(marker))
    session.commit()

    from kb_common.db import create_db_engine

    observer = create_db_engine(get_settings().db)
    try:
        with Session(observer) as watcher:
            make_and_publish(registry, publisher, new_text(marker), document_id=document_id)
            # Mid-transaction, another connection still sees the *whole* previous state.
            pointer = watcher.execute(
                text("SELECT canonical_version_id FROM documents WHERE id = :id"),
                {"id": document_id},
            ).scalar()
            live = watcher.execute(
                text("SELECT count(*) FROM chunks WHERE version_id = :id AND NOT tombstoned"),
                {"id": first_version},
            ).scalar()
            assert pointer == first_version
            assert live > 0
    finally:
        observer.dispose()
        session.rollback()
