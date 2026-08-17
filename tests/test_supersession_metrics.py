"""M9d's four metrics, and the one property they can be held to today.

The metrics themselves cannot be gated yet: a threshold for stale-answer rate or false-
supersession rate needs a corpus with real confirmed supersessions and real traffic, and this
one has neither. Inventing a number now would be inventing the finding.

What *can* be gated, and is the whole reason this file exists, is **honesty**. Every one of
these four is a ratio counting a bad thing, so a 0/0 rendered as `0.0%` reads as a clean bill of
health for a measurement that never ran — on precisely the corpus where nothing has been
measured. So the tests below are mostly about the difference between zero and nothing.
"""

from __future__ import annotations

import sys
import uuid
from datetime import date
from pathlib import Path

import pytest
from kb_registry.supersession import ClauseRef, ClauseSupersessions
from kb_registry.testing import make_chunk, make_document, make_version
from kb_schemas.enums import SupersessionBasis
from sqlalchemy import text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval" / "harness"))

from supersession import (  # noqa: E402
    Measurement,
    collect,
    declared_share,
    false_supersession_rate,
    gate_model_split,
    render,
    stale_answer_rate,
)

pytestmark = pytest.mark.integration

FROM = date(2026, 1, 1)


class FakeAnswer:
    """What `AnswerResult` gives the metric, without running the chat pipeline."""

    def __init__(self, *, cited: list[str], superseded: list[str], refused: bool = False) -> None:
        self.cited = cited
        self.superseded_citations = superseded
        self.refused = refused


# ------------------------------------------------------------------- zero is not nothing


def test_a_ratio_with_nothing_to_divide_is_not_zero() -> None:
    """The single most likely way this report could mislead."""
    empty = Measurement(name="x", numerator=0, denominator=0, population="things")

    assert empty.rate is None
    assert not empty.measured
    assert "not measured" in empty.render()
    assert "0.0%" not in empty.render()


def test_a_real_zero_is_reported_as_a_rate() -> None:
    """The other half: a measurement that ran and found nothing must not be hidden."""
    clean = Measurement(name="x", numerator=0, denominator=40, population="things")

    assert clean.rate == 0.0
    assert clean.measured
    assert "0.0%" in clean.render()


def test_a_zero_stale_rate_says_when_the_corpus_could_not_have_produced_one() -> None:
    """ "No answer cited a superseded clause" and "no clause has ever been confirmed superseded"
    are the same number and opposite findings."""
    answers = [FakeAnswer(cited=["tt41"], superseded=[]) for _ in range(5)]

    vacuous = stale_answer_rate(answers, confirmed_supersessions=0)
    real = stale_answer_rate(answers, confirmed_supersessions=7)

    assert vacuous.rate == real.rate == 0.0
    assert "0 by construction" in vacuous.note
    assert real.note == ""


# ----------------------------------------------------------------------- what each counts


def test_a_refusal_cannot_improve_the_stale_answer_rate() -> None:
    """Leaving refusals in the denominator would let the rate fall by refusing more, which is
    the wrong incentive to build into the number that says whether this milestone worked."""
    answers = [
        FakeAnswer(cited=["a"], superseded=["a"]),
        FakeAnswer(cited=[], superseded=[], refused=True),
        FakeAnswer(cited=[], superseded=[], refused=True),
    ]

    result = stale_answer_rate(answers, confirmed_supersessions=1)

    assert result.numerator == 1 and result.denominator == 1
    assert result.rate == 1.0


def test_only_the_detectors_own_proposals_count_against_it() -> None:
    """A declared supersession a steward revoked is the corpus or a reading being wrong.
    Neither is evidence about the funnel."""
    rows = [
        ("detected", "confirmed"),
        ("detected", "revoked"),
        ("declared", "revoked"),
        ("steward", "revoked"),
    ]

    result = false_supersession_rate(rows)

    assert result.numerator == 1 and result.denominator == 2
    assert result.rate == 0.5


def test_an_unworked_queue_does_not_flatter_the_detector() -> None:
    """Counting `proposed` as "not yet wrong" would make precision improve every time the
    backfill ran."""
    rows = [("detected", "proposed")] * 20 + [("detected", "revoked")]

    result = false_supersession_rate(rows)

    assert result.numerator == 1 and result.denominator == 1
    assert "20 proposal(s) still awaiting" in result.note


