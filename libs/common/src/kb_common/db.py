"""Engine/session plumbing.

Every service shares one Postgres (plan section 1). The publish path (INV-5) needs a single
transaction spanning registry writes, chunk tombstoning, graph rebuild and the outbox insert,
so services take a `Session` rather than opening their own connections per call.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from kb_common.config import DatabaseSettings, get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def create_db_engine(settings: DatabaseSettings | None = None) -> Engine:
    cfg = settings or get_settings().db
    engine = create_engine(
        cfg.url,
        pool_size=cfg.pool_size,
        max_overflow=cfg.max_overflow,
        pool_pre_ping=True,
        future=True,
    )

    # Read once, at engine construction: the connect hook must not do settings lookups.
    keyword_backend = get_settings().keyword.backend

    @event.listens_for(engine, "connect")
    def _set_session_params(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
        with dbapi_conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {cfg.statement_timeout_ms}")
            # ltree lives in public; keep search_path explicit for partitioned deployments.
            cur.execute("SET search_path = public")
            if keyword_backend == "pg_search":
                # Not a tuning knob — a crash guard, and one worth stating plainly.
                #
                # pg_search 0.25.2 segfaults the backend when its custom scan runs under a
                # *generic* plan. A parameterised BM25 query survives ten executions and dies
                # on the eleventh: psycopg prepares the statement after five, Postgres builds
                # a generic plan after five more, and the eleventh takes the whole cluster
                # into crash recovery — every other connection with it.
                #
                # Forcing custom plans costs a re-plan per execution (microseconds on these
                # queries) and removes the failure entirely; `test_pg_search_survives_a_generic
                # _plan` in libs/ports/tests guards it. See ADR-0021, operational notes.
                cur.execute("SET plan_cache_mode = force_custom_plan")

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Read-oriented session. Commits on success, rolls back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


def affected_rows(result: object) -> int:
    """Rows a DML statement touched.

    `Session.execute()` is typed as returning `Result`, which has no `rowcount`; the object
    actually returned for DML is a `CursorResult`, which does. One helper beats a cast at
    every call site.
    """
    return int(getattr(result, "rowcount", 0) or 0)


@contextmanager
def serializable_transaction(session: Session) -> Iterator[Session]:
    """Wrap the atomic publish (INV-5).

    Serializable isolation is what makes "one canonical version per document" safe against a
    concurrent publish of a second version of the same document; the partial unique index is
    the backstop, this is the first line.
    """
    session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise


def reset_engine_cache() -> None:
    """Tests only."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
