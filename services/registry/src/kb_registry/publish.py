"""The publish transaction (INV-5).

Publishing is the moment a document becomes something the bank *tells people*. Everything that
makes that safe happens here, and the ordering is deliberate:

1. **Outside the transaction**: chunk the document and embed it. Both are slow and call an
   external model; holding a serializable transaction open across them would turn a GPU
   hiccup into a registry-wide lock.
2. **Guards**, checked before *and* inside the transaction — before for a clean error, inside
   because a PII state or an approval can change between the two (TOCTOU).
3. **One serializable transaction**: canonical flip, old-chunk tombstoning, new-chunk insert,
   `graph_serving` rebuild, audit record, outbox event. Either every one of those is visible
   or none is. A reader can never see the new canonical pointer with the old chunks.

The outbox event drives the indexer: `graph_serving` summaries, the publish-to-searchable
metric, and any keyword backend that lives outside Postgres. With `pg_search` the keyword
index is written by this same transaction, so "published" and "searchable" are one instant
(ADR-0021) — the 10 s budget still applies to whatever else consumes the event.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kb_common.audit import AuditAction, AuditRecord, AuditSink
from kb_common.config import Settings, get_settings
from kb_common.db import affected_rows
from kb_common.errors import Conflict, GateBlocked, NotFound
from kb_common.logging import get_logger
from kb_indexer.chunker import Chunk, chunk_document
from kb_ports.models import EmbeddingPort
from kb_schemas.enums import (
    NO_AUTOMATION_CLASSES,
    PUBLISHABLE_PII_STATUSES,
    DocClass,
    DocStatus,
    PiiStatus,
)
from kb_schemas.kbdoc import KBDoc
from kb_schemas.orm import DocumentRow, DocumentVersionRow
from sqlalchemy import text
from sqlalchemy.orm import Session

from kb_registry import repository as repo
from kb_registry.expiry import ExpiryLedger

log = get_logger(__name__)

#: The indexer consumes this to mirror the canonical chunks into the keyword index.
TOPIC_PUBLISHED = "registry.published"


@dataclass(frozen=True, slots=True)
class PreparedChunk:
    chunk: Chunk
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class PublishResult:
    document_id: uuid.UUID
    version_id: uuid.UUID
    previous_version_id: uuid.UUID | None
    chunks_written: int
    chunks_tombstoned: int
    edges_rebuilt: int
    outbox_id: int
    published_at: datetime


@dataclass(frozen=True, slots=True)
class RechunkResult:
    document_id: uuid.UUID
    version_id: uuid.UUID
    chunks_before: int
    chunks_written: int
    edges_rebuilt: int
    outbox_id: int


@dataclass
class Approval:
    """Evidence that a human approved this version. Four-eyes: the approver is not the author."""

    approver: str
    task_id: uuid.UUID | None = None
    note: str = ""
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class PublishService:
    def __init__(
        self,
        session: Session,
        *,
        embedder: EmbeddingPort,
        audit: AuditSink | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._embedder = embedder
        self._audit = audit
        self._settings = settings or get_settings()

    # ------------------------------------------------------------------- preparation

    def prepare(self, kbdoc: KBDoc, *, legal_number: str | None = None) -> list[PreparedChunk]:
        """Chunk and embed. Deliberately outside the publish transaction."""
        chunks = chunk_document(kbdoc, legal_number=legal_number)
        if not chunks:
            raise Conflict("document produced no chunks; nothing to publish")
        vectors = self._embedder.embed_documents([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise Conflict(
                "embedding count does not match chunk count",
                chunks=len(chunks),
                vectors=len(vectors),
            )
        return [
            PreparedChunk(chunk=chunk, embedding=vector)
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]

    # ------------------------------------------------------------------------ guards

    def check_publishable(
        self, document: DocumentRow, version: DocumentVersionRow, approval: Approval | None
    ) -> None:
        """Everything that can refuse a publish, in one place."""
        if version.document_id != document.id:
            raise Conflict("version does not belong to the document")
        if version.is_canonical:
            raise Conflict("version is already canonical", version_id=str(version.id))

        # INV-7: the PII gate fails closed. `overridden` is permitted because reaching that
        # state already required a human, a justification and an audit record.
        if PiiStatus(version.pii_status) not in PUBLISHABLE_PII_STATUSES:
            raise GateBlocked(
                "version cannot be published while the PII gate is not clear",
                version_id=str(version.id),
                pii_status=version.pii_status,
                invariant="INV-7",
            )

        # INV-8: regulatory and customer-facing documents can never auto-publish. This is a
        # code guard, not configuration, because configuration can be edited in an incident.
        doc_class = DocClass(document.doc_class)
        if doc_class in NO_AUTOMATION_CLASSES:
            if approval is None:
                raise GateBlocked(
                    f"{doc_class.value} documents require human approval",
                    document_id=str(document.id),
                    invariant="INV-8",
                )
            if version.author and approval.approver == version.author:
                raise GateBlocked(
                    "four-eyes: the approver may not be the author",
                    document_id=str(document.id),
                    author=version.author,
                    invariant="INV-8",
                )

    # ---------------------------------------------------------------------- publish

    def publish(
        self,
        version_id: uuid.UUID,
        prepared: list[PreparedChunk],
        *,
        actor: str,
        approval: Approval | None = None,
    ) -> PublishResult:
        version = repo.get_version(self._session, version_id)
        if version is None:
            raise NotFound("version not found", version_id=str(version_id))
        document = repo.get_document(self._session, version.document_id)
        if document is None:
            raise NotFound("document not found", document_id=str(version.document_id))
        self.check_publishable(document, version, approval)

        started = datetime.now(UTC)
        session = self._session
        # Lock the document row for the rest of the transaction. Two concurrent publishes of
        # the same document must not both believe they are replacing the same canonical
        # version; a row lock serializes exactly that pair and nothing else.
        #
        # (SERIALIZABLE isolation would also work, but it has to be set before the
        # transaction's first statement — which forbids composing this with a caller that has
        # already read — and it aborts on unrelated conflicts, requiring retry logic. The
        # partial unique index on `is_canonical` remains the backstop either way.)
        session.execute(
            text("SELECT id FROM documents WHERE id = :id FOR UPDATE"), {"id": document.id}
        )

        # Re-read under the lock: a PII override or an approval could have been revoked
        # between the check above and here.
        version = repo.get_version(session, version_id)
        document = repo.get_document(session, version.document_id)  # type: ignore[union-attr]
        assert version is not None and document is not None
        self.check_publishable(document, version, approval)

        previous = repo.get_canonical_version(session, document.id)
        tombstoned = 0
        if previous is not None and previous.id != version.id:
            tombstoned = self._tombstone(previous.id)
            session.execute(
                text("UPDATE document_versions SET is_canonical = FALSE WHERE id = :id"),
                {"id": previous.id},
            )

        published_at = datetime.now(UTC)
        session.execute(
            text(
                "UPDATE document_versions SET is_canonical = TRUE, published_at = :ts "
                "WHERE id = :id"
            ),
            {"id": version.id, "ts": published_at},
        )
        session.execute(
            text(
                "UPDATE documents SET status = 'published', canonical_version_id = :version, "
                "updated_at = now() WHERE id = :id"
            ),
            {"version": version.id, "id": document.id},
        )

        written = self._insert_chunks(document, version, prepared)
        edges = self._rebuild_graph_serving(document)

        # `_insert_chunks` copies the *version's* dates, which know nothing about the ledger.
        # Without this a republish of an expired document would resurrect it, silently. Same
        # transaction, so there is no window in which the resurrected chunks are visible.
        ledger = ExpiryLedger(session, audit=self._audit)
        ledger.project(document.id)
        # And an instrument that repeals others now says so from a published document with a
        # fixed effective date — which is the first moment those expiries can be proposed.
        proposed = len(ledger.propose_from_abrogations(document.id, actor=actor))

        outbox = repo.enqueue(
            session,
            TOPIC_PUBLISHED,
            {
                "document_id": str(document.id),
                "version_id": str(version.id),
                "previous_version_id": str(previous.id) if previous else None,
                "chunk_count": written,
                "published_at": published_at.isoformat(),
            },
        )

        if self._audit is not None:
            self._audit.write(
                AuditRecord(
                    action=AuditAction.PUBLISH,
                    actor=actor,
                    object_ref={
                        "document_id": str(document.id),
                        "version_id": str(version.id),
                    },
                    detail={
                        "previous_version_id": str(previous.id) if previous else None,
                        "chunks_written": written,
                        "chunks_tombstoned": tombstoned,
                        "doc_class": document.doc_class,
                        "visibility": document.visibility,
                        "approver": approval.approver if approval else None,
                        "pii_status": version.pii_status,
                        "expiries_proposed": proposed,
                    },
                )
            )

        log.info(
            "version_published",
            extra={
                "document_id": str(document.id),
                "version_id": str(version.id),
                "chunks_written": written,
                "chunks_tombstoned": tombstoned,
                "prepare_to_commit_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
            },
        )
        return PublishResult(
            document_id=document.id,
            version_id=version.id,
            previous_version_id=previous.id if previous else None,
            chunks_written=written,
            chunks_tombstoned=tombstoned,
            edges_rebuilt=edges,
            outbox_id=outbox.id,
            published_at=published_at,
        )

    # --------------------------------------------------------------------------- rechunk

    def rechunk(self, document_id: uuid.UUID, kbdoc: KBDoc, *, actor: str) -> RechunkResult:
        """Rebuild the canonical version's chunks in place.

        Not a publish, and deliberately a separate method rather than a flag on one. Nothing
        about *what the bank says* changes here: same document, same canonical version, same
        text. What changes is the derived form — chunk boundaries, citation labels, embeddings
        — because the chunker or the embedding model improved since the document was published.

        So the four-eyes rule does not apply (no new content is being told to anyone) and the
        PII gate's verdict is untouched (the same text was already cleared). What does apply is
        INV-5: the replacement is one transaction, because a reader must never see the old
        chunks half-replaced by the new. And INV-11: it is audited, because it changes what
        retrieval returns and someone will ask why an answer moved.

        The version is *not* re-tombstoned: tombstoning marks a superseded version's chunks,
        and this version is still canonical. Its old chunks are replaced outright.
        """
        session = self._session
        session.execute(
            text("SELECT id FROM documents WHERE id = :id FOR UPDATE"), {"id": document_id}
        )
        document = repo.get_document(session, document_id)
        if document is None:
            raise NotFound("document not found", document_id=str(document_id))
        version = repo.get_canonical_version(session, document_id)
        if version is None:
            raise Conflict(
                "the document has no canonical version to rechunk", document_id=str(document_id)
            )

        before = (
            session.execute(
                text("SELECT count(*) FROM chunks WHERE version_id = :id AND NOT tombstoned"),
                {"id": version.id},
            ).scalar()
            or 0
        )

        prepared = self.prepare(kbdoc, legal_number=document.legal_number)
        written = self._insert_chunks(document, version, prepared)
        edges = self._rebuild_graph_serving(document)
        # Chunk ids do not survive a rechunk and neither do their dates: these are new rows
        # carrying the version's `effective_to`. The ledger is the authority, so it is re-read
        # rather than assumed (ADR-0040).
        ExpiryLedger(session, audit=self._audit).project(document.id)

        # The same event a publish emits: to anything downstream, this version's chunks changed,
        # which is all an external index needs to know.
        outbox = repo.enqueue(
            session,
            TOPIC_PUBLISHED,
            {
                "document_id": str(document.id),
                "version_id": str(version.id),
                "previous_version_id": None,
                "chunk_count": written,
                "published_at": datetime.now(UTC).isoformat(),
                "rechunked": True,
            },
        )

        if self._audit is not None:
            self._audit.write(
                AuditRecord(
                    action=AuditAction.PUBLISH,
                    actor=actor,
                    object_ref={"document_id": str(document.id), "version_id": str(version.id)},
                    detail={
                        "rechunked": True,
                        "chunks_before": int(before),
                        "chunks_written": written,
                        "doc_class": document.doc_class,
                    },
                )
            )
        log.info(
            "version_rechunked",
            extra={
                "document_id": str(document.id),
                "version_id": str(version.id),
                "chunks_before": int(before),
                "chunks_written": written,
            },
        )
        return RechunkResult(
            document_id=document.id,
            version_id=version.id,
            chunks_before=int(before),
            chunks_written=written,
            edges_rebuilt=edges,
            outbox_id=outbox.id,
        )

    # ------------------------------------------------------------------------ internals

    def _tombstone(self, version_id: uuid.UUID) -> int:
        result = self._session.execute(
            text(
                "UPDATE chunks SET tombstoned = TRUE, doc_status = 'archived' "
                "WHERE version_id = :version_id AND NOT tombstoned"
            ),
            {"version_id": version_id},
        )
        return affected_rows(result)

    def _insert_chunks(
        self, document: DocumentRow, version: DocumentVersionRow, prepared: list[PreparedChunk]
    ) -> int:
        # Republishing the same version replaces its chunks rather than duplicating them.
        self._session.execute(
            text("DELETE FROM chunks WHERE version_id = :version_id"), {"version_id": version.id}
        )
        for item in prepared:
            self._session.execute(
                text(
                    """
                    INSERT INTO chunks (id, document_id, version_id, section_path,
                        citation_label, article, anchor, subject_key, text, embedding, visibility,
                        allowed_groups, department, category_path, doc_class, doc_status,
                        effective_from, effective_to, tombstoned, ordinal, page)
                    VALUES (:id, :document_id, :version_id, :section_path, :citation_label,
                        :article, :anchor, :subject_key, :text, CAST(:embedding AS vector),
                        CAST(:visibility AS visibility),
                        CAST(:allowed_groups AS TEXT[]), :department,
                        CAST(:category_path AS ltree), CAST(:doc_class AS doc_class),
                        'published', :effective_from, :effective_to, FALSE, :ordinal, :page)
                    """
                ),
                {
                    "id": item.chunk.id,
                    "document_id": document.id,
                    "version_id": version.id,
                    "section_path": item.chunk.section_path_text or None,
                    "citation_label": item.chunk.citation_label,
                    # Derived by the chunker, stored rather than re-derived: the supersession
                    # predicate and the reference resolver both need to filter and join on
                    # them (ADR-0032, ADR-0036).
                    "article": item.chunk.article,
                    "anchor": item.chunk.anchor,
                    "subject_key": item.chunk.subject_key,
                    "text": item.chunk.text,
                    "embedding": "[" + ",".join(f"{v:.6f}" for v in item.embedding) + "]",
                    # The ACL is denormalized from the document at publish time (ADR-0003).
                    # Changing a document's ACL therefore requires republishing its chunks.
                    "visibility": document.visibility,
                    "allowed_groups": list(document.allowed_groups),
                    "department": document.department,
                    "category_path": document.category_path,
                    "doc_class": document.doc_class,
                    "effective_from": version.effective_from,
                    "effective_to": version.effective_to,
                    "ordinal": item.chunk.ordinal,
                    "page": item.chunk.page,
                },
            )
        return len(prepared)

    def _rebuild_graph_serving(self, document: DocumentRow) -> int:
        return repo.refresh_graph_serving_for(self._session, document.id)


#: An amendment supersedes its target until the consolidation is approved. The `consolidates`
#: edge, written by the consolidation workflow, is what clears the flag — which is why the
#: edge is created inside the publish transaction rather than announced afterwards.
#:
#: It carries `articles` because the answer is article-shaped: an amendment usually touches two
#: or three articles of a sixty-article circular, and warning on all sixty is how the warning
#: stops being read (ADR-0032). An empty/NULL list means the whole document, which is the
#: original behaviour and what an edge with no article detail still gets.
#:
#: `retrieval-api` runs the same predicate in bulk (`RetrievalEngine._superseded_articles`);
#: the two are asserted to agree — at article granularity — in
#: `tests/test_consolidation_flow.py`.
SUPERSEDED_PREDICATE = """
    SELECT r.dst_document_id, r.articles
    FROM document_refs r
    JOIN documents src ON src.id = r.src_document_id
    WHERE r.ref_type IN ('amends', 'abrogates')
      AND src.status = 'published'
      AND NOT EXISTS (
            SELECT 1 FROM document_refs c
            WHERE c.src_document_id = r.dst_document_id
              AND c.dst_document_id = r.src_document_id
              AND c.ref_type = 'consolidates'
      )
"""


def is_superseded(session: Session, document_id: uuid.UUID) -> bool:
    """True when *any* unconsolidated amendment targets this document.

    Deliberately still document-shaped. This is the workflow question — "does this need a
    consolidation?" — and the answer does not depend on which articles moved. Only the
    retrieval-facing answer narrows to articles (ADR-0032).
    """
    row = session.execute(
        text(f"SELECT 1 FROM ({SUPERSEDED_PREDICATE}) s WHERE s.dst_document_id = :id LIMIT 1"),
        {"id": document_id},
    ).scalar()
    return bool(row)


__all__ = [
    "TOPIC_PUBLISHED",
    "Approval",
    "DocStatus",
    "PreparedChunk",
    "PublishResult",
    "PublishService",
    "RechunkResult",
    "is_superseded",
]
