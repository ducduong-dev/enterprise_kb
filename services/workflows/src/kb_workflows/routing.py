"""Where an ingested document goes next.

Pure functions, deliberately: the branch decisions are the part of ingestion most likely to be
wrong, and they are far easier to test exhaustively here than through a workflow harness. The
workflow calls these; it does not re-implement them.
"""

from __future__ import annotations

from dataclasses import dataclass

from kb_schemas.enums import ReviewTaskType

from kb_workflows.types import IdpOutcome, PiiOutcome, RegisterOutcome, ReviewTaskRequest

#: Fallback queue when a category has no steward. Never silently unassigned: an unrouted task
#: is a document that quietly never gets published.
DEFAULT_STEWARD_GROUP = "dept/operations"

#: A PII override is not a steward's decision. It needs the `kb-pii-overrider` role, which
#: Compliance holds, so blocked documents queue there rather than with the document's owner.
PII_OVERRIDE_GROUP = "dept/compliance"


@dataclass(frozen=True, slots=True)
class Decision:
    status: str
    task: ReviewTaskRequest | None
    detail: str


def decide(idp: IdpOutcome, registered: RegisterOutcome, pii: PiiOutcome | None = None) -> Decision:
    """Choose the next step after IDP, registration and the PII gate.

    Every path ends in either a human task or an explicit terminal state. There is no branch
    that leaves a version with nobody responsible for it.
    """
    group = registered.steward_group or DEFAULT_STEWARD_GROUP

    if registered.duplicate:
        return Decision(
            status="duplicate",
            task=None,
            detail="identical content already exists as a version of this document",
        )

    if idp.requires_ocr:
        # M3 replaces this with the OCR chain. Until then the document is parked on a human
        # queue rather than dropped, so a scanned upload is visible instead of lost.
        return Decision(
            status="awaiting_ocr",
            task=ReviewTaskRequest(
                version_id=registered.version_id,
                task_type=ReviewTaskType.IDP_REVIEW.value,
                assignee_group=group,
                payload={
                    "requires_ocr": True,
                    "reason": idp.reason or "no text layer",
                    "source_format": idp.source_format,
                },
            ),
            detail="document has no text layer and needs the OCR chain",
        )

    if pii is not None and pii.status == "blocked":
        # The document is registered and its text is stored — it simply cannot be published.
        # Compliance decides whether the finding is real; nobody else can (INV-7).
        return Decision(
            status="pii_blocked",
            task=ReviewTaskRequest(
                version_id=registered.version_id,
                task_type=ReviewTaskType.PII_OVERRIDE.value,
                assignee_group=PII_OVERRIDE_GROUP,
                payload={
                    "kbdoc_ref": idp.kbdoc_ref,
                    "detected_title": idp.detected_title,
                    "pii_findings": pii.finding_count,
                    "pii_kinds": pii.kinds,
                    "pii_blocked_blocks": pii.blocked_blocks,
                    "detector": pii.detector,
                },
            ),
            detail="the PII gate blocked this document; an override requires Compliance",
        )

    if pii is not None and pii.status == "pending":
        # The gate could not finish. Not the same as clean: the document goes to review, and
        # publication stays impossible until the gate runs successfully.
        return Decision(
            status="pii_scan_incomplete",
            task=ReviewTaskRequest(
                version_id=registered.version_id,
                task_type=ReviewTaskType.PII_OVERRIDE.value,
                assignee_group=PII_OVERRIDE_GROUP,
                payload={
                    "kbdoc_ref": idp.kbdoc_ref,
                    "detected_title": idp.detected_title,
                    "scan_complete": False,
                    "reason": "the PII scan did not complete",
                },
            ),
            detail="the PII scan did not complete; the document cannot be published yet",
        )

    if registered.matched_existing:
        # The instrument is already in the registry, so this upload is a candidate revision.
        # M5's identity resolution and MergeFlow take over from this task.
        return Decision(
            status="needs_identity_review",
            task=ReviewTaskRequest(
                version_id=registered.version_id,
                task_type=ReviewTaskType.IDENTITY_REVIEW.value,
                assignee_group=group,
                payload={
                    "legal_number": idp.legal_number,
                    "document_id": registered.document_id,
                    "reason": "an existing document already claims this legal number",
                },
            ),
            detail="upload matches an existing instrument and needs a merge decision",
        )

    return Decision(
        status="awaiting_review",
        task=ReviewTaskRequest(
            version_id=registered.version_id,
            task_type=ReviewTaskType.IDP_REVIEW.value,
            assignee_group=group,
            payload={
                "kbdoc_ref": idp.kbdoc_ref,
                "detected_title": idp.detected_title,
                "language": idp.language,
                "page_count": idp.page_count,
                "block_count": idp.block_count,
                "low_confidence_blocks": idp.low_confidence_blocks,
                "detected_refs": [
                    {"legal_number": ref.legal_number, "ref_type": ref.ref_type}
                    for ref in idp.detected_refs
                ],
                "warnings": idp.warnings,
                # Scanned documents only: what the review editor renders beside the text.
                "page_refs": idp.page_refs,
                "page_scores": idp.page_scores,
                "page_sizes": [list(size) for size in idp.page_sizes],
                "escalated_pages": idp.escalated_pages,
                "scanned": bool(idp.page_refs),
            },
        ),
        detail=(
            "recognized and awaiting reviewer correction"
            if idp.page_refs
            else "parsed and awaiting reviewer confirmation"
        ),
    )
