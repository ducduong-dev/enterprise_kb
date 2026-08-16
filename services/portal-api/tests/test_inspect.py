"""Inspecting a processed document: what was indexed, and what it is connected to.

The point of this screen is that a steward can *check* the machine's work, so the tests are
about what makes checking possible: the chunks as they were indexed, the edges with their
provenance, the ACL applying to both, and the two actions that let a reviewer act on what they
find without going around the invariants.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from kb_authz.fixtures import ALL_PRINCIPALS
from kb_authz.principal import Role
from kb_common.audit import AuditAction, InMemoryAuditSink
from kb_common.config import get_settings, reset_settings_cache
from kb_common.db import get_session
from kb_common.errors import GateBlocked, NotFound, ValidationError
from kb_idp.builder import KBDocBuilder
from kb_portal_api.auth import reset_verifier_cache
from kb_portal_api.inspect import InspectService
from kb_portal_api.main import app, inspect_service
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.storage_local import LocalStorageAdapter
from kb_registry import repository as repo
from kb_registry.publish import Approval, PublishService
from kb_registry.schemas import DocumentCreate, VersionCreate
from kb_registry.service import RegistryService
from kb_schemas.enums import DocClass, PiiStatus, RefType, Visibility
from kb_schemas.kbdoc import KBDoc
from kb_schemas.orm import CategoryRow, DocumentRefRow
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

CATEGORY = "t_inspect"
RESTRICTED_CATEGORY = "t_inspect_restricted"
EMBEDDER = HashedEmbeddingAdapter()

SECTIONS = [
    ("Điều 6. Tỷ lệ dự trữ", "Tổ chức tín dụng duy trì tỷ lệ dự trữ bắt buộc tối thiểu 3%."),
    ("Điều 12. Báo cáo", "Tổ chức tín dụng gửi báo cáo trước ngày 10 hằng tháng."),
]


def kbdoc(sections: list[tuple[str, str]] | None = None) -> KBDoc:
    builder = KBDocBuilder(source_format="docx")
    for heading, body in sections or SECTIONS:
        builder.add(heading, block_type="heading")
        builder.add(body)
    return builder.build(page_count=1)


class Corpus:
    """A published circular, a policy that implements it, an amendment, and a restricted note."""

    def __init__(self, session: Session, storage: LocalStorageAdapter) -> None:
        self.session = session
        self.storage = storage
        self.registry = RegistryService(session)

    def _category(self, path: str, visibility: Visibility, groups: list[str]) -> None:
        if repo.get_category(self.session, path) is None:
            repo.add_category(
                self.session,
                CategoryRow(
                    path=path,
                    label=path,
                    default_visibility=visibility.value,
                    default_allowed_groups=groups,
                    steward_group="dept/legal",
                    existence_disclosure=False,
                ),
            )
        self.session.flush()

    def publish(
        self,
        title: str,
        *,
        category: str = CATEGORY,
        visibility: Visibility = Visibility.INTERNAL_ALL,
        groups: list[str] | None = None,
        sections: list[tuple[str, str]] | None = None,
        legal_number: str | None = None,
    ) -> uuid.UUID:
        document = self.registry.create_document(
            DocumentCreate(
                title=title,
                doc_class=DocClass.OPERATIONAL,
                category_path=category,
                legal_number=legal_number,
                visibility=visibility,
                allowed_groups=groups,
            ),
            actor="u-steward",
        )
        doc = kbdoc(sections)
        stored = self.storage.put(
            "kb-derived",
            __import__("io").BytesIO(doc.model_dump_json().encode("utf-8")),
            suffix=".kbdoc.json",
        )
        version = self.registry.create_version(
            VersionCreate(
                document_id=document.id,
                content_ref="kb-originals/x",
                content_hash=uuid.uuid4().hex,
                author="u-preparer",
                idp_report_ref=f"kb-derived/{stored.key}",
                effective_from=date(2026, 1, 1),
            ),
            actor="u-preparer",
        )
        self.registry.set_pii_status(version.id, PiiStatus.CLEAR, actor="pii-gate")
        publisher = PublishService(self.session, embedder=EMBEDDER)
        publisher.publish(
            version.id,
            publisher.prepare(doc),
            actor="u-steward",
            approval=Approval(approver="u-steward"),
        )
        return document.id

    def link(
        self, src: uuid.UUID, dst: uuid.UUID, ref_type: RefType, *, confirmed: str | None = None
    ) -> None:
        repo.add_edge(
            self.session,
            DocumentRefRow(
                id=uuid.uuid4(),
                src_document_id=src,
                dst_document_id=dst,
                ref_type=ref_type.value,
                articles=[6],
                detected_by="idp",
                confirmed_by=confirmed,
                created_at=datetime.now(UTC),
            ),
        )
        self.session.flush()

    def build(self) -> Corpus:
        self._category(CATEGORY, Visibility.INTERNAL_ALL, [])
        self._category(RESTRICTED_CATEGORY, Visibility.RESTRICTED, ["dept/legal"])

        # Unique per run: a legal number is unique in the registry, and this fixture must not
        # depend on a clean database to build twice.
        suffix = uuid.uuid4().hex[:6].upper()
        self.circular = self.publish(
            "Thông tư về dự trữ bắt buộc", legal_number=f"{suffix}/2026/TT-KB"
        )
        self.policy = self.publish("Quy định nội bộ thực hiện dự trữ")
        self.amendment = self.publish(
            "Thông tư sửa đổi Điều 6", legal_number=f"{suffix}/2026/TT-KBSD"
        )
        self.secret = self.publish(
            "Ghi chú nội bộ của Ban pháp chế",
            category=RESTRICTED_CATEGORY,
            visibility=Visibility.RESTRICTED,
            groups=["dept/legal"],
        )

        self.link(self.policy, self.circular, RefType.IMPLEMENTS, confirmed="u-legal")
        self.link(self.amendment, self.circular, RefType.AMENDS)  # detected, unconfirmed
        self.link(self.secret, self.circular, RefType.CITES)
        # Flush, never commit: committed fixtures survive the per-test rollback and then sit in
        # the index competing for the top of every result list in the rest of the suite. The
        # HTTP tests below share this very session, so nothing here needs to be durable.
        self.session.flush()
        return self


@pytest.fixture
def storage(tmp_path) -> LocalStorageAdapter:  # type: ignore[no-untyped-def]
    return LocalStorageAdapter(tmp_path)


@pytest.fixture
def audit() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def corpus(session: Session, storage: LocalStorageAdapter) -> Corpus:
    return Corpus(session, storage).build()


@pytest.fixture
def inspector(
    session: Session, storage: LocalStorageAdapter, audit: InMemoryAuditSink
) -> InspectService:
    return InspectService(session, storage=storage, embedder=EMBEDDER, audit=audit)


def principal(name: str = "user_retail_staff"):  # type: ignore[no-untyped-def]
    return ALL_PRINCIPALS[name]


# ------------------------------------------------------------------------------- chunks


def test_the_chunks_are_shown_in_reading_order_with_their_citation(
    corpus: Corpus, inspector: InspectService
) -> None:
    chunks = inspector.chunks(corpus.circular, principal())
    assert chunks
    assert [chunk.ordinal for chunk in chunks] == sorted(chunk.ordinal for chunk in chunks)
    assert any("dự trữ bắt buộc" in chunk.text for chunk in chunks)
    assert all(chunk.characters == len(chunk.text) for chunk in chunks)


def test_a_chunk_says_whether_it_has_a_vector(corpus: Corpus, inspector: InspectService) -> None:
    """A chunk with no embedding is findable by keyword and invisible to the semantic leg —
    which looks like a ranking mystery rather than a missing row."""
    assert all(chunk.embedded for chunk in inspector.chunks(corpus.circular, principal()))


def test_tombstoned_chunks_are_hidden_until_asked_for(
    corpus: Corpus, inspector: InspectService, session: Session
) -> None:
    """A steward checking a boundary wants today's text, not every boundary it ever had."""
    session.execute(
        __import__("sqlalchemy").text(
            "UPDATE chunks SET tombstoned = TRUE WHERE document_id = :id AND ordinal = 0"
        ),
        {"id": corpus.circular},
    )
    session.flush()

    live = inspector.chunks(corpus.circular, principal())
    everything = inspector.chunks(corpus.circular, principal(), include_tombstoned=True)
    assert all(not chunk.tombstoned for chunk in live)
    assert len(everything) > len(live)


