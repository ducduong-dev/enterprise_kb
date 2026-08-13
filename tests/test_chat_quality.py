"""Chat quality gate (M6 acceptance: answer faithfulness and the 20-prompt red team).

Runs the same harness `make chat-eval` runs, in-process, so a regression fails the build rather
than a nightly report. Two independent gates, because they fail for different reasons:

* **faithfulness** — did the bot answer only where it had a basis, cite it, and refuse
  otherwise. A quality property.
* **disclosure** — did any red-team attempt get a canary token, a document the principal may
  not read, or a personal identifier out. A security property, and not a score: one is a
  failure.

Questions the configured adapters cannot exercise (cross-lingual matching, model judgement)
are reported as unmeasured rather than scored — see `eval/answers/questions.yaml`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval" / "harness"))

pytestmark = pytest.mark.integration

ANSWERS = ROOT / "eval" / "answers"
REDTEAM = ROOT / "eval" / "chat_redteam"
#: The acceptance criterion names a 60-question subset.
MINIMUM_QUESTIONS = 60
MINIMUM_ATTACKS = 20


def run(pristine_corpus: Engine):
    from answers import build_generation, evaluate, load_yaml

    answers = load_yaml(ANSWERS)
    redteam = load_yaml(REDTEAM)
    assert len(answers) >= MINIMUM_QUESTIONS, "the answer set is smaller than the criterion"
    assert len(redteam) >= MINIMUM_ATTACKS, "the red team is smaller than the criterion"
    build_generation("extractive")  # fails loudly if the deterministic adapter is gone
    return evaluate(answers, redteam, "extractive")


def test_answers_meet_the_faithfulness_threshold(pristine_corpus: Engine) -> None:
    from answers import FAITHFULNESS_THRESHOLD

    answers, _ = run(pristine_corpus)
    measured = [result for result in answers if result.measured]
    assert measured, "nothing was measured"

    score = sum(1 for result in measured if result.faithful) / len(measured)
    unfaithful = [result.question_id for result in measured if not result.faithful]
    assert score >= FAITHFULNESS_THRESHOLD, (
        f"faithfulness {score:.3f} below {FAITHFULNESS_THRESHOLD}; failed: {unfaithful}"
    )


@pytest.mark.acl_sweep
def test_no_answer_cites_a_document_its_reader_may_not_see(pristine_corpus: Engine) -> None:
    """The answer set doubles as an ACL sweep through the chat surface (INV-2/3/4)."""
    answers, _ = run(pristine_corpus)
    offenders = {r.question_id: r.violations for r in answers if r.violations}
    assert not offenders, f"forbidden documents cited: {offenders}"


@pytest.mark.acl_sweep
def test_the_red_team_extracts_nothing(pristine_corpus: Engine) -> None:
    _, redteam = run(pristine_corpus)
    breached = {attack.prompt_id: attack.leaks for attack in redteam if not attack.secure}
    assert not breached, f"red-team attempts disclosed content: {breached}"


def test_a_refusal_is_what_an_unanswerable_question_gets(pristine_corpus: Engine) -> None:
    """The property the whole grounding apparatus exists for: no basis, no answer."""
    answers, _ = run(pristine_corpus)
    expected_refusals = [r for r in answers if r.expect == "refusal" and r.measured]
    assert expected_refusals
    answered = [r.question_id for r in expected_refusals if not r.refused]
    assert not answered, f"answered without a basis: {answered}"


def test_every_answer_carries_a_citation(pristine_corpus: Engine) -> None:
    answers, _ = run(pristine_corpus)
    for result in answers:
        if result.measured and result.expect == "answer" and not result.refused:
            assert result.cited, f"{result.question_id} answered with no citation"
