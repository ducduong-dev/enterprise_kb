"""The merge draft: classify what changed, then draft the consolidated text.

Two model calls with different jobs, deliberately separated:

* the **classifier** looks at the whole change set and says what *kind* of change each section
  is — cosmetic, substantive, or new/abrogated. This is what lets the merge screen tell a
  reviewer "three articles changed in substance, eleven were re-worded" instead of showing
  fourteen diffs of equal weight;
* the **drafter** works one section at a time, given only that section's before and after. A
  model handed two complete circulars will improve prose nobody asked it to touch; a model
  handed one article and told to apply one change mostly does that.

Everything here produces a *draft*. `văn bản hợp nhất` is a legal artefact — publishing one the
bank has not read would be publishing a machine's opinion of the law. INV-8 makes that
structurally impossible for regulatory documents, and this module never tries.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from kb_common.logging import get_logger
from kb_ports.models import GenerationPort, Message

from kb_identity_merge.diff import ChangeKind, DocumentDiff, SectionChange

log = get_logger(__name__)

PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"
CLASSIFIER_PROMPT = PROMPT_DIR / "merge_classifier.md"
DRAFTER_PROMPT = PROMPT_DIR / "merge_drafter.md"
PROMPT_VERSION = "2"

#: Sections whose text is long enough that drafting them costs real tokens are still drafted
#: one at a time; this caps a runaway document rather than the normal case.
MAX_SECTIONS_DRAFTED = 40

_JSON = re.compile(r"\{.*\}", re.DOTALL)


class Bucket(StrEnum):
    """The three buckets the plan asks for."""

    UNCHANGED_IN_SUBSTANCE = "unchanged_in_substance"
    AMENDED = "amended"
    NEW_OR_ABROGATED = "new_or_abrogated"


@dataclass(frozen=True, slots=True)
class SectionClassification:
    section_path: str
    bucket: Bucket
    impact: str = ""
    confidence: float = 0.0
    #: True when the model did not classify this section and the diff's own verdict was used.
    inferred: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "section_path": self.section_path,
            "bucket": self.bucket.value,
            "impact": self.impact,
            "confidence": round(self.confidence, 3),
            "inferred": self.inferred,
        }


@dataclass(frozen=True, slots=True)
class DraftedSection:
    section_path: str
    consolidated_text: str
    note: str = ""
    #: False when the model failed and the reviewer is looking at the amendment's text
    #: unmodified. The merge screen says so rather than presenting it as a draft.
    drafted: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "section_path": self.section_path,
            "consolidated_text": self.consolidated_text,
            "note": self.note,
            "drafted": self.drafted,
        }


@dataclass
class MergeDraft:
    """What the merge screen renders and what a reviewer approves."""

    classifications: list[SectionClassification] = field(default_factory=list)
    sections: list[DraftedSection] = field(default_factory=list)
    #: Set when any part of the draft could not be produced. A reviewer must be told; an
    #: incomplete draft presented as complete is worse than no draft.
    complete: bool = True
    prompt_version: str = PROMPT_VERSION
    model: str = ""

    def by_bucket(self, bucket: Bucket) -> list[SectionClassification]:
        return [item for item in self.classifications if item.bucket is bucket]

    @property
    def substantive_changes(self) -> int:
        return len(self.by_bucket(Bucket.AMENDED)) + len(self.by_bucket(Bucket.NEW_OR_ABROGATED))

    def as_dict(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "substantive_changes": self.substantive_changes,
            "classifications": [item.as_dict() for item in self.classifications],
            "sections": [item.as_dict() for item in self.sections],
        }


class MergeDrafter:
    def __init__(
        self,
        generation: GenerationPort,
        *,
        classifier_prompt: Path | None = None,
        drafter_prompt: Path | None = None,
    ) -> None:
        self._generation = generation
        self._classifier = (classifier_prompt or CLASSIFIER_PROMPT).read_text(encoding="utf-8")
        self._drafter = (drafter_prompt or DRAFTER_PROMPT).read_text(encoding="utf-8")

    def draft(self, diff: DocumentDiff) -> MergeDraft:
        changes = diff.changed[:MAX_SECTIONS_DRAFTED]
        if not changes:
            return MergeDraft(complete=True, model=self._generation.info.version)

        classifications, classify_ok = self._classify(changes)
        sections, draft_ok = self._draft_sections(changes)

        draft = MergeDraft(
            classifications=classifications,
            sections=sections,
            complete=classify_ok and draft_ok and len(diff.changed) <= MAX_SECTIONS_DRAFTED,
            model=self._generation.info.version,
        )
        log.info(
            "merge_draft_produced",
            extra={
                "sections": len(changes),
                "substantive": draft.substantive_changes,
                "complete": draft.complete,
            },
        )
        return draft

    # ------------------------------------------------------------------------ internals

    def _classify(self, changes: list[SectionChange]) -> tuple[list[SectionClassification], bool]:
        payload = "\n\n".join(
            f"### [{index}] {change.section_path} ({change.kind.value})\n"
            f"Văn bản gốc:\n{change.old_text or '(không có)'}\n"
            f"Văn bản sửa đổi:\n{change.new_text or '(bị bãi bỏ)'}"
            for index, change in enumerate(changes)
        )
        try:
            verdict = self._ask(self._classifier.replace("{sections}", payload), max_tokens=2048)
            # Inside the try on purpose: a reply the indices cannot be read from is a failed
            # call, and fails the same way a reply that was not JSON does (ADR-0034).
            by_index = _index_verdicts(verdict.get("sections", []) or [], len(changes))
        except Exception as exc:
            log.warning("merge_classifier_failed", extra={"error": str(exc)})
            return [_from_diff(change) for change in changes], False

        classifications: list[SectionClassification] = []
        for index, change in enumerate(changes):
            item = by_index.get(index)
            if item is None:
                # The model skipped a section — and now we *know* it did, rather than
                # inferring it from a lookup that could also have missed on a retyped path.
                # The diff's own verdict stands in, flagged, so the reviewer sees it was not
                # classified rather than silently omitted.
                classifications.append(_from_diff(change))
                continue
            classifications.append(
                SectionClassification(
                    # The caller owns the mapping. The model never retypes a section path, so
                    # nothing about an OCR'd heading can break it.
                    section_path=change.section_path,
                    bucket=_bucket(str(item.get("bucket", ""))),
                    impact=str(item.get("impact", "")),
                    confidence=float(item.get("confidence", 0.0) or 0.0),
                )
            )
        # Every index answered exactly once, or the draft is not complete. Duplicates and
        # out-of-range indices never reach here, so this is a count of real answers rather
        # than of items the model returned.
        return classifications, len(by_index) == len(changes)

    def _draft_sections(self, changes: list[SectionChange]) -> tuple[list[DraftedSection], bool]:
        drafted: list[DraftedSection] = []
        complete = True
        for change in changes:
            if change.kind is ChangeKind.REMOVED:
                drafted.append(
                    DraftedSection(
                        section_path=change.section_path,
                        consolidated_text="",
                        note="mục bị bãi bỏ",
                    )
                )
                continue
            if change.kind is ChangeKind.ADDED:
                # Nothing to merge: the new article is the consolidated article.
                drafted.append(
                    DraftedSection(
                        section_path=change.section_path,
                        consolidated_text=change.new_text,
                        note="mục bổ sung mới",
                    )
                )
                continue

            prompt = (
                self._drafter.replace("{section_path}", change.section_path)
                .replace("{old_text}", change.old_text)
                .replace("{new_text}", change.new_text)
            )
            try:
                verdict = self._ask(prompt, max_tokens=2048)
            except Exception as exc:
                log.warning(
                    "merge_drafter_failed",
                    extra={"section": change.section_path, "error": str(exc)},
                )
                drafted.append(
                    DraftedSection(
                        section_path=change.section_path,
                        consolidated_text=change.new_text,
                        note="không tạo được bản hợp nhất tự động; hiển thị nguyên văn sửa đổi",
                        drafted=False,
                    )
                )
                complete = False
                continue
            drafted.append(
                DraftedSection(
                    section_path=change.section_path,
                    consolidated_text=str(verdict.get("consolidated_text", "")),
                    note=str(verdict.get("note", "")),
                )
            )
        return drafted, complete

    def _ask(self, prompt: str, *, max_tokens: int) -> dict[str, Any]:
        response = self._generation.generate(
            [Message(role="user", content=prompt)], temperature=0.0, max_tokens=max_tokens
        )
        match = _JSON.search(response.text)
        if match is None:
            raise ValueError("model did not return JSON")
        parsed: dict[str, Any] = json.loads(match.group(0))
        return parsed


def _index_verdicts(items: Any, count: int) -> dict[int, dict[str, Any]]:
    """Map the model's verdicts onto the list it was given, by index (ADR-0034).

    Raises on anything that means the model lost the alignment: an index outside the list, an
    index answered twice, or a verdict carrying no index at all. None of those is repaired and
    none is dropped — a model that answers about item 7 of a five-item list did not understand
    the question, and its other four answers are not evidence that it did.
    """
    if not isinstance(items, list):
        raise ValueError("sections must be a list")
    by_index: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("idx") is None:
            raise ValueError("a verdict arrived with no index")
        try:
            index = int(item["idx"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"non-numeric index {item['idx']!r}") from exc
        if not 0 <= index < count:
            raise ValueError(f"index {index} is outside the {count}-section list")
        if index in by_index:
            raise ValueError(f"index {index} was answered twice")
        by_index[index] = item
    return by_index


def _from_diff(change: SectionChange) -> SectionClassification:
    """Fall back to what the diff itself can tell us.

    Conservative on purpose: an edit the model did not classify is treated as substantive, so
    it lands in front of the reviewer rather than in the "just re-worded" pile.
    """
    bucket = {
        ChangeKind.ADDED: Bucket.NEW_OR_ABROGATED,
        ChangeKind.REMOVED: Bucket.NEW_OR_ABROGATED,
    }.get(change.kind, Bucket.AMENDED)
    return SectionClassification(
        section_path=change.section_path,
        bucket=bucket,
        impact="chưa được phân loại tự động",
        confidence=0.0,
        inferred=True,
    )


def _bucket(value: str) -> Bucket:
    try:
        return Bucket(value)
    except ValueError:
        # An unrecognized label means the reviewer reads the section — never that it is
        # treated as cosmetic.
        return Bucket.AMENDED