def test_chunks_of_a_document_the_caller_cannot_read_are_not_served(
    corpus: Corpus, inspector: InspectService
) -> None:
    """Chunk text is document text. The same filter applies, and a refusal is a not-found."""
    with pytest.raises(NotFound):
        inspector.chunks(corpus.secret, principal("user_it_engineer"))

    assert inspector.chunks(corpus.secret, principal("user_legal_counsel"))


# -------------------------------------------------------------------------------- graph


def test_the_graph_shows_both_directions_with_provenance(
    corpus: Corpus, inspector: InspectService
) -> None:
    edges = inspector.inspect(corpus.circular, principal()).edges
    by_type = {(edge.direction, edge.ref_type) for edge in edges}
    assert ("incoming", "implements") in by_type
    assert ("incoming", "amends") in by_type

    implements = next(edge for edge in edges if edge.ref_type == "implements")
    assert implements.confirmed_by == "u-legal"
    assert implements.articles == [6]

    amends = next(edge for edge in edges if edge.ref_type == "amends")
    assert amends.detected_by == "idp"
    assert amends.confirmed is False


def test_an_edge_to_a_document_the_caller_cannot_read_is_counted_not_named(
    corpus: Corpus, inspector: InspectService
) -> None:
    """The edge exists; the title is content. Disclosing it here would be a hole in the same
    filter retrieval applies (INV-2/INV-10)."""
    seen = inspector.inspect(corpus.circular, principal("user_it_engineer"))
    titles = " ".join(edge.other_title for edge in seen.edges)

    assert seen.unreadable_edges == 1
    assert "pháp chế" not in titles
    assert all(edge.readable for edge in seen.edges)

    # Legal can see it, so for them it is an ordinary edge.
    legal = inspector.inspect(corpus.circular, principal("user_legal_counsel"))
    assert legal.unreadable_edges == 0
    assert any("pháp chế" in edge.other_title for edge in legal.edges)


