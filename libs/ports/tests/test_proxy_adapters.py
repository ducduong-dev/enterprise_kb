"""The wire shape each adapter sends through the proxy (ADR-0024).

No server: an `httpx.MockTransport` answers, and the assertions are on what was asked for. A
protocol mistake here — the wrong path, a model alias that never reaches the proxy, a batch
whose vectors come back reordered — fails in production as bad results rather than as an
error, so it is worth pinning exactly.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from kb_common.config import ModelGatewaySettings
from kb_ports.adapters.embedding_tei import TeiEmbeddingAdapter
from kb_ports.adapters.generation import OpenAiCompatibleGeneration
from kb_ports.adapters.rerank import TeiRerankAdapter
from kb_ports.adapters.vlm import VisionAdapter
from kb_ports.models import Message
from pydantic import SecretStr

PROXY = "http://litellm:4000"


def settings(**overrides: object) -> ModelGatewaySettings:
    base: dict[str, object] = {
        "use_proxy": True,
        "proxy_url": PROXY,
        "proxy_api_key": SecretStr("sk-proxy"),
        "generation_model": "kb-generation",
        "vlm_model": "kb-vlm",
        "embedding_model": "kb-embedding",
        "rerank_model": "kb-rerank",
        "embedding_dim": 3,
    }
    base.update(overrides)
    return ModelGatewaySettings(**base)  # type: ignore[arg-type]


class Recorder:
    """Captures the request and replies with whatever the test prepared."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.reply)

    @property
    def last(self) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self.requests[-1].content)
        return body

    def client(self, cfg: ModelGatewaySettings) -> httpx.Client:
        key = cfg.proxy_api_key.get_secret_value() if cfg.proxy_api_key else ""
        return httpx.Client(
            transport=httpx.MockTransport(self),
            base_url=cfg.proxy_url,
            headers={"Authorization": f"Bearer {key}"},
        )


# ------------------------------------------------------------------------------ generation


