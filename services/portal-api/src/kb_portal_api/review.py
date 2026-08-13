"""The review step: what a human does to a machine-read document before it is published.

A scanned document arrives *provisional*. OCR misread some lines, the classification is a
guess, and the references it found are proposals. Review is where each of those becomes a
decision with a name attached to it.

Three properties this module is built around:

* **Corrections create a new version.** Versions are immutable (INV-9), so a reviewer's edits
  cannot overwrite what the machine read. The OCR output stays as the record of what the
  scanner produced, and the corrected text becomes a new version whose author is the reviewer.
  That is also what makes the four-eyes rule meaningful on regulated documents: the person who
  corrected the text is not the person who may approve it (INV-8).
* **Approval publishes through the same transaction as everything else.** No shortcut, no
  second publish path — the guards in `PublishService` apply identically (INV-5/7/8).
* **Rejection is a decision too**, recorded with its reason, not a silent return to the queue.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from kb_common.audit import AuditAction, AuditRecord, AuditSink
from kb_common.errors import AuthzError, Conflict, NotFound, ValidationError
from kb_common.logging import get_logger
from kb_ports.models import EmbeddingPort
from kb_ports.storage import StoragePort
from kb_registry import repository as repo
from kb_registry.publish import Approval, PublishResult, PublishService
from kb_registry.schemas import DetectedRefIn, DocumentUpdate, VersionCreate
from kb_registry.service import RegistryService
from kb_schemas.enums import RefType, ReviewTaskState, SourceType, Visibility
from kb_schemas.kbdoc import KBDoc
from kb_schemas.orm import ReviewTaskRow
from sqlalchemy.orm import Session

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BlockCorrection:
    block_id: str
    text: str | None = None
    table_rows: list[list[str]] | None = None
    #: Set when the reviewer deletes a block the scanner invented out of speckle.
    drop: bool = False


@dataclass(frozen=True, slots=True)
class Classification:
    category_path: str | None = None
    visibility: str | None = None
    allowed_groups: list[str] | None = None
    department: str | None = None
    title: str | None = None
    #: What the reviewer says about when this text takes effect, overriding what the parser
    #: read from the document (ADR-0029). ISO date, or "" to clear a wrong detection.
    effective_from: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    decision: str  # approve | reject
    reviewer: str
    corrections: list[BlockCorrection]
    classification: Classification | None = None
    #: Reference proposals the reviewer confirmed. Anything not listed stays unconfirmed.
    confirmed_refs: list[tuple[str, str]] = ()  # type: ignore[assignment]
    note: str = ""


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    task_id: uuid.UUID
    document_id: uuid.UUID
    #: The version that now holds the reviewed text — a new one if anything was corrected.
    version_id: uuid.UUID
    corrected: bool
    published: PublishResult | None
    decision: str


class ReviewService:
    def __init__(
        self,
        session: Session,
        *,
        storage: StoragePort,
        embedder: EmbeddingPort,
        audit: AuditSink | None = None,
        derived_bucket: str = "kb-derived",
    ) -> None:
        self._session = session
        self._storage = storage
        self._embedder = embedder
        self._audit = audit
        self._bucket = derived_bucket
        self._registry = RegistryService(session, audit=audit)

    # -------------------------------------------------------------------------- reading

    def task(self, task_id: uuid.UUID, *, reviewer_groups: set[str]) -> dict[str, Any]:
        """The review screen's payload: the task, its document, and the machine's output.

        A reviewer sees only the queues of the groups they belong to. That check is here
        rather than in the UI, which is decoration.
        """
        row = repo.get_review_task(self._session, task_id)
        if row is None:
            raise NotFound("review task not found", task_id=str(task_id))
        if row.assignee_group and row.assignee_group not in reviewer_groups:
            # Not "forbidden": a reviewer has no business learning that another team's queue
            # holds a task about a document they cannot see.
            raise NotFound("review task not found", task_id=str(task_id))

        version = repo.get_version(self._session, row.version_id)
        if version is None:  # pragma: no cover - referential integrity guarantees this
            raise NotFound("version not found", version_id=str(row.version_id))
        document = repo.get_document(self._session, version.document_id)
        if document is None:  # pragma: no cover
            raise NotFound("document not found")

        payload = dict(row.payload or {})
        kbdoc = self._load_kbdoc(payload.get("kbdoc_ref"))

        return {
            "task": {
                "id": row.id,
                "task_type": row.task_type,
                "state": row.state,
                "assignee_group": row.assignee_group,
                "payload": payload,
            },
            "document": {
                "id": document.id,
                "title": document.title,
                "legal_number": document.legal_number,
                "doc_class": document.doc_class,
                "category_path": document.category_path,
                "department": document.department,
                "visibility": document.visibility,
                "allowed_groups": list(document.allowed_groups),
                "status": document.status,
            },
            "version": {
                "id": version.id,
                "author": version.author,
                "pii_status": version.pii_status,
                "source_type": version.source_type,
                "created_at": version.created_at,
                # What the parser read about effectivity, and the sentence it read it from.
                # The reviewer confirms or corrects it before this text is published, because
                # an unstated effective date silently means "always in force" (ADR-0029).
                "effective_from": (
                    version.effective_from.isoformat() if version.effective_from else None
                ),
                "effective_evidence": (
                    kbdoc.doc_meta.effective_evidence if kbdoc is not None else None
                ),
            },
            "kbdoc": kbdoc.model_dump(mode="json") if kbdoc else None,
            # Page images the reviewer compares the text against, in page order.
            "page_refs": list(payload.get("page_refs") or []),
            "escalated_pages": list(payload.get("escalated_pages") or []),
            "page_scores": list(payload.get("page_scores") or []),
            # Dimensions of the preprocessed page images, so the editor can draw a block's
            # box over the page at the right place and scale.
            "page_sizes": [
                {"width": size[0], "height": size[1]} for size in (payload.get("page_sizes") or [])
            ],
        }

    def page_image(self, task_id: uuid.UUID, page: int, *, reviewer_groups: set[str]) -> bytes:
        """Fetch one page image for the editor. Access is the task's, not the object store's."""
        detail = self.task(task_id, reviewer_groups=reviewer_groups)
        refs: list[str] = detail["page_refs"]
        if page < 1 or page > len(refs):
            raise NotFound("page not found", page=page)
        bucket, _, key = refs[page - 1].partition("/")
        return self._storage.get(bucket, key)

    # -------------------------------------------------------------------------- deciding

    def submit(
        self, task_id: uuid.UUID, decision: ReviewDecision, *, reviewer_groups: set[str]
    ) -> ReviewOutcome:
        detail = self.task(task_id, reviewer_groups=reviewer_groups)
        row = repo.get_review_task(self._session, task_id)
        assert row is not None  # task() already raised if missing
        if row.state == ReviewTaskState.DECIDED.value:
            raise Conflict("this task has already been decided", task_id=str(task_id))
        if decision.decision not in ("approve", "reject"):
            raise ValidationError("decision must be approve or reject")

        document_id = uuid.UUID(str(detail["document"]["id"]))
        version_id = uuid.UUID(str(detail["version"]["id"]))
        kbdoc = self._load_kbdoc((row.payload or {}).get("kbdoc_ref"))

        if decision.decision == "reject":
            self._close(row, decision, outcome="rejected")
            log.info(
                "review_rejected",
                extra={"task_id": str(task_id), "reviewer": decision.reviewer},
            )
            return ReviewOutcome(
                task_id=task_id,
                document_id=document_id,
                version_id=version_id,
                corrected=False,
                published=None,
                decision="reject",
            )

        if decision.classification is not None:
            self._apply_classification(document_id, decision.classification, decision.reviewer)
        if decision.confirmed_refs:
            self._confirm_refs(document_id, decision.confirmed_refs)

        corrected_version_id = version_id
        corrected = False
        if decision.corrections and kbdoc is not None:
            kbdoc = apply_corrections(kbdoc, decision.corrections)
            corrected_version_id = self._store_corrected_version(
                document_id, version_id, kbdoc, decision.reviewer
            )
            corrected = True

        # After the correction, so the date lands on the version that is actually published:
        # a correction creates a new version, and effectivity is a property of a text (INV-9).
        if (
            decision.classification is not None
            and decision.classification.effective_from is not None
        ):
            self._registry.set_effective_from(
                corrected_version_id,
                date.fromisoformat(decision.classification.effective_from)
                if decision.classification.effective_from
                else None,
                actor=decision.reviewer,
            )

        published = self._publish(corrected_version_id, kbdoc, decision)
        self._close(row, decision, outcome="approved")

        log.info(
            "review_approved",
            extra={
                "task_id": str(task_id),
                "reviewer": decision.reviewer,
                "corrections": len(decision.corrections),
                "corrected_version": str(corrected_version_id),
                "chunks": published.chunks_written if published else 0,
            },
        )
        return ReviewOutcome(
            task_id=task_id,
            document_id=document_id,
            version_id=corrected_version_id,
            corrected=corrected,
            published=published,
            decision="approve",
        )

    # ------------------------------------------------------------------------ internals

    def _publish(
        self, version_id: uuid.UUID, kbdoc: KBDoc | None, decision: ReviewDecision
    ) -> PublishResult:
        if kbdoc is None:
            raise Conflict("no parsed document to publish", version_id=str(version_id))
        publisher = PublishService(self._session, embedder=self._embedder, audit=self._audit)
        prepared = publisher.prepare(kbdoc, legal_number=kbdoc.doc_meta.legal_number)
        # The reviewer is the approver. For a regulated class this is refused when they are
        # also the author — which, after corrections, they are (INV-8). That refusal is the
        # four-eyes rule working, not a bug: a second reviewer approves the corrected text.
        return publisher.publish(
            version_id,
            prepared,
            actor=decision.reviewer,
            approval=Approval(approver=decision.reviewer, note=decision.note),
        )

    def _store_corrected_version(
        self, document_id: uuid.UUID, source_version: uuid.UUID, kbdoc: KBDoc, reviewer: str
    ) -> uuid.UUID:
        """Corrections become a new version; the machine's output stays immutable (INV-9)."""
        payload = kbdoc.model_dump_json().encode("utf-8")
        stored = self._storage.put(
            self._bucket,
            io.BytesIO(payload),
            suffix=".kbdoc.json",
            content_type="application/json",
        )
        original = repo.get_version(self._session, source_version)
        version = self._registry.create_version(
            VersionCreate(
                document_id=document_id,
                content_ref=original.content_ref if original else "",
                content_hash=stored.content_hash,
                source_type=SourceType.PORTAL_EDIT,
                author=reviewer,
                change_summary="Reviewer corrections to the recognized text",
                idp_report_ref=f"{self._bucket}/{stored.key}",
            ),
            actor=reviewer,
        )
        # The corrected text inherits the PII state of what it was corrected from: a reviewer
        # fixing OCR errors has not re-run the PII gate, and must not be able to bypass it by
        # producing a fresh "pending → cleared" version (INV-7).
        if original is not None:
            version.pii_status = original.pii_status
            self._session.flush()
        return version.id

    def _apply_classification(
        self, document_id: uuid.UUID, classification: Classification, reviewer: str
    ) -> None:
        update = DocumentUpdate(
            title=classification.title,
            department=classification.department,
            category_path=classification.category_path,
            visibility=Visibility(classification.visibility) if classification.visibility else None,
            allowed_groups=classification.allowed_groups,
        )
        self._registry.update_document(document_id, update, actor=reviewer)

    def _confirm_refs(self, document_id: uuid.UUID, refs: list[tuple[str, str]]) -> None:
        """Confirmation is what makes an edge authoritative for consolidation (M5)."""
        created, unresolved = self._registry.link_detected_refs(
            document_id,
            [
                DetectedRefIn(legal_number=number, ref_type=RefType(ref_type), detected_by="idp")
                for number, ref_type in refs
            ],
        )
        confirmed_numbers = {number for number, _ in refs}
        for edge in repo.list_edges_from(self._session, document_id):
            target = repo.get_document(self._session, edge.dst_document_id)
            if target is not None and target.legal_number in confirmed_numbers:
                edge.confirmed_by = "review"
        self._session.flush()
        if unresolved:
            log.info(
                "references_unresolved_at_review",
                extra={"document_id": str(document_id), "unresolved": unresolved},
            )
        log.info(
            "references_confirmed",
            extra={"document_id": str(document_id), "created": len(created)},
        )

    def _close(self, row: ReviewTaskRow, decision: ReviewDecision, *, outcome: str) -> None:
        row.state = ReviewTaskState.DECIDED.value
        row.decision = outcome
        row.decided_by = decision.reviewer
        row.decided_at = datetime.now(UTC)
        self._session.flush()
        if self._audit is not None:
            self._audit.write(
                AuditRecord(
                    action=AuditAction.REVIEW_DECISION,
                    actor=decision.reviewer,
                    object_ref={"task_id": str(row.id), "version_id": str(row.version_id)},
                    detail={
                        "decision": outcome,
                        "corrections": len(decision.corrections),
                        "note": decision.note,
                        "task_type": row.task_type,
                    },
                )
            )

    def _load_kbdoc(self, ref: str | None) -> KBDoc | None:
        if not ref:
            return None
        bucket, _, key = str(ref).partition("/")
        try:
            return KBDoc.model_validate_json(self._storage.get(bucket, key))
        except NotFound:
            log.warning("kbdoc_missing", extra={"ref": ref})
            return None


