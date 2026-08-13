"""Metric behaviour, especially the ACL cases where "nothing" is the right answer."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from metrics import (
    acl_violations,
    mrr,
    ndcg_at_k,
    recall_at_k,
    summarize,
)


def test_recall_counts_only_the_top_k() -> None:
    assert recall_at_k(["a", "b", "c"], ["a", "c"], k=2) == 0.5
    assert recall_at_k(["a", "b", "c"], ["a", "c"], k=3) == 1.0


def test_empty_expected_set_scores_perfect() -> None:
    """A query whose correct result is "nothing" (restricted content) must not be a miss."""
    assert recall_at_k([], [], k=10) == 1.0
    assert ndcg_at_k([], {}, k=10) == 1.0


def test_ndcg_rewards_ordering() -> None:
    grades = {"a": 3, "b": 1}
    assert ndcg_at_k(["a", "b"], grades, k=10) == 1.0
    assert ndcg_at_k(["b", "a"], grades, k=10) < 1.0


def test_mrr_uses_the_first_hit() -> None:
    assert mrr(["x", "a"], ["a"]) == 0.5
    assert mrr(["x", "y"], ["a"]) == 0.0


def test_acl_violations_are_listed_not_scored() -> None:
    assert acl_violations(["a", "canary-1"], ["canary-1"]) == ["canary-1"]
    assert acl_violations(["a"], ["canary-1"]) == []


def test_summary_surfaces_violations() -> None:
    summary = summarize(
        [
            (["a", "b"], {"a": 3}, ["canary-1"]),
            (["canary-1"], {}, ["canary-1"]),
        ],
        k=10,
    )
    assert summary["queries"] == 2.0
    assert summary["acl_violations"] == 1.0
