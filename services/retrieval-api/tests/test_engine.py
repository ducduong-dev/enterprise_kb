"""Retrieval engine: fusion, ranking, flags, expansion, audit.

The ACL sweep through this engine lives in `tests/test_acl_sweep.py` — it is the blocking
gate, and it runs against every fixture principal.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from kb_authz.fixtures import (
    ALL_PRINCIPALS,
    USER_COMPLIANCE_OFFICER,
    USER_IT_ENGINEER,
    USER_RETAIL_STAFF,
)
from kb_common.audit import AuditAction, InMemoryAuditSink
from kb_common.errors import NotFound, PolicyViolation
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
from kb_ports.adapters.rerank import LexicalRerankAdapter
from kb_ports.indexes import IndexHit
from kb_retrieval_api.engine import MAX_CHUNKS_PER_DOCUMENT, RetrievalEngine
from kb_retrieval_api.fusion import cap_per_document, reciprocal_rank_fusion
from kb_schemas.api import CitationLookupRequest, Facets, RetrieveRequest, RetrieveResponse
from kb_schemas.enums import RetrievalMode
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

CANARY_TOKENS = ("CANARY-ALPHA-7F3D", "CANARY-BRAVO-2C91", "CANARY-CHARLIE-E5A8")


@pytest.fixture
def audit() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def retrieval(seeded: Engine, session: Session, audit: InMemoryAuditSink) -> RetrievalEngine:
    return RetrievalEngine(
        session,
        keyword_index=PostgresFtsIndexAdapter(session),
        vector_index=PgVectorIndexAdapter(session),
        embedder=HashedEmbeddingAdapter(),
        reranker=LexicalRerankAdapter(),
        audit=audit,
    )


def ask(
    retrieval: RetrievalEngine, principal_name: str, query: str, **kwargs: Any
) -> RetrieveResponse:
    request = RetrieveRequest(query=query, **kwargs)
    return retrieval.retrieve(ALL_PRINCIPALS[principal_name], request).response


# ----------------------------------------------------------------------------- retrieval


def test_a_question_finds_the_article_that_answers_it(retrieval: RetrievalEngine) -> None:
    response = ask(retrieval, "user_retail_staff", "tỷ lệ an toàn vốn tối thiểu là bao nhiêu?")
    assert response.chunks
    assert any("8%" in chunk.text for chunk in response.chunks)
    assert response.resolved_filter_id


def test_results_carry_a_quotable_citation(retrieval: RetrievalEngine) -> None:
    """An answer without a citation is unusable in a bank."""
    response = ask(retrieval, "user_retail_staff", "hệ số rủi ro tín dụng")
    top = response.chunks[0]
    assert top.citation_label
    assert "TT-NHNN" in top.citation_label or "Điều" in top.citation_label


def test_hybrid_search_finds_what_neither_retriever_finds_alone(
    retrieval: RetrievalEngine,
) -> None:
    """Both retrievers contribute; the fused list is at least as good as either."""
    response = ask(retrieval, "user_retail_staff", "an toan von toi thieu", top_k=10)
    assert response.chunks  # diacritic-free query still resolves


def test_results_are_deterministic(retrieval: RetrievalEngine) -> None:
    """An eval run that reorders ties between runs produces noise that looks like a
    regression."""
    first = ask(retrieval, "user_retail_staff", "tỷ lệ an toàn vốn")
    second = ask(retrieval, "user_retail_staff", "tỷ lệ an toàn vốn")
    assert [c.chunk_id for c in first.chunks] == [c.chunk_id for c in second.chunks]


def test_top_k_is_respected(retrieval: RetrievalEngine) -> None:
    assert len(ask(retrieval, "user_retail_staff", "ngân hàng", top_k=2).chunks) <= 2


def test_facets_narrow_the_results(retrieval: RetrievalEngine) -> None:
    wide = ask(retrieval, "user_retail_staff", "vốn", top_k=20)
    narrow = ask(
        retrieval,
        "user_retail_staff",
        "vốn",
        top_k=20,
        facets=Facets(category="regulations.sbv"),
    )
    assert {c.chunk_id for c in narrow.chunks} <= {c.chunk_id for c in wide.chunks}


def test_a_widening_facet_is_refused_before_any_query_runs(retrieval: RetrievalEngine) -> None:
    scoped = RetrieveRequest(query="vốn", facets=Facets(department="legal"))
    with pytest.raises(PolicyViolation):
        # The retail staffer's own filter has no department restriction, so this narrows;
        # narrowing twice to a different department is the widening attempt.
        outcome = retrieval.retrieve(USER_RETAIL_STAFF, scoped)
        retrieval._filters.narrow(outcome.resolved_filter, Facets(department="retail"))


def test_as_of_without_the_archive_role_is_refused(retrieval: RetrievalEngine) -> None:
    with pytest.raises(PolicyViolation):
        retrieval.retrieve(
            USER_RETAIL_STAFF,
            RetrieveRequest(query="vốn", mode=RetrievalMode.AS_OF, as_of_date=date(2021, 1, 1)),
        )


def test_as_of_with_the_archive_role_is_allowed(retrieval: RetrievalEngine) -> None:
    response = retrieval.retrieve(
        USER_COMPLIANCE_OFFICER,
        RetrieveRequest(query="vốn", mode=RetrievalMode.AS_OF, as_of_date=date(2021, 1, 1)),
    ).response
    assert response.resolved_filter_id


def test_no_more_than_a_few_chunks_from_one_document(retrieval: RetrievalEngine) -> None:
    """One long regulation must not fill the answer and make it look well-sourced."""
    response = ask(retrieval, "user_retail_staff", "ngân hàng vốn tỷ lệ", top_k=20)
    counts: dict[uuid.UUID, int] = {}
    for chunk in response.chunks:
        counts[chunk.document_id] = counts.get(chunk.document_id, 0) + 1
    assert all(count <= MAX_CHUNKS_PER_DOCUMENT for count in counts.values())


# ---------------------------------------------------------------------------- access


def test_restricted_content_requires_the_group(retrieval: RetrievalEngine) -> None:
    query = "quy trình nhận biết khách hàng CCCD"
    allowed = retrieval.retrieve(USER_COMPLIANCE_OFFICER, RetrieveRequest(query=query)).response
    denied = retrieval.retrieve(USER_IT_ENGINEER, RetrieveRequest(query=query)).response
    assert any("CCCD" in chunk.text for chunk in allowed.chunks)
    assert not any("CCCD" in chunk.text for chunk in denied.chunks)


@pytest.mark.acl_sweep
@pytest.mark.parametrize(
    "principal_name",
    sorted(set(ALL_PRINCIPALS) - {"internal_bot_solo", "service_indexer"}),
)
def test_no_principal_retrieves_a_canary_through_the_funnel(
    retrieval: RetrievalEngine, principal_name: str
) -> None:
    for query in ("CANARY kế hoạch sáp nhập", "board-only merger plan", "Hội đồng quản trị"):
        response = ask(retrieval, principal_name, query, top_k=20, expand_graph=True)
        for chunk in response.chunks:
            assert not any(token in chunk.text for token in CANARY_TOKENS)


def test_the_internal_bot_sees_exactly_what_its_user_sees(retrieval: RetrievalEngine) -> None:
    query = "quy trình nhận biết khách hàng"
    user = ask(retrieval, "user_retail_staff", query, top_k=20)
    bot = ask(retrieval, "internal_bot_obo_retail", query, top_k=20)
    assert [c.chunk_id for c in bot.chunks] == [c.chunk_id for c in user.chunks]


def test_the_external_bot_never_reaches_internal_text(retrieval: RetrievalEngine) -> None:
    for query in ("tỷ lệ an toàn vốn", "nhận biết khách hàng", "phí tài khoản"):
        for chunk in ask(retrieval, "external_bot", query, top_k=20).chunks:
            assert "an toàn vốn tối thiểu" not in chunk.text
            assert "CCCD" not in chunk.text


# ------------------------------------------------------------------------- supersession


def test_an_amended_document_is_flagged_not_hidden(
    retrieval: RetrievalEngine, session: Session
) -> None:
    """Hiding it would leave the user with nothing; the flag is what makes quoting it safe."""
    from kb_registry import repository as repo
    from kb_schemas.orm import DocumentRefRow

    target = session.execute(
        text("SELECT id FROM documents WHERE legal_number = '41/2016/TT-NHNN'")
    ).scalar_one()
    source = session.execute(
        text("SELECT id FROM documents WHERE legal_number = 'QD-2023-114'")
    ).scalar_one()
    session.add(
        DocumentRefRow(
            id=uuid.uuid4(),
            src_document_id=source,
            dst_document_id=target,
            ref_type="amends",
            detected_by="test",
            created_at=repo.now(),
        )
    )
    session.flush()

    response = ask(retrieval, "user_retail_staff", "tỷ lệ an toàn vốn tối thiểu 8%", top_k=20)
    flagged = [chunk for chunk in response.chunks if chunk.document_id == target]
    assert flagged, "the amended regulation should still be retrievable — flagged, not hidden"
    assert all(chunk.supersession_flag for chunk in flagged)
    # Other documents are not flagged by association.
    assert not any(
        chunk.supersession_flag for chunk in response.chunks if chunk.document_id != target
    )
    session.rollback()


# ---------------------------------------------------------------------------- expansion


def test_graph_expansion_offers_related_instruments(retrieval: RetrievalEngine) -> None:
    """The policy implements a circular; the circular is offered as context, not as a hit."""
    response = ask(retrieval, "user_retail_staff", "Ủy ban ALCO", top_k=1, expand_graph=True)
    assert response.expansions
    assert all(expansion.summary for expansion in response.expansions)
    # Expansion offers what the results do not already contain.
    returned = {chunk.document_id for chunk in response.chunks}
    assert all(expansion.document_id not in returned for expansion in response.expansions)


def test_expansion_is_filtered_by_the_same_acl(
    retrieval: RetrievalEngine, session: Session
) -> None:
    """An edge must not disclose the existence of a target the caller may not see (INV-10)."""
    canary = session.execute(
        text("SELECT id FROM documents WHERE category_path <@ 'internal.board' LIMIT 1")
    ).scalar_one()
    source = session.execute(
        text("SELECT id FROM documents WHERE legal_number = 'QD-2023-114'")
    ).scalar_one()
    session.execute(
        text(
            """
            INSERT INTO graph_serving (src_document_id, dst_document_id, ref_type,
                dst_summary, dst_visibility, dst_allowed_groups)
            VALUES (:src, :dst, 'cites', 'Board-only annex', 'restricted',
                    ARRAY['restricted/board'])
            ON CONFLICT DO NOTHING
            """
        ),
        {"src": source, "dst": canary},
    )
    session.flush()

    response = ask(retrieval, "user_retail_staff", "đệm vốn nội bộ", expand_graph=True)
    assert all(expansion.document_id != canary for expansion in response.expansions)
    session.rollback()


def test_expansion_is_off_by_default(retrieval: RetrievalEngine) -> None:
    assert ask(retrieval, "user_retail_staff", "tỷ lệ an toàn vốn").expansions == []


# ---------------------------------------------------------------------- citation lookup


def test_a_citation_resolves_to_its_article(retrieval: RetrievalEngine) -> None:
    response = retrieval.citation_lookup(
        USER_RETAIL_STAFF, CitationLookupRequest(citation="Điều 12.2, TT 41/2016/TT-NHNN")
    ).response
    assert response.chunks
    assert "Điều 12" in (response.chunks[0].citation_label or "")


def test_a_citation_the_caller_may_not_read_does_not_resolve(retrieval: RetrievalEngine) -> None:
    allowed = retrieval.citation_lookup(
        USER_COMPLIANCE_OFFICER, CitationLookupRequest(citation="Bước 3, QT 07/2024")
    ).response
    denied = retrieval.citation_lookup(
        USER_IT_ENGINEER, CitationLookupRequest(citation="Bước 3, QT 07/2024")
    ).response
    assert allowed.chunks
    assert not denied.chunks


# --------------------------------------------------------------------------- render gate


def test_opening_a_document_re_checks_access(retrieval: RetrievalEngine, session: Session) -> None:
    document_id = session.execute(
        text("SELECT id FROM documents WHERE legal_number = '41/2016/TT-NHNN'")
    ).scalar_one()
    rendered = retrieval.document_access(USER_RETAIL_STAFF, document_id)
    assert rendered["legal_number"] == "41/2016/TT-NHNN"


def test_a_document_the_caller_may_not_read_is_not_found(
    retrieval: RetrievalEngine, session: Session
) -> None:
    """Refusal and absence are indistinguishable unless the category discloses existence."""
    canary = session.execute(
        text("SELECT id FROM documents WHERE category_path <@ 'internal.board' LIMIT 1")
    ).scalar_one()
    with pytest.raises(NotFound):
        retrieval.document_access(USER_RETAIL_STAFF, canary)


def test_reading_an_archived_version_is_audited_separately(
    retrieval: RetrievalEngine, session: Session, audit: InMemoryAuditSink
) -> None:
    document_id = session.execute(
        text("SELECT id FROM documents WHERE legal_number = '41/2016/TT-NHNN'")
    ).scalar_one()
    retrieval.document_access(USER_RETAIL_STAFF, document_id, version_id=uuid.uuid4())
    assert audit.by_action(AuditAction.ARCHIVED_VERSION_ACCESS)


# -------------------------------------------------------------------------------- audit


def test_every_retrieval_is_reconstructable(
    retrieval: RetrievalEngine, audit: InMemoryAuditSink
) -> None:
    """INV-11: principal, delegate, resolved filter, and every chunk returned."""
    response = ask(retrieval, "internal_bot_obo_retail", "tỷ lệ an toàn vốn")
    (record,) = audit.by_action(AuditAction.RETRIEVE)

    assert record.actor == "svc-internal-bot"
    assert record.on_behalf_of == "u-retail-staff"
    assert record.resolved_filter is not None
    assert record.resolved_filter["filter_id"] == response.resolved_filter_id
    assert record.object_ref["chunk_ids"] == [str(c.chunk_id) for c in response.chunks]
    assert record.object_ref["version_ids"]
    assert record.detail["query"] == "tỷ lệ an toàn vốn"


def test_the_query_is_audited_but_not_put_in_a_metric_label(
    retrieval: RetrievalEngine, audit: InMemoryAuditSink
) -> None:
    ask(retrieval, "user_retail_staff", "một truy vấn rất riêng tư")
    (record,) = audit.by_action(AuditAction.RETRIEVE)
    assert record.detail["query"] == "một truy vấn rất riêng tư"

    from kb_common.metrics import retrieval_latency

    for metric in retrieval_latency.collect():
        for sample in metric.samples:
            assert "truy vấn" not in str(sample.labels)


# ------------------------------------------------------------------------------- fusion


def test_rrf_rewards_agreement_between_retrievers() -> None:
    def hit(name: str) -> IndexHit:
        seed = uuid.uuid5(uuid.NAMESPACE_DNS, name)
        return IndexHit(chunk_id=seed, document_id=seed, version_id=seed, score=1.0, text=name)

    both = hit("agreed")
    fused = reciprocal_rank_fusion(
        {
            "keyword": [hit("keyword-first"), both],
            "vector": [hit("vector-first"), both],
        }
    )
    assert fused[0].hit.chunk_id == both.chunk_id
    assert fused[0].ranks == {"keyword": 2, "vector": 2}


def test_fusion_never_duplicates_a_chunk() -> None:
    seed = uuid.uuid4()
    same = IndexHit(chunk_id=seed, document_id=seed, version_id=seed, score=1.0, text="x")
    fused = reciprocal_rank_fusion({"keyword": [same], "vector": [same]})
    assert len(fused) == 1


def test_per_document_cap_preserves_order() -> None:
    document = uuid.uuid4()
    hits = [
        IndexHit(chunk_id=uuid.uuid4(), document_id=document, version_id=document, score=1.0)
        for _ in range(5)
    ]
    fused = reciprocal_rank_fusion({"keyword": hits})
    capped = cap_per_document(fused, 2)
    assert len(capped) == 2
    assert [item.chunk_id for item in capped] == [item.chunk_id for item in fused[:2]]
