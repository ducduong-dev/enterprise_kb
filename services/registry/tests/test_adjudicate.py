"""Gate 5 — the adjudication, and the three ways a pair never reaches the model (ADR-0033).

Every test here asserts on `scripted.calls` as well as on the verdict. What the funnel *did not
spend* is half of what this module is for, and a gate that quietly stopped short-circuiting
would otherwise pass every assertion about correctness while making the backfill unaffordable.

No database: gate 5 produces a verdict and never a row, so none of this needs one.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date

import pytest
from kb_ports.adapters.generation import ScriptedGeneration
from kb_registry.adjudicate import Clause, ClauseAdjudicator
from kb_registry.gates import Window
from kb_registry.supersession import ClauseRef
from kb_schemas.enums import SupersessionVerdict

OLD_DOC = uuid.UUID("11111111-1111-4111-8111-111111111111")
NEW_DOC = uuid.UUID("22222222-2222-4222-8222-222222222222")

Y2023 = date(2023, 1, 1)
Y2026 = date(2026, 1, 1)


def reply(
    verdict: str, *, replacement_idx: int | None = None, rationale: str = "vì mức phí đã đổi"
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "replacement_idx": replacement_idx,
            "confidence": 0.8,
            "rationale": rationale,
        }
    )


def clause(
    document_id: uuid.UUID,
    text: str,
    *,
    section_path: str = "Điều 5",
    effective_from: date | None = None,
    effective_to: date | None = None,
    instrument: str | None = None,
) -> Clause:
    return Clause(
        ref=ClauseRef(document_id=document_id, section_path=section_path),
        text=text,
        window=Window(effective_from, effective_to),
        instrument=instrument,
    )


def adjudicator(*responses: str) -> tuple[ClauseAdjudicator, ScriptedGeneration]:
    scripted = ScriptedGeneration(responses=list(responses))
    return ClauseAdjudicator(scripted), scripted


# ------------------------------------------------------------------ the pairs that never run


def test_windows_that_never_met_are_not_a_pair() -> None:
    """Gate 2. The second was not in force to replace anything while the first applied, so
    there is nothing to adjudicate and nothing to record."""
    judge, scripted = adjudicator()
    result = judge.adjudicate(
        clause(
            OLD_DOC, "Phí là 11.000 đồng.", effective_from=Y2023, effective_to=date(2023, 6, 30)
        ),
        clause(NEW_DOC, "Phí là 15.000 đồng.", effective_from=date(2023, 7, 1)),
    )
    assert result.verdict is None
    assert result.settled_by == "effectivity"
    assert not result.records_supersession
    assert scripted.calls == []


def test_identical_text_is_a_restatement_without_a_model() -> None:
    """The verbatim fast path. Two clauses whose text is the same are the same rule, and paying
    a model to say so on a corpus-wide backfill is the cost this gate exists to avoid."""
    judge, scripted = adjudicator()
    text = "Lãi suất cho vay ngắn hạn là 8%/năm."
    result = judge.adjudicate(
        clause(OLD_DOC, text, effective_from=Y2023),
        clause(NEW_DOC, f"  {text.upper()}  ", effective_from=Y2026),
    )
    assert result.verdict is SupersessionVerdict.SAME_RULE_RESTATED
    assert result.settled_by == "identical_text"
    assert scripted.calls == []


def test_a_stated_scope_conflict_is_different_scope_without_a_model() -> None:
    """The M9d acceptance criterion: a same-subject pair differing only in customer segment is
    recorded `different_scope` by gate 3, with no model call. This is the expensive false
    positive made deterministic wherever the drafter was explicit."""
    judge, scripted = adjudicator()
    result = judge.adjudicate(
        clause(
            OLD_DOC,
            "Hạn mức rút tiền tại ATM đối với khách hàng cá nhân là 50 triệu đồng/ngày.",
            effective_from=Y2023,
        ),
        clause(
            NEW_DOC,
            "Hạn mức rút tiền tại ATM đối với khách hàng doanh nghiệp là 100 triệu đồng/ngày.",
            effective_from=Y2026,
        ),
    )
    assert result.verdict is SupersessionVerdict.DIFFERENT_SCOPE
    assert result.settled_by == "scope"
    assert result.scope_facets is not None and result.scope_facets["mismatched"]
    assert not result.records_supersession
    assert scripted.calls == []


# ------------------------------------------------------------------------- the model's answer


def test_a_superseded_verdict_the_dates_agree_with_becomes_a_supersession() -> None:
    judge, scripted = adjudicator(reply("superseded", replacement_idx=1))
    result = judge.adjudicate(
        clause(OLD_DOC, "Phí chuyển tiền là 11.000 đồng.", effective_from=Y2023),
        clause(NEW_DOC, "Phí chuyển tiền là 15.000 đồng.", effective_from=Y2026),
    )
    assert result.verdict is SupersessionVerdict.SUPERSEDED
    assert result.records_supersession
    assert result.old is not None and result.old.document_id == OLD_DOC
    assert result.new is not None and result.new.document_id == NEW_DOC
    # From the newer clause's own effective date, never from today: an `as_of` query before it
    # must still show the older clause as current and unflagged.
    assert result.supersedes_from == Y2026
    assert result.model is not None and result.prompt_version == "1"
    assert len(scripted.calls) == 1


def test_argument_order_does_not_decide_direction() -> None:
    """The older text often arrives second — the archive is digitised in whatever order it
    yields — so which clause was passed first must not reach the record. Both orders produce
    the same row, and the model's nomination is read relative to the list it was shown."""
    forward, _ = adjudicator(reply("superseded", replacement_idx=1))
    backward, _ = adjudicator(reply("superseded", replacement_idx=0))
    old = clause(OLD_DOC, "Phí chuyển tiền là 11.000 đồng.", effective_from=Y2023)
    new = clause(NEW_DOC, "Phí chuyển tiền là 15.000 đồng.", effective_from=Y2026)

    first = forward.adjudicate(old, new)
    second = backward.adjudicate(new, old)

    assert first.verdict is second.verdict is SupersessionVerdict.SUPERSEDED
    assert first.old == second.old
    assert first.new == second.new
    assert first.supersedes_from == second.supersedes_from == Y2026
    # Including the delta a steward reads. It is computed left-to-right until direction is
    # known, so without recomputing it here the same change reads "11000 → 15000" or
    # "15000 → 11000" depending only on which document a backfill happened to reach first.
    assert first.quantity_delta == second.quantity_delta
    assert first.quantity_delta == {"changed": ["11000 đồng → 15000 đồng"]}


