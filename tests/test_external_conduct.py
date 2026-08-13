"""The external conduct red team as a blocking gate (M8 acceptance).

`make external-redteam` produces the report Compliance reads; this makes the same run fail the
build. Two failure kinds, both blocking here:

* **disclosure** — internal content, a canary or a personal identifier reached a stranger;
* **conduct** — the bank committed to something, advised, quoted an undated figure, or
  disparaged a competitor on a public channel.

Prompts that need model judgement to decline (rather than to avoid disclosing) are reported
separately by the harness and are not blocking with the deterministic generator — see
ADR-0019 for why that line is drawn where it is.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval" / "harness"))

pytestmark = pytest.mark.integration

SUITE = ROOT / "eval" / "external_redteam"


def run(pristine_corpus: Engine):
    from external import REQUIRED_PROMPTS, classify, evaluate, load_suite

    entries = load_suite(SUITE)
    assert len(entries) >= REQUIRED_PROMPTS, "the external suite is smaller than the criterion"
    results = evaluate(entries, "extractive")
    return results, classify


@pytest.mark.acl_sweep
def test_the_public_bot_discloses_nothing(pristine_corpus: Engine) -> None:
    results, classify = run(pristine_corpus)
    offenders = {r.prompt_id: classify(r)[0] for r in results if classify(r)[0]}
    assert not offenders, f"the public bot disclosed content: {offenders}"


def test_the_public_bot_makes_no_commitments(pristine_corpus: Engine) -> None:
    """Every word here is something the bank said to a customer on a public channel."""
    results, classify = run(pristine_corpus)
    offenders = {r.prompt_id: classify(r)[1] for r in results if classify(r)[1]}
    assert not offenders, f"conduct breaches: {offenders}"


def test_every_prompt_ran_as_the_hard_scoped_public_account(pristine_corpus: Engine) -> None:
    """A suite that quietly ran as an employee would prove nothing about the public surface."""
    from external import EXTERNAL_PRINCIPAL, EXTERNAL_SURFACE, load_suite

    for entry in load_suite(SUITE):
        assert entry["principal"] == EXTERNAL_PRINCIPAL
        assert entry["surface"] == EXTERNAL_SURFACE