def test_the_lineage_lists_what_amended_this_and_whether_it_was_consolidated(
    corpus: Corpus, inspector: InspectService
) -> None:
    """The question with legal consequences is not topological — it is *what is in force*."""
    lineage = inspector.inspect(corpus.circular, principal()).lineage
    assert len(lineage) == 1
    assert lineage[0]["ref_type"] == "amends"
    assert lineage[0]["consolidated"] is False

    corpus.link(corpus.circular, corpus.amendment, RefType.CONSOLIDATES, confirmed="u-legal")
    corpus.session.flush()
    assert inspector.inspect(corpus.circular, principal()).lineage[0]["consolidated"] is True


def test_a_document_with_no_edges_has_no_lineage(corpus: Corpus, inspector: InspectService) -> None:
    assert inspector.inspect(corpus.policy, principal()).lineage == []


def test_the_page_says_what_to_look_at_first(corpus: Corpus, inspector: InspectService) -> None:
    warnings = inspector.inspect(corpus.circular, principal()).warnings
    assert any("chưa được xác nhận" in warning for warning in warnings)
    assert any("chưa được hợp nhất" in warning for warning in warnings)


# ------------------------------------------------------------------------------ rechunk


def test_rechunk_replaces_the_chunks_of_the_canonical_version(
    corpus: Corpus, inspector: InspectService, session: Session
) -> None:
    before = inspector.chunks(corpus.circular, principal())
    result = inspector.rechunk(corpus.circular, actor="u-steward")

    after = inspector.chunks(corpus.circular, principal())
    assert result.chunks_written == len(after)
    assert result.chunks_before == len(before)
    assert {chunk.chunk_id for chunk in after} & {chunk.chunk_id for chunk in before} == set()
    # Same canonical version: nothing was published, and no version was superseded.
    document = repo.get_document(session, corpus.circular)
    assert document is not None
    assert document.canonical_version_id == result.version_id


def test_rechunk_leaves_no_tombstone_behind(corpus: Corpus, inspector: InspectService) -> None:
    """Tombstoning marks a *superseded* version's chunks. This version is still canonical, so a
    tombstoned row here would be a chunk that is neither live nor history."""
    inspector.rechunk(corpus.circular, actor="u-steward")
    everything = inspector.chunks(corpus.circular, principal(), include_tombstoned=True)
    assert all(not chunk.tombstoned for chunk in everything)


def test_rechunk_emits_the_event_an_external_index_needs(
    corpus: Corpus, inspector: InspectService, session: Session
) -> None:
    from kb_schemas.orm import OutboxRow

    result = inspector.rechunk(corpus.circular, actor="u-steward")
    event = session.get(OutboxRow, result.outbox_id)
    assert event is not None
    assert event.payload["rechunked"] is True
    assert event.payload["version_id"] == str(result.version_id)


