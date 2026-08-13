"""Confidence scoring and the escalation decision.

An OCR engine's per-character confidence is a weak signal on its own — it is high on
confidently-wrong output and low on unusual but correct words. So the page score combines it
with cheap structural evidence that a page has gone wrong in the ways this corpus actually
fails:

* **diacritic plausibility** — Vietnamese text is ~30-60% accented. A page of Vietnamese that
  comes back with almost no tone marks is not clean text, it is a page whose marks were lost;
* **garbage ratio** — runs of isolated punctuation and single characters are what speckle
  becomes;
* **coverage** — a page image with plenty of ink and almost no recognized text is a failure,
  whatever the engine reports.

Escalation to the VLM is a cost decision: Qwen2.5-VL is perhaps two orders of magnitude more
expensive per page than PaddleOCR, so it runs on the pages that need it, not the corpus.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from kb_ports.models import OcrPageResult
from kb_schemas.kbdoc import PAGE_ESCALATION_THRESHOLD

#: A page whose mean line confidence is below this is escalated regardless of other signals.
LOW_ENGINE_CONFIDENCE = 0.80
#: Vietnamese prose sits well above this share of accented letters; a page claiming to be
#: Vietnamese with less has lost its tone marks.
MIN_DIACRITIC_RATIO = 0.08
#: Above this share of junk tokens, the page is speckle rather than text.
MAX_GARBAGE_RATIO = 0.25
#: Fewer recognized characters than this on a page with ink is a failed page.
MIN_CHARS_PER_PAGE = 40

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_GARBAGE = re.compile(r"^[^\w\s]{1,}$|^\w$", re.UNICODE)


@dataclass
class PageScore:
    page: int
    engine_confidence: float
    diacritic_ratio: float
    garbage_ratio: float
    char_count: int
    score: float
    escalate: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "page": self.page,
            "engine_confidence": round(self.engine_confidence, 3),
            "diacritic_ratio": round(self.diacritic_ratio, 3),
            "garbage_ratio": round(self.garbage_ratio, 3),
            "char_count": self.char_count,
            "score": round(self.score, 3),
            "escalate": self.escalate,
            "reasons": self.reasons,
        }


def score_page(result: OcrPageResult, *, expect_vietnamese: bool = True) -> PageScore:
    """Combine the engine's confidence with structural evidence."""
    text = " ".join(line.text for line in result.lines)
    engine = result.mean_confidence or _mean(line.confidence for line in result.lines)
    diacritics = diacritic_ratio(text)
    garbage = garbage_ratio(text)

    reasons: list[str] = []
    if engine < LOW_ENGINE_CONFIDENCE:
        reasons.append(f"engine confidence {engine:.2f}")
    if expect_vietnamese and text and diacritics < MIN_DIACRITIC_RATIO:
        reasons.append(f"diacritic ratio {diacritics:.2f} — tone marks may have been lost")
    if garbage > MAX_GARBAGE_RATIO:
        reasons.append(f"garbage ratio {garbage:.2f}")
    if len(text) < MIN_CHARS_PER_PAGE:
        reasons.append(f"only {len(text)} characters recognized")

    # The score is the engine's confidence discounted by each structural failure. Multiplicative
    # rather than averaged: two independent signs of a broken page should compound, not cancel.
    score = engine
    if expect_vietnamese and text and diacritics < MIN_DIACRITIC_RATIO:
        score *= 0.6
    if garbage > MAX_GARBAGE_RATIO:
        score *= 0.7
    if len(text) < MIN_CHARS_PER_PAGE:
        score *= 0.5

    return PageScore(
        page=result.page,
        engine_confidence=engine,
        diacritic_ratio=diacritics,
        garbage_ratio=garbage,
        char_count=len(text),
        score=score,
        escalate=score < PAGE_ESCALATION_THRESHOLD,
        reasons=reasons,
    )


def diacritic_ratio(text: str) -> float:
    """Share of letters carrying a Vietnamese tone mark or modified base (ă, â, đ, ê, ô, ơ, ư)."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    marked = sum(1 for ch in letters if _is_vietnamese_letter(ch))
    return marked / len(letters)


def garbage_ratio(text: str) -> float:
    """Share of tokens that are lone characters or punctuation runs."""
    tokens = text.split()
    if not tokens:
        return 0.0
    return sum(1 for token in tokens if _GARBAGE.match(token)) / len(tokens)


def _is_vietnamese_letter(ch: str) -> bool:
    if ch in "ăâđêôơưĂÂĐÊÔƠƯ":
        return True
    decomposed = unicodedata.normalize("NFD", ch)
    return len(decomposed) > 1 and any(
        unicodedata.category(part) == "Mn" for part in decomposed[1:]
    )


def _mean(values: object) -> float:
    items = list(values)  # type: ignore[call-overload]
    return sum(items) / len(items) if items else 0.0


def block_confidence(lines: list[float]) -> float:
    """A block is as good as its worst line.

    Averaging hides the one misread line that changes a clause's meaning, and the reviewer's
    job is to find exactly that line.
    """
    return min(lines) if lines else 0.0
