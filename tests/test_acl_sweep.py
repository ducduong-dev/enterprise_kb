"""ACL sweep — blocking CI gate (INV-2/3/4/10).

Every fixture principal is run against the seeded corpus with the compiled filter, exactly as
retrieval-api will issue it. The test asserts what a principal may see, and — more
importantly — proves the three canaries are unreachable for all of them.

At M0 the query is issued straight against `chunks`, since retrieval-api does not exist yet.
When it lands in M2 this file switches to calling `/v1/retrieve` and gains the chat and graph
expansion paths; the assertions do not change, because the guarantee does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kb_authz.compile import compile_sql
from kb_authz.filters import FilterBuilder
from kb_authz.fixtures import ALL_PRINCIPALS, ZERO_VISIBILITY_PRINCIPALS
from kb_common.errors import AuthzError
from kb_retrieval_api.engine import RetrievalEngine
from kb_schemas.enums import Visibility
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.acl_sweep]

CANARY_TOKENS = ("CANARY-ALPHA-7F3D", "CANARY-BRAVO-2C91", "CANARY-CHARLIE-E5A8")

RESOLVABLE = sorted(set(ALL_PRINCIPALS) - ZERO_VISIBILITY_PRINCIPALS)


def retrieve(session: Session, principal_name: str) -> list[dict[str, object]]:
    """Everything the principal can reach, with no query terms — the widest possible set."""
    acl = FilterBuilder().base(ALL_PRINCIPALS[principal_name])
    where, params = compile_sql(acl)
    rows = session.execute(
        text(
            "SELECT c.id, c.document_id, c.text, c.visibility, c.allowed_groups, "
            "c.department, c.doc_status FROM chunks c WHERE " + where
        ),
        params,
    ).mappings()
    return [dict(row) for row in rows]


@pytest.mark.parametrize("principal_name", RESOLVABLE)
def test_no_principal_can_reach_a_canary(
    seeded: Engine, session: Session, principal_name: str
) -> None:
    hits = retrieve(session, principal_name)
    leaked = [h for h in hits if any(token in str(h["text"]) for token in CANARY_TOKENS)]
    assert not leaked, f"{principal_name} retrieved canary content: {leaked}"


@pytest.mark.parametrize("principal_name", RESOLVABLE)
def test_restricted_results_always_match_a_held_group(
    seeded: Engine, session: Session, principal_name: str
) -> None:
    acl = FilterBuilder().base(ALL_PRINCIPALS[principal_name])
    for hit in retrieve(session, principal_name):
        if hit["visibility"] == Visibility.RESTRICTED.value:
            overlap = set(hit["allowed_groups"] or []) & acl.group_scope
            assert overlap, f"{principal_name} saw restricted chunk {hit['id']} with no group"


@pytest.mark.parametrize("principal_name", RESOLVABLE)
def test_only_published_content_is_served(
    seeded: Engine, session: Session, principal_name: str
) -> None:
    for hit in retrieve(session, principal_name):
        assert hit["doc_status"] == "published"  # INV-6


def test_external_bot_sees_only_external_published(seeded: Engine, session: Session) -> None:
    hits = retrieve(session, "external_bot")
    assert hits, "the external corpus should not be empty — check the seed"
    assert {h["visibility"] for h in hits} == {Visibility.EXTERNAL.value}  # INV-4


def test_internal_bot_matches_its_user_exactly(seeded: Engine, session: Session) -> None:
    bot = {h["id"] for h in retrieve(session, "internal_bot_obo_retail")}
    user = {h["id"] for h in retrieve(session, "user_retail_staff")}
    assert bot == user  # INV-3: the bot adds nothing and subtracts nothing


@pytest.mark.parametrize("principal_name", sorted(ZERO_VISIBILITY_PRINCIPALS))
def test_zero_visibility_principals_cannot_query_at_all(
    seeded: Engine, session: Session, principal_name: str
) -> None:
    with pytest.raises(AuthzError):
        retrieve(session, principal_name)


def test_a_principal_outside_the_group_cannot_see_restricted_procedures(
    seeded: Engine, session: Session
) -> None:
    """The KYC procedure is restricted to compliance and legal."""
    it_docs = {str(h["document_id"]) for h in retrieve(session, "user_it_engineer")}
    compliance_docs = {str(h["document_id"]) for h in retrieve(session, "user_compliance_officer")}
    only_compliance = compliance_docs - it_docs
    assert only_compliance, "the fixture corpus must contain content the IT engineer cannot see"


def test_graph_expansion_uses_the_same_predicate(seeded: Engine, session: Session) -> None:
    """Edges carry the target's ACL, so expansion filters identically (INV-10)."""
    acl = FilterBuilder().base(ALL_PRINCIPALS["user_it_engineer"])
    rows = session.execute(
        text(
            """
            SELECT dst_document_id, dst_visibility, dst_allowed_groups
            FROM graph_serving
            WHERE dst_visibility <> 'restricted'
               OR dst_allowed_groups && CAST(:groups AS TEXT[])
            """
        ),
        {"groups": sorted(acl.group_scope)},
    ).mappings()
    for row in rows:
        if row["dst_visibility"] == Visibility.RESTRICTED.value:
            assert set(row["dst_allowed_groups"]) & acl.group_scope


