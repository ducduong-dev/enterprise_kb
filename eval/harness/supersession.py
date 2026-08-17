#!/usr/bin/env python
"""The four numbers M9d is judged by — and an honest account of which are measurable yet.

ADR-0033 and the plan's acceptance criteria name four:

1. **stale-answer rate** — the share of answers citing a clause a confirmed newer one replaces.
   What the whole milestone exists to reduce.
2. **false-supersession rate** — how often the detector proposed a supersession a steward then
   rejected. What must not get worse; gate 3 is expected to carry most of it.
3. **the gate/model split** — how many pairs the cheap gates settled and how many reached gate
   5. What says whether the funnel is affordable over 3,000 documents.
4. **the declared share** — of the supersessions the bank actually has, how many arrived
   *declared* in the replacing text rather than inferred by us. The ~80% estimate the entire
   M9 staging rests on, and the one the plan says to re-check against the corpus.

**Every one of them is a ratio, and every ratio here can be 0/0.** That is the design problem
this file is mostly about. On a corpus with no confirmed supersessions, "0% of answers were
stale" is true, vacuous, and reads exactly like a passing grade — so a `Measurement` carries its
numerator, its denominator and what the denominator *counts*, and refuses to print a rate when
there is nothing to divide. `demand.py` established the same discipline for the same reason: an
empty audit log and a corpus nobody serves stale clauses from look identical in the output and
mean opposite things.

**This is a report, not a gate.** The retrieval harness blocks merges on recall because 0.85 was
a criterion somebody set from data. Nothing here has that yet: thresholds for these four need a
corpus with real confirmed supersessions and real traffic, and inventing them now would be
inventing the finding. `tests/test_supersession_metrics.py` asserts the report is *honest* —
that it never prints a rate it did not measure — which is the only property that can be gated
today.

    make supersession-eval
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT))

#: Steps that reach a verdict with no model call. Anything else cost a generation request, and
#: the split between the two is metric 3.
GATE_SETTLED = frozenset({"effectivity", "identical_text", "scope"})
#: `detected` is the funnel's own conclusion and `edge_article` is one an edge put in scope.
#: Both are ours; `declared` and `steward` are not, and neither belongs in a detector's error
#: rate.
DETECTOR_BASES = ("detected", "edge_article")


@dataclass(frozen=True, slots=True)
class Measurement:
    """One ratio, with enough around it to know whether it means anything."""

    name: str
    numerator: int
    denominator: int
    #: What the denominator counts, in words. Printed beside the rate, because "3 of 4" and
    #: "3 of 4000" are different findings and the ratio alone hides which one this is.
    population: str
    #: Anything that qualifies the number — usually why it could not be measured.
    note: str = ""

    @property
    def measured(self) -> bool:
        return self.denominator > 0

    @property
    def rate(self) -> float | None:
        """None when there was nothing to divide. Deliberately not 0.0.

        Returning zero here is the single most likely way this file could mislead: every one
        of these metrics is a *bad* thing being counted, so a spurious 0.0 reads as a clean
        bill of health for a measurement that never ran.
        """
        return self.numerator / self.denominator if self.denominator else None

    def render(self) -> str:
        head = f"{self.name:<26}"
        if self.measured:
            rate = self.rate
            assert rate is not None
            line = f"{head} {rate:>7.1%}  ({self.numerator}/{self.denominator} {self.population})"
        else:
            line = f"{head} {'—':>7}  not measured: no {self.population}"
        return f"{line}\n{'':<27} {self.note}" if self.note else line

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rate": self.rate,
            "measured": self.measured,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "population": self.population,
            "note": self.note,
        }


@dataclass
class SupersessionReport:
    measurements: list[Measurement] = field(default_factory=list)
    #: Counts behind metric 3, per `settled_by`. Reported in full because "which gate is doing
    #: the work" is the actionable half; the ratio alone says only how much the model cost.
    settled_by: dict[str, int] = field(default_factory=dict)
    #: Verdict counts, so the `different_scope` population behind metric 2 is visible.
    verdicts: dict[str, int] = field(default_factory=dict)

    @property
    def unmeasured(self) -> list[Measurement]:
        return [m for m in self.measurements if not m.measured]

    def as_dict(self) -> dict[str, Any]:
        return {
            "measurements": [m.as_dict() for m in self.measurements],
            "settled_by": self.settled_by,
            "verdicts": self.verdicts,
        }


# --------------------------------------------------------------------------- 1 · stale answers


def stale_answer_rate(answers: Iterable[Any], *, confirmed_supersessions: int = 0) -> Measurement:
    """Answers that cited a clause a confirmed newer one replaces.

    Counted per *answer* rather than per citation: one answer quoting one superseded rate is
    one reader misled, and an answer that cited it twice is not twice as wrong.

    Refusals and answers with no citations are outside the denominator. A refusal cites
    nothing, so it cannot cite something stale, and leaving it in would let the rate fall by
    refusing more — which is the wrong incentive to build into the number that decides whether
    this milestone worked.
    """
    considered = [
        a for a in answers if not getattr(a, "refused", False) and getattr(a, "cited", None)
    ]
    stale = [a for a in considered if getattr(a, "superseded_citations", None)]
    note = ""
    if considered and confirmed_supersessions == 0:
        # The measurement ran and found nothing, because there was nothing to find. Saying so
        # is the difference between "we do not serve stale clauses" and "we have never
        # confirmed one".
        note = (
            "the corpus holds no confirmed supersessions, so this rate is 0 by construction "
            "rather than by performance"
        )
    return Measurement(
        name="stale-answer rate",
        numerator=len(stale),
        denominator=len(considered),
        population="answers with citations",
        note=note,
    )


# ------------------------------------------------------------------- 2 · false supersessions


def false_supersession_rate(rows: Sequence[tuple[str, str]]) -> Measurement:
    """Detector proposals a steward rejected, over the proposals a steward decided.

    `rows` is `(basis, state)` for the open row of every clause supersession, which is the
    current belief per replaced clause. Only the detector's own bases count: a declared
    supersession that a steward revoked is the corpus being wrong or a reading being wrong, and
    neither is evidence about the funnel.

    Undecided proposals are excluded rather than counted as correct. A queue nobody has worked
    says nothing about precision, and treating `proposed` as "not yet wrong" would make the
    rate fall every time the backfill ran.
    """
    decided = [state for basis, state in rows if basis in DETECTOR_BASES and state != "proposed"]
    rejected = [state for state in decided if state == "revoked"]
    pending = sum(1 for basis, state in rows if basis in DETECTOR_BASES and state == "proposed")
    note = f"{pending} proposal(s) still awaiting a steward" if pending else ""
    return Measurement(
        name="false-supersession rate",
        numerator=len(rejected),
        denominator=len(decided),
        population="detector proposals a steward decided",
        note=note,
    )


# ----------------------------------------------------------------------- 3 · the funnel split


def gate_model_split(settled_by: dict[str, int]) -> Measurement:
    """How much of the funnel's work the model had to do.

    The number that says whether gate 5 is affordable at corpus scale, and the one ADR-0033
    asks the eval to report. High is not automatically bad — Path C over 3,000 documents is
    meant to leave the model a small fraction of 1,040 pairs per instrument, and *which* gate
    is doing the work is in `settled_by` beside it.
    """
    total = sum(settled_by.values())
    by_model = sum(count for step, count in settled_by.items() if step not in GATE_SETTLED)
    return Measurement(
        name="pairs reaching the model",
        numerator=by_model,
        denominator=total,
        population="adjudicated pairs",
        note=_gate_note(settled_by),
    )


def _gate_note(settled_by: dict[str, int]) -> str:
    """Say when the gates are contributing nothing, because that is a corpus fact worth seeing.

    Gate 3 can only fire where a drafter stated an applicability facet. A corpus of fixtures
    that state none produces a 100% model share that looks like a gate regression and is not.
    """
    total = sum(settled_by.values())
    gated = sum(count for step, count in settled_by.items() if step in GATE_SETTLED)
    if total and gated == 0:
        return (
            "no pair was settled by a cheap gate — check whether the corpus states scope "
            "facets at all before reading this as a gate regression"
        )
    return ""


# --------------------------------------------------------------------- 4 · declared vs inferred


def declared_share(rows: Sequence[tuple[str, str]]) -> Measurement:
    """Of the supersessions the bank *has*, how many the corpus stated rather than we inferred.

    The estimate the whole M9 staging rests on — M9c was built before M9d because roughly four
    in five were thought to arrive declared. This is where that gets checked against the corpus
    instead of against the estimate.

    Confirmed rows only. A proposal is a claim, not a supersession, and counting the funnel's
    unreviewed output here would let the inferred share rise simply by running the backfill
    again — measuring our own activity rather than the corpus's drafting habits.
    """
    confirmed = [basis for basis, state in rows if state == "confirmed"]
    declared = [basis for basis in confirmed if basis == "declared"]
    return Measurement(
        name="declared share",
        numerator=len(declared),
        denominator=len(confirmed),
        population="confirmed supersessions",
        note="the ~80% estimate M9's staging rests on (plan §M9)",
    )


# ------------------------------------------------------------------------------- collection


def supersession_rows(session: Any) -> list[tuple[str, str]]:
    """`(basis, state)` for the current belief about every replaced clause."""
    from sqlalchemy import text

    return [
        (row.basis, row.state)
        for row in session.execute(
            text("SELECT basis, state FROM clause_supersessions WHERE closed_at IS NULL")
        )
    ]


def verdict_counts(session: Any) -> tuple[dict[str, int], dict[str, int]]:
    """`(settled_by counts, verdict counts)` from every pair the funnel concluded about."""
    from sqlalchemy import text

    settled: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    for row in session.execute(
        text("SELECT settled_by, verdict, count(*) AS n FROM clause_pair_verdicts GROUP BY 1, 2")
    ):
        settled[row.settled_by] = settled.get(row.settled_by, 0) + int(row.n)
        verdicts[row.verdict] = verdicts.get(row.verdict, 0) + int(row.n)
    return settled, verdicts


def confirmed_count(rows: Sequence[tuple[str, str]]) -> int:
    return sum(1 for _, state in rows if state == "confirmed")


def collect(session: Any, answers: Iterable[Any] = ()) -> SupersessionReport:
    """Every metric this corpus can support, in one pass."""
    rows = supersession_rows(session)
    settled, verdicts = verdict_counts(session)
    return SupersessionReport(
        measurements=[
            stale_answer_rate(answers, confirmed_supersessions=confirmed_count(rows)),
            false_supersession_rate(rows),
            gate_model_split(settled),
            declared_share(rows),
        ],
        settled_by=settled,
        verdicts=verdicts,
    )


# ----------------------------------------------------------------------------------- report


def render(report: SupersessionReport) -> str:
    lines = ["", "M9d — clause supersession", "=" * 60]
    lines.extend(m.render() for m in report.measurements)
    if report.settled_by:
        lines.append("")
        lines.append(
            "settled by:  " + ", ".join(f"{k}={v}" for k, v in sorted(report.settled_by.items()))
        )
    if report.verdicts:
        lines.append(
            "verdicts:    " + ", ".join(f"{k}={v}" for k, v in sorted(report.verdicts.items()))
        )
    if report.unmeasured:
        lines.append("")
        lines.append(
            f"{len(report.unmeasured)} of {len(report.measurements)} metrics could not be "
            "measured on this corpus. That is a statement about the corpus, not a pass."
        )
    return "\n".join(lines)


def _answers(generation: str) -> list[Any]:
    """Run the answer set so citations exist to inspect. Empty if it cannot run."""
    try:
        from answers import evaluate, load_yaml

        # A directory, not a file: `load_yaml` globs `*.yaml` so the set can be split up.
        entries = load_yaml(ROOT / "eval" / "answers")
        if not entries:
            print("answer set is empty; stale-answer rate will report as unmeasured")
            return []
        results, _ = evaluate(entries, [], generation)
        return list(results)
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"answer set not run ({exc}); stale-answer rate will report as unmeasured")
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="write the report as JSON as well")
    parser.add_argument("--generation", default="extractive")
    parser.add_argument(
        "--no-answers",
        action="store_true",
        help="ledger metrics only; skips running the answer set",
    )
    args = parser.parse_args()

    from kb_common.db import create_db_engine
    from sqlalchemy.orm import Session

    answers = [] if args.no_answers else _answers(args.generation)
    with Session(create_db_engine()) as session:
        report = collect(session, answers)

    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    # Always zero: this reports, it does not gate. See the module docstring.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