def test_rechunk_is_audited_as_the_change_to_retrieval_that_it_is(
    corpus: Corpus, inspector: InspectService, audit: InMemoryAuditSink
) -> None:
    inspector.rechunk(corpus.circular, actor="u-steward")
    record = audit.by_action(AuditAction.PUBLISH)[-1]
    assert record.actor == "u-steward"
    assert record.detail["rechunked"] is True


def test_a_document_with_nothing_published_cannot_be_rechunked(
    corpus: Corpus, inspector: InspectService
) -> None:
    from kb_common.errors import Conflict

    draft = corpus.registry.create_document(
        DocumentCreate(title="Bản nháp", doc_class=DocClass.OPERATIONAL, category_path=CATEGORY),
        actor="u-steward",
    )
    corpus.session.flush()
    with pytest.raises(Conflict):
        inspector.rechunk(draft.id, actor="u-steward")


# -------------------------------------------------------------------------- edge review


def test_confirming_an_edge_makes_it_authoritative(
    corpus: Corpus, inspector: InspectService, audit: InMemoryAuditSink
) -> None:
    outcome = inspector.set_edge_confirmation(
        corpus.amendment, corpus.circular, RefType.AMENDS.value, confirm=True, actor="u-legal"
    )
    assert outcome["decision"] == "confirmed"

    edges = inspector.inspect(corpus.circular, principal()).edges
    amends = next(edge for edge in edges if edge.ref_type == "amends")
    assert amends.confirmed_by == "u-legal"
    assert audit.by_action(AuditAction.REVIEW_DECISION)


def test_a_wrong_edge_can_be_removed(corpus: Corpus, inspector: InspectService) -> None:
    """A wrong edge is worse than a missing one: it opens impact tasks nobody expected."""
    inspector.set_edge_confirmation(
        corpus.amendment, corpus.circular, RefType.AMENDS.value, confirm=False, actor="u-legal"
    )
    edges = inspector.inspect(corpus.circular, principal()).edges
    assert not any(edge.ref_type == "amends" for edge in edges)


def test_an_incoming_edge_is_confirmed_from_the_document_it_points_at(
    corpus: Corpus, inspector: InspectService
) -> None:
    """The reviewer is on the circular's screen and the amendment points *at* it. Asking them
    to open the other document first would mean the edge nobody confirms is the one that
    matters most."""
    outcome = inspector.set_edge_confirmation(
        corpus.circular, corpus.amendment, RefType.AMENDS.value, confirm=True, actor="u-legal"
    )
    assert outcome["decision"] == "confirmed"

    amends = next(
        edge
        for edge in inspector.inspect(corpus.circular, principal()).edges
        if edge.ref_type == "amends"
    )
    assert amends.confirmed_by == "u-legal"


def test_deciding_an_edge_that_does_not_exist_is_a_not_found(
    corpus: Corpus, inspector: InspectService
) -> None:
    with pytest.raises(NotFound):
        inspector.set_edge_confirmation(
            corpus.policy, corpus.amendment, RefType.CITES.value, confirm=True, actor="u-legal"
        )


# ------------------------------------------------------------------- late-arriving targets


def test_a_reference_to_a_document_we_do_not_hold_waits_instead_of_vanishing(
    corpus: Corpus, inspector: InspectService
) -> None:
    """The corpus is digitised in arbitrary order, so "cites something we do not have" is the
    normal case, not an error. It must still be visible, and it must still become an edge."""
    from kb_registry.schemas import DetectedRefIn

    corpus.registry.link_detected_refs(
        corpus.circular,
        [DetectedRefIn(legal_number="99/2019/TT-NHNN", ref_type=RefType.CITES, detected_by="idp")],
    )
    corpus.session.flush()

    inspection = inspector.inspect(corpus.circular, principal())
    assert [item["legal_number"] for item in inspection.pending] == ["99/2019/TT-NHNN"]
    assert any("chưa có trong hệ thống" in warning for warning in inspection.warnings)


