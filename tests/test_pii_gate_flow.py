"""The PII gate in the ingest flow, and the override path (M4 acceptance, INV-7).

The red-team corpus proves the *detector*. This proves the *gate*: that a blocked document
cannot be published by any route, that the block reaches Compliance rather than the document's
steward, and that the only way past it leaves a name, a reason and an audit record behind.
"""

from __future__ import annotations

import uuid

import pytest
from kb_common.audit import AuditAction, InMemoryAuditSink
from kb_common.errors import Conflict, GateBlocked, ValidationError
from kb_idp.builder import KBDocBuilder
from kb_pii_gate.detector import PatternPiiDetector, scan_document
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_registry import repository as repo
from kb_registry.publish import PublishService
from kb_registry.schemas import DocumentCreate, VersionCreate
from kb_registry.service import RegistryService
from kb_schemas.enums import DocClass, PiiStatus, ReviewTaskType, Visibility
from kb_schemas.kbdoc import KBDoc
from kb_schemas.orm import CategoryRow
from kb_workflows.routing import PII_OVERRIDE_GROUP, decide
from kb_workflows.types import IdpOutcome, PiiOutcome, RegisterOutcome
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

CATEGORY = "t_pii"
EMBEDDER = HashedEmbeddingAdapter()
DETECTOR = PatternPiiDetector()

DIRTY = "Số tài khoản: 0123456789 của khách hàng Nguyễn Văn Minh, số dư 254.000.000 VND."
CLEAN = "Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 8% theo quy định."


@pytest.fixture
def audit() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def registry(session: Session, audit: InMemoryAuditSink) -> RegistryService:
    if repo.get_category(session, CATEGORY) is None:
        repo.add_category(
            session,
            CategoryRow(
                path=CATEGORY,
                label="PII gate tests",
                default_visibility=Visibility.INTERNAL_ALL.value,
                default_allowed_groups=[],
                steward_group="dept/legal",
                existence_disclosure=False,
            ),
        )
    session.flush()
    return RegistryService(session, audit=audit)


def kbdoc(body: str) -> KBDoc:
    builder = KBDocBuilder(source_format="docx")
    builder.add("Điều 1. Nội dung", block_type="heading")
    builder.add(body)
    return builder.build(page_count=1)


def make_version(
    registry: RegistryService, *, doc_class: DocClass = DocClass.OPERATIONAL
) -> uuid.UUID:
    document = registry.create_document(
        DocumentCreate(title="Tài liệu", doc_class=doc_class, category_path=CATEGORY),
        actor="u-uploader",
    )
    version = registry.create_version(
        VersionCreate(
            document_id=document.id,
            content_ref="kb-originals/x",
            content_hash=uuid.uuid4().hex,
            author="u-uploader",
        ),
        actor="u-uploader",
    )
    return version.id


# ------------------------------------------------------------------------- the verdict


def test_a_clean_document_is_cleared_and_publishes(
    registry: RegistryService, session: Session
) -> None:
    version_id = make_version(registry)
    scan = scan_document(DETECTOR, kbdoc(CLEAN))
    assert scan.is_clear

    registry.set_pii_status(version_id, PiiStatus.CLEAR, actor="pii-gate")
    publisher = PublishService(session, embedder=EMBEDDER)
    result = publisher.publish(version_id, publisher.prepare(kbdoc(CLEAN)), actor="u-steward")
    assert result.chunks_written > 0


def test_a_document_with_pii_is_blocked_and_cannot_be_published(
    registry: RegistryService, session: Session
) -> None:
    """INV-7: the gate's verdict is what makes publication impossible, not a UI check."""
    version_id = make_version(registry)
    scan = scan_document(DETECTOR, kbdoc(DIRTY))
    assert not scan.is_clear

    registry.set_pii_status(version_id, PiiStatus.BLOCKED, actor="pii-gate", detail=scan.summary())
    publisher = PublishService(session, embedder=EMBEDDER)
    with pytest.raises(GateBlocked) as exc:
        publisher.publish(version_id, publisher.prepare(kbdoc(DIRTY)), actor="u-steward")
    assert exc.value.detail["invariant"] == "INV-7"


