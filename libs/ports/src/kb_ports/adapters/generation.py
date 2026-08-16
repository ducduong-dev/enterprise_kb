"""Generation adapters.

One implementation for every backend that speaks the OpenAI chat-completions protocol, which
by now is all of them: local vLLM, a hosted API, and the LiteLLM proxy that fronts both
(ADR-0024). What differs between them is a base URL, a credential, and — the part that
actually matters — whether document text leaves the bank's network. `kb_ports.proxy.route`
resolves all three, refuses an external route Compliance has not approved ([OPEN]-1), and the
answer records what it resolved to.

A third, scripted adapter makes the LLM-dependent paths testable without a model. It returns
what it was told to return, in order, and fails loudly when a test asks for more responses than
it was given — a silent default would let a prompt change pass unnoticed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

import httpx
from kb_common.config import ModelGatewaySettings, get_settings
from kb_common.errors import ConfigError, UpstreamError
from kb_common.logging import get_logger

from kb_ports.base import AdapterInfo
from kb_ports.models import Generation, Message
from kb_ports.proxy import route
from kb_ports.registry import PortName, register_adapter

log = get_logger(__name__)

#: Grounded answering is not creative writing: the model must quote the retrieved text, not
#: improve on it. Temperature is a per-call argument, but this is the default everywhere.
DEFAULT_TEMPERATURE = 0.0


class OpenAiCompatibleGeneration:
    """Any OpenAI-compatible generation endpoint: the proxy, vLLM, or a hosted API."""

    def __init__(
        self,
        settings: ModelGatewaySettings | None = None,
        *,
        hosted: bool = False,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._cfg = settings or get_settings().models
        if hosted and not self._cfg.use_proxy and self._cfg.generation_api_key is None:
            raise ConfigError("the hosted generation backend needs KB_MODEL_GENERATION_API_KEY")
        self._route = route("generation", self._cfg)
        # `hosted=True` on a direct route is the pre-proxy way of saying "this leaves the
        # network"; the route already knows, and the two must not be able to disagree.
        self._hosted = hosted or self._route.leaves_network
        self._client = client or httpx.Client(
            base_url=self._route.base_url, timeout=timeout, headers=self._route.headers
        )

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            name="openai-compatible",
            version=self._route.model,
            endpoint=self._route.base_url,
            # Recorded on every answer: whether document text left the bank's network is the
            # single fact Compliance will ask about first ([OPEN]-1).
            extra={
                "hosted": self._hosted,
                "leaves_network": self._route.leaves_network,
                "proxied": self._route.proxied,
            },
        )

    def health(self) -> bool:
        try:
            return self._client.get("/health").status_code == 200
        except httpx.HTTPError:  # pragma: no cover - network dependent
            return False

    def generate(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = 1024,
        stop: Sequence[str] | None = None,
    ) -> Generation:
        payload = {
            "model": self._route.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if stop:
            payload["stop"] = list(stop)
        try:
            response = self._client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - network dependent
            raise UpstreamError("generation failed", model=self._route.model) from exc

        body = response.json()
        choice = body["choices"][0]
        usage = body.get("usage", {})
        # `content` can be null on a 200. A reasoning model generates its trace before the
        # answer and bills both against the same `max_tokens`, so a budget exhausted mid-trace
        # returns `finish_reason: "length"` with nothing in `content` — an empty answer, not an
        # error, and the caller cannot tell the difference from a model that had nothing to say.
        #
        # Coerced to a string here rather than defended against downstream: `verify` and the
        # merge classifier both call string methods on this, and a None would be an
        # AttributeError deep in the answer path instead of the refusal it should be. An empty
        # answer produces no citations, so it becomes a refusal on its own (ADR-0018).
        #
        # `finish_reason` is carried through so a caller that cares can tell "nothing to say"
        # from "ran out of room", which are the same empty string otherwise.
        return Generation(
            text=choice["message"].get("content") or "",
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            finish_reason=str(choice.get("finish_reason", "stop")),
            model=str(body.get("model", self._route.model)),
        )

    def stream(  # pragma: no cover - exercised by the chat surface in M6
        self,
        messages: Sequence[Message],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        payload = {
            "model": self._route.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        with self._client.stream("POST", "/v1/chat/completions", json=payload) as response:
            for line in response.iter_lines():
                if not line.startswith("data: ") or line.endswith("[DONE]"):
                    continue
                import json

                chunk = json.loads(line.removeprefix("data: "))
                delta = chunk["choices"][0].get("delta", {}).get("content")
                if delta:
                    yield delta


@dataclass
class ScriptedGeneration:
    """Returns prepared responses in order. Tests only.

    Records the prompts it was given, so a test can assert what the model was actually asked —
    which for a grounded-answer or PII-judgement prompt is the thing worth asserting.
    """

    responses: list[str] = field(default_factory=list)
    calls: list[list[Message]] = field(default_factory=list)
    #: Raise rather than invent a reply when the script runs out: a silently-defaulted answer
    #: would let a prompt change pass a test it should fail.
    strict: bool = True

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(name="scripted", version="1", extra={"real_model": False})

    def health(self) -> bool:
        return True

    def generate(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = 1024,
        stop: Sequence[str] | None = None,
    ) -> Generation:
        self.calls.append(list(messages))
        if not self.responses:
            if self.strict:
                raise UpstreamError("scripted generation exhausted", calls=len(self.calls))
            return Generation(text="", model="scripted")
        return Generation(text=self.responses.pop(0), model="scripted")

    def stream(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        yield self.generate(messages, temperature=temperature, max_tokens=max_tokens).text

    @property
    def last_prompt(self) -> str:
        return "\n".join(message.content for message in self.calls[-1]) if self.calls else ""


@register_adapter(PortName.GENERATION, "vllm")
def build_vllm_generation(
    settings: ModelGatewaySettings | None = None,
) -> OpenAiCompatibleGeneration:
    """Local vLLM. The default while [OPEN]-1 is unresolved: nothing leaves the network."""
    return OpenAiCompatibleGeneration(settings, hosted=False)


@register_adapter(PortName.GENERATION, "api")
def build_api_generation(
    settings: ModelGatewaySettings | None = None,
) -> OpenAiCompatibleGeneration:
    """Hosted API. Selecting this sends document text off-premises — a Compliance decision."""
    return OpenAiCompatibleGeneration(settings, hosted=True)


@register_adapter(PortName.GENERATION, "scripted")
def build_scripted_generation(responses: list[str] | None = None) -> ScriptedGeneration:
    return ScriptedGeneration(responses=list(responses or []))


class ExtractiveGeneration:
    """A grounded answer built by quoting, not by predicting. Dev and CI only.

    The chat pipeline has two kinds of property. Some are the model's — fluency, whether it
    picked the right clause out of eight. Others are the *system's*: that nothing outside the
    retrieved context can reach the answer, that every claim carries a citation that resolves,
    that an empty context produces a refusal, that the PII filter runs on the way out. The
    second kind is what the acceptance criteria are about, and testing it against a real model
    would mean grading a stochastic process on every CI run.

    So this adapter answers the way the prompt demands and nothing more: it reads the numbered
    passages back out of the prompt it was given and returns the ones whose vocabulary overlaps
    the question, each followed by its `[n]` marker. The result is a genuinely grounded answer
    with genuinely valid citations — perfectly faithful and perfectly unhelpful, which is the
    right trade for a fixture.

    It declares `real_model: False`, so an eval report produced with it cannot be mistaken for
    a measurement of the production model.
    """

    #: How many passages a single answer quotes.
    MAX_PASSAGES = 3
    #: Below this share of the question's substantive words, a passage is not quoted at all —
    #: which is what makes "the context does not answer this" reachable.
    RELEVANCE_THRESHOLD = 0.3
    #: And a passage far weaker than the best one is not quoted either. A model handed eight
    #: passages answers from the two that matter; one that recites all eight is padding, and
    #: padding is where a reader stops checking citations.
    RELATIVE_CUTOFF = 0.6

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(name="extractive", version="1", extra={"real_model": False})

    def health(self) -> bool:
        return True

    def generate(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = 1024,
        stop: Sequence[str] | None = None,
    ) -> Generation:
        prompt = "\n".join(message.content for message in messages)
        question = _section(prompt, "## CÂU HỎI")
        passages = _numbered_passages(_section(prompt, "## NGỮ CẢNH"))
        wanted = _keywords(question)

        ranked = sorted(
            ((_overlap(wanted, text), marker, text) for marker, text in passages),
            key=lambda row: (-row[0], row[1]),
        )
        best = ranked[0][0] if ranked else 0.0
        floor = max(self.RELEVANCE_THRESHOLD, best * self.RELATIVE_CUTOFF)
        scored = [row for row in ranked if row[0] >= floor][: self.MAX_PASSAGES]

        if not scored:
            return Generation(text=NO_BASIS, model="extractive", finish_reason="stop")

        sentences = []
        for _score, marker, text in scored:
            # Quote the passage, not the header line the assembler added: the label carries
            # the instrument number, and repeating it as though the passage said it would be
            # exactly the kind of borrowed authority citation verification exists to catch.
            body = text.split("\n", 1)[1] if "\n" in text else text
            # Quote the document, and only the document: the header line and the assembler's
            # supersession notice are ours, and a quote that swallowed them would attribute
            # our words to the instrument.
            body = "\n".join(
                line for line in body.splitlines() if not line.startswith("[CẢNH BÁO]")
            )
            quoted = " ".join(body.split())[:400].rstrip()
            warning = (
                " Lưu ý: điều khoản này đang có văn bản sửa đổi chưa được hợp nhất."
                if "[CẢNH BÁO]" in text
                else ""
            )
            # Marker inside the sentence it supports, which is the citation style the prompt
            # asks a real model for.
            if quoted.endswith("."):
                quoted = f"{quoted[:-1]} [{marker}]."
            else:
                quoted = f"{quoted} [{marker}]"
            sentences.append(f"{quoted}{warning}")
        return Generation(text=" ".join(sentences), model="extractive", finish_reason="stop")

    def stream(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        yield self.generate(messages, temperature=temperature, max_tokens=max_tokens).text


#: What the extractive adapter says when no passage is relevant. The pipeline turns an answer
#: with no citations into the surface's own refusal text, so this string is never user-facing.
NO_BASIS = "Ngữ cảnh được cung cấp không chứa căn cứ để trả lời câu hỏi này."


def _section(prompt: str, heading: str) -> str:
    _, _, rest = prompt.partition(heading)
    body, _, _ = rest.partition("\n## ")
    return body.strip()


def _numbered_passages(context: str) -> list[tuple[int, str]]:
    passages: list[tuple[int, str]] = []
    for block in re.split(r"\n\s*\n", context):
        match = re.match(r"\s*\[(\d{1,2})\]", block)
        if match:
            passages.append((int(match.group(1)), block))
    return passages


def _keywords(text: str) -> set[str]:
    folded = unicodedata.normalize("NFD", text.lower())
    stripped = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
    return {w for w in re.findall(r"\w+", stripped) if len(w) > 2}


def _overlap(wanted: set[str], passage: str) -> float:
    if not wanted:
        return 0.0
    return len(wanted & _keywords(passage)) / len(wanted)


@register_adapter(PortName.GENERATION, "extractive")
def build_extractive_generation() -> ExtractiveGeneration:
    """Deterministic quoting adapter for dev and CI. Never a production backend."""
    return ExtractiveGeneration()
