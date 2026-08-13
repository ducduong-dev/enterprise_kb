"""Activities for MergeFlow and ConsolidationWorkflow.

The side effects of consolidating an instrument: build the draft, open the Legal cell's review
task, publish once the approvals are in, and then tell everyone downstream that the ground
moved under them.
"""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from kb_common.audit import AuditAction, AuditRecord, SqlAuditSink
from kb_common.db import session_scope
from kb_common.logging import get_logger
from kb_identity_merge.diff import diff_documents
from kb_identity_merge.impact import assess, open_impact_tasks
from kb_identity_merge.merge import MergeDrafter
from kb_ports.adapters.generation import ScriptedGeneration
from kb_ports.models import EmbeddingPort, GenerationPort
from kb_registry import repository as repo
from kb_registry.publish import Approval as PublishApproval
from kb_registry.publish import PublishService
from kb_registry.service import RegistryService
from kb_schemas.enums import RefType, ReviewTaskType
from kb_schemas.kbdoc import DocMeta, KBDoc
from kb_schemas.orm import DocumentRefRow
from sqlalchemy.orm import Session
from temporalio import activity

from kb_workflows.activities import Deps, deps
from kb_workflows.types import ReviewTaskRequest

log = get_logger(__name__)

#: The Legal cell owns consolidation of regulatory instruments. Never the document's steward:
#: producing `văn bản hợp nhất` is a legal act, not a documentation one.
LEGAL_CELL_GROUP = "dept/legal"


@dataclass
class MergeRequest:
    """One instrument, one incoming revision."""

    document_id: str
    #: The version carrying the new text — an amendment, or a re-issued instrument.
    new_version_id: str
    actor: str
    #: Set when the new text arrives as a separate amending instrument, which is what makes
    #: this a consolidation rather than an ordinary revision.
    amending_document_id: str | None = None
    consolidation: bool = False


@dataclass
class MergeDraftOutcome:
    draft_ref: str | None = None
    section_count: int = 0
    substantive_changes: int = 0
    touched_articles: list[int] = field(default_factory=list)
    complete: bool = True
    doc_class: str = ""
    author: str = ""
    steward_group: str | None = None
    document_title: str = ""
    #: Set when there is nothing to merge — the incoming text is identical to the canonical.
    no_changes: bool = False


@dataclass
class ImpactOutcome:
    impacted: int = 0
    task_ids: list[str] = field(default_factory=list)
    touched_articles: list[int] = field(default_factory=list)


@activity.defn(name="build_merge_draft")
async def build_merge_draft(request: MergeRequest) -> MergeDraftOutcome:
    """Diff the canonical text against the incoming one and draft the consolidation."""
    context = deps()
    with session_scope() as session:
        version = repo.get_version(session, uuid.UUID(request.new_version_id))
        document = repo.get_document(session, uuid.UUID(request.document_id))
        if version is None or document is None:
            raise ValueError("merge target no longer exists")
        canonical = repo.get_canonical_version(session, document.id)

        registry = RegistryService(session)
        steward = registry.steward_group_for(document.category_path)
        doc_class = document.doc_class
        title = document.title
        author = version.author or ""
        old_ref = canonical.idp_report_ref if canonical else None
        new_ref = version.idp_report_ref

    old_doc = _load(context, old_ref) if old_ref else KBDoc(doc_meta=_empty_meta())
    new_doc = _load(context, new_ref)
    diff = diff_documents(old_doc, new_doc)

    if not diff.changed:
        log.info("merge_no_changes", extra={"document_id": request.document_id})
        return MergeDraftOutcome(
            no_changes=True,
            doc_class=doc_class,
            author=author,
            steward_group=steward,
            document_title=title,
        )

    # Without a model the drafter still returns a draft — built from the diff alone and
    # flagged incomplete. A consolidation nobody can start because the GPU is down is worse
    # than one a lawyer has to finish writing.
    drafter = MergeDrafter(context.generation or _unavailable_model())
    draft = drafter.draft(diff)

    payload = json.dumps(
        {"diff": diff.as_dict(), "draft": draft.as_dict()}, ensure_ascii=False
    ).encode("utf-8")
    stored = context.storage.put(
        context.settings.storage.bucket_derived,
        io.BytesIO(payload),
        suffix=".merge.json",
        content_type="application/json",
    )
    return MergeDraftOutcome(
        draft_ref=f"{context.settings.storage.bucket_derived}/{stored.key}",
        section_count=len(diff.changed),
        substantive_changes=draft.substantive_changes,
        touched_articles=diff.touched_articles,
        complete=draft.complete,
        doc_class=doc_class,
        author=author,
        steward_group=steward,
        document_title=title,
    )


