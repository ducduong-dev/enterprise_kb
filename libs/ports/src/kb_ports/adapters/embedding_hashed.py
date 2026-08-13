"""Deterministic local embedding — dev and CI only.

**This is not a semantic model.** It is a hashed lexical projection: tokens and character
trigrams hashed into the same 1024 dimensions BGE-M3 uses, so every schema, index, query path
and test can run without a GPU node and produce the same vectors on every machine.

It earns its place because the alternative is worse: without it, CI cannot exercise the
publish → index → retrieve path at all, and the ACL sweep — the invariant that actually
matters — would only run against a keyword index.

Character trigrams (not just tokens) because Vietnamese compounds are written as separate
syllables: "an toàn vốn" and "an toàn vốn tối thiểu" share trigrams that whole-token hashing
would miss. Retrieval quality is still lexical-only; the M2 recall target is met by hybrid
search, and the real numbers come from the GPU adapter.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence

from kb_ports.base import AdapterInfo
from kb_ports.registry import PortName, register_adapter

DIMENSIONS = 1024
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
#: Trigrams below this length are noise; above it, tokens carry the signal on their own.
_TRIGRAM_MIN_TOKEN = 4


class HashedEmbeddingAdapter:
    def __init__(self, dimensions: int = DIMENSIONS) -> None:
        self._dimensions = dimensions

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            name="hashed",
            version="1",
            extra={"semantic": False, "note": "lexical projection for dev/CI only"},
        )

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def health(self) -> bool:
        return True

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        counts = Counter(_features(text))
        if not counts:
            # A zero vector has undefined cosine distance; a fixed unit vector keeps the
            # index well-defined and simply matches nothing in particular.
            vector[0] = 1.0
            return vector

        for feature, count in counts.items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            # Sublinear term frequency: the tenth occurrence of a word says far less than
            # the first, exactly as in BM25.
            vector[index] += sign * (1.0 + math.log(count))

        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def _features(text: str) -> list[str]:
    lowered = text.lower()
    tokens = _TOKEN.findall(lowered)
    features: list[str] = []
    for token in tokens:
        features.append(token)
        # The diacritic-folded form too, so "von" retrieves "vốn" when a user types without
        # tone marks — a routine occurrence in this corpus.
        folded = _fold(token)
        if folded != token:
            features.append(f"~{folded}")
        if len(token) >= _TRIGRAM_MIN_TOKEN:
            features.extend(token[i : i + 3] for i in range(len(token) - 2))
    # Bigrams of adjacent syllables: Vietnamese compounds are multi-token by construction.
    features.extend(f"{a}_{b}" for a, b in itertools.pairwise(tokens))
    return features


def _fold(token: str) -> str:
    decomposed = unicodedata.normalize("NFD", token)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.replace("đ", "d")


@register_adapter(PortName.EMBEDDING, "hashed")
def build_hashed_embedding(dimensions: int = DIMENSIONS) -> HashedEmbeddingAdapter:
    return HashedEmbeddingAdapter(dimensions)