def test_a_nomination_against_the_dates_is_not_stored_as_a_supersession() -> None:
    """ "The model nominates; the dates dispose." The prompt is not shown the effective dates,
    so a nomination of the *older* clause is an independent reading that disagrees with them —
    a genuine finding for a person, and never a row pointing backwards."""
    judge, scripted = adjudicator(
        reply("superseded", replacement_idx=0, rationale="bản này chi tiết hơn")
    )
    result = judge.adjudicate(
        clause(OLD_DOC, "Phí chuyển tiền là 11.000 đồng.", effective_from=Y2023),
        clause(NEW_DOC, "Phí chuyển tiền là 15.000 đồng.", effective_from=Y2026),
    )
    assert result.verdict is SupersessionVerdict.CONFLICTING_UNRESOLVED
    assert result.settled_by == "direction"
    assert not result.records_supersession
    assert result.old is None and result.new is None
    # The model's own reasoning survives beside ours: it is the evidence for looking at all.
    assert "bản này chi tiết hơn" in result.rationale
    assert "trái với ngày hiệu lực" in result.rationale
    assert len(scripted.calls) == 1


def test_an_abstaining_nomination_lets_the_dates_decide_unopposed() -> None:
    """`replacement_idx: null` is "one replaced the other and the text does not say which",
    which is honest and is not the same answer as a conflict."""
    judge, _ = adjudicator(reply("superseded", replacement_idx=None))
    result = judge.adjudicate(
        clause(OLD_DOC, "Phí chuyển tiền là 11.000 đồng.", effective_from=Y2023),
        clause(NEW_DOC, "Phí chuyển tiền là 15.000 đồng.", effective_from=Y2026),
    )
    assert result.verdict is SupersessionVerdict.SUPERSEDED
    assert result.settled_by == "model"
    assert result.new is not None and result.new.document_id == NEW_DOC


@pytest.mark.parametrize(
    ("left_from", "right_from", "instrument"),
    [
        (None, Y2026, None),  # one side undated
        (Y2026, Y2026, None),  # same day, no rank to break the tie
        (Y2026, Y2026, "TT"),  # same day, same rank
    ],
)
def test_an_undecidable_direction_is_a_persons_call(
    left_from: date | None, right_from: date | None, instrument: str | None
) -> None:
    """Nothing is proposed when the dates cannot say which came first. The model's judgement
    that the two state one rule is kept; only the claim about which won is lost."""
    judge, _ = adjudicator(reply("superseded", replacement_idx=1))
    result = judge.adjudicate(
        clause(OLD_DOC, "Phí là 11.000 đồng.", effective_from=left_from, instrument=instrument),
        clause(NEW_DOC, "Phí là 15.000 đồng.", effective_from=right_from, instrument=instrument),
    )
    assert result.verdict is SupersessionVerdict.CONFLICTING_UNRESOLVED
    assert result.settled_by == "direction"
    assert not result.records_supersession


def test_a_same_day_pair_is_decided_by_instrument_rank() -> None:
    """The one tie-break `older_first` allows: a Nghị định outranks a Thông tư, so on the same
    day it is the superior instrument that displaced the other."""
    judge, _ = adjudicator(reply("superseded", replacement_idx=None))
    result = judge.adjudicate(
        clause(OLD_DOC, "Phí là 11.000 đồng.", effective_from=Y2026, instrument="TT"),
        clause(NEW_DOC, "Phí là 15.000 đồng.", effective_from=Y2026, instrument="ND"),
    )
    assert result.verdict is SupersessionVerdict.SUPERSEDED
    assert result.new is not None and result.new.document_id == NEW_DOC


