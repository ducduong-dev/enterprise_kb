"""Retrieval metrics.

Shared by the nightly quality run, the M7 keyword-engine bake-off and the M2 acceptance
gate (recall@10 >= 0.85 on the seed golden set), so the comparison numbers always come from
one implementation.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of relevant items found in the top k. Undefined-when-empty is 1.0: a query
    whose correct answer is "nothing" (an ACL case) must not be scored as a miss."""
    relevant_set = set(relevant)
    if not relevant_set:
        return 1.0
    return len(set(retrieved[:k]) & relevant_set) / len(relevant_set)


def precision_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    if k <= 0:
        return 0.0
    relevant_set = set(relevant)
    return len(set(retrieved[:k]) & relevant_set) / min(k, max(len(retrieved), 1))


def mrr(retrieved: Sequence[str], relevant: Iterable[str]) -> float:
    relevant_set = set(relevant)
    for rank, item in enumerate(retrieved, start=1):
        if item in relevant_set:
            return 1.0 / rank
    return 0.0


def dcg(gains: Sequence[float]) -> float:
    return sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, start=1))


def ndcg_at_k(retrieved: Sequence[str], grades: Mapping[str, int], k: int) -> float:
    """Graded relevance. `grades` maps item id → 0..3; anything absent scores 0."""
    if not grades:
        return 1.0
    actual = dcg([float(grades.get(item, 0)) for item in retrieved[:k]])
    ideal = dcg(sorted((float(g) for g in grades.values()), reverse=True)[:k])
    return actual / ideal if ideal else 0.0


def acl_violations(retrieved: Sequence[str], forbidden: Iterable[str]) -> list[str]:
    """Any overlap is a blocking failure, never a score. One violation fails the build."""
    forbidden_set = set(forbidden)
    return [item for item in retrieved if item in forbidden_set]


def summarize(
    results: Sequence[tuple[Sequence[str], Mapping[str, int], Sequence[str]]], k: int = 10
) -> dict[str, float]:
    """Aggregate over (retrieved, grades, forbidden) triples."""
    if not results:
        return {"queries": 0.0}
    recalls, ndcgs, mrrs, violations = [], [], [], 0
    for retrieved, grades, forbidden in results:
        relevant = [item for item, grade in grades.items() if grade > 0]
        recalls.append(recall_at_k(retrieved, relevant, k))
        ndcgs.append(ndcg_at_k(retrieved, grades, k))
        mrrs.append(mrr(retrieved, relevant))
        violations += len(acl_violations(retrieved, forbidden))
    return {
        "queries": float(len(results)),
        f"recall@{k}": sum(recalls) / len(recalls),
        f"ndcg@{k}": sum(ndcgs) / len(ndcgs),
        "mrr": sum(mrrs) / len(mrrs),
        "acl_violations": float(violations),
    }


def fact_coverage(reached: Iterable[str], facts: Iterable[str]) -> float | None:
    """Share of the documents stating a fact that the response actually reached (ADR-0037).

    `reached` is every document the response put in front of the reader — the ranked chunks
    *and* the fact-set members, because coverage is the union and a member is not a lesser
    kind of source.

    **None when the entry has no fact label**, and that is the whole difference between this
    and `recall_at_k`. Recall scores an unlabelled query 1.0 by convention, which is right
    there: "the correct answer is nothing" is a real expectation an ACL case depends on. Here
    it would be a lie in the flattering direction — a rule stated once is trivially covered, so
    counting unlabelled entries would let the headline number be raised by adding single-source
    queries rather than by covering anything.
    """
    wanted = set(facts)
    if not wanted:
        return None
    return len(wanted & set(reached)) / len(wanted)


def summarize_coverage(scores: Sequence[float | None]) -> dict[str, float]:
    """Mean coverage over the entries that carry a label, and how many did not.

    Both numbers are reported because one without the other is unreadable: 1.00 over two
    entries and 1.00 over sixty are different findings, and the count is what separates them.
    """
    measured = [score for score in scores if score is not None]
    return {
        "fact_coverage": sum(measured) / len(measured) if measured else 0.0,
        "facts_measured": float(len(measured)),
        "facts_unlabelled": float(len(scores) - len(measured)),
    }
