"""ASGI entrypoint for kb-portal-api.

Portal backend: upload, review, merge, approvals, admin. Uploads are stored here and then
handed to Temporal as a reference (ADR-0005) — document bytes never enter a workflow history.
"""

from __future__ import annotations

import io
import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, Query, Response, UploadFile
from kb_authz.compile import compile_sql_documents
from kb_authz.filters import FilterBuilder
from kb_authz.principal import Principal, Role
from kb_clients.retrieval import HttpRetrievalClient, RetrievalClient
from kb_common.app import create_app
from kb_common.audit import AuditAction, AuditRecord, SqlAuditSink
from kb_common.config import get_settings
from kb_common.db import get_session
from kb_common.errors import NotFound, ValidationError
from kb_common.logging import get_logger
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.embedding_tei import TeiEmbeddingAdapter
from kb_ports.adapters.storage_local import LocalStorageAdapter
from kb_ports.adapters.storage_s3 import S3StorageAdapter
from kb_ports.models import EmbeddingPort
from kb_ports.storage import StoragePort, content_key, hash_bytes
from kb_registry import repository as repo
from kb_registry.schemas import DocumentOut, ReviewTaskOut
from kb_registry.service import RegistryService
from kb_schemas.api import CitationLookupRequest, Facets, RetrieveRequest, RetrieveResponse
from kb_schemas.enums import DocClass, Visibility
from kb_workflows.types import Classification, IngestRequest, UploadRef
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from kb_portal_api.auth import current_principal, require_role
from kb_portal_api.inspect import InspectService
from kb_portal_api.merge import MergeDecision, MergeService
from kb_portal_api.rates import RateScheduleIn, RateScheduleOut, RateScheduleService
from kb_portal_api.review import (
    BlockCorrection,
    ReviewDecision,
    ReviewService,
    reviewer_groups,
)
from kb_portal_api.review import Classification as ReviewClassification
from kb_portal_api.workflows import TemporalStarter, WorkflowStarter

log = get_logger(__name__)

app: FastAPI = create_app("kb-portal-api")

#: Uploads larger than this are rejected before they reach storage. The corpus's largest
#: scanned instruments are well under it; anything bigger is a mistake or an attack.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def storage() -> StoragePort:
    settings = get_settings()
    if settings.env == "test":
        return LocalStorageAdapter("/tmp/kb-storage")
    return S3StorageAdapter(settings.storage)


def starter() -> WorkflowStarter:
    return TemporalStarter()


def retrieval() -> RetrievalClient:
    return HttpRetrievalClient()


def embedder() -> EmbeddingPort:
    settings = get_settings()
    # Publishing a reviewed document embeds its chunks. The deterministic adapter is dev/CI
    # only and declares itself as such; production runs BGE-M3 on the GPU node.
    return HashedEmbeddingAdapter() if settings.use_deterministic_models else TeiEmbeddingAdapter()


def review_service(
    session: Session = Depends(get_session),
    store: StoragePort = Depends(storage),
    embed: EmbeddingPort = Depends(embedder),
) -> ReviewService:
    return ReviewService(
        session,
        storage=store,
        embedder=embed,
        audit=SqlAuditSink(session),
        derived_bucket=get_settings().storage.bucket_derived,
    )


def merge_service(
    session: Session = Depends(get_session),
    store: StoragePort = Depends(storage),
) -> MergeService:
    return MergeService(session, storage=store, audit=SqlAuditSink(session))


def inspect_service(
    session: Session = Depends(get_session),
    store: StoragePort = Depends(storage),
    embed: EmbeddingPort = Depends(embedder),
) -> InspectService:
    return InspectService(session, storage=store, embedder=embed, audit=SqlAuditSink(session))


def rate_schedule_service(
    session: Session = Depends(get_session),
    store: StoragePort = Depends(storage),
) -> RateScheduleService:
    return RateScheduleService(
        session,
        storage=store,
        registry=RegistryService(session, audit=SqlAuditSink(session)),
        derived_bucket=get_settings().storage.bucket_derived,
    )


def bearer_token(authorization: str = Header(default="")) -> str:
    """The caller's own token, forwarded to retrieval-api unchanged.

    portal-api never substitutes its own identity: the filter downstream must be built from
    the end user's groups, not from a service account's (INV-2).
    """
    _, _, token = authorization.partition(" ")
    return token


