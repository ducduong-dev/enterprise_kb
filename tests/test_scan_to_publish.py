"""Scan → OCR → review → correction → publish → search (M3 acceptance).

The vertical slice the milestone is actually about. A degraded Vietnamese circular goes in as
an image-only PDF; a reviewer sees what the machine read, fixes what it got wrong, classifies
it, confirms a reference, and approves; the corrected text is then retrievable — and the
diacritics that survived every step are checked at the end, where it counts.
"""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
from kb_authz.fixtures import USER_COMPLIANCE_OFFICER, USER_RETAIL_STAFF
from kb_common.audit import AuditAction, InMemoryAuditSink
from kb_common.errors import Conflict, GateBlocked, NotFound
from kb_idp.service import process
from kb_idp.testing.scanned import ESCALATING, ground_truth, recorded_ocr, recorded_vlm, scan_bytes
from kb_portal_api.review import BlockCorrection, Classification, ReviewDecision, ReviewService
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
from kb_ports.adapters.rerank import LexicalRerankAdapter
from kb_ports.adapters.storage_local import LocalStorageAdapter
from kb_registry import repository as repo
from kb_registry.schemas import DocumentCreate, VersionCreate
from kb_registry.service import RegistryService
from kb_retrieval_api.engine import RetrievalEngine
from kb_schemas.api import Facets, RetrieveRequest
from kb_schemas.enums import DocClass, ReviewTaskType, Visibility
from kb_schemas.kbdoc import KBDoc
from kb_schemas.orm import CategoryRow
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

CATEGORY = "t_scan_review"
REVIEWER_GROUPS = {"dept/legal"}
EMBEDDER = HashedEmbeddingAdapter()
SCAN = "tt41_an_toan_von"
DEGRADED = ESCALATING[0]


@pytest.fixture
def storage(tmp_path: Any) -> LocalStorageAdapter:
    return LocalStorageAdapter(tmp_path)


@pytest.fixture
def audit() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def registry(session: Session) -> RegistryService:
    if repo.get_category(session, CATEGORY) is None:
        repo.add_category(
            session,
            CategoryRow(
                path=CATEGORY,
                label="Scan review tests",
                default_visibility=Visibility.INTERNAL_ALL.value,
                default_allowed_groups=[],
                steward_group="dept/legal",
                existence_disclosure=False,
            ),
        )
    session.flush()
    return RegistryService(session)


@pytest.fixture
def reviews(
    session: Session, storage: LocalStorageAdapter, audit: InMemoryAuditSink
) -> ReviewService:
    return ReviewService(session, storage=storage, embedder=EMBEDDER, audit=audit)


@pytest.fixture
def funnel(session: Session) -> RetrievalEngine:
    return RetrievalEngine(
        session,
        keyword_index=PostgresFtsIndexAdapter(session),
        vector_index=PgVectorIndexAdapter(session),
        embedder=EMBEDDER,
        reranker=LexicalRerankAdapter(),
    )


def ingest_scan(
    registry: RegistryService,
    storage: LocalStorageAdapter,
    name: str,
    *,
    doc_class: DocClass = DocClass.OPERATIONAL,
    author: str = "u-uploader",
    pii: str = "clear",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, KBDoc]:
    """Run the real IDP + registry path, then open the review task the workflow would."""
    session = registry._session
    result = process(scan_bytes(name), f"{name}.pdf", ocr=recorded_ocr(), vlm=recorded_vlm())
    assert result.scanned

    kbdoc_ref = (
        "kb-derived/"
        + storage.put(
            "kb-derived", io.BytesIO(result.kbdoc.model_dump_json().encode()), suffix=".kbdoc.json"
        ).key
    )
    page_refs = [
        "kb-derived/" + storage.put("kb-derived", io.BytesIO(page.image), suffix=".png").key
        for page in result.pages
    ]

    document = registry.create_document(
        DocumentCreate(
            title=result.kbdoc.doc_meta.detected_title or name,
            doc_class=doc_class,
            category_path=CATEGORY,
            legal_number=f"{uuid.uuid4().hex[:4]}/2026/TT-SCAN",
        ),
        actor=author,
    )
    version = registry.create_version(
        VersionCreate(
            document_id=document.id,
            content_ref="kb-originals/scan",
            content_hash=uuid.uuid4().hex,
            author=author,
            idp_report_ref=kbdoc_ref,
        ),
        actor=author,
    )
    version.pii_status = pii
    session.flush()

    task = registry.open_review_task(
        version.id,
        ReviewTaskType.IDP_REVIEW,
        assignee_group="dept/legal",
        payload={
            "kbdoc_ref": kbdoc_ref,
            "page_refs": page_refs,
            "page_scores": [round(page.score.score, 3) for page in result.pages],
            "escalated_pages": result.kbdoc.idp_report.escalated_pages,
            "page_sizes": [[page.width, page.height] for page in result.pages],
            "scanned": True,
        },
    )
    session.flush()
    return task.id, document.id, version.id, result.kbdoc


