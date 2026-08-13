"""The DMZ's containment, checked rather than assumed (M8, INV-4).

The external bot's scope is bound server-side by `FilterBuilder`, compiled into every query,
and tested by the ACL sweep. This file tests what is left when that fails: the database role
the public surface connects as.

Everything here runs against the real role created by `ops/dmz/external-role.sql`, because a
row-level-security policy that has never been exercised is a comment. Skipped where the SQL
cannot be applied (no `psql`, no database container), and the skip says so.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from kb_authz.fixtures import ALL_PRINCIPALS
from kb_common.config import get_settings
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
from kb_ports.adapters.rerank import LexicalRerankAdapter
from kb_retrieval_api.engine import RetrievalEngine
from kb_schemas.api import RetrieveRequest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
ROLE_SQL = ROOT / "ops" / "dmz" / "external-role.sql"
DMZ_PASSWORD = "dmz-test-password"
CANARY_TOKENS = ("CANARY-ALPHA-7F3D", "CANARY-BRAVO-2C91", "CANARY-CHARLIE-E5A8")


def apply_role_sql() -> bool:
    """Create the role exactly as a deployment would. False when that is not possible here."""
    container = os.environ.get("KB_PG_DOCKER_CONTAINER")
    settings = get_settings().db
    environment = {**os.environ, "KB_DMZ_DB_PASSWORD": DMZ_PASSWORD}
    if container:
        command = [
            "docker",
            "exec",
            "-i",
            "-e",
            "KB_DMZ_DB_PASSWORD",
            container,
            "psql",
            "-U",
            settings.user,
            "-d",
            settings.name,
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
        ]
    elif shutil.which("psql"):
        environment["PGPASSWORD"] = settings.password.get_secret_value()
        command = [
            "psql",
            "-h",
            settings.host,
            "-p",
            str(settings.port),
            "-U",
            settings.user,
            "-d",
            settings.name,
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
        ]
    else:
        return False

    with ROLE_SQL.open("rb") as script:
        result = subprocess.run(
            command, stdin=script, capture_output=True, text=True, env=environment, check=False
        )
    if result.returncode != 0:  # pragma: no cover - environment dependent
        pytest.skip(f"could not apply the DMZ role: {result.stderr.strip()[:200]}")
    return True


@pytest.fixture(scope="module")
def dmz_engine(request: pytest.FixtureRequest) -> Engine:
    """A connection as `kb_external`, the role the public surface uses."""
    request.getfixturevalue("seeded")
    if not apply_role_sql():
        pytest.skip("no psql and no KB_PG_DOCKER_CONTAINER — cannot create the DMZ role")

    settings = get_settings().db
    url = (
        f"postgresql+psycopg://kb_external:{DMZ_PASSWORD}"
        f"@{settings.host}:{settings.port}/{settings.name}"
    )
    engine = create_engine(url, pool_pre_ping=True)
    yield engine
    engine.dispose()


def scalar(engine: Engine, sql: str) -> int:
    with Session(engine) as session:
        return int(session.execute(text(sql)).scalar() or 0)


# ------------------------------------------------------------------------ what it can see


def test_the_dmz_role_sees_only_published_external_chunks(dmz_engine: Engine) -> None:
    assert scalar(dmz_engine, "SELECT count(*) FROM chunks") > 0, "the public corpus is empty"
    assert scalar(dmz_engine, "SELECT count(*) FROM chunks WHERE visibility <> 'external'") == 0
    assert scalar(dmz_engine, "SELECT count(*) FROM chunks WHERE doc_status <> 'published'") == 0
    assert scalar(dmz_engine, "SELECT count(*) FROM chunks WHERE tombstoned") == 0


def test_the_dmz_role_cannot_see_a_canary(dmz_engine: Engine) -> None:
    """The canaries are restricted to a group nobody holds. Row-level security does not care
    about groups at all — it never shows this role a non-external row, whoever asks."""
    condition = " OR ".join(f"text LIKE '%{token}%'" for token in CANARY_TOKENS)
    assert scalar(dmz_engine, f"SELECT count(*) FROM chunks WHERE {condition}") == 0


def test_the_dmz_role_sees_only_external_documents(dmz_engine: Engine) -> None:
    assert scalar(dmz_engine, "SELECT count(*) FROM documents WHERE visibility <> 'external'") == 0


# ----------------------------------------------------------------------- what it cannot do


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE chunks SET text = text",
        "DELETE FROM chunks",
        "INSERT INTO documents (id, title, doc_class, category_path, visibility, allowed_groups,"
        " status, created_at, updated_at) VALUES (gen_random_uuid(), 'x', 'operational',"
        " 'internal', 'external', '{}', 'published', now(), now())",
        "UPDATE documents SET visibility = 'external'",
    ],
)
def test_the_dmz_role_cannot_write(dmz_engine: Engine, statement: str) -> None:
    from sqlalchemy.exc import ProgrammingError

    with Session(dmz_engine) as session, pytest.raises(ProgrammingError) as exc:
        session.execute(text(statement))
    assert "permission denied" in str(exc.value)


def test_the_dmz_role_cannot_read_the_audit_trail(dmz_engine: Engine) -> None:
    """It writes its answers there (INV-11) and can never read anyone's — not even a count."""
    from sqlalchemy.exc import ProgrammingError

    with Session(dmz_engine) as session, pytest.raises(ProgrammingError):
        session.execute(text("SELECT count(*) FROM audit_log"))


def test_the_dmz_role_can_still_record_its_answers(dmz_engine: Engine) -> None:
    from kb_common.audit import AuditAction, AuditRecord, SqlAuditSink

    with Session(dmz_engine) as session:
        SqlAuditSink(session).write(
            AuditRecord(
                action=AuditAction.CHAT_ANSWER,
                actor="service-account-kb-external-bot",
                object_ref={"answer_id": "dmz-test"},
            )
        )
        session.commit()


def test_the_dmz_role_cannot_read_the_registry_tables(dmz_engine: Engine) -> None:
    from sqlalchemy.exc import ProgrammingError

    for table in ("document_versions", "review_tasks", "outbox", "graph_serving"):
        with Session(dmz_engine) as session, pytest.raises(ProgrammingError):
            session.execute(text(f"SELECT count(*) FROM {table}"))


# ------------------------------------------------------------------- retrieval through it


def test_retrieval_through_the_dmz_role_returns_only_public_text(dmz_engine: Engine) -> None:
    """The funnel, unchanged, on a connection that cannot reach anything else."""
    with Session(dmz_engine) as session:
        funnel = RetrievalEngine(
            session,
            keyword_index=PostgresFtsIndexAdapter(session),
            vector_index=PgVectorIndexAdapter(session),
            embedder=HashedEmbeddingAdapter(),
            reranker=LexicalRerankAdapter(),
        )
        for query in ("phí duy trì tài khoản", "tỷ lệ an toàn vốn", "CANARY", "kế hoạch sáp nhập"):
            response = funnel.retrieve(
                ALL_PRINCIPALS["external_bot"], RetrieveRequest(query=query, top_k=20)
            ).response
            for chunk in response.chunks:
                assert not any(token in chunk.text for token in CANARY_TOKENS)
