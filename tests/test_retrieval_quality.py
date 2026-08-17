"""Retrieval quality gate (M2 acceptance criterion: recall@10 ≥ 0.85 on the seed golden set).

Runs the same harness `make eval` runs, in-process, so a regression fails the build rather
than a nightly report. The golden set doubles as the ACL sweep: a forbidden document in the
results fails here too, whatever the quality numbers say.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval" / "harness"))

pytestmark = pytest.mark.integration


def test_recall_meets_the_acceptance_threshold(pristine_corpus: Engine) -> None:
    from run import DEFAULT_K, RECALL_THRESHOLD, evaluate, load_golden

    results = evaluate(load_golden(ROOT / "eval" / "golden_set"), DEFAULT_K)
    assert results, "the golden set is empty"

    recall = sum(result.recall for result in results) / len(results)
    assert recall >= RECALL_THRESHOLD, (
        f"recall@{DEFAULT_K} {recall:.3f} is below the {RECALL_THRESHOLD} threshold"
    )


@pytest.mark.acl_sweep
def test_the_golden_set_produces_no_acl_violations(pristine_corpus: Engine) -> None:
    from run import DEFAULT_K, evaluate, load_golden

    results = evaluate(load_golden(ROOT / "eval" / "golden_set"), DEFAULT_K)
    offenders = {result.query_id: result.violations for result in results if result.violations}
    assert not offenders, f"forbidden documents returned: {offenders}"


def test_every_graded_document_exists_in_the_corpus(pristine_corpus: Engine) -> None:
    """A golden entry naming a document that was never seeded silently scores as a miss."""
    from run import document_key_map, load_golden

    known = set(document_key_map().values())
    for entry in load_golden(ROOT / "eval" / "golden_set"):
        for item in entry.get("relevant") or []:
            assert item["doc"] in known, f"{entry['id']} grades unknown document {item['doc']!r}"
        for doc in entry.get("forbidden") or []:
            assert doc in known, f"{entry['id']} forbids unknown document {doc!r}"


def test_fact_coverage_meets_the_acceptance_threshold(pristine_corpus: Engine) -> None:
    """M10's headline number (ADR-0037): of the documents that state the answer's rule, how
    many did the response actually reach.

    Distinct from recall, which asks whether the *ranking* found the graded passages. A fact
    can be covered by a passage ranking nowhere — which is the case the coverage pass exists
    for, and q002 is that case in this set: the English query ranks the internal policy first
    and the regulator's own article arrives through the fact set.
    """
    from run import COVERAGE_THRESHOLD, DEFAULT_K, evaluate, load_golden

    results = evaluate(load_golden(ROOT / "eval" / "golden_set"), DEFAULT_K)
    measured = [result for result in results if result.coverage is not None]
    assert measured, "no golden-set entry carries a `facts` label; coverage is unmeasured"

    coverage = sum(result.coverage or 0.0 for result in measured) / len(measured)
    missed = {r.query_id: r.coverage for r in measured if (r.coverage or 0.0) < 1.0}
    assert coverage >= COVERAGE_THRESHOLD, (
        f"fact coverage {coverage:.3f} over {len(measured)} labelled fact(s) is below "
        f"{COVERAGE_THRESHOLD}; incomplete: {missed}"
    )


def test_an_unlabelled_entry_is_not_scored_as_covered(pristine_corpus: Engine) -> None:
    """The failure mode this metric could otherwise hide. A rule stated once is trivially
    covered, so scoring unlabelled entries 1.0 would let the headline number be raised by
    adding single-source queries rather than by covering anything."""
    from run import DEFAULT_K, evaluate, load_golden

    results = evaluate(load_golden(ROOT / "eval" / "golden_set"), DEFAULT_K)
    unlabelled = [result for result in results if result.coverage is None]

    assert unlabelled, "expected some entries to carry no fact label"
    assert all(result.coverage is None for result in unlabelled)


@pytest.mark.acl_sweep
def test_a_fact_set_never_reaches_a_document_the_ranking_may_not(
    pristine_corpus: Engine,
) -> None:
    """The sharpest ACL case in the corpus, and the reason the canaries were given the capital
    documents' subject key.

    A fact set is a second query, and the coverage channels join on subject key and reference
    rather than on relevance — so a canary that *shares a subject* with a legitimately
    retrieved clause is reachable by construction unless the filter is compiled into every
    channel. `retrieved` proves ranking excluded it; `reached` is what proves coverage did too.
    """
    from run import DEFAULT_K, evaluate, load_golden

    results = evaluate(load_golden(ROOT / "eval" / "golden_set"), DEFAULT_K)
    canaries = {"canary-1", "canary-2", "canary-3"}
    leaked = {
        result.query_id: sorted(canaries.intersection(result.reached))
        for result in results
        if canaries.intersection(result.reached)
    }
    assert not leaked, f"a fact set reached a canary: {leaked}"
