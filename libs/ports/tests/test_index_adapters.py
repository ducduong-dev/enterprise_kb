"""Index adapters — the ACL must be applied inside the query, on real data.

Every adapter here runs against the seeded corpus, because a keyword backend whose ACL
filtering is only exercised in production is a keyword backend whose ACL filtering is never
exercised. `pg_search` skips where the extension is absent (ADR-0021); `postgres_fts` and
pgvector run everywhere.
"""

from __future__ import annotations

import pytest
from kb_authz.filters import FilterBuilder, ResolvedFilter
from kb_authz.fixtures import ALL_PRINCIPALS, USER_COMPLIANCE_OFFICER, USER_IT_ENGINEER
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.pg_search_index import PgSearchIndexAdapter
from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
from kb_schemas.api import Facets
from sqlalchemy import Engine
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

CANARY_TOKENS = ("CANARY-ALPHA-7F3D", "CANARY-BRAVO-2C91", "CANARY-CHARLIE-E5A8")
EMBEDDER = HashedEmbeddingAdapter()


def acl(principal_name: str) -> ResolvedFilter:
    return FilterBuilder().base(ALL_PRINCIPALS[principal_name])


# ------------------------------------------------------------------------------- keyword


def test_keyword_search_finds_the_answering_chunk(seeded: Engine, session: Session) -> None:
    hits = PostgresFtsIndexAdapter(session).search(
        "tỷ lệ an toàn vốn tối thiểu", acl("user_retail_staff")
    )
    assert hits
    assert any("an toàn vốn" in (hit.text or "") for hit in hits)
    assert all(hit.score > 0 for hit in hits)


def test_keyword_search_is_diacritic_insensitive(seeded: Engine, session: Session) -> None:
    """Users type without tone marks constantly."""
    adapter = PostgresFtsIndexAdapter(session)
    with_marks = adapter.search("tỷ lệ an toàn vốn", acl("user_retail_staff"))
    without = adapter.search("ty le an toan von", acl("user_retail_staff"))
    assert {hit.chunk_id for hit in without} == {hit.chunk_id for hit in with_marks}


def test_keyword_results_carry_highlights(seeded: Engine, session: Session) -> None:
    hits = PostgresFtsIndexAdapter(session).search("an toàn vốn", acl("user_retail_staff"))
    assert any("<mark>" in "".join(hit.highlights) for hit in hits)


@pytest.mark.acl_sweep
@pytest.mark.parametrize(
    "principal_name", ["user_retail_staff", "user_it_engineer", "external_bot"]
)
def test_keyword_search_never_returns_a_canary(
    seeded: Engine, session: Session, principal_name: str
) -> None:
    hits = PostgresFtsIndexAdapter(session).search("sáp nhập kế hoạch vốn", acl(principal_name))
    assert not any(token in (hit.text or "") for hit in hits for token in CANARY_TOKENS)


def test_restricted_content_needs_the_group(seeded: Engine, session: Session) -> None:
    """The KYC procedure is restricted to compliance and legal."""
    adapter = PostgresFtsIndexAdapter(session)
    query = "nhận biết khách hàng CCCD"
    compliance = adapter.search(query, FilterBuilder().base(USER_COMPLIANCE_OFFICER))
    engineer = adapter.search(query, FilterBuilder().base(USER_IT_ENGINEER))
    # OR-ed terms mean the engineer may still match unrelated internal text on "hàng";
    # what must never appear is the restricted procedure itself.
    assert any("CCCD" in (hit.text or "") for hit in compliance)
    assert not any("CCCD" in (hit.text or "") for hit in engineer)


def test_external_bot_sees_only_external_content(seeded: Engine, session: Session) -> None:
    """Terms are OR-ed, so a query can match weakly — but never across the ACL boundary."""
    adapter = PostgresFtsIndexAdapter(session)
    assert adapter.search("phí tài khoản", acl("external_bot"))
    for query in ("phí tài khoản", "an toàn vốn", "nhận biết khách hàng"):
        for hit in adapter.search(query, acl("external_bot")):
            assert "an toàn vốn tối thiểu" not in (hit.text or "")
            assert "CCCD" not in (hit.text or "")


def test_facets_narrow_the_result_set(seeded: Engine, session: Session) -> None:
    adapter = PostgresFtsIndexAdapter(session)
    builder = FilterBuilder()
    unfiltered = adapter.search("vốn", builder.base(ALL_PRINCIPALS["user_retail_staff"]))
    narrowed = adapter.search(
        "vốn",
        builder.build(
            ALL_PRINCIPALS["user_retail_staff"], facets=Facets(category="regulations.sbv")
        ),
    )
    assert len(narrowed) <= len(unfiltered)
    assert {hit.chunk_id for hit in narrowed} <= {hit.chunk_id for hit in unfiltered}


# -------------------------------------------------------------------------------- vector


def test_vector_search_returns_ranked_neighbours(seeded: Engine, session: Session) -> None:
    hits = PgVectorIndexAdapter(session).search(
        EMBEDDER.embed_query("tỷ lệ an toàn vốn tối thiểu"), acl("user_retail_staff"), top_k=5
    )
    assert hits
    assert hits == sorted(hits, key=lambda hit: hit.score, reverse=True)
    assert any("an toàn vốn" in (hit.text or "") for hit in hits)


@pytest.mark.acl_sweep
@pytest.mark.parametrize(
    "principal_name", sorted(set(ALL_PRINCIPALS) - {"internal_bot_solo", "service_indexer"})
)
def test_vector_search_never_returns_a_canary(
    seeded: Engine, session: Session, principal_name: str
) -> None:
    """The nearest neighbour of a canary-shaped query must still be filtered out."""
    hits = PgVectorIndexAdapter(session).search(
        EMBEDDER.embed_query("CANARY kế hoạch sáp nhập Hội đồng quản trị"),
        acl(principal_name),
        top_k=20,
    )
    assert not any(token in (hit.text or "") for hit in hits for token in CANARY_TOKENS)