def test_the_edge_appears_when_the_target_is_finally_ingested(
    corpus: Corpus, inspector: InspectService
) -> None:
    """The bug this exists for: an amending decree ingested before the decree it amends never
    linked, and nothing said so — the supersession warning and the impact tasks both read
    edges, so a missing one is silence."""
    from kb_registry.schemas import DetectedRefIn

    # Unique per run. The test's premise is "a document the registry does not hold", and a
    # hard-coded number cannot express that on a machine where the platform is actually used:
    # a real upload carrying the same number promotes the reference immediately and the test
    # fails for a reason that has nothing to do with the pending-reference path.
    absent = f"{uuid.uuid4().int % 900 + 100}/2025/NĐ-CP-{uuid.uuid4().hex[:6].upper()}"

    corpus.registry.link_detected_refs(
        corpus.circular,
        [DetectedRefIn(legal_number=absent, ref_type=RefType.AMENDS, detected_by="idp")],
    )
    corpus.session.flush()
    assert inspector.inspect(corpus.circular, principal()).pending

    late = corpus.publish("Nghị định đến sau", legal_number=absent)

    inspection = inspector.inspect(corpus.circular, principal())
    assert not inspection.pending, "the parked reference should have been promoted"
    amends = next(
        edge
        for edge in inspection.edges
        if edge.ref_type == RefType.AMENDS.value and edge.other_document_id == late
    )
    assert amends.direction == "outgoing"
    assert amends.detected_by == "idp" and amends.confirmed_by is None


def test_the_promoted_edge_is_visible_to_graph_expansion(
    corpus: Corpus, inspector: InspectService
) -> None:
    """`graph_serving` is rebuilt by the publish transaction of the document being published;
    an edge attached to a document published long ago needs its copy refreshed too, or
    retrieval expands through nothing."""
    from kb_registry.schemas import DetectedRefIn
    from sqlalchemy import text as sql

    corpus.registry.link_detected_refs(
        corpus.circular,
        [DetectedRefIn(legal_number="77/2027/TT-KB", ref_type=RefType.CITES, detected_by="idp")],
    )
    corpus.session.flush()
    late = corpus.publish("Văn bản đến sau", legal_number="77/2027/TT-KB")

    served = corpus.session.execute(
        sql(
            "SELECT count(*) FROM graph_serving "
            "WHERE src_document_id = :src AND dst_document_id = :dst"
        ),
        {"src": corpus.circular, "dst": late},
    ).scalar()
    assert served == 1


# ---------------------------------------------------------------------------- over HTTP
#
# The service tests above answer "does it show the right thing". These answer the question
# only the wire can: who may change what, given a token rather than an argument.

SECRET = "kb-test-secret"


@pytest.fixture(autouse=True)
def insecure_tokens(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KB_ENV", "test")
    monkeypatch.setenv("KB_OIDC_ALLOW_INSECURE_TOKENS", "true")
    reset_settings_cache()
    reset_verifier_cache()
    yield
    reset_settings_cache()
    reset_verifier_cache()


@pytest.fixture
def client(session: Session, inspector: InspectService) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[inspect_service] = lambda: inspector
    yield TestClient(app)
    app.dependency_overrides.clear()


def auth(subject: str, roles: list[str], groups: list[str] | None = None) -> dict[str, str]:
    settings = get_settings().oidc
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": subject,
            "iss": settings.issuer,
            "aud": settings.audience,
            "iat": now,
            "exp": now + timedelta(minutes=10),
            "preferred_username": subject,
            "groups": groups if groups is not None else ["/dept/legal"],
            "realm_access": {"roles": roles},
            "azp": "kb-portal",
        },
        SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_the_inspection_endpoint_serves_the_whole_screen(
    client: TestClient, corpus: Corpus
) -> None:
    response = client.get(
        f"/v1/documents/{corpus.circular}/inspect", headers=auth("u-steward", [Role.STEWARD])
    )
    assert response.status_code == 200
    body = response.json()
    assert body["document"]["id"] == str(corpus.circular)
    assert body["chunks"] and body["edges"]
    assert body["map_node_limit"] > 0


def test_an_anonymous_caller_inspects_nothing(client: TestClient, corpus: Corpus) -> None:
    assert client.get(f"/v1/documents/{corpus.circular}/inspect").status_code == 401


def test_rechunking_needs_the_steward_role(client: TestClient, corpus: Corpus) -> None:
    """Reading this screen is for anyone who may read the document. Rewriting what retrieval
    quotes from it is not (ADR-0026)."""
    refused = client.post(f"/v1/documents/{corpus.circular}/rechunk", headers=auth("u-reader", []))
    assert refused.status_code == 403

    allowed = client.post(
        f"/v1/documents/{corpus.circular}/rechunk", headers=auth("u-steward", [Role.STEWARD])
    )
    assert allowed.status_code == 200
    assert allowed.json()["chunks_written"] > 0