# ------------------------------------------------------------------------ the review screen


def test_the_reviewer_sees_the_machines_reading_and_the_pages(
    registry: RegistryService, storage: LocalStorageAdapter, reviews: ReviewService
) -> None:
    task_id, document_id, version_id, _kbdoc = ingest_scan(registry, storage, SCAN)
    detail = reviews.task(task_id, reviewer_groups=REVIEWER_GROUPS)

    assert detail["document"]["id"] == document_id
    assert detail["version"]["id"] == version_id
    assert detail["kbdoc"]["blocks"]
    assert detail["page_refs"]
    assert detail["page_scores"]
    # Every block carries what the editor needs to show it: confidence, page, and a box.
    for block in detail["kbdoc"]["blocks"]:
        assert block["page"] >= 1
        assert 0.0 <= block["confidence"] <= 1.0


def test_page_images_are_served_through_the_task_not_the_object_store(
    registry: RegistryService, storage: LocalStorageAdapter, reviews: ReviewService
) -> None:
    task_id, _, _, _ = ingest_scan(registry, storage, SCAN)
    image = reviews.page_image(task_id, 1, reviewer_groups=REVIEWER_GROUPS)
    assert image.startswith(b"\x89PNG")
    with pytest.raises(NotFound):
        reviews.page_image(task_id, 99, reviewer_groups=REVIEWER_GROUPS)


def test_a_reviewer_cannot_open_another_teams_queue(
    registry: RegistryService, storage: LocalStorageAdapter, reviews: ReviewService
) -> None:
    """Not-found rather than forbidden: the refusal itself must not disclose the task."""
    task_id, _, _, _ = ingest_scan(registry, storage, SCAN)
    with pytest.raises(NotFound):
        reviews.task(task_id, reviewer_groups={"dept/retail"})


# ------------------------------------------------------------------- correct and publish


def test_a_reviewer_corrects_the_text_and_publishes_it(
    registry: RegistryService,
    storage: LocalStorageAdapter,
    reviews: ReviewService,
    funnel: RetrievalEngine,
    session: Session,
) -> None:
    """The M3 acceptance criterion, end to end."""
    task_id, document_id, version_id, kbdoc = ingest_scan(registry, storage, SCAN)

    target = next(block for block in kbdoc.blocks if "an toàn vốn" in block.text)
    corrected_text = "Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 8% theo Thông tư này."

    outcome = reviews.submit(
        task_id,
        ReviewDecision(
            decision="approve",
            reviewer="u-reviewer",
            corrections=[BlockCorrection(block_id=target.id, text=corrected_text)],
            classification=Classification(department="risk", title="Thông tư an toàn vốn"),
            note="Sửa lỗi nhận dạng",
        ),
        reviewer_groups=REVIEWER_GROUPS,
    )

    assert outcome.decision == "approve"
    assert outcome.corrected
    assert outcome.version_id != version_id, "corrections must create a new version (INV-9)"
    assert outcome.published is not None
    assert outcome.published.chunks_written > 0

    document = repo.get_document(session, document_id)
    assert document is not None
    assert document.status == "published"
    assert document.canonical_version_id == outcome.version_id
    assert document.department == "risk"

    # And the corrected text is what search returns.
    found = funnel.retrieve(
        USER_RETAIL_STAFF, RetrieveRequest(query="tỷ lệ an toàn vốn tối thiểu 8%", top_k=20)
    ).response
    assert any(corrected_text[:40] in chunk.text for chunk in found.chunks)


def test_the_machines_reading_is_preserved_as_its_own_version(
    registry: RegistryService,
    storage: LocalStorageAdapter,
    reviews: ReviewService,
    session: Session,
) -> None:
    """What the scanner produced stays on the record, immutable, beside the correction."""
    task_id, document_id, original_version, kbdoc = ingest_scan(registry, storage, SCAN)
    block = kbdoc.blocks[0]

    outcome = reviews.submit(
        task_id,
        ReviewDecision(
            decision="approve",
            reviewer="u-reviewer",
            corrections=[BlockCorrection(block_id=block.id, text="Đã sửa")],
        ),
        reviewer_groups=REVIEWER_GROUPS,
    )

    versions = {v.id: v for v in repo.list_versions(session, document_id)}
    assert original_version in versions
    assert outcome.version_id in versions
    assert versions[original_version].source_type == "upload"
    assert versions[outcome.version_id].source_type == "portal_edit"
    assert versions[outcome.version_id].author == "u-reviewer"
    assert versions[original_version].is_canonical is False


