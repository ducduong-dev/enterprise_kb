"""Rerankers.

The reranker is what turns "twenty plausible chunks" into "the three that answer the
question". It sees the query and the passage together, so it catches the cases hybrid
retrieval gets wrong — a passage that shares every keyword but discusses a different article.

Two adapters: the BGE reranker — through the LiteLLM proxy's `/v1/rerank`, or straight to
text-embeddings-inference (ADR-0024) — and a deterministic lexical one so CI can exercise the
full retrieval pipeline. The lexical fallback is not a quality substitute and says so.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

import httpx
from kb_common.config import ModelGatewaySettings, get_settings
from kb_common.errors import UpstreamError
from kb_common.logging import get_logger

from kb_ports.base import AdapterInfo
from kb_ports.models import RerankResult
from kb_ports.proxy import route
from kb_ports.registry import PortName, register_adapter

log = get_logger(__name__)

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


class TeiRerankAdapter:
    """BGE reranker v2-m3, through the proxy or directly."""

    def __init__(
        self,
        settings: ModelGatewaySettings | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ):
        self._cfg = settings or get_settings().models
        self._route = route("rerank", self._cfg)
        self._client = client or httpx.Client(
            base_url=self._route.base_url, timeout=timeout, headers=self._route.headers
        )

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            name="proxy-rerank" if self._route.proxied else "tei-rerank",
            version=self._route.model,
            endpoint=self._route.base_url,
            extra={
                "leaves_network": self._route.leaves_network,
                "proxied": self._route.proxied,
            },
        )

    def health(self) -> bool:
        try:
            return self._client.get("/health").status_code == 200
        except httpx.HTTPError:  # pragma: no cover - network dependent
            return False

    def rerank(
        self, query: str, passages: Sequence[str], *, top_k: int | None = None
    ) -> list[RerankResult]:
        if not passages:
            return []
        path, payload = (
            (
                "/v1/rerank",
                {
                    "model": self._route.model,
                    "query": query,
                    "documents": list(passages),
                    "top_n": top_k or len(passages),
                },
            )
            if self._route.proxied
            else ("/rerank", {"query": query, "texts": list(passages), "raw_scores": False})
        )
        try:
            response = self._client.post(path, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - network dependent
            raise UpstreamError("rerank service failed", passages=len(passages)) from exc

        body = response.json()
        # Cohere's shape through the proxy (`results: [{index, relevance_score}]`), TEI's bare
        # list directly. Both index into the passages we sent, which is the only thing the
        # caller can use — a reranker that returned text would leave us matching strings.
        items = body["results"] if isinstance(body, dict) else body
        ranked = [
            RerankResult(
                index=int(item["index"]),
                score=float(item.get("relevance_score", item.get("score", 0.0))),
            )
            for item in items
        ]
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:top_k] if top_k else ranked


class LexicalRerankAdapter:
    """Deterministic fallback: IDF-weighted token overlap with a length penalty.

    Good enough to keep the pipeline honest in CI — it does reorder results, and a bug that
    drops or duplicates candidates still shows up — but it does not understand the question.
    """

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            name="lexical", version="1", extra={"semantic": False, "note": "dev/CI only"}
        )

    def health(self) -> bool:
        return True

    def rerank(
        self, query: str, passages: Sequence[str], *, top_k: int | None = None
    ) -> list[RerankResult]:
        if not passages:
            return []

        query_terms = Counter(_tokens(query))
        document_frequency: Counter[str] = Counter()
        tokenized = [_tokens(passage) for passage in passages]
        for tokens in tokenized:
            document_frequency.update(set(tokens))

        total = len(passages)
        scored: list[RerankResult] = []
        for index, tokens in enumerate(tokenized):
            counts = Counter(tokens)
            score = 0.0
            for term, weight in query_terms.items():
                if term not in counts:
                    continue
                idf = math.log(1 + total / (1 + document_frequency[term]))
                score += weight * idf * (1 + math.log(counts[term]))
            # Normalize by length so a long passage does not win on repetition alone.
            scored.append(RerankResult(index=index, score=score / math.sqrt(len(tokens) or 1)))

        scored.sort(key=lambda item: (-item.score, item.index))
        return scored[:top_k] if top_k else scored


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


@register_adapter(PortName.RERANK, "tei")
def build_tei_rerank(settings: ModelGatewaySettings | None = None) -> TeiRerankAdapter:
    return TeiRerankAdapter(settings)


@register_adapter(PortName.RERANK, "lexical")
def build_lexical_rerank() -> LexicalRerankAdapter:
    return LexicalRerankAdapter()
