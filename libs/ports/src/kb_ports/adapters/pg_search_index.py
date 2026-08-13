"""ParadeDB `pg_search` implementation of `KeywordIndexPort` — the second [OPEN]-2 candidate.

The case for it is structural rather than linguistic: the chunks are already in Postgres, so
BM25 over the same rows means **no second store**. Nothing to keep in step, nothing to fall
behind, no window in which the keyword engine disagrees with the registry about what is
canonical. The ACL predicate is an ordinary SQL `WHERE` in the same statement as the search,
which is the strongest form INV-2 can take: there is no second system that could be queried
without it.

The case against it is that Tantivy's tokenizers are byte-oriented and know nothing about
Vietnamese diacritics, so folding has to exist as data — `ops/pg_search/install.sql` adds
generated `*_folded` columns and indexes them beside the originals, which is what lets "an
toan von" find "an toàn vốn" while a correctly-typed query still ranks exact matches first.

Reads the `chunks` table the publish transaction writes, so `upsert`/`tombstone` are no-ops for
the same reason they are in the FTS adapter: two writers of one row are two chances to diverge.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from uuid import UUID

from kb_authz.compile import compile_sql
from kb_authz.filters import ResolvedFilter
from kb_common.logging import get_logger
from kb_vntext.legal_numbers import query_terms
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from kb_ports.base import AdapterInfo
from kb_ports.indexes import IndexDocument, IndexHit
from kb_ports.registry import PortName, register_adapter

log = get_logger(__name__)

#: Characters of context around a match in the returned snippet.
SNIPPET_CHARS = 200
#: Field weights. The citation label is short and precise: a query naming an article is asking
#: for that article, not for every clause that mentions it in passing.
BOOST_TEXT = 2.0
BOOST_FOLDED = 1.0
BOOST_LABEL = 4.0
BOOST_PHRASE = 3.0

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_MIN_TERM = 2
_MAX_TERMS = 32
#: Tantivy rejects these inside a raw query string; the structured builders below avoid them,
#: but a legal number reaching a `match` value still needs its slashes to survive.
_ESCAPE = str.maketrans({ch: f"\\{ch}" for ch in '+^`:{}"[]()~!\\'})


class PgSearchIndexAdapter:
    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            name="pg_search",
            version="bm25",
            extra={"bakeoff_candidate": True, "store": "postgres"},
        )

    def health(self) -> bool:
        """True only when the extension *and* its index are installed.

        A pg_search deployment missing `ops/pg_search/install.sql` would answer every query
        with a plain sequential scan and no scores — slower, differently ranked, and silent.
        """
        installed = self._session.execute(
            text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_search'")
        ).scalar()
        if not installed:
            return False
        indexed = self._session.execute(
            text("SELECT count(*) FROM pg_class WHERE relname = 'chunks_bm25'")
        ).scalar()
        return bool(indexed)

    def search(self, query: str, acl: ResolvedFilter, *, top_k: int = 50) -> list[IndexHit]:
        terms = _terms(query)
        if not terms:
            return []

        where, params = compile_sql(acl)
        params["query"] = " ".join(terms)
        params["folded"] = " ".join(_fold(term) for term in terms)
        params["limit"] = top_k
        params["snippet_chars"] = SNIPPET_CHARS

        # One statement: the ACL predicate and the BM25 predicate are evaluated by the same
        # planner against the same rows. There is no order of operations in which a chunk is
        # scored, returned, and filtered afterwards (INV-2).
        rows = self._session.execute(
            text(
                f"""
                SELECT c.id, c.document_id, c.version_id, c.text, c.citation_label,
                       c.section_path,
                       paradedb.score(c.id) AS score,
                       paradedb.snippet(c.text, '<mark>', '</mark>', :snippet_chars) AS highlight
                FROM chunks c
                WHERE {where}
                  AND c.id @@@ paradedb.boolean(
                        should => ARRAY[
                            paradedb.boost({BOOST_TEXT}, paradedb.match('text', :query)),
                            paradedb.boost(
                                {BOOST_FOLDED}, paradedb.match('text_folded', :folded)
                            ),
                            paradedb.boost(
                                {BOOST_LABEL}, paradedb.match('citation_label', :query)
                            ),
                            paradedb.boost(
                                {BOOST_LABEL},
                                paradedb.match('citation_label_folded', :folded)
                            ),
                            paradedb.boost(
                                {BOOST_PHRASE}, paradedb.match('text', :query,
                                                               conjunction_mode => true)
                            )
                        ]
                  )
                ORDER BY score DESC, c.ordinal
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
                highlights=(row["highlight"],) if row["highlight"] else (),
            )
            for row in rows
        ]

    def explain(self, query: str, acl: ResolvedFilter, *, top_k: int = 50) -> str:
        """The execution plan for a search. The bake-off records it as evidence that the ACL
        predicate is applied inside the query rather than to its results (protocol gate 5)."""
        where, params = compile_sql(acl)
        params.update(
            {
                "query": " ".join(_terms(query)),
                "folded": " ".join(_fold(term) for term in _terms(query)),
                "limit": top_k,
            }
        )
        try:
            rows = self._session.execute(
                text(
                    f"""
                    EXPLAIN (ANALYZE, BUFFERS)
                    SELECT c.id, paradedb.score(c.id) AS score
                    FROM chunks c
                    WHERE {where}
                      AND c.id @@@ paradedb.match('text', :query)
                    ORDER BY score DESC
                    LIMIT :limit
                    """
                ),
                params,
            ).all()
        except DatabaseError as exc:  # pragma: no cover - diagnostic path
            return f"EXPLAIN failed: {exc}"
        return "\n".join(str(row[0]) for row in rows)

    def upsert(self, documents: Sequence[IndexDocument]) -> int:
        """No-op: the publish transaction already wrote these rows, and the BM25 index is
        maintained by Postgres. That is the entire argument for this backend."""
        return len(documents)

    def tombstone(self, version_ids: Sequence[UUID]) -> int:
        return 0

    def delete_by_document(self, document_id: UUID) -> int:
        return 0


def _terms(query: str) -> list[str]:
    """Shared with the other keyword backends, so "TT41" behaves the same in all of them."""
    return query_terms(query)


def _fold(term: str) -> str:
    decomposed = unicodedata.normalize("NFD", term)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.replace("đ", "d").translate(_ESCAPE)


@register_adapter(PortName.KEYWORD_INDEX, "pg_search")
def build_pg_search_index(session: Session) -> PgSearchIndexAdapter:
    return PgSearchIndexAdapter(session)
