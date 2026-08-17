"""Fact sets: the other documents that state a seed's rule (M10, ADR-0037).

Ranking answers "which passage best matches this question" and INV-13 asks a different one. The
tests here are about the second question, and they fall into three groups:

* each channel reaches what only it can reach, and none of them reaches past the filter;
* a member found twice is one member, recorded as the *stronger* evidence;
* the set is bounded, and says so when it truncated.

The filter tests are the ones that matter most. A coverage pass is exactly where somebody
writes "we already checked the seed, just fetch its neighbours" — so every channel is exercised
against a principal who may not read the member it would otherwise return.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from kb_authz.filters import FilterBuilder, ResolvedFilter
from kb_authz.fixtures import USER_COMPLIANCE_OFFICER, USER_IT_ENGINEER, USER_RETAIL_STAFF
from kb_authz.principal import Principal
from kb_common.audit import AuditAction, InMemoryAuditSink
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
from kb_ports.adapters.rerank import LexicalRerankAdapter
from kb_registry.testing import make_chunk, make_document, make_version
from kb_retrieval_api.coverage import (
    MAX_MEMBERS,
    Seed,
    cover,
    load_seed,
    withheld_count,
)
from kb_retrieval_api.engine import RetrievalEngine
from kb_schemas.api import RetrieveRequest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

RATE = "Lãi suất cho vay ngắn hạn bằng đồng Việt Nam là 8%/năm."
SUBJECT = "lai suat cho vay ngan han"
FROM = date(2023, 1, 1)
#: The shared `make_chunk` writes a zero vector, and pgvector's `<=>` returns NaN for one — so
#: the vector channel is silently inert in any fixture that does not supply a real embedding.
EMBEDDER = HashedEmbeddingAdapter()
#: A group the compliance officer actually holds. `grp-board` is nobody's, which is what makes
#: the canary sweep work and would make a "may read" assertion vacuous here.
COMPLIANCE_GROUP = "dept/compliance"


def acl(principal: Principal = USER_RETAIL_STAFF) -> ResolvedFilter:
    return FilterBuilder().build(principal)


def seed_of(session: Session, chunk_id: uuid.UUID) -> Seed:
    """`load_seed` returns None for a chunk that is not there; in these tests it always is,
    and asserting that here keeps every call site free of a null check that cannot fire."""
    seed = load_seed(session, chunk_id)
    assert seed is not None
    return seed


def _engine(session: Session, audit: InMemoryAuditSink | None = None) -> RetrievalEngine:
    return RetrievalEngine(
        session,
        keyword_index=PostgresFtsIndexAdapter(session),
        vector_index=PgVectorIndexAdapter(session),
        embedder=EMBEDDER,
        reranker=LexicalRerankAdapter(),
        audit=audit,
    )


@pytest.fixture
def retrieval_engine(pristine_corpus: Engine, session: Session) -> RetrievalEngine:
    return _engine(session)


@pytest.fixture
def retrieval_engine_with_audit(
    pristine_corpus: Engine, session: Session
) -> tuple[RetrievalEngine, InMemoryAuditSink]:
    audit = InMemoryAuditSink()
    return _engine(session, audit), audit


def clause(
    session: Session,
    *,
    title: str,
    body: str = RATE,
    subject_key: str | None = SUBJECT,
    section_path: str = "Điều 5",
    anchor: str | None = None,
    visibility: str = "internal_all",
    groups: list[str] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """One published document holding one clause. Returns `(document_id, chunk_id)`."""
    document_id = make_document(session, title=title)
    version_id = make_version(session, document_id, effective_from=FROM)
    chunk_id = make_chunk(
        session,
        document_id,
        version_id,
        effective_from=FROM,
        section_path=section_path,
        body=body,
        subject_key=subject_key,
    )
    session.execute(
        text("UPDATE chunks SET embedding = CAST(:e AS vector) WHERE id = :id"),
        {"e": str(list(EMBEDDER.embed_documents([body])[0])), "id": chunk_id},
    )
    if anchor is not None:
        session.execute(
            text("UPDATE chunks SET anchor = :a WHERE id = :id"), {"a": anchor, "id": chunk_id}
        )
    if visibility != "internal_all":
        session.execute(
            text(
                "UPDATE chunks SET visibility = :v, allowed_groups = CAST(:g AS text[]) "
                "WHERE id = :id"
            ),
            {"v": visibility, "g": groups or [], "id": chunk_id},
        )
    session.flush()
    return document_id, chunk_id


def edge(
    session: Session, src: uuid.UUID, dst: uuid.UUID, *, anchors: list[str], confirmed: bool = True
) -> None:
    session.execute(
        text(
            "INSERT INTO document_refs (id, src_document_id, dst_document_id, ref_type, "
            "anchors, detected_by, confirmed_by, created_at) "
            "VALUES (:id, :src, :dst, 'cites', CAST(:anchors AS text[]), 'test', :by, now())"
        ),
        {
            "id": uuid.uuid4(),
            "src": src,
            "dst": dst,
            "anchors": anchors,
            "by": "steward@bank" if confirmed else None,
        },
    )
    session.flush()


# --------------------------------------------------------------------------------- channels


def test_the_reference_channel_reaches_the_clause_the_corpus_named(
    pristine_corpus: Engine, session: Session
) -> None:
    """The strongest signal there is: an implementing procedure that names *khoản 2 Điều 12* is
    about that rule by the bank's own statement, not by our inference."""
    target_doc, _ = clause(session, title="Thông tư gốc", subject_key=None, anchor="12.2")
    seed_doc, seed_chunk = clause(session, title="Quy trình", subject_key=None)
    edge(session, seed_doc, target_doc, anchors=["12.2"])

    result = cover(session, acl(), seed_of(session, seed_chunk))

    assert [m.document_id for m in result.members] == [target_doc]
    assert result.members[0].channel == "reference"


