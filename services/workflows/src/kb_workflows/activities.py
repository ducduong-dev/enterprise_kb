"""Ingest activities — every side effect the workflow has.

Activities are idempotent by construction wherever Temporal might retry them: object keys are
content hashes, version creation deduplicates on content hash, and edge creation checks for
an existing edge. A retry after a partial failure therefore converges rather than duplicating.
"""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from kb_common.audit import SqlAuditSink
from kb_common.config import Settings, get_settings
from kb_common.db import session_scope
from kb_common.logging import get_logger
from kb_idp.service import process
from kb_pii_gate.detector import PatternPiiDetector, scan_document
from kb_ports.adapters.storage_local import LocalStorageAdapter
from kb_ports.adapters.storage_s3 import S3StorageAdapter
from kb_ports.models import GenerationPort, OcrPort, PiiDetectorPort, VlmPort
from kb_ports.storage import StoragePort
from kb_registry import repository as repo
from kb_registry.schemas import DetectedRefIn, DocumentCreate
from kb_registry.service import TOPIC_VERSION_CREATED, RegistryService
from kb_schemas.enums import DocClass, PiiStatus, RefType, ReviewTaskType, Visibility
from kb_schemas.kbdoc import KBDoc
from temporalio import activity

from kb_workflows.types import (
    DetectedRefOut,
    IdpOutcome,
    IngestRequest,
    PiiOutcome,
    RegisterOutcome,
    ReviewTaskRequest,
)

log = get_logger(__name__)


@dataclass
class Deps:
    """Injected so activities are testable without Temporal, MinIO or a GPU node."""

    storage: StoragePort
    settings: Settings
    #: Scanned documents need these. Absent, a scan is queued for OCR rather than parsed —
    #: a routing outcome, never a silent empty document.
    ocr: OcrPort | None = None
    vlm: VlmPort | None = None
    #: The PII gate. Defaults to the deterministic rules, which are also the production
    #: fallback: a gate that stops working when a GPU is busy is not a gate.
    pii: PiiDetectorPort | None = None
    #: The consolidation drafter's model. Absent, `build_merge_draft` still produces a draft
    #: from the mechanical diff — marked incomplete, so a human writes the prose.
    generation: GenerationPort | None = None

    @classmethod
    def build(cls, settings: Settings | None = None) -> Deps:
        cfg = settings or get_settings()
        storage: StoragePort = (
            LocalStorageAdapter("/tmp/kb-storage")
            if cfg.env == "test"
            else S3StorageAdapter(cfg.storage)
        )
        ocr: OcrPort | None = None
        vlm: VlmPort | None = None
        generation: GenerationPort | None = None
        # No model node to call (test, or a dev machine with no GPU): the model-shaped
        # dependencies stay unset, so an activity that needs one fails saying so rather than
        # inventing a transcription or a draft.
        if not cfg.use_deterministic_models:
            from kb_ports.adapters.generation import OpenAiCompatibleGeneration
            from kb_ports.adapters.vlm import VisionAdapter

            vlm = VisionAdapter(cfg.models)
            generation = OpenAiCompatibleGeneration(cfg.models)
            if cfg.models.ocr_engine == "paddle":
                # Loading PaddleOCR costs hundreds of megabytes, so it is constructed lazily
                # by the adapter itself; this only decides which adapter the worker will use.
                # With `ocr_engine=vlm` it is not constructed at all — the vision model reads
                # every page, and a deployment that chose that should not be carrying an OCR
                # engine it never calls (ADR-0025).
                from kb_ports.adapters.ocr_paddle import PaddleOcrAdapter

                ocr = PaddleOcrAdapter()
        return cls(
            storage=storage,
            settings=cfg,
            ocr=ocr,
            vlm=vlm,
            pii=PatternPiiDetector(),
            generation=generation,
        )


_deps: Deps | None = None


def set_deps(deps: Deps) -> None:
    global _deps
    _deps = deps


def deps() -> Deps:
    global _deps
    if _deps is None:
        _deps = Deps.build()
    return _deps


# ------------------------------------------------------------------------------------ IDP


