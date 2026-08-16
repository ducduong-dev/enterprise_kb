"""The prompt-output cache (ADR-0035).

The cache has no semantics of its own — dropping the table changes nothing but the bill — so
almost every test here is about a way it could acquire some by accident: serving an answer for
text that has changed, for a prompt that has changed, or in place of the variation a caller
asked for.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence

import pytest
from kb_common.db import create_db_engine
from kb_ports.adapters.generation_cached import CachedGeneration, cache_key, evict
from kb_ports.base import AdapterInfo
from kb_ports.models import Generation, Message
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


class Counting:
    """A generator that answers differently every call, so a cache hit is unmistakable."""

    def __init__(self, model: str = "test-model") -> None:
        self.calls = 0
        self._model = model

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(name="counting", version=self._model, extra={"real_model": False})

    def health(self) -> bool:
        return True

    def generate(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        stop: Sequence[str] | None = None,
    ) -> Generation:
        self.calls += 1
        return Generation(
            text=f"answer {self.calls}",
            prompt_tokens=10,
            completion_tokens=5,
            finish_reason="stop",
            model=self._model,
        )

    def stream(self, messages: Sequence[Message], **kwargs: object) -> Iterator[str]:
        yield "chunk"


@pytest.fixture
def sessions(migrated: Engine):  # type: ignore[no-untyped-def]
    engine = create_db_engine()
    yield lambda: Session(engine)
    with Session(engine) as session:
        session.execute(text("DELETE FROM model_cache WHERE model_id LIKE 'test-%'"))
        session.commit()


def _ask(adapter: CachedGeneration, prompt: str = "tỷ lệ an toàn vốn?") -> str:
    return adapter.generate([Message(role="user", content=prompt)]).text


def test_the_same_question_is_asked_once(sessions) -> None:  # type: ignore[no-untyped-def]
    """The property the backfill needs: re-running it costs a scan, not the model again."""
    inner = Counting(f"test-{uuid.uuid4().hex[:8]}")
    adapter = CachedGeneration(inner, sessions)

    assert _ask(adapter) == "answer 1"
    assert _ask(adapter) == "answer 1"
    assert inner.calls == 1
    assert (adapter.hits, adapter.misses) == (1, 1)


def test_changed_text_is_a_different_question(sessions) -> None:  # type: ignore[no-untyped-def]
    """The failure the cache could otherwise cause. The document's text is *in* the rendered
    prompt, so different text hashes to a different key — which is why the key is not a document
    or chunk id, since those survive an edit that changes the answer."""
    inner = Counting(f"test-{uuid.uuid4().hex[:8]}")
    adapter = CachedGeneration(inner, sessions)

    assert _ask(adapter, "tỷ lệ tối thiểu là 8%") == "answer 1"
    assert _ask(adapter, "tỷ lệ tối thiểu là 10%") == "answer 2"
    assert inner.calls == 2


def test_editing_the_prompt_invalidates_exactly_its_own_entries(sessions) -> None:  # type: ignore[no-untyped-def]
    """The failure this prevents is invisible in every output the system produces: today's
    prompt served yesterday's verdict."""
    inner = Counting(f"test-{uuid.uuid4().hex[:8]}")
    before = CachedGeneration(inner, sessions, prompt_version="1")
    after = CachedGeneration(inner, sessions, prompt_version="2")

    assert _ask(before) == "answer 1"
    assert _ask(after) == "answer 2", "a new prompt version must not read the old verdict"
    assert _ask(before) == "answer 1", "and the old one is still there for what still uses it"


def test_a_different_model_is_a_different_question(sessions) -> None:  # type: ignore[no-untyped-def]
    """Two models do not answer alike, and a swap that silently reused the other's judgements
    would be a model change with no observable effect."""
    first = CachedGeneration(Counting(f"test-{uuid.uuid4().hex[:8]}"), sessions)
    second = CachedGeneration(Counting(f"test-{uuid.uuid4().hex[:8]}"), sessions)

    assert _ask(first) == "answer 1"
    assert _ask(second) == "answer 1", "a fresh model, a fresh call — not the first one's answer"


def test_variation_is_never_served_from_a_store(sessions) -> None:  # type: ignore[no-untyped-def]
    """`temperature > 0` means the caller is asking for variation, and answering from a store
    silently defeats the request."""
    inner = Counting(f"test-{uuid.uuid4().hex[:8]}")
    adapter = CachedGeneration(inner, sessions)
    messages = [Message(role="user", content="viết lại câu này")]

    first = adapter.generate(messages, temperature=0.7).text
    second = adapter.generate(messages, temperature=0.7).text

    assert first != second
    assert inner.calls == 2
    assert (adapter.hits, adapter.misses) == (0, 0), "not even counted; it never reached the cache"


def test_max_tokens_is_part_of_the_question(sessions) -> None:  # type: ignore[no-untyped-def]
    """A budget-truncated answer is not the answer the same call with room would have given."""
    inner = Counting(f"test-{uuid.uuid4().hex[:8]}")
    adapter = CachedGeneration(inner, sessions)
    messages = [Message(role="user", content="tóm tắt")]

    adapter.generate(messages, max_tokens=64)
    adapter.generate(messages, max_tokens=800)

    assert inner.calls == 2


def test_a_broken_cache_is_not_a_broken_answer(sessions) -> None:  # type: ignore[no-untyped-def]
    """A database hiccup must not turn into a failed ingest. The store is an optimisation with
    no semantics of its own."""

    def explode() -> Session:
        raise RuntimeError("no database today")

    inner = Counting(f"test-{uuid.uuid4().hex[:8]}")
    adapter = CachedGeneration(inner, explode)

    assert _ask(adapter) == "answer 1"
    assert inner.calls == 1


def test_the_adapter_still_names_the_model_that_answered(sessions) -> None:  # type: ignore[no-untyped-def]
    """An answer log saying "cached" instead of naming the model would lose the fact
    Compliance asks for first (INV-11)."""
    inner = Counting(f"test-{uuid.uuid4().hex[:8]}")
    info = CachedGeneration(inner, sessions).info

    assert info.version == inner.info.version
    assert info.extra["cached"] is True


def test_the_key_cannot_collide_by_concatenation() -> None:
    """`("a", "bc")` and `("ab", "c")` are different calls and must not hash alike."""
    left = cache_key(model_id="a", prompt_version="bc", rendered="x", temperature=0.0, max_tokens=1)
    right = cache_key(
        model_id="ab", prompt_version="c", rendered="x", temperature=0.0, max_tokens=1
    )
    assert left != right


def test_eviction_keeps_the_most_recently_used(sessions) -> None:  # type: ignore[no-untyped-def]
    """Size-based, not age-based: an entry's age says nothing about whether it is still the
    right answer — the key is what says that."""
    inner = Counting(f"test-{uuid.uuid4().hex[:8]}")
    adapter = CachedGeneration(inner, sessions)
    for index in range(3):
        _ask(adapter, f"câu hỏi {index}")

    with sessions() as session:
        before = session.execute(
            text("SELECT count(*) FROM model_cache WHERE model_id = :m"),
            {"m": inner.info.version},
        ).scalar()
        evict(session, keep=1)
        session.commit()
        after = session.execute(
            text("SELECT count(*) FROM model_cache WHERE model_id = :m"),
            {"m": inner.info.version},
        ).scalar()

    assert before == 3
    assert after == 1
