"""Turning a conversation into a query.

"Vậy còn ngân hàng nhỏ thì sao?" retrieves nothing on its own. Condensation is what makes the
second and third questions in a conversation work at all, and it is the one model call in the
pipeline whose output goes straight into a *search*, which gives it a specific risk: a
condenser that invents an entity sends the retriever looking for something the user never
asked about, and the answer is then grounded in genuinely irrelevant documents.

So the condensed query is validated, not trusted:

* it may only contain words that appear in the conversation, plus function words — a query
  naming "Thông tư 41" when nobody said 41 is rejected;
* it is length-capped;
* anything that fails falls back to the user's last message verbatim, which is always a legal
  query and never a fabricated one.

With no model configured the fallback is the whole implementation, and the pipeline still
works for single-turn questions — which is most of them.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from kb_common.logging import get_logger
from kb_ports.models import GenerationPort, Message
from kb_schemas.api import ChatMessage

log = get_logger(__name__)

PROMPT_FILE = Path(__file__).resolve().parents[2] / "prompts" / "query_condenser.md"
PROMPT_VERSION = "1"

MAX_QUERY_CHARS = 300
_JSON = re.compile(r"\{.*\}", re.DOTALL)
_WORD = re.compile(r"\w+", re.UNICODE)

#: Words a condenser may introduce without having seen them: the connective tissue of a
#: Vietnamese question. Anything outside this set has to come from the conversation.
_FUNCTION_WORDS = frozenset(
    """
    la gi nao the nay do kia va hoac cua cho voi tu den ve theo tai trong ngoai tren duoi
    bao nhieu nhu khi neu thi ma cac nhung mot hai co khong duoc phai can nen se dang da
    chua hay hoi quy dinh the_nao ra sao vay con o boi vi nham muc dich su dung ap what is a
    an of for in on to how much many which when where why does are
    """.split()  # noqa: SIM905 — a word list stays readable as prose, not as 60 quoted items
)


@dataclass(frozen=True, slots=True)
class Condensed:
    query: str
    #: False when the model was not used or its output was rejected. Logged, and visible in
    #: the answer's audit record: a bad answer to a condensed question is a different bug
    #: from a bad answer to the question as asked.
    condensed: bool
    reason: str = ""


def _fold(text: str) -> str:
    """Lowercase, strip diacritics. Used only for the vocabulary check — never for the query
    itself, where diacritics carry meaning."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _vocabulary(messages: list[ChatMessage]) -> set[str]:
    return {word for message in messages for word in _WORD.findall(_fold(message.content))}


def transcript(messages: list[ChatMessage], turns: int) -> str:
    kept = messages[-turns:] if turns > 0 else messages
    role = {"user": "Người dùng", "assistant": "Trợ lý"}
    return "\n".join(f"{role.get(m.role, m.role)}: {m.content}" for m in kept)


def needs_condensing(messages: list[ChatMessage]) -> bool:
    """A single question is already standalone; spending a model call on it adds latency and
    a chance to get it wrong."""
    return len(messages) > 1


class QueryCondenser:
    def __init__(self, generation: GenerationPort | None, *, prompt_file: Path | None = None):
        self._generation = generation
        self._prompt = (prompt_file or PROMPT_FILE).read_text(encoding="utf-8")

    def condense(self, messages: list[ChatMessage], *, history_turns: int = 6) -> Condensed:
        last = messages[-1].content.strip()
        if self._generation is None or not needs_condensing(messages):
            return Condensed(query=last, condensed=False, reason="single turn")

        prompt = self._prompt.replace("{{conversation}}", transcript(messages, history_turns))
        try:
            raw = self._generation.generate(
                [Message(role="user", content=prompt)], temperature=0.0, max_tokens=200
            ).text
        except Exception as exc:
            log.warning("condense_failed", extra={"error": str(exc)})
            return Condensed(query=last, condensed=False, reason="model unavailable")

        candidate = self._parse(raw)
        if candidate is None:
            return Condensed(query=last, condensed=False, reason="unparseable output")

        problem = self._reject(candidate, messages)
        if problem:
            # Not an error: this is the guard working. The user's own words are always a
            # query we can defend.
            log.info("condense_rejected", extra={"reason": problem})
            return Condensed(query=last, condensed=False, reason=problem)

        return Condensed(query=candidate, condensed=candidate != last)

    # ------------------------------------------------------------------------ internals

    def _parse(self, raw: str) -> str | None:
        match = _JSON.search(raw)
        if match is None:
            return None
        try:
            value = json.loads(match.group(0)).get("query")
        except json.JSONDecodeError:
            return None
        text = str(value or "").strip()
        return text or None

    def _reject(self, candidate: str, messages: list[ChatMessage]) -> str:
        if len(candidate) > MAX_QUERY_CHARS:
            return "too long"
        known = _vocabulary(messages) | _FUNCTION_WORDS
        invented = [
            word for word in _WORD.findall(_fold(candidate)) if word not in known and len(word) > 1
        ]
        if invented:
            # The retriever would go looking for something nobody asked about, and the answer
            # would be grounded in documents that are genuinely irrelevant.
            return f"invented terms: {', '.join(sorted(set(invented))[:5])}"
        return ""
