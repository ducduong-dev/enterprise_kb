"""The retrieval funnel (INV-1).

Every read path in the platform — search UI, internal bot, external bot, citation click, graph
expansion — ends up in this class. That is the entire point: there is one place where "what may
this caller see" is decided, one place where it is compiled into the query, and one place where
the answer is logged well enough to reconstruct it later.

Pipeline:

    principal → ResolvedFilter (INV-2)
              → keyword search ∥ vector search, both filtered inside the query
              → RRF fusion
              → drop a superseded clause whose replacement is also here (ADR-0033)
              → rerank
              → per-document cap, top_k
              → supersession flags and replacement pointers
              → graph expansion, filtered by the same ACL (INV-10)
              → coverage: the other documents stating each seed's rule (ADR-0037)
              → audit record naming principal, delegate, filter, and every chunk returned (INV-11)

Nothing is filtered after the fact. If a chunk reaches this code, the index already decided the
caller may see it.

The supersession drop is the one place this pipeline removes a passage the filter admitted, and
it is not an access decision: the clause is still readable on its own document page and under
`as_of`, and the audit record names every passage it removed. It exists because an answer that
quotes a 2023 rate beside the 2026 rate that replaced it leaves the reader to choose, which is
worse than either rate alone.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from kb_authz.compile import compile_sql, compile_sql_expired, compile_sql_graph
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
    ExpiredMatch,
    FactMember,
    FactSet,
    GraphExpansion,
    ResolveAnchorRequest,
    ResolveAnchorResponse,
    ResolvedAnchor,
    RetrievedChunk,
    RetrieveRequest,
    RetrieveResponse,
    SupersededBy,
)
from kb_schemas.enums import RetrievalMode
from kb_vntext.language import fold_diacritics
from kb_vntext.legal_numbers import extract_legal_numbers, query_terms
from kb_vntext.sections import anchor_families, anchor_matches
from sqlalchemy import text
from sqlalchemy.orm import Session

from kb_retrieval_api.coverage import cover, load_seed, withheld_count
from kb_retrieval_api.fusion import (
    ClauseKey,
    FusedHit,
    cap_per_document,
    drop_superseded,
    reciprocal_rank_fusion,
)

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
#: How many expired instruments a refusal may name. A refusal listing nine repealed documents
#: is not more helpful than one naming the two that matter.
MAX_EXPIRED_MATCHES = 3
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
        # Before the reranker, so the freed slot goes to the next-best passage rather than
        # shortening the answer, and so the reranker is never asked to order a clause against
        # the clause that replaced it (ADR-0033).
        clause_supersessions = self._clause_supersessions(
            {item.hit.document_id for item in candidates}, acl.effective_on
        )
        candidates, dropped = drop_superseded(
            candidates, {old: new for old, (new, _) in clause_supersessions.items()}
        )
        ranked = self._rerank(request.query, candidates)
        selected = cap_per_document(ranked, MAX_CHUNKS_PER_DOCUMENT)[: request.top_k]

        document_ids = {item.hit.document_id for item in selected}
        superseded = self._superseded_articles(document_ids)
        titles = self._titles(document_ids)
        pointers = self._replacement_labels(clause_supersessions, acl)
        chunks = [
            RetrievedChunk(
                chunk_id=item.hit.chunk_id,
                version_id=item.hit.version_id,
                document_id=item.hit.document_id,
                citation_label=item.hit.citation_label,
                document_title=titles.get(item.hit.document_id),
                section_path=item.hit.section_path,
                article=item.hit.article,
                text=item.hit.text or "",
                score=round(item.score, 6),
                highlights=list(item.hit.highlights),
                supersession_flag=_is_superseded(superseded, item.hit),
                # A clause that survived the drop because its replacement was not retrieved is
                # still superseded, and saying so is the point: out of date with a pointer is
                # more useful than out of date in silence.
                superseded_by=pointers.get((item.hit.document_id, item.hit.section_path or "")),
            )
            for item in selected
        ]

        expansions = (
            self._expand(acl, [item.hit.document_id for item in selected])
            if request.expand_graph
            else []
        )

        # Only when the funnel found nothing current. An expired instrument is invisible to the
        # query above — its chunks failed the effectivity predicate — so without this the caller
        # cannot tell "the bank never said anything about this" from "the bank said it, and it
        # stopped applying in December" (ADR-0028/0030).
        expired = self._expired_matches(request.query, acl) if not chunks else []

        # After the drop, so a fact set never gains a member the fusion stage just removed, and
        # after the per-document cap, so the seeds are the passages the answer is built from.
        fact_sets, withheld = self._cover(acl, chunks, request) if request.cover_facts else ([], 0)

        elapsed = time.perf_counter() - started
        retrieval_latency.labels(stage="total").observe(elapsed)
        retrieval_results.observe(len(chunks))

        self._audit_retrieval(
            principal,
            acl,
            chunks,
            expansions,
            request,
            dropped,
            fact_sets,
            withheld,
            duration_ms=int(elapsed * 1000),
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
                "expired_matches": len(expired),
                "fact_members": sum(len(f.members) for f in fact_sets),
                "mode": request.mode.value,
                "duration_ms": int(elapsed * 1000),
            },
        )
        return RetrievalOutcome(
            response=RetrieveResponse(
                chunks=chunks,
                expansions=expansions,
                expired_matches=expired,
                fact_sets=fact_sets,
                resolved_filter_id=acl.filter_id,
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
                       c.section_path, c.article,
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
                article=row["article"],
                text=row["text"],
                score=float(row["score"]),
            )
            for row in rows
        ]
        superseded = self._superseded_articles({chunk.document_id for chunk in chunks})
        for chunk in chunks:
            chunk.supersession_flag = _is_superseded_article(
                superseded, chunk.document_id, chunk.article
            )

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

    def _cover(
        self, acl: ResolvedFilter, chunks: list[RetrievedChunk], request: RetrieveRequest
    ) -> tuple[list[FactSet], int]:
        """The fact set for each of the top seeds (M10, ADR-0037).

        Runs last, after the drop and after the per-document cap, for two reasons. The seeds
        are then the passages the answer is actually built from rather than whatever ranking
        put in front of them; and a fact set cannot re-admit a clause the supersession drop
        just removed, which would undo the drop through a side door.

        Members already present in `chunks` are excluded: they are not *coverage*, they are the
        ranking, and counting them would make a set look wider than the corpus it reached.

        A confirmed supersession inside a set is labelled, not removed. The set answers "who
        else states this rule", and a clause that stated it until last January is part of that
        answer — with its pointer, so nobody reads it as current.
        """
        seeds = chunks[: request.fact_seeds]
        if not seeds:
            return [], 0

        present = {chunk.chunk_id for chunk in chunks}
        sets: list[FactSet] = []
        withheld = 0
        for chunk in seeds:
            seed = load_seed(self._session, chunk.chunk_id)
            if seed is None:  # pragma: no cover - the chunk came from this database
                continue
            coverage = cover(self._session, acl, seed)
            members = [m for m in coverage.members if m.chunk_id not in present]
            withheld += withheld_count(self._session, seed, coverage.members)
            if not members:
                continue
            pointers = self._replacement_labels(
                self._clause_supersessions({m.document_id for m in members}, acl.effective_on),
                acl,
            )
            sets.append(
                FactSet(
                    seed_chunk_id=chunk.chunk_id,
                    members=[
                        FactMember(
                            chunk_id=m.chunk_id,
                            document_id=m.document_id,
                            version_id=m.version_id,
                            document_title=m.document_title,
                            citation_label=m.citation_label,
                            section_path=m.section_path,
                            text=m.text,
                            channel=m.channel,
                            superseded_by=pointers.get((m.document_id, m.section_path or "")),
                        )
                        for m in members
                    ],
                    truncated=coverage.truncated,
                )
            )
            present.update(m.chunk_id for m in members)
        return sets, withheld

    def _clause_supersessions(
        self, document_ids: set[uuid.UUID], on: date
    ) -> dict[ClauseKey, tuple[ClauseKey, date]]:
        """Confirmed clause supersessions in force on `on`, keyed by the clause they replace.

        Three conditions, and each is load-bearing:

        * **`state = 'confirmed'`.** A proposal changes nothing a user can see. That is the
          whole of ADR-0033 and it is enforced here rather than trusted upstream, because this
          is the only place the funnel's output could ever reach a reader.
        * **`closed_at IS NULL`**, so a revoked or superseded belief is not consulted. The open
          row is the current belief; an earlier one wearing today's date would be a lie the
          answer repeats.
        * **`supersedes_from <= :on`**, evaluated against the query's own effective date and
          never against "now". An `as_of` query before the replacement took effect must show
          the older clause as current and *unflagged* — that is the property the whole expiry
          programme exists to establish, and a pointer rendered on that query would break it
          just as surely as hiding the clause would.

        Raw SQL rather than `ClauseSupersessions.served`, because retrieval-api does not depend
        on the registry and should not start: this is a read path and that is a write path. The
        two queries are asserted to agree in `tests/test_clause_supersession_flow.py`, the same
        arrangement `_superseded_articles` has with `kb_registry.publish`.

        A replacement whose document the caller may not read is a separate problem and is
        handled where the label is fetched, not here.
        """
        if not document_ids:
            return {}
        rows = self._session.execute(
            text(
                """
                SELECT old_document_id, old_section_path, new_document_id, new_section_path,
                       supersedes_from
                FROM clause_supersessions
                WHERE old_document_id = ANY(CAST(:ids AS uuid[]))
                  AND closed_at IS NULL
                  AND state = 'confirmed'
                  AND new_document_id IS NOT NULL
                  AND supersedes_from <= :on
                """
            ),
            {"ids": [str(document_id) for document_id in document_ids], "on": on},
        ).mappings()
        return {
            (row["old_document_id"], row["old_section_path"]): (
                (row["new_document_id"], row["new_section_path"]),
                row["supersedes_from"],
            )
            for row in rows
        }

    def _replacement_labels(
        self, supersessions: dict[ClauseKey, tuple[ClauseKey, date]], acl: ResolvedFilter
    ) -> dict[ClauseKey, SupersededBy]:
        """Turn each pointer into something an answer can say out loud — as far as it may.

        Two halves with different rules. **That** a clause was replaced is a fact about the
        clause the caller is already reading, and it is always returned: without it they act on
        a stale figure with no reason to doubt it. **What** replaced it names another document,
        and naming a document the filter excluded would disclose its existence — which is
        exactly what `compile_sql_graph` refuses to do for an edge, on the same INV-10
        reasoning, and what `[OPEN]`-3 leaves defaulting to "do not disclose".

        So the identity is fetched through the caller's own chunk filter. A replacement they
        may read yields the citation label and title, so the sentence is "đã được thay thế bởi
        Điều 7, Biểu phí dịch vụ 2026" rather than a UUID. Anything else yields a bare pointer
        carrying only the date.

        "Anything else" deliberately includes a case that is not an access decision: a
        replacement whose chunk has been rechunked away or tombstoned resolves to nothing here
        and is also left unnamed. It would be possible to recover its title from `documents`
        and name it anyway — but only by hand-writing a second ACL predicate over that table,
        and `compile_sql_graph`'s own docstring says why that is a bad trade. One audited
        predicate that occasionally says less is worth more than two that can disagree.
        """
        if not supersessions:
            return {}
        keys = {new for new, _ in supersessions.values()}
        where, params = compile_sql(acl, alias="c")
        rows = self._session.execute(
            text(
                f"""
                SELECT c.document_id, c.section_path, c.citation_label, d.title
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.document_id = ANY(CAST(:ids AS uuid[]))
                  AND c.section_path = ANY(CAST(:paths AS text[]))
                  AND {where}
                """
            ),
            {
                **params,
                "ids": [str(document_id) for document_id, _ in keys],
                "paths": [section_path for _, section_path in keys],
            },
        ).mappings()
        visible = {
            (row["document_id"], row["section_path"]): (row["citation_label"], row["title"])
            for row in rows
        }
        pointers: dict[ClauseKey, SupersededBy] = {}
        for old, (new, supersedes_from) in supersessions.items():
            if new not in visible:
                pointers[old] = SupersededBy(supersedes_from=supersedes_from)
                continue
            citation_label, title = visible[new]
            pointers[old] = SupersededBy(
                supersedes_from=supersedes_from,
                document_id=new[0],
                section_path=new[1],
                document_title=title,
                citation_label=citation_label,
            )
        return pointers

    def _superseded_articles(self, document_ids: set[uuid.UUID]) -> dict[uuid.UUID, set[int]]:
        """Which articles of these documents an unconsolidated amendment targets.

        `document_id → set[int]`, and **an empty set means the whole document** — which is
        exactly the old behaviour, so an edge with no article detail loses nothing.

        Flagged rather than hidden: the text is still the canonical text, and hiding it would
        leave the user with nothing. The warning is what makes quoting it safe, and it clears
        when the Legal cell approves the consolidation (M5) — which writes a `consolidates`
        edge, the `NOT EXISTS` clause below.

        Narrowed to articles because an amendment usually touches two or three of sixty, and a
        banner over all sixty is a banner nobody reads — while the one article it was right
        about is the one nobody notices (ADR-0032).

        The same predicate lives in `kb_registry.publish.SUPERSEDED_PREDICATE`; the two are
        asserted to agree at article granularity in `tests/test_consolidation_flow.py`.
        """
        if not document_ids:
            return {}
        rows = self._session.execute(
            text(
                """
                SELECT r.dst_document_id, r.articles
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
        ).all()

        superseded: dict[uuid.UUID, set[int]] = {}
        for row in rows:
            articles = set(row.articles or ())
            if row.dst_document_id not in superseded:
                superseded[row.dst_document_id] = articles
            elif not articles or not superseded[row.dst_document_id]:
                # One amendment naming no articles outranks another that named some: the
                # unspecific edge means "somewhere in here", and narrowing it to the articles a
                # *different* amendment happened to name would under-warn.
                superseded[row.dst_document_id] = set()
            else:
                superseded[row.dst_document_id] |= articles
        return superseded

    def resolve_anchors(
        self, principal: Principal, request: ResolveAnchorRequest
    ) -> tuple[ResolveAnchorResponse, ResolvedFilter]:
        """Resolve a parsed reference to the clauses it names.

        Public because a reference is a first-class read path: the portal follows one, chat-api
        follows one, and both must go through the funnel rather than reading `chunks` directly
        (INV-1).
        """
        acl = self._filters.build(
            principal,
            mode=RetrievalMode.AS_OF if request.as_of_date else RetrievalMode.CURRENT,
            as_of_date=request.as_of_date,
        )
        resolved, unresolved = self._resolve(acl, request.document_id, request.anchors)
        self._write_audit(
            AuditRecord(
                action=AuditAction.CITATION_LOOKUP,
                actor=principal.audit_actor,
                on_behalf_of=principal.audit_on_behalf_of,
                object_ref={
                    "document_id": str(request.document_id),
                    "chunk_ids": [str(item.chunk_id) for item in resolved],
                },
                resolved_filter=acl.as_dict(),
                detail={"anchors": list(request.anchors), "unresolved": unresolved},
            )
        )
        return (
            ResolveAnchorResponse(
                resolved=resolved, unresolved=unresolved, resolved_filter_id=acl.filter_id
            ),
            acl,
        )

    def _resolve(
        self, acl: ResolvedFilter, document_id: uuid.UUID, anchors: Sequence[str]
    ) -> tuple[list[ResolvedAnchor], list[str]]:
        """The equality join: `(document_id, anchor)` against the target's serving chunks.

        Under the **chunk** predicate, not the graph one. `compile_sql_graph` omits status and
        effectivity because it answers "may this person know the target exists"; returning the
        target's text is a different question, and it takes the full predicate — including
        effectivity, so a reference into a repealed clause resolves to nothing on the default
        path and still resolves under `as_of` (ADR-0036).

        No threshold and nothing to tune: the document is already identified and the anchor is
        already structured. `citation_lookup` keeps trigram matching for the free-text case,
        which is the only case that needs it.
        """
        wanted = [anchor.strip() for anchor in anchors if anchor.strip()]
        if not wanted:
            return [], []

        # An anchor addresses a *location*, and a location contains everything under it:
        # "12" is Điều 12 and every clause of it, "12.2" is that clause. So the match is the
        # anchor itself or anything below it, which is a prefix on the dotted form — and it
        # is exact where a naked prefix would not be: "12" reaches "12.1" and never "12a",
        # because Điều 12a is a different article that amendments insert routinely.
        families = anchor_families(wanted)
        where, params = compile_sql(acl)
        params["document_id"] = str(document_id)
        params["families"] = families
        rows = (
            self._session.execute(
                text(
                    f"""
                SELECT c.id, c.version_id, c.anchor, c.citation_label, c.text, c.ordinal,
                       v.published_at
                FROM chunks c
                JOIN document_versions v ON v.id = c.version_id
                CROSS JOIN unnest(CAST(:families AS TEXT[])) AS f(family)
                WHERE c.document_id = CAST(:document_id AS uuid)
                  AND c.anchor IS NOT NULL
                  AND (c.anchor = f.family OR c.anchor LIKE f.family || '.%')
                  AND {where}
                ORDER BY c.ordinal
                """
                ),
                params,
            )
            .mappings()
            .all()
        )

        # Two maps, because the fallback below must be narrower than the match.
        under: dict[str, list[Any]] = {}  # the anchor and everything beneath it
        exact: dict[str, list[Any]] = {}  # only a chunk anchored at precisely that location
        for row in rows:
            anchor = str(row["anchor"])
            for family in families:
                if anchor_matches(anchor, family):
                    bucket = under.setdefault(family, [])
                    if all(item["id"] != row["id"] for item in bucket):
                        bucket.append(row)
            exact.setdefault(anchor, []).append(row)

        resolved: list[ResolvedAnchor] = []
        unresolved: list[str] = []
        detected_at = self._edge_detected_at(document_id)
        for anchor in wanted:
            # The clause → article fallback. The chunker merges two short clauses into one
            # chunk and backs its anchor off to their common article, so a reference to
            # "khoản 2 Điều 12" can find no chunk at "12.2" while the text sits under "12".
            # Reporting that unresolved would tell a steward a good reference is broken.
            #
            # It falls back to a chunk anchored at the article *exactly* — the merged-clause
            # signature — and never to the article's other clauses. Khoản 1 is not an answer
            # to a reference naming khoản 2, and it is what a filtered-out clause would
            # otherwise silently resolve to: a clause the caller may not read, or one that has
            # been repealed, must come back unresolved rather than as its neighbour.
            hits = under.get(anchor) or exact.get(anchor.split(".")[0]) or []
            if not hits:
                unresolved.append(anchor)
                continue
            for hit in hits:
                published_at: datetime | None = hit["published_at"]
                resolved.append(
                    ResolvedAnchor(
                        # Labelled with what was asked for, not what matched: the reader asked
                        # about khoản 2 and deserves to see that, even when the passage that
                        # answers it covers the whole article.
                        anchor=anchor,
                        chunk_id=hit["id"],
                        version_id=hit["version_id"],
                        citation_label=hit["citation_label"],
                        excerpt=_excerpt(str(hit["text"] or "")),
                        stale=bool(
                            detected_at is not None
                            and published_at is not None
                            and detected_at < published_at
                        ),
                    )
                )
        return resolved, unresolved

    def _edge_detected_at(self, document_id: uuid.UUID) -> datetime | None:
        """When the *earliest* reference into this document was read.

        Used only to mark an anchor stale. Earliest rather than latest because staleness is a
        warning and under-warning is the worse failure here: if any reference into this target
        predates its current text, the reader deserves to know the numbering may have moved.
        """
        return self._session.execute(
            text("SELECT min(created_at) FROM document_refs WHERE dst_document_id = :id"),
            {"id": document_id},
        ).scalar()

    def _expired_matches(self, query: str, acl: ResolvedFilter) -> list[ExpiredMatch]:
        """What would have answered, had it not ceased to apply.

        Lexical only, and on purpose: this runs when the answer is already going to be a
        refusal, so it must be cheap, and its job is to recognise the instrument a reader is
        asking about rather than to rank passages. It never returns text — the refusal names
        the document and the date, and anyone who wants the repealed wording asks for it
        through `as_of`, which is audited.
        """
        terms = query_terms(query)
        if not terms:
            return []
        where, params = compile_sql_expired(acl)
        params["probe_query"] = " | ".join(fold_diacritics(term) for term in terms)
        params["probe_limit"] = MAX_EXPIRED_MATCHES
        rows = self._session.execute(
            text(
                f"""
                SELECT DISTINCT ON (c.document_id)
                       c.document_id, c.citation_label, c.effective_to, d.title
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE {where}
                  AND to_tsvector('simple', kb_unaccent(c.text))
                      @@ to_tsquery('simple', :probe_query)
                ORDER BY c.document_id, c.ordinal
                LIMIT :probe_limit
                """
            ),
            params,
        ).all()
        return [
            ExpiredMatch(
                document_id=row.document_id,
                document_title=row.title,
                citation_label=row.citation_label,
                expired_on=row.effective_to,
            )
            for row in rows
        ]

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
                       d.legal_number, r.anchors
                FROM graph_serving g
                JOIN documents d ON d.id = g.dst_document_id
                LEFT JOIN document_refs r
                       ON r.src_document_id = g.src_document_id
                      AND r.dst_document_id = g.dst_document_id
                      AND r.ref_type = g.ref_type
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
        expansions: list[GraphExpansion] = []
        for row in rows:
            # Resolved here rather than left to the caller: a reference the platform parsed
            # itself should arrive as the clause it names, and resolving it anywhere else
            # would be a second read path with its own filter (INV-1).
            resolved, unresolved = self._resolve(acl, row["dst_document_id"], row["anchors"] or [])
            expansions.append(
                GraphExpansion(
                    document_id=row["dst_document_id"],
                    ref_type=row["ref_type"],
                    summary=row["dst_summary"],
                    citation_label=row["legal_number"],
                    anchors=resolved,
                    unresolved_anchors=unresolved,
                )
            )
        return expansions

    def _audit_retrieval(
        self,
        principal: Principal,
        acl: ResolvedFilter,
        chunks: list[RetrievedChunk],
        expansions: list[GraphExpansion],
        request: RetrieveRequest,
        dropped: list[FusedHit],
        fact_sets: list[FactSet],
        withheld: int,
        *,
        duration_ms: int,
    ) -> None:
        """INV-11: enough to reconstruct this answer months later.

        `superseded_dropped` is why this needs the dropped list. A passage the funnel removed
        because its replacement was also retrieved is invisible in the response by design, and
        "why was this clause not in the answer" is exactly the question somebody asks months
        later. Unlike the ACL's silent counter (ADR-0023), there is nothing to disclose here —
        the caller could read the clause on its document page — so it is recorded in full.
        """
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
                    "superseded_dropped": [str(item.hit.chunk_id) for item in dropped],
                    "fact_member_ids": [
                        str(m.chunk_id) for fact_set in fact_sets for m in fact_set.members
                    ],
                },
                resolved_filter=acl.audit_payload(),
                detail={
                    # The query is user text and belongs in the audit record, which is
                    # access-controlled — never in a metric label or an ordinary log line.
                    "query": request.query,
                    "mode": request.mode.value,
                    "top_k": request.top_k,
                    "duration_ms": duration_ms,
                    # How many documents the *filter* kept out of the fact sets. Audit-only and
                    # never returned: a reviewer asking why an answer missed something has to
                    # tell "we did not have it" from "they could not see it" (INV-11), and a
                    # count in the response would let the filter's work be inferred (ADR-0023).
                    "fact_members_withheld": withheld,
                    "fact_members_truncated": sum(f.truncated for f in fact_sets),
                },
            )
        )

    def _write_audit(self, record: AuditRecord) -> None:
        if self._audit is not None:
            self._audit.write(record)