def test_confirming_an_edge_needs_the_steward_role(client: TestClient, corpus: Corpus) -> None:
    body = {
        "other_document_id": str(corpus.amendment),
        "ref_type": RefType.AMENDS.value,
        "confirm": True,
    }
    refused = client.post(
        f"/v1/documents/{corpus.circular}/edges", json=body, headers=auth("u-reader", [])
    )
    assert refused.status_code == 403

    allowed = client.post(
        f"/v1/documents/{corpus.circular}/edges",
        json=body,
        headers=auth("u-steward", [Role.STEWARD]),
    )
    assert allowed.status_code == 200
    assert allowed.json()["decision"] == "confirmed"


def test_the_registry_listing_does_not_name_documents_the_caller_cannot_read(
    client: TestClient, corpus: Corpus
) -> None:
    """The listing is metadata, and metadata is still disclosure: a title and a legal number
    say an instrument exists and what it covers. The restricted note in this corpus belongs to
    a group the caller is not in, so the row must not be there at all."""
    response = client.get(
        "/v1/documents?limit=200",
        headers=auth("u-ops-steward", [Role.STEWARD], groups=["/dept/operations"]),
    )
    assert response.status_code == 200
    titles = {row["title"] for row in response.json()}
    assert "Thông tư về dự trữ bắt buộc" in titles
    assert "Ghi chú nội bộ của Ban pháp chế" not in titles


# ------------------------------------------------------------------ expiry panel (M9a)


def _proposal(inspector: InspectService, document_id: uuid.UUID, actor: str = "user_retail_staff"):  # type: ignore[no-untyped-def]
    return inspector.mark_expired(
        document_id,
        principal(actor),
        effective_to=date(2026, 12, 31),
        evidence="Bị thay thế bởi Thông tư 15/2026 có hiệu lực từ 01/01/2027.",
    )


def test_the_panel_shows_what_is_in_force_and_where_it_came_from(
    corpus: Corpus, inspector: InspectService
) -> None:
    panel = inspector.inspect(corpus.circular, principal()).expiry
    assert panel.in_force is None
    assert panel.history == []

    row = _proposal(inspector, corpus.circular)
    inspector.decide_expiry(corpus.circular, uuid.UUID(row["row_id"]), principal(), confirm=True)

    panel = inspector.inspect(corpus.circular, principal()).expiry
    assert panel.in_force == "2026-12-31"
    assert panel.in_force_source == "ledger"


def test_the_panel_keeps_the_whole_sequence_and_marks_the_open_row(
    corpus: Corpus, inspector: InspectService
) -> None:
    """ "Why did this disappear from search in May, and who decided that?" is answered by
    reading down this list — nothing is ever deleted (ADR-0030)."""
    row = _proposal(inspector, corpus.circular)
    confirmed = inspector.decide_expiry(
        corpus.circular, uuid.UUID(row["row_id"]), principal(), confirm=True
    )
    inspector.decide_expiry(
        corpus.circular,
        uuid.UUID(confirmed["row_id"]),
        principal(),
        confirm=False,
        reason="đọc nhầm điều khoản bãi bỏ",
    )

    panel = inspector.inspect(corpus.circular, principal()).expiry
    assert [row.state for row in panel.history] == ["proposed", "confirmed", "revoked"]
    assert [row.open for row in panel.history] == [False, False, True]
    assert len(panel.current) == 1
    assert panel.in_force is None, "revoking put the document back"


def test_the_panel_carries_both_clocks_and_the_evidence(
    corpus: Corpus, inspector: InspectService
) -> None:
    row = _proposal(inspector, corpus.circular)
    view = inspector.inspect(corpus.circular, principal()).expiry.history[0]

    assert view.row_id == uuid.UUID(row["row_id"])
    assert view.effective_to == "2026-12-31", "when it stopped applying in the world"
    assert view.created_at, "when this platform started believing it"
    assert "Thông tư 15/2026" in (view.evidence or "")
    assert view.basis == "steward"
    assert view.detected_by == principal().audit_actor, (
        "a steward's own proposal records the person, so four eyes can see them"
    )