# ---------------------------------------------------------------------------------------
# The same sweep through the retrieval funnel (M2). Above, the filter is applied to the
# chunks table directly; here it goes through retrieval-api exactly as the search UI, the
# internal bot and the external bot do — including fusion, reranking and graph expansion,
# each of which is a place a filter could be lost.
# ---------------------------------------------------------------------------------------


@pytest.fixture
def funnel(seeded: Engine, session: Session) -> RetrievalEngine:
    from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
    from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
    from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
    from kb_ports.adapters.rerank import LexicalRerankAdapter

    return RetrievalEngine(
        session,
        keyword_index=PostgresFtsIndexAdapter(session),
        vector_index=PgVectorIndexAdapter(session),
        embedder=HashedEmbeddingAdapter(),
        reranker=LexicalRerankAdapter(),
    )


def golden_queries() -> list[str]:
    """The graded query set, plus direct probes for the canaries."""
    import yaml

    path = Path(__file__).resolve().parents[1] / "eval" / "golden_set" / "queries.yaml"
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [entry["query"] for entry in entries]


@pytest.mark.parametrize("principal_name", RESOLVABLE)
def test_the_funnel_never_returns_a_canary_to_anyone(funnel, principal_name: str) -> None:
    """14 principals across the whole golden query set, zero violations (M2 criterion)."""
    from kb_schemas.api import RetrieveRequest

    principal = ALL_PRINCIPALS[principal_name]
    for query in golden_queries():
        response = funnel.retrieve(
            principal, RetrieveRequest(query=query, top_k=20, expand_graph=True)
        ).response
        for chunk in response.chunks:
            assert not any(token in chunk.text for token in CANARY_TOKENS), (
                f"{principal_name} retrieved canary content for {query!r}"
            )
        for expansion in response.expansions:
            assert "Canary" not in (expansion.summary or "")


@pytest.mark.parametrize("principal_name", RESOLVABLE)
def test_the_funnel_agrees_with_the_raw_predicate(
    funnel: RetrievalEngine, session: Session, principal_name: str
) -> None:
    """Everything the funnel returns must also be reachable by the filter applied directly.

    If these ever disagree, some stage between the index and the response is adding rows —
    which is the shape an INV-1/INV-2 break would take.
    """
    from kb_schemas.api import RetrieveRequest

    reachable = {str(hit["id"]) for hit in retrieve(session, principal_name)}
    for query in golden_queries()[:6]:
        response = funnel.retrieve(
            ALL_PRINCIPALS[principal_name], RetrieveRequest(query=query, top_k=20)
        ).response
        assert {str(chunk.chunk_id) for chunk in response.chunks} <= reachable