@activity.defn(name="run_idp")
async def run_idp(request: IngestRequest) -> IdpOutcome:
    """Parse the stored original into KBDoc and persist the result.

    The KBDoc is written to the derived bucket rather than returned: a large document's
    structure would otherwise land in the workflow history, which is durable storage tuned for
    small state, not documents.
    """
    context = deps()
    data = context.storage.get(request.upload.bucket, request.upload.key)
    result = process(data, request.upload.filename, ocr=context.ocr, vlm=context.vlm)

    if result.requires_ocr:
        log.info("ingest_requires_ocr", extra={"upload_id": request.upload_id})
        return IdpOutcome(
            requires_ocr=True,
            source_format=result.kbdoc.doc_meta.source_format,
            reason=result.reason,
        )

    kbdoc = result.kbdoc
    # Page images are part of the artefact for a scan: the reviewer compares text against
    # them, and they are content-addressed like everything else, so re-processing the same
    # scan reuses them.
    page_refs = [
        f"{context.settings.storage.bucket_derived}/"
        + context.storage.put(
            context.settings.storage.bucket_derived,
            io.BytesIO(page.image),
            suffix=".png",
            content_type="image/png",
        ).key
        for page in result.pages
    ]
    payload = kbdoc.model_dump_json(indent=None).encode("utf-8")
    stored = context.storage.put(
        context.settings.storage.bucket_derived,
        io.BytesIO(payload),
        suffix=".kbdoc.json",
        content_type="application/json",
    )
    return IdpOutcome(
        requires_ocr=False,
        kbdoc_ref=f"{context.settings.storage.bucket_derived}/{stored.key}",
        detected_title=kbdoc.doc_meta.detected_title,
        legal_number=kbdoc.doc_meta.legal_number,
        language=kbdoc.doc_meta.language,
        source_format=kbdoc.doc_meta.source_format,
        page_count=kbdoc.doc_meta.page_count,
        issued_date=kbdoc.doc_meta.issued_date,
        effective_from=kbdoc.doc_meta.effective_from,
        effective_evidence=kbdoc.doc_meta.effective_evidence,
        block_count=len(kbdoc.blocks),
        low_confidence_blocks=len(kbdoc.low_confidence_blocks),
        detected_refs=[
            DetectedRefOut(
                legal_number=ref.legal_number,
                ref_type=ref.ref_type_guess,
                block_id=ref.block_id,
                confidence=ref.confidence,
            )
            for ref in kbdoc.detected_refs
            if ref.legal_number
        ],
        warnings=list(kbdoc.idp_report.warnings),
        page_refs=page_refs,
        page_scores=[round(page.score.score, 3) for page in result.pages],
        escalated_pages=list(kbdoc.idp_report.escalated_pages),
        page_sizes=[(page.width, page.height) for page in result.pages],
    )


# ------------------------------------------------------------------------------- registry


@activity.defn(name="register_ingest")
async def register_ingest(request: IngestRequest, idp: IdpOutcome) -> RegisterOutcome:
    """Create or match the document, attach the version, record the outbox event."""
    from kb_schemas.kbdoc import DocMeta, KBDoc

    classification = request.classification
    with session_scope() as session:
        registry = RegistryService(session, audit=SqlAuditSink(session))
        spec = DocumentCreate(
            title=classification.title or idp.detected_title or request.upload.filename,
            doc_class=DocClass(classification.doc_class),
            category_path=classification.category_path,
            legal_number=classification.legal_number,
            department=classification.department,
            visibility=Visibility(classification.visibility) if classification.visibility else None,
            allowed_groups=classification.allowed_groups or None,
        )
        result = registry.ingest(
            KBDoc(
                doc_meta=DocMeta(
                    source_format=idp.source_format,
                    legal_number=idp.legal_number,
                    detected_title=idp.detected_title,
                )
            ),
            spec=spec,
            content_ref=f"{request.upload.bucket}/{request.upload.key}",
            content_hash=request.upload.content_hash,
            idp_report_ref=idp.kbdoc_ref,
            actor=request.actor,
        )

        if not result.duplicate_of_version_id:
            # The indexer consumes this once the version is published (M2). Written in the
            # same transaction as the rows it describes (INV-5).
            repo.enqueue(
                session,
                TOPIC_VERSION_CREATED,
                {
                    "document_id": str(result.document_id),
                    "version_id": str(result.version_id),
                    "kbdoc_ref": idp.kbdoc_ref,
                    "upload_id": request.upload_id,
                },
            )

        document = repo.get_document(session, result.document_id)
        steward = registry.steward_group_for(
            document.category_path if document else classification.category_path
        )
        return RegisterOutcome(
            document_id=str(result.document_id),
            version_id=str(result.version_id),
            created_document=result.created_document,
            matched_existing=result.matched_existing,
            duplicate=result.duplicate_of_version_id is not None,
            steward_group=steward,
        )


