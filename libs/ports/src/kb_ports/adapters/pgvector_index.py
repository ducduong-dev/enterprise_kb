"""pgvector implementation of `VectorIndexPort`.

The ACL predicate is compiled by `kb_authz.compile_sql` and concatenated into the WHERE clause
*of the same query* that does the nearest-neighbour scan (INV-2). It is not a post-filter, and
there is no code path that can return a row the predicate excluded.

One consequence worth stating: with an HNSW index, a filtered ANN search can under-return when
the filter is very selective, because the graph walk visits `ef_search` neighbours before
filtering. `ef_search` is raised for the session accordingly — recall matters more here than a
few milliseconds, and a missed regulation is not an acceptable trade.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from kb_authz.compile import compile_sql
from kb_authz.filters import ResolvedFilter
from kb_common.db import affected_rows
from kb_common.logging import get_logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from kb_ports.base import AdapterInfo
from kb_ports.indexes import IndexDocument, IndexHit
from kb_ports.registry import PortName, register_adapter

log = get_logger(__name__)

#: HNSW candidate list size. Higher = better recall under selective ACL filters, slower query.
EF_SEARCH = 200


class PgVectorIndexAdapter:
    """Reads and writes the `chunks` table. The caller owns the session and the transaction —
    the publish path needs these writes inside its own transaction (INV-5)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(name="pgvector", version="hnsw-cosine")

    def health(self) -> bool:
        return bool(self._session.execute(text("SELECT 1")).scalar())

    def search(
        self, embedding: Sequence[float], acl: ResolvedFilter, *, top_k: int = 50
    ) -> list[IndexHit]:
        where, params = compile_sql(acl)
        params["query_vector"] = _vector_literal(embedding)
        params["limit"] = top_k
        self._session.execute(text(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}"))

        rows = self._session.execute(
            text(
                f"""
                SELECT c.id, c.document_id, c.version_id, c.text, c.citation_label,
                       c.section_path,
                       1 - (c.embedding <=> CAST(:query_vector AS vector)) AS score
                FROM chunks c
                WHERE {where} AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> CAST(:query_vector AS vector)
                LIMIT :limit
                """
            ),
            params,
        ).mappings()

        return [
            IndexHit(
                chunk_id=row["id"],
                document_id=row["document_id"],
                version_id=row["version_id"],
                score=float(row["score"]),
                text=row["text"],
                citation_label=row["citation_label"],
                section_path=row["section_path"],
            )
            for row in rows
        ]

    def upsert(self, documents: Sequence[IndexDocument]) -> int:
        """Backfill and reindex path. The publish transaction writes chunks directly, because
        it must do so in the same transaction as the canonical flip."""
        for document in documents:
            self._session.execute(
                text(
                    """
                    INSERT INTO chunks (id, document_id, version_id, section_path,
                        citation_label, text, embedding, visibility, allowed_groups,
                        department, category_path, doc_class, doc_status, effective_from,
                        effective_to, tombstoned, ordinal, page)
                    VALUES (:id, :document_id, :version_id, :section_path, :citation_label,
                        :text, CAST(:embedding AS vector), CAST(:visibility AS visibility),
                        CAST(:allowed_groups AS TEXT[]), :department,
                        CAST(:category_path AS ltree), CAST(:doc_class AS doc_class),
                        CAST(:doc_status AS doc_status), :effective_from, :effective_to,
                        :tombstoned, :ordinal, :page)
                    ON CONFLICT (id) DO UPDATE SET
                        text = EXCLUDED.text,
                        embedding = EXCLUDED.embedding,
                        visibility = EXCLUDED.visibility,
                        allowed_groups = EXCLUDED.allowed_groups,
                        doc_status = EXCLUDED.doc_status,
                        tombstoned = EXCLUDED.tombstoned
                    """
                ),
                {
                    "id": document.chunk_id,
                    "document_id": document.document_id,
                    "version_id": document.version_id,
                    "section_path": document.section_path,
                    "citation_label": document.citation_label,
                    "text": document.text,
                    "embedding": _vector_literal(document.embedding or []),
                    "visibility": document.visibility,
                    "allowed_groups": document.allowed_groups,
                    "department": document.department,
                    "category_path": document.category_path,
                    "doc_class": document.doc_class,
                    "doc_status": document.doc_status,
                    "effective_from": document.effective_from,
                    "effective_to": document.effective_to,
                    "tombstoned": document.tombstoned,
                    "ordinal": int(document.extra.get("ordinal", 0)),
                    "page": int(document.extra.get("page", 1)),
                },
            )
        return len(documents)

    def tombstone(self, version_ids: Sequence[UUID]) -> int:
        if not version_ids:
            return 0
        result = self._session.execute(
            text(
                "UPDATE chunks SET tombstoned = TRUE "
                "WHERE version_id = ANY(CAST(:ids AS uuid[])) AND NOT tombstoned"
            ),
            {"ids": [str(version_id) for version_id in version_ids]},
        )
        return affected_rows(result)

    def delete_by_document(self, document_id: UUID) -> int:
        result = self._session.execute(
            text("DELETE FROM chunks WHERE document_id = :document_id"),
            {"document_id": document_id},
        )
        return affected_rows(result)


def _vector_literal(values: Sequence[float]) -> str:
    """pgvector's text input format. Passing a list would need the pgvector type adapter on
    every connection; the literal works with a plain cast."""
    return "[" + ",".join(f"{value:.6f}" for value in values) + "]"


@register_adapter(PortName.VECTOR_INDEX, "pgvector")
def build_pgvector_index(session: Session) -> PgVectorIndexAdapter:
    return PgVectorIndexAdapter(session)