def test_the_version_date_stays_visible_beside_the_ledgers(
    corpus: Corpus, inspector: InspectService, session: Session
) -> None:
    """A steward who typed an end date into a rate schedule has to see why a different one is
    in force, or the screen is lying by omission (ADR-0030)."""
    session.execute(
        __import__("sqlalchemy").text(
            "UPDATE document_versions SET effective_to = DATE '2030-06-30' "
            "WHERE document_id = :id AND is_canonical"
        ),
        {"id": corpus.circular},
    )
    session.flush()
    row = _proposal(inspector, corpus.circular)
    inspector.decide_expiry(corpus.circular, uuid.UUID(row["row_id"]), principal(), confirm=True)

    inspection = inspector.inspect(corpus.circular, principal())
    assert inspection.expiry.version_effective_to == "2030-06-30"
    assert inspection.expiry.in_force == "2026-12-31"
    assert any("sổ quyết định được ưu tiên" in w for w in inspection.warnings)


def test_a_pending_proposal_is_warned_about_and_changes_nothing(
    corpus: Corpus, inspector: InspectService
) -> None:
    _proposal(inspector, corpus.circular)
    inspection = inspector.inspect(corpus.circular, principal())

    assert inspection.expiry.in_force is None
    assert any("chờ xác nhận" in w for w in inspection.warnings)


def test_a_confirmed_partial_expiry_names_the_clauses_it_ended(
    corpus: Corpus, inspector: InspectService
) -> None:
    """A partially expired document stays published, and the screen has to say which clauses
    went so nobody reads "published" as "wholly in force" (ADR-0040)."""
    row = inspector.mark_expired(
        corpus.circular,
        principal(),
        effective_to=date(2026, 12, 31),
        evidence="Bãi bỏ khoản 2 Điều 12 theo Thông tư 15/2026.",
        anchors=["12.2"],
    )
    decision = inspector.decide_expiry(
        corpus.circular, uuid.UUID(row["row_id"]), principal(), confirm=True
    )

    assert decision["state"] == "confirmed"
    inspection = inspector.inspect(corpus.circular, principal())
    assert inspection.expiry.in_force is None, "the document as a whole is still in force"
    assert any("Một số điều khoản đã hết hiệu lực" in w for w in inspection.warnings)


def test_confirming_takes_the_document_out_of_the_default_path(
    corpus: Corpus, inspector: InspectService, session: Session
) -> None:
    """The action's real effect: the dates land on the chunks in this transaction, so the
    document leaves search on its date with no scheduled job involved (ADR-0031)."""
    row = _proposal(inspector, corpus.circular)
    decision = inspector.decide_expiry(
        corpus.circular, uuid.UUID(row["row_id"]), principal(), confirm=True
    )

    assert decision["applied"] is True
    assert decision["chunks_projected"] > 0
    still_served = session.execute(
        __import__("sqlalchemy").text(
            "SELECT count(*) FROM chunks WHERE document_id = :id AND NOT tombstoned "
            "AND (effective_to IS NULL OR effective_to >= DATE '2027-01-01')"
        ),
        {"id": corpus.circular},
    ).scalar()
    assert still_served == 0


def test_marking_a_document_expired_needs_a_stated_reason(
    corpus: Corpus, inspector: InspectService
) -> None:
    """An expiry with no reason cannot be reviewed years later, which is when it will be."""
    with pytest.raises(ValidationError):
        inspector.mark_expired(
            corpus.circular, principal(), effective_to=date(2026, 12, 31), evidence="   "
        )


def test_a_document_the_caller_cannot_read_cannot_be_expired(
    corpus: Corpus, inspector: InspectService
) -> None:
    """The action inherits the read filter: you cannot withdraw what you may not see."""
    with pytest.raises(NotFound):
        inspector.mark_expired(
            corpus.secret,
            principal("user_it_engineer"),
            effective_to=date(2026, 12, 31),
            evidence="Không được phép đọc văn bản này.",
        )


def test_a_row_from_another_document_is_refused(corpus: Corpus, inspector: InspectService) -> None:
    """The row id is a path parameter; it must not be a way to act on a different document."""
    row = _proposal(inspector, corpus.circular)
    with pytest.raises(NotFound):
        inspector.decide_expiry(corpus.policy, uuid.UUID(row["row_id"]), principal(), confirm=True)