def test_an_incomplete_scan_leaves_the_version_unpublishable(
    registry: RegistryService, session: Session
) -> None:
    """A gate that could not finish has not cleared anything."""
    version_id = make_version(registry)
    version = repo.get_version(session, version_id)
    assert version is not None and version.pii_status == PiiStatus.PENDING.value

    publisher = PublishService(session, embedder=EMBEDDER)
    with pytest.raises(GateBlocked):
        publisher.publish(version_id, publisher.prepare(kbdoc(CLEAN)), actor="u-steward")


def test_the_verdict_is_audited_with_what_was_found(
    registry: RegistryService, audit: InMemoryAuditSink
) -> None:
    version_id = make_version(registry)
    scan = scan_document(DETECTOR, kbdoc(DIRTY))
    registry.set_pii_status(version_id, PiiStatus.BLOCKED, actor="pii-gate", detail=scan.summary())

    (record,) = audit.by_action(AuditAction.PII_BLOCK)
    assert record.actor == "pii-gate"
    assert record.detail["pii_status"] == "blocked"
    assert record.detail["kinds"]
    # The audit record names the kinds found, never the values themselves.
    assert "0123456789" not in str(record.detail)


# ---------------------------------------------------------------------------- routing


def test_a_blocked_document_goes_to_compliance_not_to_the_steward() -> None:
    """An override needs the `kb-pii-overrider` role. The steward does not have it."""
    decision = decide(
        IdpOutcome(requires_ocr=False, kbdoc_ref="kb-derived/x", block_count=2),
        RegisterOutcome(
            document_id=str(uuid.uuid4()),
            version_id=str(uuid.uuid4()),
            created_document=True,
            matched_existing=False,
            duplicate=False,
            steward_group="dept/legal",
        ),
        PiiOutcome(status="blocked", finding_count=2, kinds={"account_number": 2}),
    )
    assert decision.status == "pii_blocked"
    assert decision.task is not None
    assert decision.task.task_type == ReviewTaskType.PII_OVERRIDE.value
    assert decision.task.assignee_group == PII_OVERRIDE_GROUP
    assert decision.task.payload["pii_findings"] == 2


def test_an_incomplete_scan_also_reaches_a_human() -> None:
    """Failing closed must not mean failing silently: someone has to be told."""
    decision = decide(
        IdpOutcome(requires_ocr=False, kbdoc_ref="kb-derived/x"),
        RegisterOutcome(
            document_id=str(uuid.uuid4()),
            version_id=str(uuid.uuid4()),
            created_document=True,
            matched_existing=False,
            duplicate=False,
            steward_group="dept/legal",
        ),
        PiiOutcome(status="pending", scan_complete=False),
    )
    assert decision.status == "pii_scan_incomplete"
    assert decision.task is not None
    assert decision.task.payload["scan_complete"] is False


def test_a_clean_document_still_goes_to_its_steward() -> None:
    decision = decide(
        IdpOutcome(requires_ocr=False, kbdoc_ref="kb-derived/x"),
        RegisterOutcome(
            document_id=str(uuid.uuid4()),
            version_id=str(uuid.uuid4()),
            created_document=True,
            matched_existing=False,
            duplicate=False,
            steward_group="dept/legal",
        ),
        PiiOutcome(status="clear"),
    )
    assert decision.status == "awaiting_review"
    assert decision.task is not None
    assert decision.task.assignee_group == "dept/legal"


# --------------------------------------------------------------------------- override


def test_an_override_requires_a_written_justification(registry: RegistryService) -> None:
    """An override without a reason is a bypass with extra steps."""
    version_id = make_version(registry)
    registry.set_pii_status(version_id, PiiStatus.BLOCKED, actor="pii-gate")

    with pytest.raises(ValidationError):
        registry.override_pii(version_id, actor="u-compliance", justification="ok")


