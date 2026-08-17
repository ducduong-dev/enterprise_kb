"""The clause-adjudicator fixture set, held to what it can be held to without a model.

`eval/clause_adjudicator/pairs.yaml` is the eval that pairs with the prompt (M9d, ADR-0033).
Most of it grades a model and cannot run in CI. Two things can, and both are worth a gate:

* the **deterministic entries** — the ones whose `settled_by` is `identical_text`, `scope` or
  `effectivity` — must reach their verdict with **no model call at all**. This is the funnel's
  cost property, and it is the one that decays silently: a change that made gate 3 stop
  short-circuiting would leave every bucket correct and quietly put the whole corpus through a
  model on the next backfill;
* the **file's own shape**, so an entry cannot rot into a bucket that does not exist or a
  replacement index that is not one of the two clauses.

The model-graded entries are counted and reported here, never scored — the same discipline as
`eval/answers`' `requires:`, so a green run never implies a capability that was not exercised.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from kb_ports.adapters.generation import ScriptedGeneration
from kb_registry.adjudicate import Clause, ClauseAdjudicator
from kb_registry.gates import Window
from kb_registry.supersession import ClauseRef
from kb_schemas.enums import SupersessionVerdict

FIXTURES = Path(__file__).resolve().parents[3] / "eval" / "clause_adjudicator" / "pairs.yaml"

#: The steps that reach a verdict without asking anything. Everything else needs a model and is
#: reported rather than graded.
DETERMINISTIC = frozenset({"identical_text", "scope", "effectivity"})

DOC_A = ClauseRef(document_id=uuid.UUID(int=1), section_path="Điều 5")
DOC_B = ClauseRef(document_id=uuid.UUID(int=2), section_path="Điều 5")


def load() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = yaml.safe_load(FIXTURES.read_text(encoding="utf-8"))
    return entries


def build(side: dict[str, Any], ref: ClauseRef) -> Clause:
    return Clause(
        ref=ref,
        text=side["text"],
        window=Window(side.get("effective_from"), side.get("effective_to")),
        instrument=side.get("instrument"),
    )


def ids() -> list[str]:
    return [entry["id"] for entry in load()]


def test_the_fixture_file_is_well_formed() -> None:
    """A rotted entry is worse than a missing one: it grades the prompt against a bucket that
    no longer exists and reports a pass."""
    seen: set[str] = set()
    buckets = {item.value for item in SupersessionVerdict}
    for entry in load():
        name = entry["id"]
        assert name not in seen, f"duplicate fixture id {name}"
        seen.add(name)
        assert entry["expect"] is None or entry["expect"] in buckets, name
        assert entry["settled_by"] in DETERMINISTIC | {"model", "direction"}, name
        assert entry.get("note", "").strip(), f"{name} does not say what it is here to catch"
        if "replacement" in entry:
            assert entry["replacement"] in (0, 1, None), name
        for side in ("left", "right"):
            assert entry[side]["text"].strip(), f"{name}.{side} has no text"
        # A `superseded` expectation the dates cannot support would grade the model on
        # something the platform would refuse to record anyway.
        if entry["expect"] == "superseded":
            assert entry["left"].get("effective_from") and entry["right"].get("effective_from")


@pytest.mark.parametrize("name", ids())
def test_the_cheap_gates_settle_what_they_claim_to(name: str) -> None:
    """Run with a script of zero responses, so any model call raises rather than being invented.

    The deterministic entries must land their verdict untouched; the rest must be the ones that
    reach the model, which this asserts from the other side — an entry marked `model` that the
    gates settled anyway is also a drift worth knowing about, because it means the fixture is no
    longer exercising the prompt it was written for.
    """
    entry = next(item for item in load() if item["id"] == name)
    scripted = ScriptedGeneration(responses=[])
    judge = ClauseAdjudicator(scripted)

    result = judge.adjudicate(build(entry["left"], DOC_A), build(entry["right"], DOC_B))

    if entry["settled_by"] in DETERMINISTIC:
        assert scripted.calls == [], f"{name} now costs a model call"
        assert result.settled_by == entry["settled_by"]
        expected = entry["expect"]
        assert (result.verdict.value if result.verdict else None) == expected
    else:
        assert len(scripted.calls) == 1, f"{name} no longer reaches the prompt"
        # The call was made and the empty script refused it, which is the model being down.
        assert result.settled_by == "model_unavailable"


def test_the_set_covers_every_bucket_and_both_kinds_of_entry() -> None:
    """The false-supersession rate is measured on `different_scope`, so a set with no
    different-scope pairs would report a perfect score for a prompt nobody tested."""
    entries = load()
    expected = {entry["expect"] for entry in entries}
    for bucket in SupersessionVerdict:
        assert bucket.value in expected, f"no fixture expects {bucket.value}"
    assert None in expected, "no fixture exercises a pair the gates reject outright"

    deterministic = [e for e in entries if e["settled_by"] in DETERMINISTIC]
    graded_by_model = [e for e in entries if e["settled_by"] not in DETERMINISTIC]
    assert len(deterministic) >= 3
    # Both stated and unstated scope conflicts, because gate 3 can only help with the first and
    # the second is what the prompt is actually for.
    scope_entries = [e for e in entries if e["expect"] == "different_scope"]
    assert {e["settled_by"] for e in scope_entries} == {"scope", "model"}
    assert graded_by_model, "nothing in this set exercises the prompt"