@activity.defn(name="open_merge_review")
async def open_merge_review(request: MergeRequest, draft: MergeDraftOutcome) -> str:
    """Open the review task the approvals will arrive against."""
    group = LEGAL_CELL_GROUP if request.consolidation else (draft.steward_group or LEGAL_CELL_GROUP)
    task_request = ReviewTaskRequest(
        version_id=request.new_version_id,
        task_type=ReviewTaskType.MERGE_REVIEW.value,
        assignee_group=group,
        payload={
            "draft_ref": draft.draft_ref,
            "document_id": request.document_id,
            "amending_document_id": request.amending_document_id,
            "consolidation": request.consolidation,
            "sections_changed": draft.section_count,
            "substantive_changes": draft.substantive_changes,
            "touched_articles": draft.touched_articles,
            "draft_complete": draft.complete,
            "doc_class": draft.doc_class,
            "prepared_by": draft.author,
            # The portal signals this workflow when the approvals arrive. Absent (a direct
            # activity call in a test), the portal records the decision and says it could not
            # deliver it, rather than pretending it did.
            "workflow_id": _workflow_id(),
        },
    )
    with session_scope() as session:
        registry = RegistryService(session, audit=SqlAuditSink(session))
        task = registry.open_review_task(
            uuid.UUID(task_request.version_id),
            ReviewTaskType(task_request.task_type),
            assignee_group=task_request.assignee_group,
            payload=task_request.payload,
        )
        return str(task.id)


@activity.defn(name="publish_merged")
async def publish_merged(request: MergeRequest, approver: str, note: str) -> int:
    """Publish the consolidated version through the ordinary publish transaction.

    No shortcut exists: the PII gate (INV-7), the four-eyes guard (INV-8) and the atomic
    canonical flip (INV-5) apply exactly as they do to any other publish.
    """
    context = deps()
    with session_scope() as session:
        version = repo.get_version(session, uuid.UUID(request.new_version_id))
        if version is None:
            raise ValueError("version no longer exists")
        kbdoc = _load(context, version.idp_report_ref)

        publisher = PublishService(
            session, embedder=_embedder(context), audit=SqlAuditSink(session)
        )
        prepared = publisher.prepare(kbdoc, legal_number=kbdoc.doc_meta.legal_number)
        result = publisher.publish(
            uuid.UUID(request.new_version_id),
            prepared,
            actor=approver,
            approval=PublishApproval(approver=approver, note=note),
        )
        if request.amending_document_id:
            _record_consolidation(session, request, approver)
        return result.chunks_written


@activity.defn(name="assess_impact")
async def assess_impact(request: MergeRequest, touched_articles: list[int]) -> ImpactOutcome:
    """Open a review task on every document that depends on what just changed."""
    with session_scope() as session:
        document = repo.get_document(session, uuid.UUID(request.document_id))
        if document is None:
            return ImpactOutcome()
        registry = RegistryService(session, audit=SqlAuditSink(session))
        assessment = assess(session, document.id, touched_articles=touched_articles)
        task_ids = open_impact_tasks(
            session, assessment, source_title=document.title, steward_for=registry
        )
        return ImpactOutcome(
            impacted=len(assessment.impacted),
            task_ids=[str(task_id) for task_id in task_ids],
            touched_articles=list(assessment.touched_articles),
        )


def _record_consolidation(session: Session, request: MergeRequest, approver: str) -> None:
    """Record that the amendment has been folded in.

    The `consolidates` edge is what clears the supersession flag: until it exists, retrieval
    warns that an unconsolidated amendment targets this instrument (M2).
    """
    amending = uuid.UUID(str(request.amending_document_id))
    target = uuid.UUID(request.document_id)
    if repo.get_edge(session, target, amending, RefType.CONSOLIDATES.value) is None:
        repo.add_edge(
            session,
            DocumentRefRow(
                id=uuid.uuid4(),
                src_document_id=target,
                dst_document_id=amending,
                ref_type=RefType.CONSOLIDATES.value,
                detected_by="consolidation",
                confirmed_by=approver,
                created_at=repo.now(),
            ),
        )
    SqlAuditSink(session).write(
        AuditRecord(
            action=AuditAction.MERGE_APPROVAL,
            actor=approver,
            object_ref={
                "document_id": str(target),
                "amending_document_id": str(amending),
                "version_id": request.new_version_id,
            },
            detail={"consolidation": True},
        )
    )


def _workflow_id() -> str:
    try:
        return str(activity.info().workflow_id)
    except RuntimeError:
        return ""


def _unavailable_model() -> GenerationPort:
    """A port that always fails, so the drafter takes its conservative path (`inferred=True`,
    `complete=False`) instead of the activity raising and the review never opening."""
    return ScriptedGeneration(responses=[])


def _load(context: Deps, ref: str | None) -> KBDoc:
    if not ref:
        raise ValueError("no parsed document available for the merge")
    bucket, _, key = ref.partition("/")
    return KBDoc.model_validate_json(context.storage.get(bucket, key))


def _empty_meta() -> DocMeta:
    return DocMeta(source_format="txt")


def _embedder(context: Deps) -> EmbeddingPort:
    from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
    from kb_ports.adapters.embedding_tei import TeiEmbeddingAdapter

    return (
        HashedEmbeddingAdapter()
        if context.settings.use_deterministic_models
        else TeiEmbeddingAdapter(context.settings.models)
    )


MERGE_ACTIVITIES: Sequence[Callable[..., Any]] = [
    build_merge_draft,
    open_merge_review,
    publish_merged,
    assess_impact,
]