def test_generation_asks_the_proxy_for_its_model_alias() -> None:
    cfg = settings()
    recorder = Recorder(
        {
            "model": "kb-generation",
            "choices": [{"message": {"content": "8%"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
    )
    adapter = OpenAiCompatibleGeneration(cfg, client=recorder.client(cfg))
    result = adapter.generate([Message(role="user", content="tỷ lệ?")])

    assert result.text == "8%"
    assert recorder.requests[-1].url.path == "/v1/chat/completions"
    assert recorder.last["model"] == "kb-generation"
    assert recorder.requests[-1].headers["authorization"] == "Bearer sk-proxy"


def test_a_reasoning_model_that_ran_out_of_room_yields_an_empty_answer() -> None:
    """A 200 with `content: null`.

    Qwen3.5 generates its reasoning trace before the answer and bills both against the same
    `max_tokens`, so a budget exhausted mid-trace comes back successful and empty. Left as
    None it would be an AttributeError deep in the answer path — `verify` and the merge
    classifier both call string methods on it — instead of the refusal an answer with no
    citation is supposed to become (ADR-0018).
    """
    cfg = settings()
    recorder = Recorder(
        {
            "model": "kb-generation",
            "choices": [{"message": {"content": None}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 800},
        }
    )
    adapter = OpenAiCompatibleGeneration(cfg, client=recorder.client(cfg))
    result = adapter.generate([Message(role="user", content="tỷ lệ?")])

    assert result.text == ""
    # Carried through, so a caller can tell "nothing to say" from "ran out of room" — which
    # are the same empty string otherwise, and only one of them is worth raising the budget for.
    assert result.finish_reason == "length"
    assert result.completion_tokens == 800


def test_a_message_with_no_content_key_at_all_is_still_an_empty_answer() -> None:
    """Some providers omit the key rather than sending null. Same outcome either way."""
    cfg = settings()
    recorder = Recorder(
        {"model": "kb-generation", "choices": [{"message": {}, "finish_reason": "stop"}]}
    )
    adapter = OpenAiCompatibleGeneration(cfg, client=recorder.client(cfg))

    assert adapter.generate([Message(role="user", content="?")]).text == ""


def test_generation_records_where_it_ran() -> None:
    cfg = settings()
    adapter = OpenAiCompatibleGeneration(cfg, client=Recorder({}).client(cfg))
    assert adapter.info.extra["proxied"] is True
    assert adapter.info.extra["leaves_network"] is False
    assert adapter.info.version == "kb-generation"


# ---------------------------------------------------------------------------------- vision


def test_vision_sends_the_page_as_an_image_part() -> None:
    cfg = settings()
    recorder = Recorder({"choices": [{"message": {"content": "Điều 6. Tỷ lệ"}}]})
    adapter = VisionAdapter(cfg, client=recorder.client(cfg))
    result = adapter.transcribe_page(b"\x89PNG-not-really")

    assert [line.text for line in result.lines] == ["Điều 6. Tỷ lệ"]
    body = recorder.last
    assert body["model"] == "kb-vlm"
    assert body["temperature"] == 0.0
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "text" and "chép lại CHÍNH XÁC" in parts[0]["text"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_public_vision_model_is_marked_as_leaving_the_bank() -> None:
    """The page image is the document. Where it was read travels with the transcription."""
    cfg = settings(
        vlm_model="gemini-2.0-flash",
        external_models=frozenset({"gemini-2.0-flash"}),
        allow_external_processing=True,
    )
    adapter = VisionAdapter(cfg, client=Recorder({}).client(cfg))
    assert adapter.info.extra["leaves_network"] is True
    assert adapter.info.version == "gemini-2.0-flash"


# ------------------------------------------------------------------------------- embedding


def test_embedding_uses_the_openai_shape_through_the_proxy() -> None:
    cfg = settings()
    recorder = Recorder(
        {
            "data": [
                {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                {"index": 0, "embedding": [0.1, 0.2, 0.3]},
            ]
        }
    )
    adapter = TeiEmbeddingAdapter(cfg, client=recorder.client(cfg))
    vectors = adapter.embed_documents(["một", "hai"])

    assert recorder.requests[-1].url.path == "/v1/embeddings"
    assert recorder.last["model"] == "kb-embedding"
    assert recorder.last["input"] == ["một", "hai"]
    # Returned out of order on purpose: a vector attached to the wrong chunk is undetectable
    # downstream, so the adapter sorts by index rather than trusting the response order.
    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


def test_embedding_still_speaks_tei_when_the_proxy_is_off() -> None:
    cfg = settings(use_proxy=False, embedding_url="http://tei:80")
    recorder = Recorder([[0.1, 0.2, 0.3]])  # type: ignore[arg-type]
    client = httpx.Client(transport=httpx.MockTransport(recorder), base_url=cfg.embedding_url)
    adapter = TeiEmbeddingAdapter(cfg, client=client)

    assert adapter.embed_query("xin chào") == [0.1, 0.2, 0.3]
    assert recorder.requests[-1].url.path == "/embed"
    assert recorder.last["truncate"] is True


def test_a_dimension_mismatch_fails_instead_of_corrupting_the_index() -> None:
    from kb_common.errors import UpstreamError

    cfg = settings()
    recorder = Recorder({"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
    adapter = TeiEmbeddingAdapter(cfg, client=recorder.client(cfg))
    with pytest.raises(UpstreamError) as exc:
        adapter.embed_query("xin chào")
    assert exc.value.detail["expected"] == 3


# ---------------------------------------------------------------------------------- rerank


def test_rerank_uses_the_proxy_rerank_endpoint() -> None:
    cfg = settings()
    recorder = Recorder(
        {"results": [{"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.4}]}
    )
    adapter = TeiRerankAdapter(cfg, client=recorder.client(cfg))
    ranked = adapter.rerank("tỷ lệ", ["a", "b", "c"], top_k=2)

    assert recorder.requests[-1].url.path == "/v1/rerank"
    assert recorder.last["model"] == "kb-rerank"
    assert recorder.last["documents"] == ["a", "b", "c"]
    assert [item.index for item in ranked] == [2, 0]
    assert ranked[0].score == pytest.approx(0.9)


def test_rerank_still_speaks_tei_when_the_proxy_is_off() -> None:
    cfg = settings(use_proxy=False, rerank_url="http://tei-rerank:80")
    recorder = Recorder([{"index": 1, "score": 0.7}, {"index": 0, "score": 0.2}])  # type: ignore[arg-type]
    client = httpx.Client(transport=httpx.MockTransport(recorder), base_url=cfg.rerank_url)
    adapter = TeiRerankAdapter(cfg, client=client)

    ranked = adapter.rerank("tỷ lệ", ["a", "b"])
    assert recorder.requests[-1].url.path == "/rerank"
    assert [item.index for item in ranked] == [1, 0]