def apply_corrections(kbdoc: KBDoc, corrections: list[BlockCorrection]) -> KBDoc:
    """Return a copy of the document with the reviewer's edits applied.

    A corrected block is marked `engine="human"` with full confidence: downstream, "a human
    typed this" is a different kind of fact from "an engine guessed it", and the review editor
    shows the difference on a later pass.
    """
    by_id = {correction.block_id: correction for correction in corrections}
    unknown = set(by_id) - {block.id for block in kbdoc.blocks}
    if unknown:
        raise ValidationError("correction refers to unknown blocks", block_ids=sorted(unknown))

    updated = kbdoc.model_copy(deep=True)
    blocks = []
    for block in updated.blocks:
        correction = by_id.get(block.id)
        if correction is None:
            blocks.append(block)
            continue
        if correction.drop:
            continue
        if correction.text is not None:
            block.text = correction.text
        if correction.table_rows is not None and block.table is not None:
            block.table.rows = correction.table_rows
            block.text = "\n".join(" | ".join(row) for row in correction.table_rows)
        block.confidence = 1.0
        block.engine = "human"
        blocks.append(block)

    updated.blocks = blocks
    updated.idp_report.warnings.append(f"{len(corrections)} block(s) corrected by a reviewer")
    return updated


def reviewer_groups(principal: Any) -> set[str]:
    """Groups whose queues this principal may see."""
    user = getattr(principal, "effective_user", None) or principal
    groups = set(getattr(user, "groups", set()))
    if not groups:
        raise AuthzError("reviewer belongs to no group", subject=getattr(principal, "subject", ""))
    return groups
