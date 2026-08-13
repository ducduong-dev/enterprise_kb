"""Ingest routing and activities.

Routing is tested exhaustively as a pure function; the activities are tested against a real
database and a real storage adapter. The Temporal workflow itself is a thin composition of
the two — the end-to-end run through a live Temporal server is the M1 e2e flow, not a unit
test, because a workflow harness would test Temporal rather than our logic.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from pathlib import Path

import pytest
from kb_common.config import get_settings
from kb_idp.testing.fixtures import fixture_bytes
from kb_ports.adapters.storage_local import LocalStorageAdapter
from kb_registry import repository as repo
from kb_schemas.enums import ReviewTaskType
from kb_workflows.activities import Deps, register_ingest, run_idp, set_deps
from kb_workflows.routing import DEFAULT_STEWARD_GROUP, decide
from kb_workflows.types import (
    Classification,
    DetectedRefOut,
    IdpOutcome,
    IngestRequest,
    RegisterOutcome,
    UploadRef,
)
from sqlalchemy.orm import Session

BUCKET = "kb-originals"


# ------------------------------------------------------------------------------- routing


def idp_outcome(**overrides: object) -> IdpOutcome:
    base: dict[str, object] = {
        "requires_ocr": False,
        "kbdoc_ref": "kb-derived/originals/aa/bb/aabb.kbdoc.json",
        "detected_title": "Thông tư thử nghiệm",
        "legal_number": "41/2016/TT-NHNN",
        "language": "vi",
        "source_format": "docx",
        "page_count": 3,
        "block_count": 40,
    }
    base.update(overrides)
    return IdpOutcome(**base)  # type: ignore[arg-type]


def registered(**overrides: object) -> RegisterOutcome:
    base: dict[str, object] = {
        "document_id": str(uuid.uuid4()),
        "version_id": str(uuid.uuid4()),
        "created_document": True,
        "matched_existing": False,
        "duplicate": False,
        "steward_group": "dept/legal",
    }
    base.update(overrides)
    return RegisterOutcome(**base)  # type: ignore[arg-type]


def test_a_parsed_document_goes_to_its_steward_for_review() -> None:
    decision = decide(idp_outcome(), registered())
    assert decision.status == "awaiting_review"
    assert decision.task is not None
    assert decision.task.task_type == ReviewTaskType.IDP_REVIEW.value
    assert decision.task.assignee_group == "dept/legal"


def test_a_category_without_a_steward_still_gets_a_queue() -> None:
    """An unassigned task is a document that quietly never gets published."""
    decision = decide(idp_outcome(), registered(steward_group=None))
    assert decision.task is not None
    assert decision.task.assignee_group == DEFAULT_STEWARD_GROUP


def test_a_scan_is_parked_for_ocr_not_dropped() -> None:
    decision = decide(
        idp_outcome(requires_ocr=True, reason="no text layer", source_format="pdf_scanned"),
        registered(),
    )
    assert decision.status == "awaiting_ocr"
    assert decision.task is not None
    assert decision.task.payload["requires_ocr"] is True


def test_a_known_instrument_needs_an_identity_decision() -> None:
    """Creating a second document for one instrument would split its history."""
    decision = decide(idp_outcome(), registered(matched_existing=True, created_document=False))
    assert decision.status == "needs_identity_review"
    assert decision.task is not None
    assert decision.task.task_type == ReviewTaskType.IDENTITY_REVIEW.value


def test_identical_content_ends_without_a_task() -> None:
    decision = decide(idp_outcome(), registered(duplicate=True, matched_existing=True))
    assert decision.status == "duplicate"
    assert decision.task is None


def test_review_payload_carries_what_the_reviewer_needs() -> None:
    decision = decide(
        idp_outcome(
            low_confidence_blocks=4,
            warnings=["file content does not match its extension"],
            detected_refs=[DetectedRefOut(legal_number="88/2019/NĐ-CP", ref_type="cites")],
        ),
        registered(),
    )
    assert decision.task is not None
    payload = decision.task.payload
    assert payload["low_confidence_blocks"] == 4
    assert payload["warnings"]
    assert payload["detected_refs"] == [{"legal_number": "88/2019/NĐ-CP", "ref_type": "cites"}]


@pytest.mark.parametrize(
    "outcome",
    [
        registered(),
        registered(matched_existing=True),
        registered(steward_group=None),
        registered(duplicate=True),
    ],
)
def test_every_path_ends_in_a_task_or_a_terminal_state(outcome: RegisterOutcome) -> None:
    decision = decide(idp_outcome(), outcome)
    assert decision.task is not None or decision.status == "duplicate"
    assert decision.detail


# ---------------------------------------------------------------------------- activities

pytestmark_integration = pytest.mark.integration


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorageAdapter:
    adapter = LocalStorageAdapter(tmp_path)
    set_deps(Deps(storage=adapter, settings=get_settings()))
    return adapter


def store_fixture(storage: LocalStorageAdapter, name: str) -> UploadRef:
    data = fixture_bytes(name)
    stored = storage.put(BUCKET, io.BytesIO(data), suffix=Path(name).suffix)
    return UploadRef(
        bucket=BUCKET,
        key=stored.key,
        content_hash=stored.content_hash,
        filename=name,
        size=stored.size,
    )


def ingest_request(upload: UploadRef, **overrides: object) -> IngestRequest:
    classification = Classification(
        title="",
        doc_class="regulatory",
        category_path="t_wf_regulations",
        **overrides,  # type: ignore[arg-type]
    )
    return IngestRequest(
        upload=upload, classification=classification, actor="u-uploader", upload_id="up-1"
    )


def test_idp_activity_stores_the_kbdoc_and_summarizes_it(
    storage: LocalStorageAdapter,
) -> None:
    upload = store_fixture(storage, "tt41_capital.docx")
    outcome = asyncio.run(run_idp(ingest_request(upload)))

    assert not outcome.requires_ocr
    assert outcome.legal_number == "41/2016/TT-NHNN"
    assert outcome.language == "vi"
    assert outcome.block_count > 0
    # The structure itself is in object storage, not in the workflow history.
    assert outcome.kbdoc_ref and outcome.kbdoc_ref.endswith(".kbdoc.json")
    bucket, _, key = outcome.kbdoc_ref.partition("/")
    assert storage.exists(bucket, key)


def test_idp_activity_reports_a_scan_instead_of_returning_an_empty_document(
    storage: LocalStorageAdapter,
) -> None:
    upload = store_fixture(storage, "scanned_notice.pdf")
    outcome = asyncio.run(run_idp(ingest_request(upload)))
    assert outcome.requires_ocr
    assert outcome.kbdoc_ref is None


@pytest.mark.integration
def test_register_activity_creates_rows_and_an_outbox_event(
    session: Session, storage: LocalStorageAdapter
) -> None:
    from kb_schemas.orm import CategoryRow, OutboxRow

    if repo.get_category(session, "t_wf_regulations") is None:
        repo.add_category(
            session,
            CategoryRow(
                path="t_wf_regulations",
                label="Workflow test",
                default_visibility="internal_all",
                default_allowed_groups=[],
                steward_group="dept/legal",
                existence_disclosure=False,
            ),
        )
    session.commit()

    # Unique bytes and a unique instrument number: this activity commits, so the test must
    # not collide with the seeded corpus or with its own previous runs.
    marker = uuid.uuid4().hex[:8]
    legal_number = f"{marker[:4]}/2026/TT-WFTEST"
    stored = storage.put(
        BUCKET,
        io.BytesIO(f"THÔNG BÁO\n\nNội dung thử nghiệm {marker}\n".encode()),
        suffix=".txt",
    )
    upload = UploadRef(
        bucket=BUCKET,
        key=stored.key,
        content_hash=stored.content_hash,
        filename=f"notice-{marker}.txt",
        size=stored.size,
    )
    request = ingest_request(upload, legal_number=legal_number)
    idp = asyncio.run(run_idp(request))
    outcome = asyncio.run(register_ingest(request, idp))

    assert outcome.created_document
    assert outcome.steward_group == "dept/legal"

    document = repo.get_document(session, uuid.UUID(outcome.document_id))
    assert document is not None
    assert document.legal_number == legal_number

    version = repo.get_version(session, uuid.UUID(outcome.version_id))
    assert version is not None
    assert version.content_hash == upload.content_hash
    assert version.pii_status == "pending"  # INV-7: nothing is publishable yet

    events = (
        session.query(OutboxRow)
        .filter(OutboxRow.payload["version_id"].astext == outcome.version_id)
        .all()
    )
    assert len(events) == 1

    # Clean up: this test commits, unlike the rolled-back service tests.
    session.query(OutboxRow).filter(
        OutboxRow.payload["version_id"].astext == outcome.version_id
    ).delete(synchronize_session=False)
    session.commit()


def test_a_vision_first_deployment_does_not_load_an_ocr_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`KB_MODEL_OCR_ENGINE=vlm` means the vision model reads every page (ADR-0025), so
    PaddleOCR — hundreds of megabytes of it — should not be part of that deployment at all."""
    from kb_common.config import get_settings, reset_settings_cache

    monkeypatch.setenv("KB_ENV", "dev")
    monkeypatch.setenv("KB_MODEL_OCR_ENGINE", "vlm")
    reset_settings_cache()
    try:
        deps = Deps.build(get_settings())
        assert deps.ocr is None
        assert deps.vlm is not None
    finally:
        monkeypatch.delenv("KB_MODEL_OCR_ENGINE", raising=False)
        monkeypatch.setenv("KB_ENV", "test")
        reset_settings_cache()