def test_an_unconfirmed_edge_is_not_the_corpus_speaking(
    pristine_corpus: Engine, session: Session
) -> None:
    """An unconfirmed reference is a detection nobody has checked. Putting its target into an
    answer labelled `reference` would claim the bank's authority for our own parser.

    The assertion is about the *label*, not about absence: another channel may legitimately
    reach the same document on its own evidence, and it should — what it must not do is arrive
    wearing the one label that means the corpus said so.
    """
    target_doc, _ = clause(session, title="Thông tư gốc", subject_key=None, anchor="12.2")
    seed_doc, seed_chunk = clause(session, title="Quy trình", subject_key=None)
    edge(session, seed_doc, target_doc, anchors=["12.2"], confirmed=False)

    members = cover(session, acl(), seed_of(session, seed_chunk)).members

    assert all(m.channel != "reference" for m in members)


def test_the_subject_channel_reaches_a_document_that_cites_nothing(
    pristine_corpus: Engine, session: Session
) -> None:
    """What reference cannot do: two instruments about the same subject with no edge between
    them — which is the ordinary case across departments and decades."""
    other_doc, _ = clause(session, title="Biểu phí 2026", body="Lãi suất là 10%/năm.")
    _, seed_chunk = clause(session, title="Biểu phí 2023")

    result = cover(session, acl(), seed_of(session, seed_chunk))

    assert [m.document_id for m in result.members] == [other_doc]
    assert result.members[0].channel == "subject"


def test_a_seed_with_no_subject_key_falls_through_to_the_vector_channel(
    pristine_corpus: Engine, session: Session
) -> None:
    """The channel that reaches a rule phrased in words nothing else shares. Seeded by the
    passage's own vector, never by the question's."""
    _, seed_chunk = clause(session, title="Biểu phí 2023", subject_key=None)
    other_doc, _ = clause(session, title="Biểu phí 2026", subject_key=None)

    result = cover(session, acl(), seed_of(session, seed_chunk))

    assert [m.document_id for m in result.members] == [other_doc]
    assert result.members[0].channel == "vector"
    assert result.members[0].distance is not None


# ----------------------------------------------------------------------------- one per thing


