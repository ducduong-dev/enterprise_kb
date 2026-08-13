"""Adapter resolution.

Adapters register themselves by (port, name); services ask for a port and get whatever the
settings select. This is the seam that keeps the [OPEN] decisions cheap: `KB_KEYWORD_BACKEND`
picks pg_search or the Postgres FTS fallback, `KB_MODEL_GENERATION_BACKEND` picks vLLM or
the hosted API.

Adapters live in `services/*/adapters/` (or `libs/ports/adapters/` when shared) and are
imported for their side effect of registering. Nothing else may import them (INV-1/INV-12).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from kb_common.errors import ConfigError

T = TypeVar("T")

_REGISTRY: dict[tuple[str, str], Callable[..., Any]] = {}


def register_adapter(port: str, name: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def decorator(factory: Callable[..., T]) -> Callable[..., T]:
        key = (port, name)
        if key in _REGISTRY:
            raise ConfigError(f"adapter already registered for {port}:{name}")
        _REGISTRY[key] = factory
        return factory

    return decorator


def get_adapter(port: str, name: str, **kwargs: Any) -> Any:
    try:
        factory = _REGISTRY[(port, name)]
    except KeyError as exc:
        available = sorted(n for p, n in _REGISTRY if p == port)
        raise ConfigError(f"no adapter {name!r} for port {port!r}", available=available) from exc
    return factory(**kwargs)


def registered(port: str | None = None) -> list[tuple[str, str]]:
    return sorted(k for k in _REGISTRY if port is None or k[0] == port)


def clear_registry() -> None:
    """Tests only."""
    _REGISTRY.clear()


class PortName:
    STORAGE = "storage"
    KEYWORD_INDEX = "keyword_index"
    VECTOR_INDEX = "vector_index"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    GENERATION = "generation"
    OCR = "ocr"
    VLM = "vlm"
    PII = "pii"