def test_an_override_unblocks_publication_and_is_audited(
    registry: RegistryService, session: Session, audit: InMemoryAuditSink
) -> None:
    version_id = make_version(registry)
    scan = scan_document(DETECTOR, kbdoc(DIRTY))
    registry.set_pii_status(version_id, PiiStatus.BLOCKED, actor="pii-gate", detail=scan.summary())

    justification = (
        "Số tài khoản trong tài liệu là số tài khoản nội bộ của ngân hàng, "
        "không thuộc về khách hàng. Đã đối chiếu với Khối Vận hành."
    )
    registry.override_pii(
        version_id,
        actor="u-compliance",
        justification=justification,
        findings=scan.summary(),
    )

    version = repo.get_version(session, version_id)
    assert version is not None and version.pii_status == PiiStatus.OVERRIDDEN.value

    (record,) = audit.by_action(AuditAction.PII_OVERRIDE)
    assert record.actor == "u-compliance"
    assert record.detail["justification"] == justification
    assert record.detail["previous_status"] == "blocked"

    # And the document can now be published — by the normal path, with the normal guards.
    publisher = PublishService(session, embedder=EMBEDDER)
    assert (
        publisher.publish(
            version_id, publisher.prepare(kbdoc(DIRTY)), actor="u-steward"
        ).chunks_written
        > 0
    )


def test_only_a_blocked_version_can_be_overridden(registry: RegistryService) -> None:
    """Overriding a clean version would put a meaningless override in the audit trail."""
    version_id = make_version(registry)
    registry.set_pii_status(version_id, PiiStatus.CLEAR, actor="pii-gate")
    with pytest.raises(Conflict):
        registry.override_pii(
            version_id,
            actor="u-compliance",
            justification="Không có lý do gì đặc biệt nhưng vẫn muốn ghi đè quyết định.",
        )


def test_a_later_scan_cannot_quietly_discard_a_human_override(
    registry: RegistryService,
) -> None:
    """Re-processing must not erase a decision a person signed their name to."""
    version_id = make_version(registry)
    registry.set_pii_status(version_id, PiiStatus.BLOCKED, actor="pii-gate")
    registry.override_pii(
        version_id,
        actor="u-compliance",
        justification="Tài khoản nội bộ, đã đối chiếu với Khối Vận hành ngày 11/8/2026.",
    )

    with pytest.raises(Conflict):
        registry.set_pii_status(version_id, PiiStatus.CLEAR, actor="pii-gate")
    # A *new* finding may still block it again — the override was for what was found then.
    registry.set_pii_status(version_id, PiiStatus.BLOCKED, actor="pii-gate")


# ------------------------------------------------------------------------ output filter


def test_the_output_filter_and_the_gate_share_a_detector() -> None:
    """An answer must not be able to disclose what ingestion refused to publish (INV-7)."""
    answer = (
        "Theo hồ sơ, khách hàng Nguyễn Văn Minh có số tài khoản 0123456789 "
        "và số dư 254.000.000 VND."
    )
    ingestion = scan_document(DETECTOR, kbdoc(answer))
    filtered, output = DETECTOR.redact(answer)

    assert not ingestion.is_clear
    assert output.findings
    assert "0123456789" not in filtered
    assert {finding.kind for finding in output.findings} == {
        finding.kind for finding in ingestion.result.findings
    }


def test_the_filter_redacts_rather_than_refusing() -> None:
    """An answer with an identifier removed is still an answer; refusing sends users
    elsewhere, which is where the data actually leaks."""
    answer = "Phí duy trì tài khoản là 0 đồng. Liên hệ số điện thoại 0912345678 để biết thêm."
    filtered, result = DETECTOR.redact(answer)
    assert "Phí duy trì tài khoản là 0 đồng." in filtered
    assert "0912345678" not in filtered
    assert result.findings
