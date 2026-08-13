"""Is this upload the same instrument we already hold?

The question the whole consolidation chain rests on. Get it wrong in one direction and a
circular's history forks into two documents that each look complete and neither is; get it
wrong in the other and an unrelated instrument is quietly filed as a revision of something
else, its predecessor's text tombstoned.

So the matcher is layered, and each layer states what it is confident about:

1. **Exact legal number** — `41/2016/TT-NHNN` is a globally unique identifier issued by the
   State Bank. An exact match is the same instrument. Decided automatically.
2. **Normalized number** — `41/2016/QD-NHNN` and `41/2016/QĐ-NHNN` are the same string typed
   two ways, and OCR produces both. Also decided automatically: folding diacritics in the type
   code cannot merge two genuinely different instruments, because the code is a closed set.
3. **Fuzzy number plus title** — a number recovered from a bad scan (`41/2Ol6/TT-NHNN`) or a
   document that carries no number at all. Never decided automatically. This layer *proposes*,
   and a human confirms, because the failure it guards against is silent history corruption.

Everything below the thresholds is a new document. That is the safe direction: a duplicate
document is visible and fixable; a wrongly-merged one has already tombstoned the text it
replaced.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from kb_vntext.legal_numbers import normalize_legal_number

#: Above this, a fuzzy number match is worth showing a human. Below it, nothing is proposed.
FUZZY_NUMBER_THRESHOLD = 0.85
#: Titles are noisy — the same circular is filed under half a dozen phrasings — so a title
#: alone never proposes a match. It only corroborates a number that nearly matched.
TITLE_SUPPORT_THRESHOLD = 0.55
#: Both together, when the number is absent entirely. Deliberately high: matching two
#: documents on their titles alone is how unrelated policies get merged.
TITLE_ONLY_THRESHOLD = 0.90

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
#: Words that appear in almost every instrument title and carry no discriminating signal.
_TITLE_STOPWORDS = frozenset(
    {
        "thông", "tư", "nghị", "định", "quyết", "quy", "chế", "trình", "về", "việc",
        "của", "và", "các", "ban", "hành", "sửa", "đổi", "bổ", "sung", "một", "số", "điều",
    }
)  # fmt: skip


class MatchDecision(StrEnum):
    """What the platform may do without asking a human."""

    #: Same instrument, certain. The upload becomes a new version of the existing document.
    SAME_INSTRUMENT = "same_instrument"
    #: Plausibly the same. Opens an identity_review task; nothing is merged until confirmed.
    NEEDS_REVIEW = "needs_review"
    #: Nothing close enough. A new document.
    NEW_DOCUMENT = "new_document"


@dataclass(frozen=True, slots=True)
class Candidate:
    """A registry document the upload might be a revision of."""

    document_id: str
    legal_number: str | None
    title: str
    doc_class: str | None = None


@dataclass(frozen=True, slots=True)
class MatchResult:
    decision: MatchDecision
    candidate: Candidate | None = None
    score: float = 0.0
    #: Which layer decided, and why — shown on the review task and recorded in the audit log.
    layer: str = "none"
    reason: str = ""
    #: Other plausible candidates, so a reviewer sees the alternatives rather than one guess.
    alternatives: tuple[tuple[Candidate, float], ...] = ()

    @property
    def is_automatic(self) -> bool:
        return self.decision is MatchDecision.SAME_INSTRUMENT

    def as_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "layer": self.layer,
            "score": round(self.score, 3),
            "reason": self.reason,
            "candidate_id": self.candidate.document_id if self.candidate else None,
            "candidate_number": self.candidate.legal_number if self.candidate else None,
            "alternatives": [
                {"document_id": c.document_id, "score": round(s, 3)} for c, s in self.alternatives
            ],
        }


def match(
    *,
    legal_number: str | None,
    title: str,
    candidates: list[Candidate],
) -> MatchResult:
    """Decide whether this upload is a revision of something already in the registry."""
    if not candidates:
        return MatchResult(
            decision=MatchDecision.NEW_DOCUMENT,
            layer="none",
            reason="no document in the registry resembles this one",
        )

    # Layer 1 and 2: the number, exactly and then normalized.
    if legal_number:
        for candidate in candidates:
            if candidate.legal_number == legal_number:
                return MatchResult(
                    decision=MatchDecision.SAME_INSTRUMENT,
                    candidate=candidate,
                    score=1.0,
                    layer="exact_number",
                    reason=f"legal number {legal_number} matches exactly",
                )

        normalized = normalize_legal_number(legal_number)
        for candidate in candidates:
            if (
                candidate.legal_number
                and normalize_legal_number(candidate.legal_number) == normalized
            ):
                return MatchResult(
                    decision=MatchDecision.SAME_INSTRUMENT,
                    candidate=candidate,
                    score=0.99,
                    layer="normalized_number",
                    reason=(
                        f"legal number matches {candidate.legal_number} once diacritics in the "
                        "instrument code are folded"
                    ),
                )

    # Layer 3: fuzzy. Proposes, never decides.
    scored = sorted(
        ((candidate, _score(legal_number, title, candidate)) for candidate in candidates),
        key=lambda pair: pair[1],
        reverse=True,
    )
    best, best_score = scored[0]
    alternatives = tuple((c, s) for c, s in scored[1:4] if s > 0.5)

    if legal_number and best.legal_number:
        number_score = _similarity(
            normalize_legal_number(legal_number), normalize_legal_number(best.legal_number)
        )
        title_score = _title_similarity(title, best.title)
        if number_score >= FUZZY_NUMBER_THRESHOLD and title_score >= TITLE_SUPPORT_THRESHOLD:
            return MatchResult(
                decision=MatchDecision.NEEDS_REVIEW,
                candidate=best,
                score=best_score,
                layer="fuzzy_number",
                reason=(
                    f"number is {number_score:.0%} similar to {best.legal_number} and the "
                    f"titles are {title_score:.0%} similar — possibly the same instrument "
                    "misread from a scan"
                ),
                alternatives=alternatives,
            )

    if not legal_number:
        title_score = _title_similarity(title, best.title)
        if title_score >= TITLE_ONLY_THRESHOLD:
            return MatchResult(
                decision=MatchDecision.NEEDS_REVIEW,
                candidate=best,
                score=title_score,
                layer="title_only",
                reason=(
                    f"no instrument number, but the title is {title_score:.0%} similar to an "
                    "existing document"
                ),
                alternatives=alternatives,
            )

    return MatchResult(
        decision=MatchDecision.NEW_DOCUMENT,
        score=best_score,
        layer="below_threshold",
        reason="nothing in the registry is close enough to be the same instrument",
        alternatives=alternatives,
    )


def _score(legal_number: str | None, title: str, candidate: Candidate) -> float:
    """A single comparable number, for ordering candidates on the review screen."""
    number_score = 0.0
    if legal_number and candidate.legal_number:
        number_score = _similarity(
            normalize_legal_number(legal_number), normalize_legal_number(candidate.legal_number)
        )
    title_score = _title_similarity(title, candidate.title)
    # The number dominates when there is one: titles are re-phrased constantly, numbers are not.
    return 0.7 * number_score + 0.3 * title_score if legal_number else title_score


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def _title_similarity(left: str, right: str) -> float:
    """Token overlap after stripping the boilerplate every instrument title carries.

    "Thông tư quy định về tỷ lệ an toàn vốn" and "Quy định tỷ lệ an toàn vốn" are the same
    document filed twice; comparing the raw strings scores them far apart.
    """
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    return overlap / min(len(left_tokens), len(right_tokens))


def _title_tokens(title: str) -> set[str]:
    folded = unicodedata.normalize("NFC", title.lower())
    return {
        token
        for token in _TOKEN.findall(folded)
        if len(token) > 1 and token not in _TITLE_STOPWORDS
    }
