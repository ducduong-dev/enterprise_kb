"""Shared fixtures for tests that need live infrastructure.

Everything here degrades to a skip when the database is not reachable, so `make test` works
on a laptop with no stack running, while CI (which has Postgres) runs the full set.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from kb_common.config import get_settings
from kb_common.db import create_db_engine
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    db_engine = create_db_engine(get_settings().db)
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database not reachable: {exc}")
    yield db_engine
    db_engine.dispose()


@pytest.fixture(scope="session")
def migrated(engine: Engine) -> Engine:
    with engine.connect() as connection:
        tables = connection.execute(
            text("SELECT to_regclass('public.chunks') IS NOT NULL")
        ).scalar()
    if not tables:  # pragma: no cover - environment dependent
        pytest.skip("schema not migrated — run `make migrate`")
    return engine


#: Categories reserved for tests. Anything under them is disposable.
TEST_CATEGORY_PREFIX = "t_"


def purge_test_documents(migrated: Engine) -> Engine:
    """Remove rows left by tests that commit.

    Several tests must commit — publish consistency is about what *other* connections
    observe, and the outbox consumer commits by design. Their rows survive the usual
    per-test rollback, and accumulating them across local runs quietly degrades retrieval
    quality until the suite behaves differently on a laptop than in CI.

    Deletion goes through the same purge path production uses (INV-9): retention is cleared
    explicitly and the `kb.purge_authorized` flag is set, so this exercises the guard rather
    than bypassing it.
    """
    with Session(migrated) as session:
        session.execute(text("SET LOCAL kb.purge_authorized = 'on'"))
        scope = {"prefix": f"{TEST_CATEGORY_PREFIX}%"}
        doc_ids = "SELECT id FROM documents WHERE category_path::text LIKE :prefix"

        session.execute(
            text(
                "UPDATE document_versions SET retention_until = DATE '2000-01-01' "
                f"WHERE document_id IN ({doc_ids})"
            ),
            scope,
        )
        session.execute(text(f"DELETE FROM chunks WHERE document_id IN ({doc_ids})"), scope)
        session.execute(
            text(
                "DELETE FROM review_tasks WHERE version_id IN "
                f"(SELECT id FROM document_versions WHERE document_id IN ({doc_ids}))"
            ),
            scope,
        )
        session.execute(
            text(
                f"DELETE FROM graph_serving WHERE src_document_id IN ({doc_ids}) "
                f"OR dst_document_id IN ({doc_ids})"
            ),
            scope,
        )
        session.execute(
            text(
                f"DELETE FROM document_refs WHERE src_document_id IN ({doc_ids}) "
                f"OR dst_document_id IN ({doc_ids})"
            ),
            scope,
        )
        session.execute(
            text(
                "DELETE FROM outbox WHERE payload->>'document_id' IN "
                f"(SELECT id::text FROM ({doc_ids}) AS d)"
            ),
            scope,
        )
        session.execute(
            text(f"UPDATE documents SET canonical_version_id = NULL WHERE id IN ({doc_ids})"),
            scope,
        )
        session.execute(
            text(f"DELETE FROM document_versions WHERE document_id IN ({doc_ids})"), scope
        )
        session.execute(text(f"DELETE FROM documents WHERE id IN ({doc_ids})"), scope)
        session.commit()
    return migrated


@pytest.fixture(scope="session")
def clean_test_data(migrated: Engine) -> Engine:
    """Once per session, before anything runs: start from what the seed script produced."""
    return purge_test_documents(migrated)


@pytest.fixture
def pristine_corpus(seeded: Engine) -> Iterator[Engine]:
    """The seeded corpus and nothing else, at the moment this test runs.

    The quality gates measure retrieval and answers against a known corpus. Tests that commit
    their own fixtures earlier in the same session would otherwise sit in the index competing
    for the top of every result list, and the gate would fail — or worse, pass — for reasons
    that have nothing to do with the change under test.

    Purging by category prefix handles the tests' own leftovers and cannot handle the other
    source: **real documents uploaded through the portal**, which land in the same categories
    the fixtures live in. On a developer's machine that is normal and desirable — it is what
    the platform is for — and it silently invalidates every ranking threshold.

    So the rest are *hidden* rather than deleted. Tombstoning puts them outside the ACL
    predicate, which is inside the query, so retrieval genuinely cannot see them and the
    measurement stays honest — a post-filter over results would hide a real ranking failure
    instead of measuring it. Nothing is destroyed: the documents, versions and chunk rows are
    untouched, and exactly the rows this fixture hid are restored afterwards.
    """
    from scripts.seed import fixture_document_ids

    engine = purge_test_documents(seeded)
    with Session(engine) as session:
        hidden = [
            row[0]
            for row in session.execute(
                text(
                    "SELECT id FROM chunks WHERE NOT tombstoned "
                    "AND document_id <> ALL(CAST(:keep AS uuid[]))"
                ),
                {"keep": [str(item) for item in fixture_document_ids()]},
            )
        ]
        if hidden:
            session.execute(
                text("UPDATE chunks SET tombstoned = TRUE WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": [str(item) for item in hidden]},
            )
            session.commit()
    try:
        yield engine
    finally:
        if hidden:
            with Session(engine) as session:
                session.execute(
                    text(
                        "UPDATE chunks SET tombstoned = FALSE WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [str(item) for item in hidden]},
                )
                session.commit()


@pytest.fixture
def session(clean_test_data: Engine) -> Iterator[Session]:
    """Rolled back after each test, so ordinary tests leave nothing behind."""
    with Session(clean_test_data) as db_session:
        yield db_session
        db_session.rollback()


@pytest.fixture(scope="session")
def seeded(clean_test_data: Engine) -> Engine:
    """Ensure the fixture corpus (including the canaries) is loaded."""
    migrated = clean_test_data
    from scripts.seed import CANARIES
    from scripts.seed import main as seed_main

    with Session(migrated) as db_session:
        count = db_session.execute(text("SELECT count(*) FROM chunks")).scalar() or 0
    if count == 0:  # pragma: no cover - environment dependent
        seed_main()
    with Session(migrated) as db_session:
        canaries = db_session.execute(
            text("SELECT count(*) FROM documents WHERE category_path <@ 'internal.board'")
        ).scalar()
    assert canaries == len(CANARIES), "the ACL sweep is meaningless without the canaries"
    return migrated