def test_a_regulated_document_needs_a_second_pair_of_eyes(
    corpus: Corpus, inspector: InspectService, session: Session
) -> None:
    """Withdrawing what the bank tells people changes what the bank tells people as surely as
    publishing does, which is what INV-8 exists for."""
    session.execute(
        __import__("sqlalchemy").text(
            "UPDATE documents SET doc_class = 'regulatory' WHERE id = :id"
        ),
        {"id": corpus.circular},
    )
    session.flush()
    row = _proposal(inspector, corpus.circular, actor="user_retail_staff")

    with pytest.raises(GateBlocked, match="four-eyes"):
        inspector.decide_expiry(
            corpus.circular, uuid.UUID(row["row_id"]), principal("user_retail_staff"), confirm=True
        )

    decision = inspector.decide_expiry(
        corpus.circular, uuid.UUID(row["row_id"]), principal("user_legal_counsel"), confirm=True
    )
    assert decision["state"] == "confirmed"


def test_a_machine_proposal_on_a_regulated_document_needs_exactly_one_human(
    corpus: Corpus, inspector: InspectService, session: Session
) -> None:
    """`detected_by` is a detector, never an actor, so it can never collide with the confirmer.
    That is ADR-0030's "a detector writes proposed, a human confirms", enforced."""
    from kb_registry.expiry import ExpiryLedger
    from kb_schemas.enums import ExpiryBasis

    session.execute(
        __import__("sqlalchemy").text(
            "UPDATE documents SET doc_class = 'regulatory' WHERE id = :id"
        ),
        {"id": corpus.circular},
    )
    session.flush()
    proposed = ExpiryLedger(session).propose(
        corpus.circular,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.ABROGATED_BY,
        detected_by="abrogates_edge:1",
        actor="system",
        source_document_id=corpus.amendment,
        evidence="Thông tư sửa đổi bãi bỏ văn bản này.",
    )

    decision = inspector.decide_expiry(corpus.circular, proposed.row_id, principal(), confirm=True)
    assert decision["state"] == "confirmed"


def test_the_expiry_names_the_instrument_that_ended_it(
    corpus: Corpus, inspector: InspectService, session: Session
) -> None:
    from kb_registry.expiry import ExpiryLedger
    from kb_schemas.enums import ExpiryBasis

    ExpiryLedger(session).propose(
        corpus.circular,
        effective_to=date(2026, 12, 31),
        basis=ExpiryBasis.ABROGATED_BY,
        detected_by="abrogates_edge:1",
        actor="system",
        source_document_id=corpus.amendment,
        evidence="Thông tư sửa đổi bãi bỏ văn bản này.",
    )

    view = inspector.inspect(corpus.circular, principal()).expiry.history[0]
    assert view.source_document_id == corpus.amendment
    assert view.source_title == "Thông tư sửa đổi Điều 6"


def test_expiring_a_document_needs_the_steward_role(client: TestClient, corpus: Corpus) -> None:
    """Reading this screen is for anyone who may read the document. Withdrawing it from every
    answer the platform gives is not."""
    body = {
        "effective_to": "2026-12-31",
        "evidence": "Bị thay thế bởi Thông tư 15/2026 có hiệu lực từ 01/01/2027.",
    }
    refused = client.post(
        f"/v1/documents/{corpus.circular}/expiry", json=body, headers=auth("u-reader", [])
    )
    assert refused.status_code == 403

    allowed = client.post(
        f"/v1/documents/{corpus.circular}/expiry",
        json=body,
        headers=auth("u-steward", [Role.STEWARD]),
    )
    assert allowed.status_code == 200
    proposal = allowed.json()
    assert proposal["state"] == "proposed"
    assert proposal["applied"] is False

    decided = client.post(
        f"/v1/documents/{corpus.circular}/expiry/{proposal['row_id']}",
        json={"confirm": True},
        headers=auth("u-approver", [Role.STEWARD]),
    )
    assert decided.status_code == 200
    assert decided.json()["state"] == "confirmed"
    assert decided.json()["applied"] is True


def test_an_expiry_without_a_reason_is_refused_at_the_edge(
    client: TestClient, corpus: Corpus
) -> None:
    """Validation, not a service-layer surprise: the reason is what an auditor reads later."""
    response = client.post(
        f"/v1/documents/{corpus.circular}/expiry",
        json={"effective_to": "2026-12-31", "evidence": "sai"},
        headers=auth("u-steward", [Role.STEWARD]),
    )
    assert response.status_code == 422
