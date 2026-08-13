"""ASGI entrypoint for kb-indexer.

Outbox consumer and index writer. Write path only: it never serves a read, which is why the
invariant lint lets it hold an index adapter at all (INV-1). The HTTP surface exists for
health, metrics and operator-triggered reindexing — the real work runs in the poll loop.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from kb_common.app import create_app
from kb_common.config import get_settings
from kb_common.db import get_session
from kb_ports.adapters.pg_search_index import PgSearchIndexAdapter
from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
from kb_ports.indexes import KeywordIndexPort
from pydantic import BaseModel
from sqlalchemy.orm import Session

from kb_indexer.consumer import OutboxConsumer

app: FastAPI = create_app("kb-indexer")


def keyword_index(session: Session = Depends(get_session)) -> KeywordIndexPort:
    """`KB_KEYWORD_BACKEND` selects; the caller only knows the port (ADR-0021)."""
    settings = get_settings()
    if settings.keyword.backend == "pg_search":
        return PgSearchIndexAdapter(session)
    return PostgresFtsIndexAdapter(session)


class DrainResult(BaseModel):
    processed: int
    failed: int
    skipped: int


@app.post("/v1/drain", response_model=DrainResult)
def drain(
    batch_size: int = 20,
    session: Session = Depends(get_session),
    index: KeywordIndexPort = Depends(keyword_index),
) -> DrainResult:
    """Process one batch now. Used by tests and by an operator catching up a backlog."""
    result = OutboxConsumer(session, keyword_index=index).run_once(batch_size=batch_size)
    return DrainResult(processed=result.processed, failed=result.failed, skipped=result.skipped)


def run_consumer() -> None:  # pragma: no cover - process entrypoint
    """The indexer's actual job: poll the outbox forever."""
    from kb_common.db import session_scope
    from kb_common.logging import configure_logging

    settings = get_settings()
    configure_logging("kb-indexer", settings.log_level, settings.log_format)
    with session_scope() as session:
        index = (
            PgSearchIndexAdapter(session)
            if settings.keyword.backend == "pg_search"
            else PostgresFtsIndexAdapter(session)
        )
        OutboxConsumer(session, keyword_index=index).run_forever()


def run() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