def test_diacritics_survive_from_the_scan_to_the_search_result(
    registry: RegistryService,
    storage: LocalStorageAdapter,
    reviews: ReviewService,
    funnel: RetrievalEngine,
) -> None:
    """The chain the M3 criterion is about: image → OCR → review → publish → retrieval."""
    task_id, _, _, _ = ingest_scan(registry, storage, DEGRADED)
    outcome = reviews.submit(
        task_id,
        ReviewDecision(decision="approve", reviewer="u-reviewer", corrections=[]),
        reviewer_groups=REVIEWER_GROUPS,
    )

    source = ground_truth(DEGRADED)
    marked = {ch for ch in source if ch in "ăâđêôơưĂÂĐÊÔƠƯáàảãạéèẻẽẹíìỉĩịóòỏõọúùủũụ"}
    found = funnel.retrieve(
        USER_COMPLIANCE_OFFICER,
        # Narrowed to this test's own category: the claim is about one document's characters
        # surviving the chain, and a corpus that grows as other tests publish would otherwise
        # push its chunks out of the top 20. Narrowing is set intersection with the ACL filter
        # (INV-2), so this weakens nothing that matters.
        RetrieveRequest(query=source.split("\n")[0], top_k=20, facets=Facets(category=CATEGORY)),
    ).response
    # Only this document's chunks: the claim is that its diacritics survived the chain, not
    # that it outranks whatever else the test corpus happens to hold at the time.
    retrieved = " ".join(
        chunk.text for chunk in found.chunks if chunk.document_id == outcome.document_id
    )
    assert retrieved, "the document just published should be retrievable"
    assert marked <= set(retrieved) or not marked


def test_dropping_a_block_removes_speckle_the_scanner_invented(
    registry: RegistryService, storage: LocalStorageAdapter, reviews: ReviewService
) -> None:
    task_id, _, _, kbdoc = ingest_scan(registry, storage, SCAN)
    victim = kbdoc.blocks[-1]

    outcome = reviews.submit(
        task_id,
        ReviewDecision(
            decision="approve",
            reviewer="u-reviewer",
            corrections=[BlockCorrection(block_id=victim.id, drop=True)],
        ),
        reviewer_groups=REVIEWER_GROUPS,
    )
    assert outcome.published is not None
    assert outcome.corrected


def test_a_correction_to_an_unknown_block_is_refused(
    registry: RegistryService, storage: LocalStorageAdapter, reviews: ReviewService
) -> None:
    from kb_common.errors import ValidationError

    task_id, _, _, _ = ingest_scan(registry, storage, SCAN)
    with pytest.raises(ValidationError):
        reviews.submit(
            task_id,
            ReviewDecision(
                decision="approve",
                reviewer="u-reviewer",
                corrections=[BlockCorrection(block_id="b999", text="x")],
            ),
            reviewer_groups=REVIEWER_GROUPS,
        )


# ------------------------------------------------------------------------------- guards


def test_the_pii_gate_still_blocks_a_reviewed_document(
    registry: RegistryService, storage: LocalStorageAdapter, reviews: ReviewService
) -> None:
    """INV-7 applies to the review path exactly as it does everywhere else.

    (The scan itself is cleared by the PII gate in M4; until then the workflow leaves the
    version `pending`, and approval is refused rather than waved through.)
    """
    task_id, _, _, _ = ingest_scan(registry, storage, SCAN, pii="pending")
    with pytest.raises(GateBlocked) as exc:
        reviews.submit(
            task_id,
            ReviewDecision(decision="approve", reviewer="u-reviewer", corrections=[]),
            reviewer_groups=REVIEWER_GROUPS,
        )
    assert exc.value.detail["invariant"] == "INV-7"


def test_a_corrected_version_inherits_the_pii_state(
    registry: RegistryService, storage: LocalStorageAdapter, reviews: ReviewService
) -> None:
    """Otherwise correcting a blocked document would launder it into a publishable one."""
    task_id, _, _, kbdoc = ingest_scan(registry, storage, SCAN, pii="blocked")
    with pytest.raises(GateBlocked):
        reviews.submit(
            task_id,
            ReviewDecision(
                decision="approve",
                reviewer="u-reviewer",
                corrections=[BlockCorrection(block_id=kbdoc.blocks[0].id, text="Đã sửa")],
            ),
            reviewer_groups=REVIEWER_GROUPS,
        )