def _chunk_acl(acl: ResolvedFilter, alias: str = "c") -> tuple[str, dict[str, object]]:
    from kb_authz.compile import compile_sql

    return compile_sql(acl, alias=alias)


def _is_superseded(superseded: dict[uuid.UUID, set[int]], hit: IndexHit) -> bool:
    return _is_superseded_article(superseded, hit.document_id, hit.article)


def _is_superseded_article(
    superseded: dict[uuid.UUID, set[int]], document_id: uuid.UUID, article: int | None
) -> bool:
    """Whether *this* chunk carries the warning.

    An empty article set means the whole document. A chunk with no article of its own — front
    matter, an appendix, a table before Điều 1 — is flagged only in that case, which is the
    right default: an amendment to Điều 12 does not put the appendix out of date.
    """
    if document_id not in superseded:
        return False
    articles = superseded[document_id]
    if not articles:
        return True
    return article is not None and article in articles


#: How much of a resolved clause travels back with the reference. Enough to recognise it,
#: never enough to answer from — quoting the text is a separate act through the ordinary path.
EXCERPT_CHARS = 240


def _excerpt(body: str) -> str | None:
    text_only = " ".join(body.split())
    if not text_only:
        return None
    if len(text_only) <= EXCERPT_CHARS:
        return text_only
    return text_only[:EXCERPT_CHARS].rstrip() + "…"