def test_the_declared_share_counts_confirmed_supersessions_only() -> None:
    """A proposal is a claim, not a supersession. Counting the funnel's unreviewed output here
    would let the inferred share rise simply by running the backfill again — measuring our own
    activity rather than the corpus's drafting habits."""
    rows = [
        ("declared", "confirmed"),
        ("declared", "confirmed"),
        ("declared", "confirmed"),
        ("declared", "confirmed"),
        ("detected", "confirmed"),
        ("detected", "proposed"),
        ("detected", "proposed"),
    ]

    result = declared_share(rows)

    assert result.denominator == 5
    assert result.rate == pytest.approx(0.8)


def test_the_gate_split_names_a_corpus_that_gives_the_gates_nothing() -> None:
    """A fixture corpus that states no scope facets produces a 100% model share that looks like
    a gate regression and is not. Saying so is the difference between a finding and a scare."""
    silent = gate_model_split({"model": 12, "direction": 3})
    working = gate_model_split({"model": 4, "scope": 10, "identical_text": 6})

    assert silent.rate == 1.0
    assert "states scope facets" in silent.note
    assert working.rate == pytest.approx(0.2)
    assert working.note == ""


# --------------------------------------------------------------------- against the database


def test_the_report_reads_the_ledger_and_the_pair_table(session: Session) -> None:
    """The collectors, against real rows. A metric that reads the wrong column is invisible
    until somebody tries to act on the number."""
    old_doc = make_document(session, title="Biểu phí 2023")
    old_version = make_version(session, old_doc, effective_from=date(2023, 1, 1))
    make_chunk(session, old_doc, old_version, section_path="Điều 5")
    new_doc = make_document(session, title="Biểu phí 2026")
    new_version = make_version(session, new_doc, effective_from=FROM)
    make_chunk(session, new_doc, new_version, section_path="Điều 7")
    session.flush()

    ledger = ClauseSupersessions(session)
    row_id = ledger.propose(
        ClauseRef(old_doc, "Điều 5"),
        new=ClauseRef(new_doc, "Điều 7"),
        supersedes_from=FROM,
        basis=SupersessionBasis.DETECTED,
        detected_by="m9d_funnel",
        actor="m9d_funnel",
    ).row_id
    ledger.confirm(row_id, actor="steward@bank")
    session.execute(
        text(
            "INSERT INTO clause_pair_verdicts (id, left_document_id, left_section_path, "
            "right_document_id, right_section_path, verdict, settled_by, text_digest, "
            "detected_by, created_at) VALUES (:id, :ld, :lp, :rd, :rp, 'different_scope', "
            "'scope', 'digest', 'm9d_funnel', now())"
        ),
        _normalized(old_doc, "Điều 5", new_doc, "Điều 7"),
    )
    session.flush()

    report = collect(session, answers=[])

    by_name = {m.name: m for m in report.measurements}
    assert by_name["declared share"].rate == 0.0  # one confirmed, and we inferred it
    assert by_name["declared share"].denominator == 1
    # Nothing decided *against* the detector yet, so precision is not yet measurable.
    assert by_name["false-supersession rate"].numerator == 0
    assert report.settled_by == {"scope": 1}
    assert report.verdicts == {"different_scope": 1}
    assert by_name["pairs reaching the model"].rate == 0.0


def test_the_rendered_report_never_prints_a_rate_it_did_not_measure(session: Session) -> None:
    """The gate. Everything above is about one metric; this is about the page a person reads."""
    report = collect(session, answers=[])
    text_out = render(report)

    for measurement in report.measurements:
        if not measurement.measured:
            assert measurement.name in text_out
            assert "not measured" in text_out
    if report.unmeasured:
        assert "statement about the corpus, not a pass" in text_out


def _normalized(
    left_doc: uuid.UUID, left_path: str, right_doc: uuid.UUID, right_path: str
) -> dict[str, object]:
    """`clause_pair_verdicts` enforces its own ordering; the test has to respect it."""
    first, second = sorted(
        [(str(left_doc), left_path), (str(right_doc), right_path)],
        key=lambda pair: (pair[0], pair[1]),
    )
    return {
        "id": uuid.uuid4(),
        "ld": first[0],
        "lp": first[1],
        "rd": second[0],
        "rp": second[1],
    }
