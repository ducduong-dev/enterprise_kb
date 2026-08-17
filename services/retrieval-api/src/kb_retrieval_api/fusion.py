"""Reciprocal Rank Fusion.

Keyword and vector search disagree, and their scores are not comparable — BM25 is unbounded,
cosine sits in [-1, 1], and both shift with corpus size. RRF ignores the scores and uses only
the ranks, which is why it needs no tuning per corpus and cannot be destabilised by one engine
returning unusually large numbers.

    score(d) = Σ_lists 1 / (k + rank_list(d))

`k` damps the head: with k=60, being first instead of second is worth much less than being
present in both lists at all. That is the property we want here — a chunk both engines
surface is more trustworthy than one either ranks first alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from kb_ports.indexes import IndexHit

#: Standard RRF constant from the original paper; robust enough that tuning it is rarely the
#: highest-value knob. If it ever needs changing, that is a bake-off finding, not a guess.
RRF_K = 60


@dataclass(slots=True)
class FusedHit:
    hit: IndexHit
    score: float
    #: Which retrievers found it, and where. Kept for the audit record and for debugging a
    #: "why did this rank here" question months later.
    ranks: dict[str, int] = field(default_factory=dict)

    @property
    def chunk_id(self) -> UUID:
        return self.hit.chunk_id


def reciprocal_rank_fusion(
    result_lists: dict[str, Sequence[IndexHit]], *, k: int = RRF_K
) -> list[FusedHit]:
    """Fuse named result lists into one ranking.

    A chunk found by several retrievers is merged into a single entry — never duplicated —
    and the merged entry keeps the richest payload available (highlights come from the
    keyword engine, text from whichever returned it).
    """
    fused: dict[UUID, FusedHit] = {}

    for source, hits in result_lists.items():
        for rank, hit in enumerate(hits, start=1):
            existing = fused.get(hit.chunk_id)
            if existing is None:
                fused[hit.chunk_id] = FusedHit(
                    hit=hit, score=1.0 / (k + rank), ranks={source: rank}
                )
            else:
                existing.score += 1.0 / (k + rank)
                existing.ranks[source] = rank
                existing.hit = _merge(existing.hit, hit)

    # Ties broken by chunk id so the order is deterministic: an eval run that reorders ties
    # between runs produces noise that looks like a regression.
    return sorted(fused.values(), key=lambda item: (-item.score, str(item.chunk_id)))


def _merge(primary: IndexHit, other: IndexHit) -> IndexHit:
    """Keep whichever fields are populated. Highlights only the keyword engine produces."""
    return IndexHit(
        chunk_id=primary.chunk_id,
        document_id=primary.document_id,
        version_id=primary.version_id,
        score=max(primary.score, other.score),
        text=primary.text or other.text,
        citation_label=primary.citation_label or other.citation_label,
        section_path=primary.section_path or other.section_path,
        article=primary.article if primary.article is not None else other.article,
        highlights=primary.highlights or other.highlights,
    )


def cap_per_document(hits: Iterable[FusedHit], limit: int) -> list[FusedHit]:
    """Keep at most `limit` chunks per document, preserving order.

    Without this, one long regulation fills the whole result set and the answer is built from
    a single source — which reads as confident and is exactly when it is most likely wrong.
    """
    seen: dict[UUID, int] = {}
    kept: list[FusedHit] = []
    for item in hits:
        count = seen.get(item.hit.document_id, 0)
        if count >= limit:
            continue
        seen[item.hit.document_id] = count + 1
        kept.append(item)
    return kept


#: A clause, addressed the way `clause_supersessions` addresses it. Never a chunk id: chunk ids
#: are re-minted by every rechunk, and the supersession record deliberately does not use them.
ClauseKey = tuple[UUID, str]


def drop_superseded(
    hits: Sequence[FusedHit], replacements: Mapping[ClauseKey, ClauseKey]
) -> tuple[list[FusedHit], list[FusedHit]]:
    """Remove a superseded clause when its replacement is here too (ADR-0033).

    Returns `(kept, dropped)`, because a caller that silently discarded passages would be
    unauditable — the dropped list goes to the audit record, so "why was this not in the
    answer" has an answer months later.

    **Only when the replacement is actually in the candidate set.** Dropping a superseded
    clause whose replacement the query did not find would leave the reader with nothing where
    they previously had something out of date, and out of date with a pointer is strictly more
    useful than absent. This is the whole condition: without it the answer quotes two different
    interest rates and leaves the customer to choose, which is the actual harm; with it applied
    unconditionally the answer would sometimes say nothing at all.

    Runs on the fused candidates rather than on the final selection, so the freed slot goes to
    the next-best passage instead of shortening the answer. The residual case — the replacement
    is here now and the reranker or the per-document cap cuts it later — leaves the reader with
    neither, and is accepted because the replacement outranking its own predecessor on a query
    that matched the predecessor is the ordinary outcome, not the exception.

    A chain (A replaced by B, B replaced by C, all three present) drops A and B on the same
    pass: each is judged against its own replacement, so no ordering of the map matters and
    nothing needs to walk the chain.
    """
    if not replacements:
        return list(hits), []

    present = {
        (item.hit.document_id, item.hit.section_path)
        for item in hits
        if item.hit.section_path is not None
    }
    kept: list[FusedHit] = []
    dropped: list[FusedHit] = []
    for item in hits:
        if item.hit.section_path is None:
            kept.append(item)
            continue
        replacement = replacements.get((item.hit.document_id, item.hit.section_path))
        if replacement is not None and replacement in present:
            dropped.append(item)
        else:
            kept.append(item)
    return kept, dropped