def test_the_other_buckets_are_recorded_but_never_as_supersessions() -> None:
    for name in ("same_rule_restated", "different_scope", "conflicting_unresolved"):
        judge, _ = adjudicator(reply(name))
        result = judge.adjudicate(
            clause(OLD_DOC, "Phí chuyển tiền là 11.000 đồng.", effective_from=Y2023),
            clause(NEW_DOC, "Phí chuyển tiền trong nước là 15.000 đồng.", effective_from=Y2026),
        )
        assert result.verdict is SupersessionVerdict(name)
        assert result.settled_by == "model"
        assert not result.records_supersession
        assert result.old is None and result.supersedes_from is None


# ----------------------------------------------------------------- what the model is told


def test_the_prompt_carries_the_computed_delta_and_never_the_dates() -> None:
    """Two deliberate properties of the prompt, both load-bearing.

    The delta is computed by `kb_vntext.quantities` because "1.500" is 1500 in Vietnamese
    convention and 1.5 in English, and a model doing that arithmetic is a source of errors the
    extractor does not have. The dates are withheld because `replacement_idx` is only worth
    having as a cross-check if it was not read off them.
    """
    judge, scripted = adjudicator(reply("superseded", replacement_idx=1))
    result = judge.adjudicate(
        clause(OLD_DOC, "Phí chuyển tiền là 11.000 đồng.", effective_from=Y2023),
        clause(NEW_DOC, "Phí chuyển tiền là 15.000 đồng.", effective_from=Y2026),
    )
    prompt = scripted.last_prompt
    assert "11000 đồng → 15000 đồng" in prompt
    # No four-digit year anywhere — including in the prompt file's own header comment, which is
    # sent to the model along with the rest of the file. An illustrative "TT 09/2026" written
    # into that comment would reintroduce the ordering the rule exists to withhold, and did.
    assert re.search(r"\b(19|20)\d{2}\b", prompt) is None
    assert result.quantity_delta == {"changed": ["11000 đồng → 15000 đồng"]}


def test_a_citation_label_never_reaches_the_prompt() -> None:
    """The subtler half of withholding the dates. A citation label is "Điều 7, TT 09/2026" and
    an instrument's legal number carries its year, so rendering it would hand the model the
    ordering by the back door and turn `replacement_idx` back into an echo of the dates. The
    label is for the steward's screen; the model gets the section path."""
    judge, scripted = adjudicator(reply("superseded", replacement_idx=1))
    judge.adjudicate(
        Clause(
            ref=ClauseRef(document_id=OLD_DOC, section_path="Điều 5"),
            text="Phí chuyển tiền là 11.000 đồng.",
            window=Window(Y2023),
            label="Điều 5, TT 01/2023",
        ),
        Clause(
            ref=ClauseRef(document_id=NEW_DOC, section_path="Điều 7"),
            text="Phí chuyển tiền là 15.000 đồng.",
            window=Window(Y2026),
            label="Điều 7, TT 09/2026",
        ),
    )
    prompt = scripted.last_prompt
    assert "TT 01/2023" not in prompt and "TT 09/2026" not in prompt
    # The clauses are still named, and distinguishably.
    assert "Điều 5" in prompt and "Điều 7" in prompt


# ------------------------------------------------------------------------ when it goes wrong


@pytest.mark.parametrize(
    "text",
    [
        "tôi nghĩ hai điều khoản này giống nhau",  # no JSON at all
        '{"verdict": "probably_superseded", "replacement_idx": null}',  # not one of the four
        '{"verdict": "superseded", "replacement_idx": 7}',  # outside the pair
        '{"verdict": "superseded"}',  # no index key
    ],
)
def test_an_unreadable_reply_is_unknown_and_not_a_cautious_default(text: str) -> None:
    """A pair nobody could adjudicate is *unknown*, not `conflicting_unresolved`. Defaulting it
    would look cautious and would in fact put a model's outage in front of a steward wearing a
    judgement's clothes. The backfill is re-runnable; that is what ADR-0035's cache is for."""
    judge, _ = adjudicator(text)
    result = judge.adjudicate(
        clause(OLD_DOC, "Phí là 11.000 đồng.", effective_from=Y2023),
        clause(NEW_DOC, "Phí là 15.000 đồng.", effective_from=Y2026),
    )
    assert result.verdict is None
    assert result.settled_by == "model_unavailable"
    assert not result.records_supersession


def test_a_model_that_is_down_does_not_fail_the_pair_loudly() -> None:
    """`ScriptedGeneration` raises when its script runs out, which is how an unreachable model
    arrives. The funnel records nothing and carries on to the next pair."""
    judge, _ = adjudicator()
    result = judge.adjudicate(
        clause(OLD_DOC, "Phí là 11.000 đồng.", effective_from=Y2023),
        clause(NEW_DOC, "Phí là 15.000 đồng.", effective_from=Y2026),
    )
    assert result.verdict is None
    assert result.settled_by == "model_unavailable"