def test_a_member_found_twice_keeps_the_stronger_evidence(
    pristine_corpus: Engine, session: Session
) -> None:
    """A document reached by both reference and subject is one member, labelled `reference` —
    so a reader can tell "the bank said these are the same rule" from "they look alike"."""
    other_doc, _ = clause(session, title="Thông tư gốc", anchor="12.2")
    seed_doc, seed_chunk = clause(session, title="Quy trình")
    edge(session, seed_doc, other_doc, anchors=["12.2"])

    result = cover(session, acl(), seed_of(session, seed_chunk))

    assert len(result.members) == 1
    assert result.members[0].channel == "reference"


def test_only_one_member_per_document(pristine_corpus: Engine, session: Session) -> None:
    """A fact set is about documents. The second clause of a document already present adds
    nothing to "who else says this" and spends budget the next document needed."""
    other_doc = make_document(session, title="Biểu phí 2026")
    version = make_version(session, other_doc, effective_from=FROM)
    for ordinal in range(4):
        make_chunk(
            session,
            other_doc,
            version,
            effective_from=FROM,
            section_path=f"Điều {ordinal + 1}",
            body=RATE,
            subject_key=SUBJECT,
            ordinal=ordinal,
        )
    _, seed_chunk = clause(session, title="Biểu phí 2023")
    session.flush()

    result = cover(session, acl(), seed_of(session, seed_chunk))

    assert [m.document_id for m in result.members] == [other_doc]


# ---------------------------------------------------------------------------------- the ACL


@pytest.mark.acl_sweep
def test_no_channel_reaches_past_the_filter(pristine_corpus: Engine, session: Session) -> None:
    """The rule this module exists to keep. Every channel is a second query through the same
    `FilterBuilder`, and a fact set is never a reason to widen (INV-1/2).

    All three channels are set up to return the same restricted document, so a regression in
    any one of them fails here rather than only in the channel somebody remembered to test.
    """
    restricted_doc, _ = clause(
        session,
        title="Ghi chú hạn chế",
        anchor="12.2",
        visibility="restricted",
        groups=[COMPLIANCE_GROUP],
    )
    seed_doc, seed_chunk = clause(session, title="Biểu phí 2023")
    edge(session, seed_doc, restricted_doc, anchors=["12.2"])
    seed = seed_of(session, seed_chunk)

    denied = cover(session, acl(USER_IT_ENGINEER), seed)
    assert all(m.document_id != restricted_doc for m in denied.members)

    # And the same query as somebody who may read it, so the test proves a filter rather than
    # an empty corpus.
    allowed = cover(session, acl(USER_COMPLIANCE_OFFICER), seed)
    assert any(m.document_id == restricted_doc for m in allowed.members)


def test_what_the_filter_withheld_is_counted_but_never_returned(
    pristine_corpus: Engine, session: Session
) -> None:
    """A reviewer reconstructing why an answer missed something needs to tell "we did not have
    it" from "they could not see it" (INV-11). The reader never learns either."""
    clause(session, title="Ghi chú hạn chế", visibility="restricted", groups=[COMPLIANCE_GROUP])
    _, seed_chunk = clause(session, title="Biểu phí 2023")
    seed = seed_of(session, seed_chunk)

    result = cover(session, acl(USER_IT_ENGINEER), seed)

    assert result.members == []
    assert withheld_count(session, seed, result.members) == 1


# ------------------------------------------------------------------------------- boundaries


def test_the_set_is_capped_and_says_when_it_truncated(
    pristine_corpus: Engine, session: Session
) -> None:
    """ "All related passages" is unbounded over 3,000 documents. What is promised is every
    document that states the fact *up to N*, and a statement when there were more."""
    for index in range(MAX_MEMBERS + 3):
        clause(session, title=f"Biểu phí {index}")
    _, seed_chunk = clause(session, title="Biểu phí gốc")

    result = cover(session, acl(), seed_of(session, seed_chunk))

    assert len(result.members) == MAX_MEMBERS
    assert result.truncated == 3