@activity.defn(name="scan_pii")
async def scan_pii(register: RegisterOutcome, idp: IdpOutcome) -> PiiOutcome:
    """Run the PII gate over the parsed document and record its verdict (INV-7).

    Fails closed in every direction. A detector that errors, a KBDoc that cannot be loaded, a
    model that times out — all leave the version `pending`, and a pending version cannot be
    published by any path.
    """
    context = deps()
    detector = context.pii or PatternPiiDetector()

    try:
        kbdoc = _load_kbdoc(context, idp.kbdoc_ref)
        scan = scan_document(detector, kbdoc)
        status = (
            PiiStatus.CLEAR
            if scan.is_clear
            else (PiiStatus.BLOCKED if scan.result.findings else PiiStatus.PENDING)
        )
        outcome = PiiOutcome(
            status=status.value,
            finding_count=len(scan.result.findings),
            kinds=dict(scan.summary()["kinds"]),
            blocked_blocks=list(scan.blocked_blocks),
            scan_complete=scan.result.scan_complete,
            detector=detector.info.name,
        )
    except Exception as exc:
        log.error("pii_scan_failed", extra={"error": str(exc), "kbdoc_ref": idp.kbdoc_ref})
        outcome = PiiOutcome(status=PiiStatus.PENDING.value, scan_complete=False)

    with session_scope() as session:
        registry = RegistryService(session, audit=SqlAuditSink(session))
        if outcome.status != PiiStatus.PENDING.value:
            registry.set_pii_status(
                uuid.UUID(register.version_id),
                PiiStatus(outcome.status),
                actor="pii-gate",
                detail={
                    "findings": outcome.finding_count,
                    "kinds": outcome.kinds,
                    "detector": outcome.detector,
                },
            )
    return outcome


def _load_kbdoc(context: Deps, kbdoc_ref: str | None) -> KBDoc:
    if not kbdoc_ref:
        raise ValueError("no parsed document to scan")
    bucket, _, key = kbdoc_ref.partition("/")
    return KBDoc.model_validate_json(context.storage.get(bucket, key))


@activity.defn(name="link_detected_refs")
async def link_detected_refs(document_id: str, refs: list[DetectedRefOut]) -> list[str]:
    """Create reference edges for targets we already hold; return the unresolved numbers."""
    with session_scope() as session:
        registry = RegistryService(session, audit=SqlAuditSink(session))
        _created, unresolved = registry.link_detected_refs(
            uuid.UUID(document_id),
            [
                DetectedRefIn(
                    legal_number=ref.legal_number,
                    ref_type=_safe_ref_type(ref.ref_type),
                    detected_by="idp",
                )
                for ref in refs
            ],
        )
        return unresolved


def _safe_ref_type(value: str) -> RefType:
    try:
        return RefType(value)
    except ValueError:
        # An unrecognized guess degrades to the weakest relationship rather than failing the
        # ingest: a wrong "cites" is harmless, a lost document is not.
        return RefType.CITES


@activity.defn(name="open_review_task")
async def open_review_task(request: ReviewTaskRequest) -> str:
    with session_scope() as session:
        registry = RegistryService(session, audit=SqlAuditSink(session))
        task = registry.open_review_task(
            uuid.UUID(request.version_id),
            ReviewTaskType(request.task_type),
            assignee_group=request.assignee_group,
            payload=_jsonable(request.payload),
        )
        return str(task.id)


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    """Round-trip through JSON so a payload with dates or UUIDs is storable as JSONB."""
    encoded: dict[str, Any] = json.loads(json.dumps(payload, default=str))
    return encoded


INGEST_ACTIVITIES: Sequence[Callable[..., Any]] = [
    run_idp,
    register_ingest,
    scan_pii,
    link_detected_refs,
    open_review_task,
]
