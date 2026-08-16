"""A cache in front of any `GenerationPort` (ADR-0035).

A wrapper and not a new concept: INV-12 already puts everything model-shaped behind a port, so
caching is something that goes *around* one. Any adapter works — the proxy, a direct vLLM
endpoint, the deterministic stand-ins — and nothing calling `generate` learns a new protocol.

**The key is a hash of everything that can change the answer**, the rendered prompt included.
That is the property the whole design turns on, and it is also what makes the cache safe: a
document whose text changed renders a different prompt and therefore hashes to a different key,
so a stored judgement can never be served for text it was not made about. Keying on a document
or chunk id would have exactly that bug.

Three constraints from the ADR:

* **Only deterministic calls are cached.** `temperature > 0` means the caller is asking for
  variation, and serving it a stored answer silently defeats the request.
* **The prompt version is in the key and on the row.** Strictly the rendered prompt already
  covers it — the template's text is part of what gets rendered — but recording it makes "which
  prompt produced this verdict" a query rather than an archaeology exercise, and it is a second
  lock on the failure this prevents: today's prompt served yesterday's verdict, which is
  invisible in every output the system produces.
* **A miss is never an error.** Nor is a broken cache. The store is an optimisation with no
  semantics of its own, so every failure to read or write it is logged and swallowed — a
  database hiccup must not turn into a failed ingest.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime

from kb_common.db import affected_rows
from sqlalchemy import text
from sqlalchemy.orm import Session

from kb_ports.adapters.generation import DEFAULT_TEMPERATURE
from kb_ports.base import AdapterInfo
from kb_ports.models import Generation, GenerationPort, Message

#: Below this the caller wants one answer, repeatedly. At or above it they want variation, and
#: a cache would quietly refuse to give it.
DETERMINISTIC: float = 0.0


def cache_key(
    *,
    model_id: str,
    prompt_version: str,
    rendered: str,
    temperature: float,
    max_tokens: int,
) -> str:
    """Everything that can change the answer, in one hash.

    Joined with a separator that cannot appear in any part, so two different calls cannot
    collide by concatenating to the same string — `("a", "bc")` and `("ab", "c")` are different
    keys, which they would not be under naive concatenation.
    """
    parts = (model_id, prompt_version, rendered, f"{temperature!r}", str(max_tokens))
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


class CachedGeneration:
    """`GenerationPort` semantics, with deterministic answers remembered."""

    def __init__(
        self,
        inner: GenerationPort,
        session_factory: Callable[[], Session],
        *,
        prompt_version: str = "",
    ) -> None:
        self._inner = inner
        self._sessions = session_factory
        self._prompt_version = prompt_version
        self.hits = 0
        self.misses = 0

    @property
    def info(self) -> AdapterInfo:
        """The wrapped adapter's own identity, with the cache declared.

        Not a new adapter name: what answered is still the model underneath, and an answer log
        that said "cached" instead of naming the model would lose the fact Compliance asks for
        first (INV-11, `leaves_network`).
        """
        inner = self._inner.info
        return AdapterInfo(
            name=inner.name,
            version=inner.version,
            endpoint=inner.endpoint,
            extra={**inner.extra, "cached": True},
        )

    def health(self) -> bool:
        return self._inner.health()

    def generate(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = 1024,
        stop: Sequence[str] | None = None,
    ) -> Generation:
        if temperature > DETERMINISTIC:
            # The caller asked for variation. Answering from a store would silently refuse it.
            return self._inner.generate(
                messages, temperature=temperature, max_tokens=max_tokens, stop=stop
            )

        rendered = _render(messages)
        key = cache_key(
            model_id=self._inner.info.version,
            prompt_version=self._prompt_version,
            rendered=rendered,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        stored = self._read(key)
        if stored is not None:
            self.hits += 1
            return stored

        self.misses += 1
        result = self._inner.generate(
            messages, temperature=temperature, max_tokens=max_tokens, stop=stop
        )
        self._write(key, result)
        return result

    def stream(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        """Never cached. A stream's value is that it arrives progressively, and replaying one
        from a store is a non-streaming answer wearing a stream's shape."""
        return self._inner.stream(messages, temperature=temperature, max_tokens=max_tokens)

    # -------------------------------------------------------------------------- internals

    def _read(self, key: str) -> Generation | None:
        try:
            with self._sessions() as session:
                row = (
                    session.execute(
                        text(
                            "UPDATE model_cache SET hits = hits + 1, last_used_at = :now "
                            "WHERE key = :key "
                            "RETURNING response, finish_reason, prompt_tokens, completion_tokens, "
                            "model_id"
                        ),
                        {"key": key, "now": datetime.now(UTC)},
                    )
                    .mappings()
                    .one_or_none()
                )
                session.commit()
        except Exception as exc:  # pragma: no cover - the cache has no semantics of its own
            _log_failure("read", exc)
            return None
        if row is None:
            return None
        return Generation(
            text=row["response"],
            prompt_tokens=int(row["prompt_tokens"]),
            completion_tokens=int(row["completion_tokens"]),
            finish_reason=str(row["finish_reason"] or "stop"),
            model=str(row["model_id"]),
        )

    def _write(self, key: str, result: Generation) -> None:
        now = datetime.now(UTC)
        try:
            with self._sessions() as session:
                session.execute(
                    text(
                        """
                        INSERT INTO model_cache (key, model_id, prompt_version, response,
                            finish_reason, prompt_tokens, completion_tokens, created_at,
                            last_used_at, hits)
                        VALUES (:key, :model, :version, :response, :finish, :prompt_tokens,
                            :completion_tokens, :now, :now, 0)
                        ON CONFLICT (key) DO NOTHING
                        """
                    ),
                    {
                        "key": key,
                        "model": result.model,
                        "version": self._prompt_version or None,
                        "response": result.text,
                        "finish": result.finish_reason,
                        "prompt_tokens": result.prompt_tokens,
                        "completion_tokens": result.completion_tokens,
                        "now": now,
                    },
                )
                session.commit()
        except Exception as exc:  # pragma: no cover - see above
            _log_failure("write", exc)


def evict(session: Session, *, keep: int) -> int:
    """Trim the cache to its most recently used `keep` rows.

    Size-based rather than time-based: an entry's age says nothing about whether it is still
    the right answer — that is what the key is for — so what matters is only how much room the
    table is allowed to take.
    """
    result = session.execute(
        text(
            """
            DELETE FROM model_cache
            WHERE key IN (
                SELECT key FROM model_cache ORDER BY last_used_at DESC OFFSET :keep
            )
            """
        ),
        {"keep": keep},
    )
    return affected_rows(result)


def _render(messages: Sequence[Message]) -> str:
    return "\x00".join(f"{message.role}\x01{message.content}" for message in messages)


def _log_failure(action: str, exc: Exception) -> None:
    from kb_common.logging import get_logger

    get_logger(__name__).warning(
        "model_cache_unavailable", extra={"action": action, "error": str(exc)}
    )


__all__ = ["DETERMINISTIC", "CachedGeneration", "cache_key", "evict"]
