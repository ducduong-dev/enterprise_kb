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