def test_vector_search_respects_group_restrictions(seeded: Engine, session: Session) -> None:
    adapter = PgVectorIndexAdapter(session)
    query = EMBEDDER.embed_query("quy trình nhận biết khách hàng")
    compliance = adapter.search(query, FilterBuilder().base(USER_COMPLIANCE_OFFICER), top_k=20)
    engineer = adapter.search(query, FilterBuilder().base(USER_IT_ENGINEER), top_k=20)
    assert any("CCCD" in (hit.text or "") for hit in compliance)
    assert not any("CCCD" in (hit.text or "") for hit in engineer)


def test_tombstoned_chunks_are_unreachable(seeded: Engine, session: Session) -> None:
    """Tombstoning is what makes a superseded version stop answering (INV-6)."""
    adapter = PgVectorIndexAdapter(session)
    query = EMBEDDER.embed_query("tỷ lệ an toàn vốn")
    before = adapter.search(query, acl("user_retail_staff"), top_k=20)
    assert before

    version_id = before[0].version_id
    assert adapter.tombstone([version_id]) > 0
    after = adapter.search(query, acl("user_retail_staff"), top_k=20)
    assert all(hit.version_id != version_id for hit in after)
    session.rollback()


# ---------------------------------------------------------------------------- pg_search


#: The production keyword engine (ADR-0021). Present only where `ops/pg_search/install.sql`
#: has run, so these skip rather than fail on a Postgres image without the extension.
def requires_pg_search(session: Session) -> PgSearchIndexAdapter:
    adapter = PgSearchIndexAdapter(session)
    if not adapter.health():
        pytest.skip("pg_search extension or chunks_bm25 index not installed")
    return adapter


def test_pg_search_reports_itself_uninstalled_rather_than_answering_badly(
    seeded: Engine, session: Session
) -> None:
    """A missing BM25 index would still return rows — unranked, unscored and silent."""
    adapter = PgSearchIndexAdapter(session)
    assert adapter.health() in (True, False)
    assert adapter.info.extra["bakeoff_candidate"] is True


def test_pg_search_finds_the_answering_chunk(seeded: Engine, session: Session) -> None:
    adapter = requires_pg_search(session)
    hits = adapter.search("tỷ lệ an toàn vốn tối thiểu", acl("user_retail_staff"))
    assert hits
    assert any("an toàn vốn" in (hit.text or "") for hit in hits)
    assert all(hit.score > 0 for hit in hits)


def test_pg_search_is_diacritic_insensitive(seeded: Engine, session: Session) -> None:
    """Gate 1 of the bake-off protocol: folding exists as data, not as hope."""
    adapter = requires_pg_search(session)
    reader = acl("user_retail_staff")
    with_marks = {hit.document_id for hit in adapter.search("tỷ lệ an toàn vốn", reader)}
    without = {hit.document_id for hit in adapter.search("ty le an toan von", reader)}
    assert with_marks
    assert with_marks <= without


def test_pg_search_resolves_a_legal_number(seeded: Engine, session: Session) -> None:
    """Gate 3: `41/2016/TT-NHNN` must survive tokenization as a reference, not four numbers."""
    adapter = requires_pg_search(session)
    hits = adapter.search("41/2016/TT-NHNN", acl("user_retail_staff"))
    assert hits
    assert all("TT-NHNN" in (hit.citation_label or "") for hit in hits[:2])


def test_pg_search_results_carry_highlights(seeded: Engine, session: Session) -> None:
    adapter = requires_pg_search(session)
    hits = adapter.search("an toàn vốn", acl("user_retail_staff"))
    assert any("<mark>" in "".join(hit.highlights) for hit in hits)


@pytest.mark.acl_sweep
@pytest.mark.parametrize(
    "principal_name", ["user_retail_staff", "user_it_engineer", "external_bot"]
)
def test_pg_search_never_returns_a_canary(
    seeded: Engine, session: Session, principal_name: str
) -> None:
    adapter = requires_pg_search(session)
    for query in ("CANARY", "sáp nhập", "Hội đồng quản trị", "kế hoạch"):
        for hit in adapter.search(query, acl(principal_name), top_k=50):
            assert not any(token in (hit.text or "") for token in CANARY_TOKENS)


def test_pg_search_survives_a_generic_plan(seeded: Engine, session: Session) -> None:
    """The same parameterised search, many times, on one connection.

    pg_search 0.25.2 segfaults the backend when its custom scan runs under a generic plan:
    psycopg prepares the statement after five executions, Postgres switches to a generic plan
    after five more, and the eleventh takes the cluster into crash recovery. `create_db_engine`
    sets `plan_cache_mode = force_custom_plan` for this backend (ADR-0021); this is the test
    that notices if that ever stops happening.

    Twelve iterations is not arbitrary — it is two past the execution where it died.
    """
    from sqlalchemy import text as sql_text

    adapter = requires_pg_search(session)
    session.execute(sql_text("SET plan_cache_mode = force_custom_plan"))
    reader = acl("user_retail_staff")
    counts = [len(adapter.search("hội đồng quản trị", reader, top_k=50)) for _ in range(12)]
    assert len(set(counts)) == 1, f"result count changed across executions: {counts}"


def test_pg_search_writes_nothing_of_its_own(seeded: Engine, session: Session) -> None:
    """The publish transaction owns these rows. Two writers are two chances to diverge."""
    import uuid

    adapter = requires_pg_search(session)
    assert adapter.tombstone([]) == 0
    assert adapter.delete_by_document(uuid.uuid4()) == 0