def test_a_seed_alone_in_the_corpus_has_an_empty_set(
    pristine_corpus: Engine, session: Session
) -> None:
    """Not an error and not a warning: most rules are stated once."""
    _, seed_chunk = clause(session, title="Biểu phí duy nhất", subject_key="khong ai khac noi")

    assert cover(session, acl(), seed_of(session, seed_chunk)).members == []


def test_a_missing_seed_is_reported_rather_than_guessed(
    pristine_corpus: Engine, session: Session
) -> None:
    assert load_seed(session, uuid.uuid4()) is None


def test_a_seed_with_neither_key_nor_edges_reaches_only_the_vector_channel(
    pristine_corpus: Engine, session: Session
) -> None:
    """The exact channels contribute nothing rather than falling back to something fuzzy under
    their own name — the labels are what a reader trusts."""
    seed = Seed(chunk_id=uuid.uuid4(), document_id=uuid.uuid4())

    result = cover(session, acl(), seed)

    assert result.members == []


# ------------------------------------------------------------------ through the whole funnel


def test_coverage_is_off_unless_asked_for(
    retrieval_engine: RetrievalEngine, session: Session
) -> None:
    """`chunks` answers "what best matches this question" and paging it is what search does.
    Coverage costs extra queries per seed, so it is a request option rather than a default."""
    clause(session, title="Biểu phí 2026")
    clause(session, title="Biểu phí 2023")

    response = retrieval_engine.retrieve(
        USER_RETAIL_STAFF, RetrieveRequest(query="lãi suất cho vay ngắn hạn", top_k=5)
    ).response

    assert response.fact_sets == []


def test_a_fact_set_reaches_a_document_the_ranking_did_not(
    retrieval_engine: RetrievalEngine, session: Session
) -> None:
    """The point of the milestone, end to end: `top_k=1` returns one passage, and the fact set
    names the other document stating the same rule."""
    clause(session, title="Biểu phí 2026")
    clause(session, title="Biểu phí 2023")

    response = retrieval_engine.retrieve(
        USER_RETAIL_STAFF,
        RetrieveRequest(query="lãi suất cho vay ngắn hạn", top_k=1, cover_facts=True),
    ).response

    assert len(response.chunks) == 1
    assert response.fact_sets, "the second document states the same rule and was not covered"
    covered = {m.document_id for s in response.fact_sets for m in s.members}
    assert covered - {response.chunks[0].document_id}


def test_a_member_already_in_the_ranking_is_not_also_a_member(
    retrieval_engine: RetrievalEngine, session: Session
) -> None:
    """Coverage is what ranking missed. Counting a passage twice would make a set look wider
    than the corpus it actually reached."""
    clause(session, title="Biểu phí 2026")
    clause(session, title="Biểu phí 2023")

    response = retrieval_engine.retrieve(
        USER_RETAIL_STAFF,
        RetrieveRequest(query="lãi suất cho vay ngắn hạn", top_k=10, cover_facts=True),
    ).response

    ranked = {chunk.chunk_id for chunk in response.chunks}
    members = {m.chunk_id for s in response.fact_sets for m in s.members}
    assert not (ranked & members)


def test_what_the_filter_withheld_reaches_the_audit_and_not_the_caller(
    retrieval_engine_with_audit: tuple[RetrievalEngine, InMemoryAuditSink], session: Session
) -> None:
    """INV-11 and ADR-0023 pulling in opposite directions, resolved the way they always are: the
    reviewer gets the count, the reader gets nothing."""
    engine, audit = retrieval_engine_with_audit
    clause(session, title="Ghi chú hạn chế", visibility="restricted", groups=[COMPLIANCE_GROUP])
    clause(session, title="Biểu phí 2023")

    response = engine.retrieve(
        USER_IT_ENGINEER,
        RetrieveRequest(query="lãi suất cho vay ngắn hạn", top_k=5, cover_facts=True),
    ).response

    assert all(
        "hạn chế" not in (m.document_title or "") for s in response.fact_sets for m in s.members
    )
    record = next(r for r in audit.records if r.action is AuditAction.RETRIEVE)
    assert record.detail["fact_members_withheld"] >= 1
