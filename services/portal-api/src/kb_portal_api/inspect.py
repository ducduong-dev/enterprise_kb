"""What a steward needs to check a processed document: its chunks, and its edges.

Two questions this answers that nothing else in the platform did.

**"What actually got indexed?"** Until now the only way to see a document's chunks was to guess
a query that retrieved them. Chunk boundaries decide what a citation points at and what an
answer can quote, so a steward who suspects a bad split had no way to look. The chunk view is
that look — text, boundary, citation label, page, ordinal, and whether the row is live or a
tombstone from a superseded version.

**"Is the graph right?"** The reference graph is built by a machine from detected legal numbers
and confirmed by humans at review. Its edges drive supersession warnings, graph expansion and
the impact tasks that consolidation opens — so a wrong edge is not cosmetic, it is a policy
owner who never hears that the regulation under their document changed. The graph view is
built around the questions that make an edge checkable: which direction, what type, which
articles, who said so, and does the target still exist and still say what it said.

Everything here reads through the registry (ADR-0006) and is metadata-plus-text for documents
the *caller* may see: chunk text is document text, so the ACL applies exactly as it does in
retrieval (INV-2). Nothing here is a second retrieval path — it never ranks, never searches,
and is always scoped to one document the caller named.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from kb_authz.compile import compile_sql_documents
from kb_authz.filters import FilterBuilder
from kb_authz.principal import Principal
from kb_common.audit import AuditAction, AuditRecord, AuditSink
from kb_common.errors import Conflict, NotFound, ValidationError
from kb_common.logging import get_logger
from kb_ports.models import EmbeddingPort
from kb_ports.storage import StoragePort
from kb_registry import repository as repo
from kb_registry.publish import PublishService, RechunkResult
from kb_schemas.enums import RefType
from kb_schemas.kbdoc import KBDoc
from sqlalchemy import text
from sqlalchemy.orm import Session

log = get_logger(__name__)

#: Chunk text is shown in full — a truncated chunk cannot be checked for a bad boundary, which
#: is the whole reason to look at one.
MAX_CHUNKS = 500
#: Neighbours drawn on the map. Beyond this the picture stops helping and the lists carry it.
MAP_NODE_LIMIT = 24


@dataclass(frozen=True, slots=True)
class ChunkView:
    chunk_id: uuid.UUID
    ordinal: int
    section_path: str | None
    citation_label: str | None
    text: str
    page: int
    characters: int
    tombstoned: bool
    embedded: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": str(self.chunk_id),
            "ordinal": self.ordinal,
            "section_path": self.section_path,
            "citation_label": self.citation_label,
            "text": self.text,
            "page": self.page,
            "characters": self.characters,
            "tombstoned": self.tombstoned,
            # Whether the vector exists at all: a chunk with no embedding is invisible to the
            # semantic leg of retrieval and visible to the keyword one, which looks like a
            # ranking mystery rather than a missing row.
            "embedded": self.embedded,
        }


@dataclass(frozen=True, slots=True)
class EdgeView:
    """One reference, from the point of view of the document being inspected."""

    direction: str  # outgoing | incoming
    ref_type: str
    other_document_id: uuid.UUID
    other_title: str
    other_legal_number: str | None
    other_status: str
    articles: list[int]
    detected_by: str | None
    confirmed_by: str | None
    readable: bool

    @property
    def confirmed(self) -> bool:
        return bool(self.confirmed_by)

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "ref_type": self.ref_type,
            "document_id": str(self.other_document_id),
            "title": self.other_title,
            "legal_number": self.other_legal_number,
            "status": self.other_status,
            "articles": self.articles,
            "detected_by": self.detected_by,
            "confirmed_by": self.confirmed_by,
            "confirmed": self.confirmed,
            # False when the edge exists but its target is outside what this caller may read.
            # Shown as a count, never as a title (INV-2, and [OPEN]-3's default: absence).
            "readable": self.readable,
        }


@dataclass
class DocumentInspection:
    document: dict[str, Any]
    versions: list[dict[str, Any]] = field(default_factory=list)
    chunks: list[ChunkView] = field(default_factory=list)
    edges: list[EdgeView] = field(default_factory=list)
    lineage: list[dict[str, Any]] = field(default_factory=list)
    #: References this document makes to instruments the bank does not hold. Not edges — the
    #: target does not exist — but the reviewer's most useful question about the graph is
    #: "what is missing", and the answer is exactly this list (ADR-0028).
    pending: list[dict[str, Any]] = field(default_factory=list)
    unreadable_edges: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "versions": self.versions,
            "chunks": [chunk.as_dict() for chunk in self.chunks],
            "edges": [edge.as_dict() for edge in self.edges],
            "lineage": self.lineage,
            "pending": self.pending,
            "unreadable_edges": self.unreadable_edges,
            "warnings": self.warnings,
            "map_node_limit": MAP_NODE_LIMIT,
        }


class InspectService:
    def __init__(
        self,
        session: Session,
        *,
        storage: StoragePort,
        embedder: EmbeddingPort,
        audit: AuditSink | None = None,
    ) -> None:
        self._session = session
        self._storage = storage
        self._embedder = embedder
        self._audit = audit
        self._filters = FilterBuilder()

    # -------------------------------------------------------------------------- reading

    def inspect(self, document_id: uuid.UUID, principal: Principal) -> DocumentInspection:
        document = self._readable_document(document_id, principal)
        canonical_id = document.canonical_version_id

        inspection = DocumentInspection(
            document={
                "id": str(document.id),
                "title": document.title,
                "legal_number": document.legal_number,
                "doc_class": document.doc_class,
                "category_path": document.category_path,
                "department": document.department,
                "visibility": document.visibility,
                "status": document.status,
                "canonical_version_id": str(canonical_id) if canonical_id else None,
                "review_by": document.review_by.isoformat() if document.review_by else None,
            }
        )
        inspection.versions = self._versions(document_id, canonical_id)
        inspection.chunks = self.chunks(document_id, principal)
        inspection.edges, inspection.unreadable_edges = self._edges(document_id, principal)
        inspection.lineage = self._lineage(document_id, inspection.edges)
        inspection.pending = [
            {
                "legal_number": row.target_legal_number,
                "ref_type": row.ref_type,
                "detected_by": row.detected_by,
            }
            for row in repo.pending_refs_from(self._session, document_id)
        ]
        inspection.warnings = self._warnings(document, inspection)
        return inspection

    def chunks(
        self,
        document_id: uuid.UUID,
        principal: Principal,
        *,
        include_tombstoned: bool = False,
    ) -> list[ChunkView]:
        """The chunks of this document, in reading order.

        Live chunks belong to the canonical version (INV-6). Tombstoned ones are the trail of
        what a previous version said and are shown only when asked for, because a steward
        checking a boundary wants today's text, not a history of every boundary it ever had.
        """
        self._readable_document(document_id, principal)
        rows = (
            self._session.execute(
                text(
                    """
                    SELECT c.id, c.ordinal, c.section_path, c.citation_label, c.text, c.page,
                           c.tombstoned, (c.embedding IS NOT NULL) AS embedded
                    FROM chunks c
                    WHERE c.document_id = :document_id
                      AND (:include_tombstoned OR NOT c.tombstoned)
                    ORDER BY c.tombstoned, c.ordinal
                    LIMIT :limit
                    """
                ),
                {
                    "document_id": document_id,
                    "include_tombstoned": include_tombstoned,
                    "limit": MAX_CHUNKS,
                },
            )
            .mappings()
            .all()
        )
        return [
            ChunkView(
                chunk_id=row["id"],
                ordinal=int(row["ordinal"]),
                section_path=row["section_path"],
                citation_label=row["citation_label"],
                text=row["text"],
                page=int(row["page"]),
                characters=len(row["text"]),
                tombstoned=bool(row["tombstoned"]),
                embedded=bool(row["embedded"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------------- the graph

    def _edges(self, document_id: uuid.UUID, principal: Principal) -> tuple[list[EdgeView], int]:
        """Both directions, typed, with provenance and the target's current state."""
        # The visibility rule comes from the one module that owns it; writing a second copy of
        # "restricted unless you hold the group" here is how the two drift apart (INV-2).
        readable, acl_params = compile_sql_documents(self._filters.base(principal), alias="d")

        rows = (
            self._session.execute(
                text(
                    f"""
                    SELECT r.ref_type, r.articles, r.detected_by, r.confirmed_by,
                           CASE WHEN r.src_document_id = :id THEN 'outgoing' ELSE 'incoming' END
                               AS direction,
                           d.id AS other_id, d.title, d.legal_number, d.status,
                           {readable} AS readable
                    FROM document_refs r
                    JOIN documents d
                      ON d.id = CASE WHEN r.src_document_id = :id
                                     THEN r.dst_document_id ELSE r.src_document_id END
                    WHERE r.src_document_id = :id OR r.dst_document_id = :id
                    ORDER BY direction, r.ref_type, d.title
                    """
                ),
                {"id": document_id, **acl_params},
            )
            .mappings()
            .all()
        )

        edges: list[EdgeView] = []
        hidden = 0
        for row in rows:
            if not row["readable"]:
                # The edge exists and its target is not this caller's to see. Counted, never
                # named: a title is content, and disclosing one through the graph would be a
                # hole in the same filter retrieval applies (INV-2/INV-10).
                hidden += 1
                continue
            edges.append(
                EdgeView(
                    direction=str(row["direction"]),
                    ref_type=str(row["ref_type"]),
                    other_document_id=row["other_id"],
                    other_title=str(row["title"]),
                    other_legal_number=row["legal_number"],
                    other_status=str(row["status"]),
                    articles=list(row["articles"] or []),
                    detected_by=row["detected_by"],
                    confirmed_by=row["confirmed_by"],
                    readable=True,
                )
            )
        return edges, hidden

    def _lineage(self, document_id: uuid.UUID, edges: list[EdgeView]) -> list[dict[str, Any]]:
        """The amendment chain, in time order.

        A separate view from the neighbour lists because it answers a different question, and
        the one with legal consequences: *what is in force*. Topology does not show that —
        "amended by three instruments, two consolidated" is a sequence, and a reader needs to
        see where the gap is.
        """
        relevant = [
            edge
            for edge in edges
            if edge.ref_type in (RefType.AMENDS.value, RefType.ABROGATES.value)
            and edge.direction == "incoming"
        ]
        if not relevant:
            return []

        consolidated = {
            edge.other_document_id
            for edge in edges
            if edge.ref_type == RefType.CONSOLIDATES.value and edge.direction == "outgoing"
        }
        rows = (
            self._session.execute(
                text(
                    """
                    SELECT d.id, d.title, d.legal_number, d.status,
                           v.effective_from, v.published_at
                    FROM documents d
                    LEFT JOIN document_versions v ON v.id = d.canonical_version_id
                    WHERE d.id = ANY(CAST(:ids AS uuid[]))
                    """
                ),
                {"ids": [str(edge.other_document_id) for edge in relevant]},
            )
            .mappings()
            .all()
        )
        by_id = {row["id"]: row for row in rows}

        lineage = []
        for edge in relevant:
            row = by_id.get(edge.other_document_id)
            effective = row["effective_from"] if row else None
            lineage.append(
                {
                    "document_id": str(edge.other_document_id),
                    "title": edge.other_title,
                    "legal_number": edge.other_legal_number,
                    "ref_type": edge.ref_type,
                    "effective_from": effective.isoformat() if effective else None,
                    "status": edge.other_status,
                    "consolidated": edge.other_document_id in consolidated,
                    "confirmed": edge.confirmed,
                }
            )
        lineage.sort(key=lambda item: (item["effective_from"] or "", item["title"]))
        return lineage

    def _warnings(self, document: Any, inspection: DocumentInspection) -> list[str]:
        """What a reviewer should look at first, stated rather than left to be noticed."""
        warnings: list[str] = []
        live = [chunk for chunk in inspection.chunks if not chunk.tombstoned]
        if document.status == "published" and not live:
            warnings.append("Đã ban hành nhưng không có đoạn nào được lập chỉ mục.")
        if any(not chunk.embedded for chunk in live):
            missing = sum(1 for chunk in live if not chunk.embedded)
            warnings.append(
                f"{missing} đoạn chưa có vector — chỉ tìm được bằng từ khoá, không tìm được "
                "theo ngữ nghĩa."
            )
        unconfirmed = [edge for edge in inspection.edges if not edge.confirmed]
        if unconfirmed:
            warnings.append(f"{len(unconfirmed)} liên kết do máy phát hiện chưa được xác nhận.")
        if inspection.pending:
            warnings.append(
                f"{len(inspection.pending)} tham chiếu tới văn bản chưa có trong hệ thống — "
                "liên kết sẽ tự tạo khi văn bản đó được nạp."
            )
        pending = [item for item in inspection.lineage if not item["consolidated"]]
        if pending:
            warnings.append(
                f"{len(pending)} văn bản sửa đổi/bãi bỏ chưa được hợp nhất — kết quả tìm kiếm "
                "vẫn kèm cảnh báo."
            )
        return warnings

    # ------------------------------------------------------------------------- actions

    def rechunk(self, document_id: uuid.UUID, *, actor: str) -> RechunkResult:
        """Rebuild this document's chunks from the version that is already canonical.

        For when the chunker or the embedding model improved, not for when the text is wrong:
        text is fixed by correcting a version and republishing (ADR-0012), which is a different
        act with a different guard.
        """
        document = repo.get_document(self._session, document_id)
        if document is None:
            raise NotFound("document not found", document_id=str(document_id))
        version = repo.get_canonical_version(self._session, document_id)
        if version is None:
            raise Conflict("nothing to rechunk: the document has no canonical version")

        kbdoc = self._load_kbdoc(version.idp_report_ref)
        publisher = PublishService(self._session, embedder=self._embedder, audit=self._audit)
        result = publisher.rechunk(document_id, kbdoc, actor=actor)
        log.info(
            "document_rechunked",
            extra={
                "document_id": str(document_id),
                "actor": actor,
                "chunks_before": result.chunks_before,
                "chunks_written": result.chunks_written,
            },
        )
        return result

    def set_edge_confirmation(
        self,
        document_id: uuid.UUID,
        other_document_id: uuid.UUID,
        ref_type: str,
        *,
        confirm: bool,
        actor: str,
    ) -> dict[str, Any]:
        """Confirm a machine-detected edge, or remove one that is wrong.

        Confirmation is what turns a detected reference into an authoritative one: the
        consolidation workflow and the impact traversal both act on edges, so an unconfirmed
        guess that nobody checks becomes a task nobody expected — or, worse, a policy owner
        who is never told that what they implement has changed.
        """
        # Either direction: the screen is ego-centric, so a reviewer confirms "the amends edge
        # between this document and that one" without caring which end the row is stored at,
        # and an incoming edge is exactly the one most worth confirming (INV-10 keeps the
        # storage direction meaningful; the reviewer's question does not depend on it).
        edge = repo.get_edge(
            self._session, document_id, other_document_id, ref_type
        ) or repo.get_edge(self._session, other_document_id, document_id, ref_type)
        if edge is None:
            raise NotFound(
                "edge not found",
                document_id=str(document_id),
                other_document_id=str(other_document_id),
                ref_type=ref_type,
            )
        if confirm:
            edge.confirmed_by = actor
            self._session.flush()
            outcome = "confirmed"
        else:
            self._session.delete(edge)
            self._session.flush()
            outcome = "removed"

        if self._audit is not None:
            self._audit.write(
                AuditRecord(
                    action=AuditAction.REVIEW_DECISION,
                    actor=actor,
                    object_ref={
                        "document_id": str(document_id),
                        "other_document_id": str(other_document_id),
                    },
                    detail={"edge": ref_type, "decision": outcome, "detected_by": edge.detected_by},
                )
            )
        log.info(
            "edge_reviewed",
            extra={
                "document_id": str(document_id),
                "other_document_id": str(other_document_id),
                "ref_type": ref_type,
                "decision": outcome,
            },
        )
        return {"ref_type": ref_type, "decision": outcome}

    # ----------------------------------------------------------------------- internals

    def _readable_document(self, document_id: uuid.UUID, principal: Principal) -> Any:
        """The document, if this caller may read it.

        The same predicate retrieval applies, asked of one named document: visibility, or
        membership of a group on its ACL. A refusal is a not-found, because "this exists and
        you may not see it" is itself disclosure ([OPEN]-3 defaults to absence).
        """
        document = repo.get_document(self._session, document_id)
        if document is None:
            raise NotFound("document not found", document_id=str(document_id))

        readable, params = compile_sql_documents(self._filters.base(principal), alias="d")
        allowed = self._session.execute(
            text(f"SELECT {readable} FROM documents d WHERE d.id = :document_id"),
            {**params, "document_id": document_id},
        ).scalar()
        if not allowed:
            raise NotFound("document not found", document_id=str(document_id))
        return document

    def _versions(
        self, document_id: uuid.UUID, canonical_id: uuid.UUID | None
    ) -> list[dict[str, Any]]:
        return [
            {
                "version_id": str(version.id),
                "author": version.author,
                "source_type": version.source_type,
                "pii_status": version.pii_status,
                "created_at": version.created_at.isoformat(),
                "effective_from": (
                    version.effective_from.isoformat() if version.effective_from else None
                ),
                "canonical": version.id == canonical_id,
                "change_summary": version.change_summary,
            }
            for version in repo.list_versions(self._session, document_id)
        ]

    def _load_kbdoc(self, ref: str | None) -> KBDoc:
        if not ref:
            raise ValidationError(
                "this version has no parsed document stored; it cannot be rechunked"
            )
        bucket, _, key = str(ref).partition("/")
        return KBDoc.model_validate_json(self._storage.get(bucket, key))