class UploadAccepted(BaseModel):
    upload_id: str
    workflow_id: str
    document_hash: str
    #: True when these exact bytes were already stored — the workflow still runs, and the
    #: registry decides whether this is a duplicate version.
    already_stored: bool


class UploadStatus(BaseModel):
    workflow_id: str
    status: str
    outcome: dict[str, Any] | None = None


@app.post("/v1/uploads", response_model=UploadAccepted, status_code=202)
async def create_upload(
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()] = "",
    doc_class: Annotated[str, Form()] = DocClass.OPERATIONAL.value,
    category_path: Annotated[str, Form()] = "",
    legal_number: Annotated[str, Form()] = "",
    department: Annotated[str, Form()] = "",
    visibility: Annotated[str, Form()] = "",
    allowed_groups: Annotated[str, Form()] = "",
    principal: Principal = Depends(current_principal),
    store: StoragePort = Depends(storage),
    workflows: WorkflowStarter = Depends(starter),
    session: Session = Depends(get_session),
) -> UploadAccepted:
    """Accept a document and start `IngestWorkflow`.

    The original is stored *before* the workflow starts, so a Temporal outage loses nothing
    but the trigger — the bytes are already durable and content-addressed, and re-submitting
    the same file is a no-op rather than a second copy.
    """
    if not category_path:
        raise ValidationError("category_path is required")
    if repo.get_category(session, category_path) is None:
        raise ValidationError("unknown category", category_path=category_path)
    _validate_classification(doc_class, visibility)

    data = await file.read()
    if not data:
        raise ValidationError("uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError("file is too large", size=len(data), limit=MAX_UPLOAD_BYTES)

    bucket = get_settings().storage.bucket_originals
    suffix = _suffix(file.filename)
    # Content addressing makes re-upload observable: same bytes, same key. Worth reporting —
    # a steward re-uploading a file usually wants to know it is already in the system.
    already_stored = store.exists(bucket, content_key("originals", hash_bytes(data), suffix))
    stored = store.put(bucket, io.BytesIO(data), suffix=suffix, content_type=file.content_type)

    upload_id = uuid.uuid4().hex
    workflow_id = f"ingest-{stored.content_hash[:16]}-{upload_id[:8]}"

    request = IngestRequest(
        upload=UploadRef(
            bucket=bucket,
            key=stored.key,
            content_hash=stored.content_hash,
            filename=file.filename or "upload",
            size=stored.size,
        ),
        classification=Classification(
            title=title,
            doc_class=doc_class,
            category_path=category_path,
            legal_number=legal_number or None,
            department=department or None,
            visibility=visibility or None,
            allowed_groups=[g.strip() for g in allowed_groups.split(",") if g.strip()],
        ),
        actor=principal.audit_actor,
        upload_id=upload_id,
    )
    await workflows.start_ingest(request, workflow_id=workflow_id)

    SqlAuditSink(session).write(
        AuditRecord(
            action=AuditAction.UPLOAD,
            actor=principal.audit_actor,
            on_behalf_of=principal.audit_on_behalf_of,
            object_ref={"upload_id": upload_id, "content_hash": stored.content_hash},
            detail={
                "filename": file.filename,
                "size": stored.size,
                "category_path": category_path,
                "workflow_id": workflow_id,
            },
        )
    )
    log.info(
        "upload_accepted",
        extra={
            "upload_id": upload_id,
            "workflow_id": workflow_id,
            "content_hash": stored.content_hash,
            "size": stored.size,
        },
    )
    return UploadAccepted(
        upload_id=upload_id,
        workflow_id=workflow_id,
        document_hash=stored.content_hash,
        already_stored=already_stored,
    )


@app.get("/v1/uploads/{workflow_id}", response_model=UploadStatus)
async def upload_status(
    workflow_id: str,
    _principal: Principal = Depends(current_principal),
    workflows: WorkflowStarter = Depends(starter),
) -> UploadStatus:
    status = await workflows.status(workflow_id)
    return UploadStatus(
        workflow_id=workflow_id, status=status, outcome=await workflows.result(workflow_id)
    )


class CategoryOut(BaseModel):
    path: str
    label: str
    default_visibility: str
    default_allowed_groups: list[str]
    steward_group: str | None


@app.get("/v1/categories", response_model=list[CategoryOut])
def list_categories(
    prefix: str | None = None,
    _principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[CategoryOut]:
    """The category tree drives the upload form: it supplies each document's ACL defaults."""
    return [
        CategoryOut.model_validate(row, from_attributes=True)
        for row in repo.list_categories(session, prefix)
    ]


class SearchRequest(BaseModel):
    """The search screen's request. Facets narrow; there is no field that widens (INV-2)."""

    model_config = {"extra": "forbid"}

    query: str
    top_k: int = 10
    category: str | None = None
    department: str | None = None
    doc_class: str | None = None
    expand_graph: bool = False


@app.post("/v1/search", response_model=RetrieveResponse)
def search(
    request: SearchRequest,
    _principal: Principal = Depends(current_principal),
    token: str = Depends(bearer_token),
    client: RetrievalClient = Depends(retrieval),
) -> RetrieveResponse:
    """Search for the portal UI.

    A thin pass-through to the funnel — deliberately. Any ranking, filtering or trimming done
    here would be a second retrieval implementation, and the one guarantee this platform makes
    is that there is only one (INV-1).
    """
    facets = Facets(
        category=request.category,
        department=request.department,
        doc_class=request.doc_class,
    )
    return client.retrieve(
        token,
        RetrieveRequest(
            query=request.query,
            top_k=request.top_k,
            facets=facets,
            expand_graph=request.expand_graph,
        ),
    )


@app.post("/v1/citation-lookup", response_model=RetrieveResponse)
def citation_lookup(
    request: CitationLookupRequest,
    _principal: Principal = Depends(current_principal),
    token: str = Depends(bearer_token),
    client: RetrievalClient = Depends(retrieval),
) -> RetrieveResponse:
    """Follow a citation from an answer back to the article it names."""
    return client.citation_lookup(token, request)


@app.get("/v1/documents", response_model=list[DocumentOut])
def list_documents(
    category: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    _principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[DocumentOut]:
    """Registry listing for the portal's document table.

    This is metadata for stewards, not a content read path: document *text* is only ever
    served through retrieval-api, which applies the mandatory filter (INV-1/INV-2).
    """
    # Metadata is still a disclosure: a title and a legal number tell a reader that an
    # instrument exists and roughly what it covers. The listing therefore carries the same
    # visibility predicate the content path does, compiled in one place (INV-2).
    acl = compile_sql_documents(FilterBuilder().base(_principal), alias="documents")
    rows = repo.list_documents(
        session, category_prefix=category, status=status, limit=limit, acl=acl
    )
    return [DocumentOut.model_validate(row) for row in rows]


@app.get("/v1/review-tasks", response_model=list[ReviewTaskOut])
def list_review_tasks(
    state: str = "open",
    task_type: str | None = None,
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[ReviewTaskOut]:
    """A reviewer sees the queues of the groups they belong to, and no others."""
    groups = sorted((principal.effective_user or principal).groups)
    rows = repo.list_review_tasks(session, assignee_groups=groups, state=state, task_type=task_type)
    return [ReviewTaskOut.model_validate(row) for row in rows]


class CorrectionIn(BaseModel):
    model_config = {"extra": "forbid"}

    block_id: str
    text: str | None = None
    table_rows: list[list[str]] | None = None
    drop: bool = False


class ClassificationIn(BaseModel):
    model_config = {"extra": "forbid"}

    title: str | None = None
    category_path: str | None = None
    visibility: str | None = None
    allowed_groups: list[str] | None = None
    department: str | None = None
    #: ISO date, or "" to clear a detection the reviewer disagrees with (ADR-0029).
    effective_from: str | None = None


class ReviewDecisionIn(BaseModel):
    model_config = {"extra": "forbid"}

    decision: str
    corrections: list[CorrectionIn] = []
    classification: ClassificationIn | None = None
    #: [(legal_number, ref_type)] the reviewer confirms. Unlisted proposals stay unconfirmed.
    confirmed_refs: list[tuple[str, str]] = []
    note: str = ""


class ReviewDecisionOut(BaseModel):
    task_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    corrected: bool
    decision: str
    published: bool
    chunks_written: int = 0


@app.get("/v1/review-tasks/{task_id}")
def get_review_task(
    task_id: uuid.UUID,
    principal: Principal = Depends(current_principal),
    reviews: ReviewService = Depends(review_service),
) -> dict[str, Any]:
    """Everything the review editor needs: the document, the machine's reading, the pages."""
    return reviews.task(task_id, reviewer_groups=reviewer_groups(principal))


@app.get("/v1/review-tasks/{task_id}/pages/{page}")
def get_review_page(
    task_id: uuid.UUID,
    page: int,
    principal: Principal = Depends(current_principal),
    reviews: ReviewService = Depends(review_service),
) -> Response:
    """A page image, proxied.

    Deliberately not a presigned object-store URL: access follows the *task*, so a link
    cannot outlive the reviewer's assignment or be forwarded to someone without it.
    """
    image = reviews.page_image(task_id, page, reviewer_groups=reviewer_groups(principal))
    return Response(content=image, media_type="image/png")


@app.post("/v1/review-tasks/{task_id}/decision", response_model=ReviewDecisionOut)
def submit_review(
    task_id: uuid.UUID,
    body: ReviewDecisionIn,
    principal: Principal = Depends(current_principal),
    reviews: ReviewService = Depends(review_service),
) -> ReviewDecisionOut:
    """Approve (with corrections) or reject.

    Approval publishes through the same transaction as every other publish — the PII gate
    (INV-7) and the four-eyes rule for regulated classes (INV-8) apply here exactly as they
    do elsewhere, and a refusal from either is returned as such rather than swallowed.
    """
    outcome = reviews.submit(
        task_id,
        ReviewDecision(
            decision=body.decision,
            reviewer=principal.audit_actor,
            corrections=[
                BlockCorrection(
                    block_id=item.block_id,
                    text=item.text,
                    table_rows=item.table_rows,
                    drop=item.drop,
                )
                for item in body.corrections
            ],
            classification=(
                ReviewClassification(**body.classification.model_dump())
                if body.classification
                else None
            ),
            confirmed_refs=[(number, ref_type) for number, ref_type in body.confirmed_refs],
            note=body.note,
        ),
        reviewer_groups=reviewer_groups(principal),
    )
    return ReviewDecisionOut(
        task_id=outcome.task_id,
        document_id=outcome.document_id,
        version_id=outcome.version_id,
        corrected=outcome.corrected,
        decision=outcome.decision,
        published=outcome.published is not None,
        chunks_written=outcome.published.chunks_written if outcome.published else 0,
    )


class MergeDecisionIn(BaseModel):
    model_config = {"extra": "forbid"}

    decision: str
    note: str = ""
    #: The draft the approver had on screen. Sent back so an approval given for text that has
    #: since been redrafted is refused rather than counted (INV-8).
    draft_ref: str = ""


class MergeDecisionOut(BaseModel):
    task_id: uuid.UUID
    decision: str
    required: int
    received: int
    satisfied: bool
    #: False when the workflow could not be told. The decision is stored either way; the UI
    #: says so rather than showing a merge that looks approved and never publishes.
    signalled: bool
    detail: str


@app.get("/v1/merge-tasks/{task_id}")
def get_merge_screen(
    task_id: uuid.UUID,
    principal: Principal = Depends(current_principal),
    merges: MergeService = Depends(merge_service),
) -> dict[str, Any]:
    """The three panes: current canonical, incoming version, drafted consolidation."""
    return merges.screen(task_id, reviewer_groups=reviewer_groups(principal))


@app.post("/v1/merge-tasks/{task_id}/decision", response_model=MergeDecisionOut)
async def submit_merge_decision(
    task_id: uuid.UUID,
    body: MergeDecisionIn,
    principal: Principal = Depends(current_principal),
    merges: MergeService = Depends(merge_service),
    workflows: WorkflowStarter = Depends(starter),
) -> MergeDecisionOut:
    """One approval or one rejection.

    The decision is recorded here — which is what lets a duplicate or self-approval be refused
    with a reason — and then delivered to the workflow that publishes. Recording first means a
    Temporal outage costs the delivery, never the decision.
    """
    groups = reviewer_groups(principal)
    outcome = merges.decide(
        task_id,
        MergeDecision(
            decision=body.decision,
            approver=principal.audit_actor,
            note=body.note,
            draft_ref=body.draft_ref,
        ),
        reviewer_groups=groups,
    )

    signalled = False
    workflow_id = merges.workflow_id(task_id, reviewer_groups=groups)
    if workflow_id:
        signalled = await workflows.signal(
            workflow_id,
            "approve" if outcome.decision == "approve" else "reject",
            approver=principal.audit_actor,
            **({"note": body.note} if outcome.decision == "approve" else {"reason": body.note}),
            **({"draft_ref": body.draft_ref} if outcome.decision == "approve" else {}),
        )

    return MergeDecisionOut(
        task_id=outcome.task_id,
        decision=outcome.decision,
        required=outcome.required,
        received=outcome.received,
        satisfied=outcome.satisfied,
        signalled=signalled,
        detail=outcome.detail,
    )


@app.post("/v1/rate-schedules", response_model=RateScheduleOut, status_code=202)
def create_rate_schedule(
    body: RateScheduleIn,
    principal: Principal = Depends(current_principal),
    rates: RateScheduleService = Depends(rate_schedule_service),
) -> RateScheduleOut:
    """Publish a fee or interest-rate schedule as structured items (M8).

    202, not 201: this creates a version and a review task. Rates are `customer_facing`, the
    class that can never publish itself (INV-8) — the public is told what a human approved.
    """
    return rates.create(body, actor=principal.audit_actor)


class RechunkOut(BaseModel):
    document_id: uuid.UUID
    version_id: uuid.UUID
    chunks_before: int
    chunks_written: int
    edges_rebuilt: int


class EdgeDecisionIn(BaseModel):
    model_config = {"extra": "forbid"}

    other_document_id: uuid.UUID
    ref_type: str
    #: True confirms a machine-detected reference; false removes one that is wrong.
    confirm: bool


@app.get("/v1/documents/{document_id}/inspect")
def inspect_document(
    document_id: uuid.UUID,
    principal: Principal = Depends(current_principal),
    inspector: InspectService = Depends(inspect_service),
) -> dict[str, Any]:
    """What was indexed and what it is connected to: chunks, edges, versions, lineage.

    Read as the caller, not as the service: chunk text is document text, so the same filter
    retrieval applies decides what comes back (INV-2), and an edge whose target the caller may
    not read is counted rather than named.
    """
    return inspector.inspect(document_id, principal).as_dict()


@app.get("/v1/documents/{document_id}/chunks")
def list_document_chunks(
    document_id: uuid.UUID,
    include_tombstoned: bool = False,
    principal: Principal = Depends(current_principal),
    inspector: InspectService = Depends(inspect_service),
) -> list[dict[str, Any]]:
    """Just the chunks, for a steward comparing boundaries against the source."""
    return [
        chunk.as_dict()
        for chunk in inspector.chunks(document_id, principal, include_tombstoned=include_tombstoned)
    ]


@app.post("/v1/documents/{document_id}/rechunk", response_model=RechunkOut)
def rechunk_document(
    document_id: uuid.UUID,
    principal: Principal = Depends(require_role(Role.STEWARD)),
    inspector: InspectService = Depends(inspect_service),
) -> RechunkOut:
    """Rebuild the canonical version's chunks and embeddings, in one transaction (INV-5).

    A maintenance act on derived data, not a publication: the text, the version and the
    approval behind it are unchanged, so this asks for the steward role rather than four eyes.
    Text that is *wrong* is fixed by correcting a version and republishing (ADR-0012).
    """
    result = inspector.rechunk(document_id, actor=principal.audit_actor)
    return RechunkOut(
        document_id=result.document_id,
        version_id=result.version_id,
        chunks_before=result.chunks_before,
        chunks_written=result.chunks_written,
        edges_rebuilt=result.edges_rebuilt,
    )


@app.post("/v1/documents/{document_id}/edges")
def decide_edge(
    document_id: uuid.UUID,
    body: EdgeDecisionIn,
    principal: Principal = Depends(require_role(Role.STEWARD)),
    inspector: InspectService = Depends(inspect_service),
) -> dict[str, Any]:
    """Confirm a detected reference, or remove one that is wrong.

    Edges drive supersession warnings, graph expansion and the impact tasks consolidation
    opens, so an unchecked guess is a task nobody expected — or a policy owner nobody told.
    """
    return inspector.set_edge_confirmation(
        document_id,
        body.other_document_id,
        body.ref_type,
        confirm=body.confirm,
        actor=principal.audit_actor,
    )


class ExpiryProposalIn(BaseModel):
    model_config = {"extra": "forbid"}

    #: The last day the instrument applied. Inclusive, matching the effectivity predicate: a
    #: rule replaced from 01/01 has an `effective_to` of 31/12.
    effective_to: date
    #: Why. Required, and stored on the row — the sentence it was read from where there is one,
    #: the steward's reasoning where there is not. An expiry with no stated reason cannot be
    #: reviewed years later, which is when it will be (ADR-0029/0030).
    evidence: str = Field(min_length=10, max_length=2000)
    #: Clause anchors ("12", "12.2") for a partial expiry. Recorded but not applied until M9c.
    anchors: list[str] = Field(default_factory=list, max_length=64)


class ExpiryDecisionIn(BaseModel):
    model_config = {"extra": "forbid"}

    #: True confirms the proposal and takes the document out of every default answer; false
    #: withdraws it and puts the document back.
    confirm: bool
    #: Required to withdraw. Same reason the proposal needs one.
    reason: str = Field(default="", max_length=2000)


@app.post("/v1/documents/{document_id}/expiry")
def propose_expiry(
    document_id: uuid.UUID,
    body: ExpiryProposalIn,
    principal: Principal = Depends(require_role(Role.STEWARD)),
    inspector: InspectService = Depends(inspect_service),
) -> dict[str, Any]:
    """Mark a document as having stopped applying — as a proposal, never as a fact.

    Nothing changes for any reader until it is confirmed. That separation is the whole design:
    a detector and a steward write the same kind of row, and only a decision serves it.
    """
    return inspector.mark_expired(
        document_id,
        principal,
        effective_to=body.effective_to,
        evidence=body.evidence,
        anchors=body.anchors,
    )


@app.post("/v1/documents/{document_id}/expiry/{row_id}")
def decide_expiry(
    document_id: uuid.UUID,
    row_id: uuid.UUID,
    body: ExpiryDecisionIn,
    principal: Principal = Depends(require_role(Role.STEWARD)),
    inspector: InspectService = Depends(inspect_service),
) -> dict[str, Any]:
    """Confirm a proposed expiry, or withdraw one.

    Confirming removes the document from every default answer from its date, so the guards are
    the ledger's rather than this route's: a regulated document needs a second pair of eyes
    (INV-8), and the dates land on the chunks inside this transaction so serving never waits
    for the nightly sweep (ADR-0031).

    The steward role rather than the approver role, with four eyes enforced *within* it: the
    people who know an instrument is finished are the ones who own it. What INV-8 requires is
    that no single person does it alone on regulated content, not that a different job title
    does it.
    """
    return inspector.decide_expiry(
        document_id,
        row_id,
        principal,
        confirm=body.confirm,
        reason=body.reason,
    )


class PiiOverrideIn(BaseModel):
    model_config = {"extra": "forbid"}

    #: In the overrider's own words. The gate found something; this is why it may publish
    #: anyway, and it is what an auditor reads years later.
    justification: str


class PiiOverrideOut(BaseModel):
    version_id: uuid.UUID
    pii_status: str


@app.post("/v1/versions/{version_id}/pii-override", response_model=PiiOverrideOut)
def override_pii(
    version_id: uuid.UUID,
    body: PiiOverrideIn,
    principal: Principal = Depends(require_role(Role.PII_OVERRIDER)),
    session: Session = Depends(get_session),
) -> PiiOverrideOut:
    """Override a blocked PII verdict (INV-7).

    Three things are required and none is optional: the `kb-pii-overrider` role, a written
    justification, and an audit record naming who decided. The role check is here; the
    justification and the audit record are enforced in the registry, so no other caller can
    record an override without them either.
    """
    registry = RegistryService(session, audit=SqlAuditSink(session))
    version = registry.override_pii(
        version_id, actor=principal.audit_actor, justification=body.justification
    )
    return PiiOverrideOut(version_id=version.id, pii_status=version.pii_status)


@app.get("/v1/documents/{document_id}", response_model=DocumentOut)
def get_document(
    document_id: uuid.UUID,
    _principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> DocumentOut:
    row = repo.get_document(session, document_id)
    if row is None:
        raise NotFound("document not found", document_id=str(document_id))
    return DocumentOut.model_validate(row)


def _validate_classification(doc_class: str, visibility: str) -> None:
    try:
        DocClass(doc_class)
    except ValueError as exc:
        raise ValidationError(
            "unknown doc_class", doc_class=doc_class, allowed=[c.value for c in DocClass]
        ) from exc
    if visibility:
        try:
            Visibility(visibility)
        except ValueError as exc:
            raise ValidationError("unknown visibility", visibility=visibility) from exc


def _suffix(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[1].lower()


def run() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
