"""Postgres full-text implementation of `KeywordIndexPort`.

The fallback backend: a deployment — or a developer — without the `pg_search` extension can
still run the whole publish → index → retrieve path, including the ACL sweep. A keyword
backend that only exists in production is a keyword backend whose ACL filtering is never
tested, and this is what keeps the platform runnable on a stock Postgres image.

It was explicitly **not** a bake-off candidate ([OPEN]-2, resolved in ADR-0021):
`to_tsvector`/`ts_rank_cd` is not BM25, and `simple` does no Vietnamese word segmentation.
Matching is diacritic-insensitive on both sides via the immutable `kb_unaccent` wrapper, which
is what makes "an toan von" find "an toàn vốn".
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
from sqlalchemy.orm import Session

from kb_ports.base import AdapterInfo
from kb_ports.indexes import IndexDocument, IndexHit
from kb_ports.registry import PortName, register_adapter

log = get_logger(__name__)

#: Words of context either side of a match in the returned snippet.
HIGHLIGHT_WORDS = 20

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
#: Single characters carry no signal and blow up the OR query.
_MIN_TERM = 2
#: A long query is a paste, not a search; the tail contributes nothing but cost.
_MAX_TERMS = 32


class PostgresFtsIndexAdapter:
    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            name="postgres_fts",
            version="simple+unaccent",
            extra={"bakeoff_candidate": False, "note": "dev/CI keyword backend"},
        )

    def health(self) -> bool:
        return bool(self._session.execute(text("SELECT 1")).scalar())

    def search(self, query: str, acl: ResolvedFilter, *, top_k: int = 50) -> list[IndexHit]:
        terms = _terms(query)
        if not terms:
            return []

        where, params = compile_sql(acl)
        # Two queries over the same terms: the matching one is diacritic-folded to hit the
        # index, the highlighting one keeps the tone marks so the snippet shown to the user
        # is the document's actual text, not a stripped rendering of it.
        params["match_query"] = " | ".join(_fold(term) for term in terms)
        params["highlight_query"] = " | ".join(terms)
        params["limit"] = top_k

        rows = self._session.execute(
            text(
                f"""
                WITH q AS (
                    SELECT to_tsquery('simple', :match_query) AS tsq,
                           to_tsquery('simple', :highlight_query) AS tsq_display
                )
                SELECT c.id, c.document_id, c.version_id, c.text, c.citation_label,
                       c.section_path,
                       ts_rank_cd(to_tsvector('simple', kb_unaccent(c.text)), q.tsq) AS score,
                       ts_headline('simple', c.text, q.tsq_display,
                           'MaxWords={HIGHLIGHT_WORDS}, MinWords=5, ShortWord=2,'
                           ' StartSel=<mark>, StopSel=</mark>, MaxFragments=2') AS highlight
                FROM chunks c, q
                WHERE {where}
                  AND to_tsvector('simple', kb_unaccent(c.text)) @@ q.tsq
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

    def upsert(self, documents: Sequence[IndexDocument]) -> int:
        """No-op: this backend reads the `chunks` table, which the publish transaction has
        already written. Returning the count keeps the port's contract honest."""
        return len(documents)

    def tombstone(self, version_ids: Sequence[UUID]) -> int:
        """Also a no-op — the publish transaction tombstones the rows this adapter reads.
        Two backends writing the same rows would be two chances to diverge."""
        return 0

    def delete_by_document(self, document_id: UUID) -> int:
        return 0


def _terms(query: str) -> list[str]:
    """Query terms, OR-ed rather than AND-ed.

    `websearch_to_tsquery` ANDs everything, so "quy trình nhận biết khách hàng" would only
    match a chunk containing all five words — a recall disaster on a corpus where the answer
    is usually one clause. OR plus `ts_rank_cd` gives recall and lets ranking sort it out,
    which is also how the BM25 backend behaves (a `should` array, one clause required).

    Splitting instrument shorthand ("TT41" → "tt", "41") is shared with the other keyword
    backends through `kb_vntext`, so one query cannot mean different things to different
    engines (bake-off gate 3).
    """
    return query_terms(query)


def _fold(term: str) -> str:
    decomposed = unicodedata.normalize("NFD", term)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.replace("đ", "d")


@register_adapter(PortName.KEYWORD_INDEX, "postgres_fts")
def build_postgres_fts_index(session: Session) -> PostgresFtsIndexAdapter:
    return PostgresFtsIndexAdapter(session)
