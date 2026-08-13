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