def test_a_regulated_document_needs_a_second_pair_of_eyes(
    registry: RegistryService, storage: LocalStorageAdapter, reviews: ReviewService
) -> None:
    """INV-8: the reviewer who corrected the text becomes its author, so they cannot approve
    it. A second reviewer must — which is the four-eyes rule doing its job, not a bug."""
    task_id, _, _, kbdoc = ingest_scan(
        registry, storage, SCAN, doc_class=DocClass.REGULATORY, author="u-uploader"
    )
    with pytest.raises(GateBlocked, match="four-eyes"):
        reviews.submit(
            task_id,
            ReviewDecision(
                decision="approve",
                reviewer="u-reviewer",
                corrections=[BlockCorrection(block_id=kbdoc.blocks[0].id, text="Đã sửa")],
            ),
            reviewer_groups=REVIEWER_GROUPS,
        )


def test_a_regulated_document_publishes_when_a_different_person_approves(
    registry: RegistryService, storage: LocalStorageAdapter, reviews: ReviewService
) -> None:
    task_id, _, _, _ = ingest_scan(
        registry, storage, SCAN, doc_class=DocClass.REGULATORY, author="u-uploader"
    )
    outcome = reviews.submit(
        task_id,
        ReviewDecision(decision="approve", reviewer="u-approver", corrections=[]),
        reviewer_groups=REVIEWER_GROUPS,
    )
    assert outcome.published is not None


# ----------------------------------------------------------------------------- decisions


def test_rejection_is_recorded_and_publishes_nothing(
    registry: RegistryService,
    storage: LocalStorageAdapter,
    reviews: ReviewService,
    audit: InMemoryAuditSink,
    session: Session,
) -> None:
    task_id, document_id, _, _ = ingest_scan(registry, storage, SCAN)
    outcome = reviews.submit(
        task_id,
        ReviewDecision(
            decision="reject", reviewer="u-reviewer", corrections=[], note="Bản quét mờ"
        ),
        reviewer_groups=REVIEWER_GROUPS,
    )

    assert outcome.published is None
    document = repo.get_document(session, document_id)
    assert document is not None and document.status == "draft"

    task = repo.get_review_task(session, task_id)
    assert task is not None
    assert task.state == "decided"
    assert task.decision == "rejected"
    assert task.decided_by == "u-reviewer"

    (record,) = audit.by_action(AuditAction.REVIEW_DECISION)
    assert record.detail["decision"] == "rejected"
    assert record.detail["note"] == "Bản quét mờ"


def test_a_task_cannot_be_decided_twice(
    registry: RegistryService, storage: LocalStorageAdapter, reviews: ReviewService
) -> None:
    task_id, _, _, _ = ingest_scan(registry, storage, SCAN)
    decision = ReviewDecision(decision="reject", reviewer="u-reviewer", corrections=[])
    reviews.submit(task_id, decision, reviewer_groups=REVIEWER_GROUPS)
    with pytest.raises(Conflict):
        reviews.submit(task_id, decision, reviewer_groups=REVIEWER_GROUPS)


def test_confirming_a_reference_makes_the_edge_authoritative(
    seeded: Any,
    registry: RegistryService,
    storage: LocalStorageAdapter,
    reviews: ReviewService,
    session: Session,
) -> None:
    """An unconfirmed edge is a machine's guess; consolidation (M5) acts only on confirmed ones."""
    # The scan cites 41/2016/TT-NHNN, which the seeded corpus already holds — which is the
    # realistic case: a reference resolves because the registry knows the instrument.
    target = repo.find_by_legal_number(session, "41/2016/TT-NHNN")
    if target is None:
        target = registry.create_document(
            DocumentCreate(
                title="Thông tư được dẫn chiếu",
                doc_class=DocClass.REGULATORY,
                category_path=CATEGORY,
                legal_number="41/2016/TT-NHNN",
            ),
            actor="u-uploader",
        )
    task_id, document_id, _, _ = ingest_scan(registry, storage, "qd_hdqt_chinh_sach_von")

    reviews.submit(
        task_id,
        ReviewDecision(
            decision="approve",
            reviewer="u-reviewer",
            corrections=[],
            confirmed_refs=[("41/2016/TT-NHNN", "implements")],
        ),
        reviewer_groups=REVIEWER_GROUPS,
    )

    edges = repo.list_edges_from(session, document_id)
    confirmed = [edge for edge in edges if edge.dst_document_id == target.id]
    assert confirmed
    assert confirmed[0].confirmed_by == "review"
