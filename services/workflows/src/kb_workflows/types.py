"""Workflow and activity payloads.

Temporal persists every argument and result in the workflow history, so these types are the
*durable* contract: renaming a field breaks workflows that are mid-flight, and a document in
review can sit mid-flight for weeks. Add fields with defaults; never repurpose one.

Document bytes are never a payload — only storage references (ADR-0005).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Classification:
    """What the uploader asserted about the document. Anything omitted falls back to the
    category default in the registry."""

    title: str
    doc_class: str
    category_path: str
    legal_number: str | None = None
    department: str | None = None
    visibility: str | None = None
    allowed_groups: list[str] = field(default_factory=list)


@dataclass
class UploadRef:
    """An original already in object storage, addressed by content hash."""

    bucket: str
    key: str
    content_hash: str
    filename: str
    size: int = 0


@dataclass
class IngestRequest:
    upload: UploadRef
    classification: Classification
    actor: str
    #: Correlates the workflow with the portal's upload record and the audit trail.
    upload_id: str = ""


@dataclass
class DetectedRefOut:
    legal_number: str
    ref_type: str
    #: Clause-precise addresses read from the citing text — "12", "12.2". Carried through the
    #: workflow so the edge is as precise as the citation was (ADR-0036).
    anchors: list[str] = field(default_factory=list)
    block_id: str | None = None
    confidence: float = 0.0


@dataclass
class IdpOutcome:
    requires_ocr: bool
    kbdoc_ref: str | None = None
    detected_title: str | None = None
    legal_number: str | None = None
    language: str = "unknown"
    source_format: str = "unknown"
    page_count: int = 0
    block_count: int = 0
    low_confidence_blocks: int = 0
    #: ISO dates the instrument states about itself. `effective_from` is what point-in-time
    #: retrieval, the lineage strip and expiry all read (ADR-0029).
    issued_date: str | None = None
    effective_from: str | None = None
    effective_evidence: str | None = None
    detected_refs: list[DetectedRefOut] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: str | None = None
    #: Scanned route only. Object-storage references to the rendered page images, in page
    #: order — the review editor shows these beside the recognized text.
    page_refs: list[str] = field(default_factory=list)
    #: Per-page confidence, so the reviewer's queue can show which pages need attention.
    page_scores: list[float] = field(default_factory=list)
    escalated_pages: list[int] = field(default_factory=list)
    #: Preprocessed page dimensions, so the editor can place block outlines on the image.
    page_sizes: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class RegisterOutcome:
    document_id: str
    version_id: str
    created_document: bool
    matched_existing: bool
    duplicate: bool
    steward_group: str | None = None


@dataclass
class PiiOutcome:
    """The gate's verdict on a version (INV-7).

    `status` is the value written to `document_versions.pii_status`. `pending` means the scan
    could not complete — which is not the same as clean, and leaves publication impossible.
    """

    status: str  # clear | blocked | pending
    finding_count: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    blocked_blocks: list[str] = field(default_factory=list)
    scan_complete: bool = True
    detector: str = ""


@dataclass
class ReviewTaskRequest:
    version_id: str
    task_type: str
    assignee_group: str | None
    #: `Any`, not `object`: Temporal's payload converter reconstructs dataclass fields from
    #: their type hints and cannot convert a value *to* `object`, so a non-empty payload fails
    #: to decode at the worker — which only shows up against a real Temporal, never in a test
    #: that calls the activity directly.
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestOutcome:
    """What the workflow returns, and what the portal shows on the upload screen."""

    status: str  # awaiting_review | awaiting_ocr | duplicate | needs_identity_review
    document_id: str | None = None
    version_id: str | None = None
    review_task_id: str | None = None
    detail: str = ""
