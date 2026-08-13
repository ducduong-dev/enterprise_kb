"""Language detection for a bilingual corpus.

Deliberately not a model: the decision only needs to pick an analyzer and a prompt language,
the corpus is exactly two languages, and Vietnamese is trivially separable by its diacritics
and function words. A dependency-free heuristic is auditable and cannot drift.

A document is `mixed` when both languages carry real weight — common here, since internal
policies are often bilingual and the retrieval prompts must cover both.
"""

from __future__ import annotations

import re
import unicodedata

#: Vietnamese-only letters (excluding those shared with other Latin languages).
_VN_CHARS = set("ăâđêôơưĂÂĐÊÔƠƯ")

_VN_WORDS = frozenset(
    {
        "và", "của", "các", "được", "cho", "trong", "với", "này", "theo", "khoản",
        "điều", "chương", "quy", "định", "tại", "là", "có", "không", "phải", "về",
        "ngân", "hàng", "khách", "tài", "thực", "hiện", "đối",
    }
)  # fmt: skip

_EN_WORDS = frozenset(
    {
        "the", "and", "of", "to", "in", "for", "is", "are", "shall", "must", "with",
        "this", "that", "be", "on", "by", "as", "at", "from", "bank", "account",
        "customer", "regulation", "policy", "article", "section",
    }
)  # fmt: skip

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

#: Minimum share of recognized words a language needs before it counts at all.
_PRESENCE = 0.10
#: Below this many words, the sample is too small for word statistics — fall back to
#: diacritics alone, which stay reliable at any length.
_MIN_WORDS = 8


def detect_language(text: str) -> str:
    """Return `vi`, `en`, `mixed` or `unknown`."""
    if not text or not text.strip():
        return "unknown"

    words = [w.lower() for w in _WORD.findall(text)]
    has_vn_chars = _vietnamese_char_ratio(text) > 0

    if len(words) < _MIN_WORDS:
        if has_vn_chars:
            return "vi"
        return "en" if words else "unknown"

    vn_score = sum(1 for w in words if w in _VN_WORDS) / len(words)
    en_score = sum(1 for w in words if w in _EN_WORDS) / len(words)
    if has_vn_chars:
        # Diacritics are decisive evidence that word frequencies alone can miss on short or
        # jargon-heavy Vietnamese text.
        vn_score = max(vn_score, _vietnamese_char_ratio(text) * 4)

    vn_present = vn_score >= _PRESENCE
    en_present = en_score >= _PRESENCE

    if vn_present and en_present:
        return "mixed"
    if vn_present:
        return "vi"
    if en_present:
        return "en"
    return "vi" if has_vn_chars else "unknown"


def _vietnamese_char_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    vietnamese = sum(1 for ch in letters if ch in _VN_CHARS or _has_tone_mark(ch))
    return vietnamese / len(letters)


def _has_tone_mark(ch: str) -> bool:
    """True for a Latin letter carrying a combining mark — Vietnamese tones."""
    decomposed = unicodedata.normalize("NFD", ch)
    return len(decomposed) > 1 and any(
        unicodedata.category(part) == "Mn" for part in decomposed[1:]
    )
