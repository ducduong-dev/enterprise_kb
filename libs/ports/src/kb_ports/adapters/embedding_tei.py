"""BGE-M3 behind `EmbeddingPort` — through the LiteLLM proxy, or straight to TEI.

BGE-M3 is asymmetric: passages and queries are encoded differently, and mixing the two costs
recall silently rather than failing. That is why the port has separate methods and why this
adapter keeps the distinction rather than collapsing it.

Two request shapes, one adapter (ADR-0024). The proxy speaks OpenAI's `/v1/embeddings`;
text-embeddings-inference speaks its own `/embed`. Which one is in use is a route, and the
dimension check below is what catches the case where the configured model and the served model
have quietly diverged — a mismatch there corrupts the index rather than failing a request.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx
from kb_common.config import ModelGatewaySettings, get_settings
from kb_common.errors import UpstreamError
from kb_common.logging import get_logger

from kb_ports.base import AdapterInfo
from kb_ports.proxy import route
from kb_ports.registry import PortName, register_adapter

log = get_logger(__name__)

#: BGE-M3 needs no instruction prefix for passages, and the query prefix is empty too — but
#: the seam exists here so switching to a model that does need one is an adapter change.
QUERY_PREFIX = ""
PASSAGE_PREFIX = ""

#: Batch size that keeps a single request under the server's payload limit for long chunks.
BATCH = 32


class TeiEmbeddingAdapter:
    def __init__(
        self,
        settings: ModelGatewaySettings | None = None,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ):
        self._cfg = settings or get_settings().models
        self._route = route("embedding", self._cfg)
        self._client = client or httpx.Client(
            base_url=self._route.base_url, timeout=timeout, headers=self._route.headers
        )

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            name="openai-embeddings" if self._route.proxied else "tei",
            version=self._route.model,
            endpoint=self._route.base_url,
            extra={
                "leaves_network": self._route.leaves_network,
                "proxied": self._route.proxied,
            },
        )

    @property
    def dimensions(self) -> int:
        return self._cfg.embedding_dim

    def health(self) -> bool:
        try:
            return self._client.get("/health").status_code == 200
        except httpx.HTTPError:  # pragma: no cover - network dependent
            return False

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            batch = [f"{PASSAGE_PREFIX}{text}" for text in texts[start : start + BATCH]]
            vectors.extend(self._embed(batch))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed([f"{QUERY_PREFIX}{text}"])[0]

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        path, payload = (
            ("/v1/embeddings", {"model": self._route.model, "input": inputs})
            if self._route.proxied
            else ("/embed", {"inputs": inputs, "truncate": True})
        )
        try:
            response = self._client.post(path, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - network dependent
            raise UpstreamError("embedding service failed", inputs=len(inputs)) from exc

        body = response.json()
        # OpenAI returns `{"data": [{"embedding": [...], "index": n}]}` in request order, TEI a
        # bare list. Sorting by index rather than trusting the order is cheap insurance: a
        # reordered batch would mis-attach every vector to the wrong chunk, and nothing
        # downstream could tell.
        vectors: list[list[float]] = (
            [item["embedding"] for item in sorted(body["data"], key=lambda i: i["index"])]
            if isinstance(body, dict)
            else body
        )
        if any(len(vector) != self.dimensions for vector in vectors):
            # A dimension mismatch means the served model is not the configured one. Writing
            # those vectors would corrupt the index silently, so fail loudly instead.
            raise UpstreamError(
                "embedding dimension mismatch",
                expected=self.dimensions,
                got=[len(v) for v in vectors[:1]],
                model=self._route.model,
            )
        return vectors


@register_adapter(PortName.EMBEDDING, "tei")
def build_tei_embedding(settings: ModelGatewaySettings | None = None) -> TeiEmbeddingAdapter:
    return TeiEmbeddingAdapter(settings)
