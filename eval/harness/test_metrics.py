"""Metric behaviour, especially the ACL cases where "nothing" is the right answer."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from metrics import (
    acl_violations,
    fact_coverage,
    mrr,
    ndcg_at_k,
    recall_at_k,
    summarize,
    summarize_coverage,
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


def test_fact_coverage_scores_the_documents_that_state_the_rule() -> None:
    assert fact_coverage(["a", "b", "c"], ["a", "b"]) == 1.0
    assert fact_coverage(["a", "c"], ["a", "b"]) == 0.5
    assert fact_coverage(["c"], ["a", "b"]) == 0.0


def test_an_unlabelled_fact_is_none_and_not_one() -> None:
    """The one place this differs from `recall_at_k`, which scores an empty expectation 1.0.

    There, "the correct answer is nothing" is a real expectation an ACL case depends on. Here
    it would be a lie in the flattering direction: a rule stated once is trivially covered, so
    counting unlabelled entries would let the headline number be raised by adding single-source
    queries rather than by covering anything.
    """
    assert fact_coverage(["a"], []) is None
    assert recall_at_k(["a"], [], 10) == 1.0


def test_the_summary_reports_how_many_facts_it_actually_measured() -> None:
    """1.00 over two entries and 1.00 over sixty are different findings, and only the count
    separates them."""
    summary = summarize_coverage([1.0, 0.5, None, None])

    assert summary["fact_coverage"] == 0.75
    assert summary["facts_measured"] == 2.0
    assert summary["facts_unlabelled"] == 2.0


def test_a_summary_with_nothing_labelled_is_not_a_pass() -> None:
    """Zero rather than one, so an emptied label set reads as a floor to investigate instead of
    a green run. The runner prints the count beside it and declines to gate on it."""
    summary = summarize_coverage([None, None])

    assert summary["fact_coverage"] == 0.0
    assert summary["facts_measured"] == 0.0
