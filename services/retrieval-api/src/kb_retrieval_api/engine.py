"""The retrieval funnel (INV-1).

Every read path in the platform — search UI, internal bot, external bot, citation click, graph
expansion — ends up in this class. That is the entire point: there is one place where "what may
this caller see" is decided, one place where it is compiled into the query, and one place where
the answer is logged well enough to reconstruct it later.

Pipeline:

    principal → ResolvedFilter (INV-2)
              → keyword search ∥ vector search, both filtered inside the query
              → RRF fusion
              → rerank
              → per-document cap, top_k
              → supersession flags
              → graph expansion, filtered by the same ACL (INV-10)
              → audit record naming principal, delegate, filter, and every chunk returned (INV-11)

Nothing is filtered after the fact. If a chunk reaches this code, the index already decided the
caller may see it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date

from kb_authz.compile import compile_sql_graph
from kb_authz.filters import FilterBuilder, ResolvedFilter
from kb_authz.principal import Principal
from kb_common.audit import AuditAction, AuditRecord, AuditSink
from kb_common.errors import NotFound
from kb_common.logging import get_logger
from kb_common.metrics import retrieval_latency, retrieval_results
from kb_ports.indexes import IndexHit, KeywordIndexPort, VectorIndexPort
from kb_ports.models import EmbeddingPort, RerankPort
from kb_schemas.api import (
    CitationLookupRequest,
    GraphExpansion,
    RetrievedChunk,
    RetrieveRequest,
    RetrieveResponse,
)
from kb_schemas.enums import RetrievalMode
from kb_vntext.legal_numbers import extract_legal_numbers
from sqlalchemy import text
from sqlalchemy.orm import Session

from kb_retrieval_api.fusion import FusedHit, cap_per_document, reciprocal_rank_fusion

log = get_logger(__name__)

#: Candidates pulled from each retriever before fusion. Wide enough that the reranker has
#: something to work with, narrow enough that reranking stays inside the latency budget.
CANDIDATES_PER_RETRIEVER = 50
#: How many fused candidates go to the reranker.
RERANK_DEPTH = 30
#: At most this many chunks from one document, so a single long regulation cannot fill the
#: answer and make it look better-sourced than it is.
MAX_CHUNKS_PER_DOCUMENT = 3
#: Expansion breadth. A reference graph is dense; showing everything is showing nothing.
MAX_EXPANSIONS = 8
#: A citation is a precise reference, so the match must be close. Set loosely, trigram
#: similarity happily matches "Bước 3, QT 07/2024" against "Mục 3, QĐ 114/2023" — a different
#: instrument entirely, which is worse than returning nothing.
CITATION_SIMILARITY_THRESHOLD = 0.4


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    response: RetrieveResponse
    resolved_filter: ResolvedFilter


class RetrievalEngine:
    def __init__(
        self,
        session: Session,
        *,
        keyword_index: KeywordIndexPort,
        vector_index: VectorIndexPort,
        embedder: EmbeddingPort,
        reranker: RerankPort,
        audit: AuditSink | None = None,
        today: date | None = None,
    ) -> None:
        self._session = session
        self._keyword = keyword_index
        self._vector = vector_index
        self._embedder = embedder
        self._reranker = reranker
        self._audit = audit
        self._filters = FilterBuilder(today=today)

    # --------------------------------------------------------------------------- retrieve

    def retrieve(self, principal: Principal, request: RetrieveRequest) -> RetrievalOutcome:
        started = time.perf_counter()
        acl = self._filters.build(
            principal,
            facets=request.facets,
            mode=request.mode,
            as_of_date=request.as_of_date,
        )

        candidates = self._gather(request.query, acl)
        ranked = self._rerank(request.query, candidates)
        selected = cap_per_document(ranked, MAX_CHUNKS_PER_DOCUMENT)[: request.top_k]

        document_ids = {item.hit.document_id for item in selected}
        superseded = self._superseded_documents(document_ids)
        titles = self._titles(document_ids)
        chunks = [
            RetrievedChunk(
                chunk_id=item.hit.chunk_id,
                version_id=item.hit.version_id,
                document_id=item.hit.document_id,
                citation_label=item.hit.citation_label,
                document_title=titles.get(item.hit.document_id),
                section_path=item.hit.section_path,
                text=item.hit.text or "",
                score=round(item.score, 6),
                highlights=list(item.hit.highlights),
                supersession_flag=item.hit.document_id in superseded,
            )
            for item in selected
        ]

        expansions = (
            self._expand(acl, [item.hit.document_id for item in selected])
            if request.expand_graph
            else []
        )

        elapsed = time.perf_counter() - started
        retrieval_latency.labels(stage="total").observe(elapsed)
        retrieval_results.observe(len(chunks))

        self._audit_retrieval(
            principal, acl, chunks, expansions, request, duration_ms=int(elapsed * 1000)
        )
        log.info(
            "retrieval_complete",
            extra={
                "principal": principal.audit_actor,
                "on_behalf_of": principal.audit_on_behalf_of,
                "filter_id": acl.filter_id,
                "candidates": len(candidates),
                "returned": len(chunks),
                "expansions": len(expansions),
                "mode": request.mode.value,
                "duration_ms": int(elapsed * 1000),
            },
        )
        return RetrievalOutcome(
            response=RetrieveResponse(
                chunks=chunks, expansions=expansions, resolved_filter_id=acl.filter_id
            ),
            resolved_filter=acl,
        )

    # ---------------------------------------------------------------------- citation lookup

    def citation_lookup(
        self, principal: Principal, request: CitationLookupRequest
    ) -> RetrievalOutcome:
        """Resolve "Điều 12 Thông tư 41/2016/TT-NHNN" to the article's chunks.

        A citation is a precise reference, so this is a lookup, not a search: it matches the
        stored `citation_label` rather than ranking text. It goes through the same filter as
        everything else — a citation someone is not allowed to read simply does not resolve.
        """
        acl = self._filters.build(
            principal,
            mode=RetrievalMode.AS_OF if request.as_of_date else RetrievalMode.CURRENT,
            as_of_date=request.as_of_date,
        )
        where, params = _chunk_acl(acl)
        params["citation"] = request.citation
        params["citation_threshold"] = CITATION_SIMILARITY_THRESHOLD
        numbers = extract_legal_numbers(request.citation)
        params["legal_number"] = numbers[0].value if numbers else None

        rows = (
            self._session.execute(
                text(
                    f"""
                SELECT c.id, c.document_id, c.version_id, c.text, c.citation_label,
                       c.section_path,
                       similarity(coalesce(c.citation_label, ''), :citation) AS score
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE {where}
                  AND (
                        CAST(:legal_number AS TEXT) IS NULL
                        OR d.legal_number = CAST(:legal_number AS TEXT)
                        OR coalesce(c.citation_label, '')
                             ILIKE '%' || CAST(:legal_number AS TEXT) || '%'
                      )
                  AND similarity(coalesce(c.citation_label, ''), :citation)
                        > :citation_threshold
                ORDER BY score DESC, c.ordinal
                LIMIT 20
                """
                ),
                params,
            )
            .mappings()
            .all()
        )

        chunks = [
            RetrievedChunk(
                chunk_id=row["id"],
                version_id=row["version_id"],
                document_id=row["document_id"],
                citation_label=row["citation_label"],
                section_path=row["section_path"],
                text=row["text"],
                score=float(row["score"]),
            )
            for row in rows
        ]
        superseded = self._superseded_documents({chunk.document_id for chunk in chunks})
        for chunk in chunks:
            chunk.supersession_flag = chunk.document_id in superseded

        self._write_audit(
            AuditRecord(
                action=AuditAction.CITATION_LOOKUP,
                actor=principal.audit_actor,
                on_behalf_of=principal.audit_on_behalf_of,
                object_ref={
                    "citation": request.citation,
                    "chunk_ids": [str(chunk.chunk_id) for chunk in chunks],
                },
                resolved_filter=acl.audit_payload(),
            )
        )
        return RetrievalOutcome(
            response=RetrieveResponse(chunks=chunks, resolved_filter_id=acl.filter_id),
            resolved_filter=acl,
        )

    # ------------------------------------------------------------------------ render gate

    def document_access(
        self, principal: Principal, document_id: uuid.UUID, *, version_id: uuid.UUID | None = None
    ) -> dict[str, object]:
        """Gate 3: the re-check when a user opens a document from a citation.

        Search decided what could be *found*; this decides what can be *read*, now, with the
        document's current ACL. The two can disagree — an index write may lag a reclassification
        by seconds — and when they do, this answer wins.
        """
        acl = self._filters.base(principal)
        where, params = _chunk_acl(acl, alias="c")
        params["document_id"] = document_id

        row = (
            self._session.execute(
                text(
                    f"""
                SELECT d.id, d.title, d.legal_number, d.doc_class, d.category_path::text AS
                       category_path, d.visibility, d.status, d.canonical_version_id,
                       cat.existence_disclosure,
                       EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id AND {where})
                           AS readable
                FROM documents d
                JOIN categories cat ON cat.path = d.category_path
                WHERE d.id = :document_id
                """
                ),
                params,
            )
            .mappings()
            .first()
        )

        if row is None or not row["readable"]:
            # [OPEN]-3: unless the category says otherwise, refusal and absence are
            # indistinguishable — otherwise the 403 itself discloses that the document exists.
            if row is not None and row["existence_disclosure"]:
                from kb_common.errors import AuthzError

                raise AuthzError(
                    "not permitted to read this document", document_id=str(document_id)
                )
            raise NotFound("document not found", document_id=str(document_id))

        is_archived = version_id is not None and version_id != row["canonical_version_id"]
        self._write_audit(
            AuditRecord(
                action=(
                    AuditAction.ARCHIVED_VERSION_ACCESS
                    if is_archived
                    else AuditAction.DOCUMENT_RENDER
                ),
                actor=principal.audit_actor,
                on_behalf_of=principal.audit_on_behalf_of,
                object_ref={
                    "document_id": str(document_id),
                    "version_id": str(version_id) if version_id else None,
                },
                resolved_filter=acl.audit_payload(),
            )
        )
        return {
            "id": row["id"],
            "title": row["title"],
            "legal_number": row["legal_number"],
            "doc_class": row["doc_class"],
            "category_path": row["category_path"],
            "visibility": row["visibility"],
            "status": row["status"],
            "canonical_version_id": row["canonical_version_id"],
        }

    # -------------------------------------------------------------------------- internals

    def _gather(self, query: str, acl: ResolvedFilter) -> list[FusedHit]:
        keyword_hits: list[IndexHit] = []
        vector_hits: list[IndexHit] = []

        started = time.perf_counter()
        keyword_hits = list(self._keyword.search(query, acl, top_k=CANDIDATES_PER_RETRIEVER))
        retrieval_latency.labels(stage="keyword").observe(time.perf_counter() - started)

        started = time.perf_counter()
        embedding = self._embedder.embed_query(query)
        vector_hits = list(self._vector.search(embedding, acl, top_k=CANDIDATES_PER_RETRIEVER))
        retrieval_latency.labels(stage="vector").observe(time.perf_counter() - started)

        return reciprocal_rank_fusion({"keyword": keyword_hits, "vector": vector_hits})

    def _rerank(self, query: str, candidates: list[FusedHit]) -> list[FusedHit]:
        head = candidates[:RERANK_DEPTH]
        if not head:
            return []

        started = time.perf_counter()
        ordering = self._reranker.rerank(query, [item.hit.text or "" for item in head])
        retrieval_latency.labels(stage="rerank").observe(time.perf_counter() - started)

        reranked = [head[result.index] for result in ordering]
        # Candidates beyond the rerank depth keep their fused order behind the reranked head:
        # dropping them would silently cap recall at RERANK_DEPTH.
        return reranked + candidates[RERANK_DEPTH:]

    def _titles(self, document_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Document titles for the selected chunks.

        Not denormalized onto `chunks` like the ACL columns are: a title is display metadata,
        it changes without republishing, and a stale one in an answer's citation would be
        worse than an extra query. The rows are already filtered — these ids came out of a
        query that applied the filter (INV-2).
        """
        if not document_ids:
            return {}
        rows = self._session.execute(
            text("SELECT id, title FROM documents WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": [str(document_id) for document_id in document_ids]},
        ).all()
        return {row[0]: row[1] for row in rows}

    def _superseded_documents(self, document_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        """Documents an amendment targets that has not been consolidated yet.

        Flagged rather than hidden: the text is still the canonical text, and hiding it would
        leave the user with nothing. The warning is what makes quoting it safe, and it clears
        when the Legal cell approves the consolidation (M5) — which writes a `consolidates`
        edge, the `NOT EXISTS` clause below.

        The same predicate lives in `kb_registry.publish.SUPERSEDED_PREDICATE`; the two are
        asserted to agree in `tests/test_consolidation_flow.py`.
        """
        if not document_ids:
            return set()
        rows = (
            self._session.execute(
                text(
                    """
                SELECT DISTINCT r.dst_document_id
                FROM document_refs r
                JOIN documents src ON src.id = r.src_document_id
                WHERE r.dst_document_id = ANY(CAST(:ids AS uuid[]))
                  AND r.ref_type IN ('amends', 'abrogates')
                  AND src.status = 'published'
                  AND NOT EXISTS (
                        SELECT 1 FROM document_refs c
                        WHERE c.src_document_id = r.dst_document_id
                          AND c.dst_document_id = r.src_document_id
                          AND c.ref_type = 'consolidates'
                  )
                """
                ),
                {"ids": [str(document_id) for document_id in document_ids]},
            )
            .scalars()
            .all()
        )
        return set(rows)

    def _expand(self, acl: ResolvedFilter, document_ids: list[uuid.UUID]) -> list[GraphExpansion]:
        if not document_ids:
            return []
        where, params = compile_sql_graph(acl)
        params["ids"] = [str(document_id) for document_id in document_ids]
        params["limit"] = MAX_EXPANSIONS

        rows = (
            self._session.execute(
                text(
                    f"""
                SELECT DISTINCT g.dst_document_id, g.ref_type, g.dst_summary,
                       d.legal_number
                FROM graph_serving g
                JOIN documents d ON d.id = g.dst_document_id
                WHERE g.src_document_id = ANY(CAST(:ids AS uuid[]))
                  AND NOT g.dst_document_id = ANY(CAST(:ids AS uuid[]))
                  AND {where}
                LIMIT :limit
                """
                ),
                params,
            )
            .mappings()
            .all()
        )
        return [
            GraphExpansion(
                document_id=row["dst_document_id"],
                ref_type=row["ref_type"],
                summary=row["dst_summary"],
                citation_label=row["legal_number"],
            )
            for row in rows
        ]

    def _audit_retrieval(
        self,
        principal: Principal,
        acl: ResolvedFilter,
        chunks: list[RetrievedChunk],
        expansions: list[GraphExpansion],
        request: RetrieveRequest,
        *,
        duration_ms: int,
    ) -> None:
        """INV-11: enough to reconstruct this answer months later."""
        self._write_audit(
            AuditRecord(
                action=AuditAction.RETRIEVE,
                actor=principal.audit_actor,
                on_behalf_of=principal.audit_on_behalf_of,
                object_ref={
                    "chunk_ids": [str(chunk.chunk_id) for chunk in chunks],
                    "version_ids": sorted({str(chunk.version_id) for chunk in chunks}),
                    "document_ids": sorted({str(chunk.document_id) for chunk in chunks}),
                    "expansion_document_ids": [str(e.document_id) for e in expansions],
                },
                resolved_filter=acl.audit_payload(),
                detail={
                    # The query is user text and belongs in the audit record, which is
                    # access-controlled — never in a metric label or an ordinary log line.
                    "query": request.query,
                    "mode": request.mode.value,
                    "top_k": request.top_k,
                    "duration_ms": duration_ms,
                },
            )
        )

    def _write_audit(self, record: AuditRecord) -> None:
        if self._audit is not None:
            self._audit.write(record)


def _chunk_acl(acl: ResolvedFilter, alias: str = "c") -> tuple[str, dict[str, object]]:
    from kb_authz.compile import compile_sql

    return compile_sql(acl, alias=alias)
