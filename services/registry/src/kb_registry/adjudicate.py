"""Gate 5 — what two clauses are to each other, decided once the cheap gates give up (ADR-0033).

The funnel's expensive end. Gates 1 to 4 exist to make sure this runs on as few pairs as
possible; this module is what happens to the ones that survive, and it produces a *verdict*,
never a row. Writing is the caller's act, because a proposal that changes nothing until a
steward confirms it is worth exactly one code path and it already exists
(`ClauseSupersessions.propose`).

**Not every pair reaches the model, and the ones that do not are the point.** In order:

* **gate 2, effectivity** — two clauses whose windows never intersected cannot supersede one
  another, so the pair is not a pair. Nothing is recorded.
* **identical text** — a fast path borrowed from Graphiti's `resolve_extracted_edge`, which
  short-circuits on a verbatim match before spending a call. Two clauses whose text is the same
  are the same rule restated, and no model is needed to say so.
* **gate 3, scope** — where both clauses state an applicability facet and the stated values are
  disjoint, the pair is `different_scope` with no model call. This is the expensive false
  positive made deterministic wherever the drafter was explicit.
* **gate 4, quantities** — which does *not* decide. It is tempting to read "same numbers ⇒
  restatement" off `quantities.compare`, and it is wrong: a rule changes without a number
  changing every time a condition is added or an exception removed. The delta is computed
  because it belongs on the review screen and because a model should not be doing arithmetic on
  Vietnamese numerals, not because it settles anything.

**The model nominates; the dates dispose.** The prompt is not shown the effective dates —
deliberately, see the prompt's own header — so `replacement_idx` is an independent reading of
which text *sounds* like the replacement. Direction is then decided by `gates.older_first` from
the legal dates alone, and the two answers are compared. A model that nominates the older clause
as the replacement has told us something real: that this pair is not clear-cut. It becomes
`conflicting_unresolved` for a person rather than a `superseded` row pointing backwards.

**Only `superseded` has a home in `clause_supersessions`.** That table answers "what replaced
this clause", one open row per clause, and a `different_scope` verdict stored there with a null
replacement would read as an abrogation — the opposite of what it means. So `Adjudication`
carries all four verdicts and `records_supersession` marks the one the caller can hand to
`propose()`. Where the other three are persisted, so a backfill does not re-adjudicate the same
pair every run, is the orchestrator's decision and needs a table of its own.

Nothing here caches. `CachedGeneration` wraps any port (ADR-0035) and the caller composes it,
passing `PROMPT_VERSION` — which is what makes the corpus backfill re-runnable without paying
for every verdict twice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Final, Literal

from kb_common.logging import get_logger
from kb_ports.models import GenerationPort, Message
from kb_schemas.enums import SupersessionVerdict
from kb_vntext.quantities import compare as compare_quantities
from kb_vntext.quantities import describe as describe_quantities
from kb_vntext.scope import conflict as scope_conflict
from kb_vntext.scope import find_scope

from kb_registry.gates import Window, older_first, windows_overlap
from kb_registry.supersession import ClauseRef

log = get_logger(__name__)

PROMPT_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "prompts"
ADJUDICATOR_PROMPT: Final[Path] = PROMPT_DIR / "clause_adjudicator.md"
PROMPT_VERSION: Final[str] = "1"

#: A verdict is four fields and a short rationale. Generous enough for a reasoning model's
#: preamble to be discarded by the JSON search below, tight enough that a runaway answer costs
#: little on a corpus-wide backfill.
MAX_TOKENS: Final[int] = 512

_JSON = re.compile(r"\{.*\}", re.DOTALL)
_WHITESPACE = re.compile(r"\s+")

#: How the verdict was reached. On the row it is the difference between "a model judged this"
#: and "the text settled it", which is the split the eval reports and the number that says
#: whether gate 5 is affordable.
SettledBy = Literal[
    "effectivity",
    "identical_text",
    "scope",
    "model",
    "direction",
    "model_unavailable",
]


@dataclass(frozen=True, slots=True)
class Clause:
    """One side of a candidate pair, with everything the gates need and nothing more."""

    ref: ClauseRef
    text: str
    window: Window = field(default_factory=Window)
    #: Instrument kind (`"ND"`, `"TT"`, …), for `older_first`'s one tie-break. Absent means the
    #: tie-break cannot run, which makes a same-day pair undecidable — correctly.
    instrument: str | None = None
    #: What a steward sees on the review screen. **Never rendered into the prompt**: a citation
    #: label is "Điều 7, TT 09/2026", and an instrument's legal number carries its year, so
    #: showing it would hand the model exactly the date ordering the prompt withholds — and
    #: `replacement_idx` would go back to being an echo of the dates. The clauses are named to
    #: the model by section path, which identifies them without dating them.
    label: str | None = None


@dataclass(frozen=True, slots=True)
class Adjudication:
    """What the funnel concluded about one pair, in the shape `propose()` takes."""

    verdict: SupersessionVerdict | None
    settled_by: SettledBy
    rationale: str = ""
    #: The clause that was replaced, and the one that replaced it. Both are None unless the
    #: verdict is `SUPERSEDED` and the dates decided a direction.
    old: ClauseRef | None = None
    new: ClauseRef | None = None
    supersedes_from: date | None = None
    quantity_delta: dict[str, Any] | None = None
    scope_facets: dict[str, Any] | None = None
    score: float | None = None
    model: str | None = None
    prompt_version: str | None = None

    @property
    def called_model(self) -> bool:
        return self.settled_by in ("model", "direction")

    @property
    def records_supersession(self) -> bool:
        """Whether this is the one verdict `clause_supersessions` can hold.

        `old` and `supersedes_from` are non-None exactly when this is True, so a caller that
        checks it can hand the fields straight to `propose()` without re-testing them.
        """
        return self.verdict is SupersessionVerdict.SUPERSEDED and self.old is not None


class ClauseAdjudicator:
    """Gate 5. One pair in, one verdict out, a model consulted only when it has to be."""

    def __init__(self, generation: GenerationPort, *, prompt: Path | None = None) -> None:
        self._generation = generation
        self._prompt = (prompt or ADJUDICATOR_PROMPT).read_text(encoding="utf-8")

    @property
    def prompt_version(self) -> str:
        """What a stored verdict has to match to still be current. Callers that cache a verdict
        put this in their key, so a prompt change invalidates exactly its own results."""
        return PROMPT_VERSION

    def adjudicate(self, left: Clause, right: Clause) -> Adjudication:
        """What these two clauses are to each other.

        Order is cheapest-first and each step can end it. Every return carries `settled_by`, so
        "how many pairs did the gates resolve and how many did the model" is a count rather than
        an estimate.
        """
        if not windows_overlap(left.window, right.window):
            # Not a pair at all: the second was not in force to replace anything while the
            # first applied. Nothing is recorded, and nothing should be.
            return Adjudication(
                verdict=None,
                settled_by="effectivity",
                rationale="hai điều khoản chưa từng cùng có hiệu lực",
            )

        delta = compare_quantities(left.text, right.text)
        facets = scope_conflict(find_scope(left.text), find_scope(right.text))
        quantity_delta = {"changed": describe_quantities(delta)} if delta else {"changed": []}

        if _normalize(left.text) == _normalize(right.text):
            return Adjudication(
                verdict=SupersessionVerdict.SAME_RULE_RESTATED,
                settled_by="identical_text",
                rationale="nội dung hai điều khoản giống hệt nhau",
                quantity_delta=quantity_delta,
                scope_facets=facets,
            )

        if facets["mismatched"]:
            # Gate 3. The drafter said who each clause applies to and the answers are disjoint,
            # so this is the expensive false positive caught for free.
            return Adjudication(
                verdict=SupersessionVerdict.DIFFERENT_SCOPE,
                settled_by="scope",
                rationale="đối tượng áp dụng do hai điều khoản tự nêu không giao nhau: "
                + ", ".join(sorted(facets["mismatched"])),
                quantity_delta=quantity_delta,
                scope_facets=facets,
            )

        return self._ask_model(left, right, quantity_delta=quantity_delta, facets=facets)

    # ------------------------------------------------------------------------- internals

    def _ask_model(
        self,
        left: Clause,
        right: Clause,
        *,
        quantity_delta: dict[str, Any],
        facets: dict[str, Any],
    ) -> Adjudication:
        prompt = (
            self._prompt.replace("{left_ref}", left.ref.section_path)
            .replace("{right_ref}", right.ref.section_path)
            .replace("{left_text}", left.text)
            .replace("{right_text}", right.text)
            .replace("{quantity_delta}", _render_delta(quantity_delta))
            .replace("{scope_facets}", _render_facets(facets))
        )
        try:
            reply = self._ask(prompt)
            verdict = _verdict(reply)
            # Inside the try on purpose, exactly as the merge classifier does it: a reply whose
            # index cannot be read is a failed call and fails the same way non-JSON does.
            nominated = _replacement_index(reply)
        except Exception as exc:
            # A pair nobody could adjudicate is unknown, not unresolved. Recording it as
            # `conflicting_unresolved` would fill a steward's queue with the model's outages.
            # The backfill is re-runnable and the cache means the second run is cheap.
            log.warning(
                "clause_adjudicator_failed",
                extra={
                    "pair": f"{left.ref.section_path}|{right.ref.section_path}",
                    "error": str(exc),
                },
            )
            return Adjudication(
                verdict=None,
                settled_by="model_unavailable",
                rationale="không nhận được kết luận từ mô hình",
                quantity_delta=quantity_delta,
                scope_facets=facets,
            )

        judged = Adjudication(
            verdict=verdict,
            settled_by="model",
            rationale=str(reply.get("rationale", ""))[:300],
            quantity_delta=quantity_delta,
            scope_facets=facets,
            score=_confidence(reply),
            model=self._generation.info.version,
            prompt_version=PROMPT_VERSION,
        )
        if verdict is not SupersessionVerdict.SUPERSEDED:
            return judged
        return self._direct(left, right, nominated=nominated, judged=judged)

    def _direct(
        self,
        left: Clause,
        right: Clause,
        *,
        nominated: int | None,
        judged: Adjudication,
    ) -> Adjudication:
        """Turn a `superseded` verdict into roles, or decline to.

        The dates decide, always. The model's nomination is only ever a cross-check, and this is
        the whole of "the model nominates, the dates dispose": nothing below trusts the model
        about which clause came first, and nothing below writes a row the dates did not agree to.
        """
        direction = older_first(
            left.window.effective_from,
            right.window.effective_from,
            left_instrument=left.instrument,
            right_instrument=right.instrument,
        )
        if direction == "undecidable":
            # Undated, or same day and same rank. ADR-0033 says this is a person's call and a
            # genuine finding rather than a failure, so it keeps the model's judgement that the
            # two state one rule and loses only the claim about which won.
            return replace(
                judged,
                verdict=SupersessionVerdict.CONFLICTING_UNRESOLVED,
                settled_by="direction",
                rationale=_note(judged, "không xác định được văn bản nào có sau"),
            )

        older, newer = (left, right) if direction == "left_older" else (right, left)
        newer_index = 1 if direction == "left_older" else 0

        if nominated is not None and nominated != newer_index:
            # The model read the *older* text as the replacement. It was not shown the dates, so
            # this is an independent reading that disagrees with them — evidence that the pair
            # is not clear-cut, not noise to discard. A `superseded` row here would point
            # backwards, which is worse than not detecting the pair at all.
            log.info(
                "clause_adjudicator_direction_disagreed",
                extra={
                    "nominated": nominated,
                    "by_date": newer_index,
                    "pair": f"{older.ref.section_path}|{newer.ref.section_path}",
                },
            )
            return replace(
                judged,
                verdict=SupersessionVerdict.CONFLICTING_UNRESOLVED,
                settled_by="direction",
                rationale=_note(
                    judged, "mô hình đọc bản cũ là bản thay thế, trái với ngày hiệu lực"
                ),
            )

        return replace(
            judged,
            old=older.ref,
            new=newer.ref,
            # Guaranteed non-None: `older_first` returns a direction only when both dates exist.
            supersedes_from=newer.window.effective_from,
            # Recomputed old→new, not left→right. Until this point the delta ran in whatever
            # order the caller passed the pair, so a funnel started at the newer document
            # produced "10%/năm → 8%/năm" — a steward's queue reading the change backwards, and
            # the same rule appearing to move in opposite directions depending on which
            # document a backfill happened to reach first. Direction is only knowable here.
            quantity_delta={
                "changed": describe_quantities(compare_quantities(older.text, newer.text))
            },
        )

    def _ask(self, prompt: str) -> dict[str, Any]:
        response = self._generation.generate(
            [Message(role="user", content=prompt)], temperature=0.0, max_tokens=MAX_TOKENS
        )
        match = _JSON.search(response.text)
        if match is None:
            raise ValueError("model did not return JSON")
        parsed: dict[str, Any] = json.loads(match.group(0))
        return parsed


def _normalize(text: str) -> str:
    """Collapse whitespace and case, and nothing else.

    Deliberately *not* diacritic-folded, unlike the scope reader. There, folding rescues an
    OCR'd document whose tone marks were lost; here it would declare two clauses identical on
    the strength of the difference between *phải* and *phai*, and skip the model on a pair it
    should have read.
    """
    return _WHITESPACE.sub(" ", text).strip().casefold()


def _verdict(reply: dict[str, Any]) -> SupersessionVerdict:
    """The bucket, or a failure. Never a default.

    A reply whose verdict is unreadable is a failed call. Defaulting it to
    `conflicting_unresolved` would look like a cautious choice and would in fact put a model's
    parse error in front of a steward wearing a judgement's clothes.
    """
    raw = reply.get("verdict")
    try:
        return SupersessionVerdict(str(raw))
    except ValueError as exc:
        raise ValueError(f"unknown verdict {raw!r}") from exc


def _replacement_index(reply: dict[str, Any]) -> int | None:
    """Which clause the model read as the replacement, by position (ADR-0034).

    `null` is a real answer — "one replaced the other and the text does not say which" — and is
    not the same as a missing key or an index outside the pair, both of which mean the model
    lost the alignment and are failures.
    """
    if "replacement_idx" not in reply:
        raise ValueError("reply carried no replacement_idx")
    raw = reply["replacement_idx"]
    if raw is None:
        return None
    try:
        index = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"non-numeric replacement_idx {raw!r}") from exc
    if index not in (0, 1):
        raise ValueError(f"replacement_idx {index} is outside the pair")
    return index


def _confidence(reply: dict[str, Any]) -> float:
    try:
        return max(0.0, min(1.0, float(reply.get("confidence", 0.0) or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _note(judged: Adjudication, reason: str) -> str:
    """Keep the model's own reasoning and say why we did not take its conclusion.

    The steward reads both. A rationale replaced by ours would hide that a model *did* judge
    this pair and what it judged, which is the evidence for looking at it at all.
    """
    rationale = judged.rationale.strip()
    return f"{rationale} [{reason}]" if rationale else f"[{reason}]"


def _render_delta(quantity_delta: dict[str, Any]) -> str:
    changed = quantity_delta.get("changed") or []
    return "\n".join(f"- {line}" for line in changed) if changed else "(không có thay đổi số liệu)"


def _render_facets(facets: dict[str, Any]) -> str:
    lines: list[str] = []
    for facet, values in sorted((facets.get("matched") or {}).items()):
        lines.append(f"- {facet}: cả hai cùng nêu {', '.join(values)}")
    for facet, values in sorted((facets.get("unstated") or {}).items()):
        lines.append(f"- {facet}: chỉ một bên nêu ({', '.join(values)})")
    return "\n".join(lines) if lines else "(hai điều khoản không tự nêu đối tượng áp dụng)"


__all__ = [
    "ADJUDICATOR_PROMPT",
    "PROMPT_VERSION",
    "Adjudication",
    "Clause",
    "ClauseAdjudicator",
    "SettledBy",
]
