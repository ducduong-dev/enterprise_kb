"""Registry business rules.

Two things live here that must not live anywhere else:

* **Classification defaults.** A document's ACL comes from its category unless the uploader
  overrides it. Having one place decide this means the category tree really is the policy
  surface, rather than each upload path inventing its own default.
* **Retention.** `retention_until` is set at version creation from the class schedule
  ([OPEN]-4) and never recomputed — a version created under one policy keeps its hold even if
  the schedule later changes (INV-9).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from kb_common.audit import AuditAction, AuditRecord, AuditSink
from kb_common.config import Settings, get_settings
from kb_common.errors import Conflict, NotFound, ValidationError
from kb_common.logging import get_logger
from kb_schemas.enums import (
    DocClass,
    DocStatus,
    PiiStatus,
    RefType,
    ReviewTaskState,
    ReviewTaskType,
    Visibility,
)
from kb_schemas.kbdoc import KBDoc
from kb_schemas.orm import (
    DocumentRefRow,
    DocumentRow,
    DocumentVersionRow,
    PendingDocumentRefRow,
    ReviewTaskRow,
)
from kb_vntext.legal_numbers import normalize_legal_number
from sqlalchemy.orm import Session

from kb_registry import repository as repo
from kb_registry.schemas import DetectedRefIn, DocumentCreate, DocumentUpdate, VersionCreate

log = get_logger(__name__)


def _as_date(value: str | None) -> date | None:
    """An ISO date the parser read, or nothing. Malformed input is nothing, not an error: the
    reviewer supplies the date, and an ingest must not fail on a badly written preamble."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:  # pragma: no cover - the detector only emits ISO dates
        return None


def _articles_for(ref: DetectedRefIn) -> list[int]:
    """The article numbers behind a reference's anchors.

    Derived rather than taken from the caller, so `articles` and `anchors` cannot disagree
    about which articles a reference touches — the impact traversal reads the first and the
    resolver reads the second, and a reference that means different things to the two is a
    policy owner told the wrong thing (ADR-0036).

    Falls back to whatever the caller supplied when there are no anchors: an edge confirmed by
    hand on the review screen still names articles that way.
    """
    if not ref.anchors:
        return list(ref.articles)
    numbers = set()
    for anchor in ref.anchors:
        head = anchor.split(".")[0].strip()
        if head.isdigit():
            numbers.add(int(head))
    return sorted(numbers)


#: Topic for the outbox event the indexer consumes (M2).
TOPIC_VERSION_CREATED = "registry.version_created"

#: An override without a reason is not an override, it is a bypass. Short enough not to be
#: bureaucratic, long enough that "ok" does not qualify.
MIN_OVERRIDE_JUSTIFICATION = 20


@dataclass(frozen=True, slots=True)
class IngestResult:
    document_id: uuid.UUID
    version_id: uuid.UUID
    created_document: bool
    #: Set when an existing document already claims this legal number — the ingest becomes a
    #: candidate revision rather than a new document, and a human decides (M5).
    matched_existing: bool = False
    duplicate_of_version_id: uuid.UUID | None = None


class RegistryService:
    def __init__(
        self,
        session: Session,
        *,
        audit: AuditSink | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._audit = audit
        self._settings = settings or get_settings()

    # ------------------------------------------------------------------------ documents

    def create_document(self, spec: DocumentCreate, *, actor: str) -> DocumentRow:
        category = repo.get_category(self._session, spec.category_path)
        if category is None:
            raise ValidationError("unknown category", category_path=spec.category_path)

        visibility = spec.visibility or Visibility(category.default_visibility)
        allowed_groups = (
            spec.allowed_groups
            if spec.allowed_groups is not None
            else list(category.default_allowed_groups)
        )
        if visibility is Visibility.RESTRICTED and not allowed_groups:
            # The database rejects this too; failing here gives the uploader a usable message.
            raise ValidationError(
                "restricted visibility requires at least one allowed group",
                category_path=spec.category_path,
            )

        if spec.legal_number:
            existing = repo.find_by_legal_number(self._session, spec.legal_number)
            if existing is not None:
                raise Conflict(
                    "a document with this legal number already exists",
                    legal_number=spec.legal_number,
                    document_id=str(existing.id),
                )

        row = DocumentRow(
            id=uuid.uuid4(),
            title=spec.title,
            legal_number=spec.legal_number,
            doc_class=spec.doc_class.value,
            category_path=spec.category_path,
            department=spec.department,
            visibility=visibility.value,
            allowed_groups=allowed_groups,
            # New documents are drafts. Publication is a separate, guarded act (INV-5/7/8).
            status=DocStatus.DRAFT.value,
            canonical_version_id=None,
            review_by=spec.review_by,
            created_at=repo.now(),
            updated_at=repo.now(),
        )
        repo.add_document(self._session, row)
        self._record(
            AuditAction.DOCUMENT_CREATE,
            actor,
            {"document_id": str(row.id)},
            {
                "title": spec.title,
                "category_path": spec.category_path,
                "visibility": visibility.value,
            },
        )
        log.info(
            "document_created",
            extra={
                "document_id": str(row.id),
                "category_path": spec.category_path,
                "visibility": visibility.value,
                "doc_class": spec.doc_class.value,
            },
        )
        # A document arriving is also an *answer* to references parked earlier: whatever cited
        # or amended this instrument before the bank held it becomes an edge now (ADR-0028).
        # Here rather than in the ingest workflow, because this is the one moment a legal
        # number enters the registry — from the workflow, the registry API or the seeder alike.
        if row.legal_number:
            self.resolve_pending_refs(row.id)
        return row

    def set_effective_from(
        self, version_id: uuid.UUID, effective_from: date | None, *, actor: str
    ) -> None:
        """Correct when a version takes effect, before it is published.

        On the version rather than the document because effectivity belongs to a text: an
        amendment changes what is in force from a date, and the previous version was in force
        until then. Refused after publication — the chunks carry a copy of these dates and the
        filter reads them, so moving the date under a published version would change what a
        past query would have returned (INV-9).
        """
        version = repo.get_version(self._session, version_id)
        if version is None:
            raise NotFound("version not found", version_id=str(version_id))
        if version.published_at is not None:
            raise Conflict(
                "a published version's effective date is fixed; publish a new version instead",
                version_id=str(version_id),
            )
        before = version.effective_from
        version.effective_from = effective_from
        self._session.flush()
        self._record(
            AuditAction.VERSION_UPDATE,
            actor,
            {"document_id": str(version.document_id), "version_id": str(version_id)},
            {
                "field": "effective_from",
                "from": before.isoformat() if before else None,
                "to": effective_from.isoformat() if effective_from else None,
            },
        )

    def update_document(
        self, document_id: uuid.UUID, spec: DocumentUpdate, *, actor: str
    ) -> DocumentRow:
        row = self._require_document(document_id)
        before = {
            "visibility": row.visibility,
            "allowed_groups": list(row.allowed_groups),
            "category_path": row.category_path,
        }

        if spec.title is not None:
            row.title = spec.title
        if spec.department is not None:
            row.department = spec.department
        if spec.category_path is not None:
            if repo.get_category(self._session, spec.category_path) is None:
                raise ValidationError("unknown category", category_path=spec.category_path)
            row.category_path = spec.category_path
        if spec.visibility is not None:
            row.visibility = spec.visibility.value
        if spec.allowed_groups is not None:
            row.allowed_groups = spec.allowed_groups
        if spec.review_by is not None:
            row.review_by = spec.review_by

        if row.visibility == Visibility.RESTRICTED.value and not row.allowed_groups:
            raise ValidationError("restricted visibility requires at least one allowed group")

        self._session.flush()
        after = {
            "visibility": row.visibility,
            "allowed_groups": list(row.allowed_groups),
            "category_path": row.category_path,
        }
        if before != after:
            # An ACL change is a security event: it changes who can read published content,
            # and the indexed chunks must be rewritten before it takes effect (ADR-0003).
            self._record(
                AuditAction.ACL_CHANGE,
                actor,
                {"document_id": str(document_id)},
                {"before": before, "after": after},
            )
        return row

    # ------------------------------------------------------------------------- versions

    def create_version(self, spec: VersionCreate, *, actor: str) -> DocumentVersionRow:
        document = self._require_document(spec.document_id)

        duplicate = repo.find_version_by_hash(self._session, spec.document_id, spec.content_hash)
        if duplicate is not None:
            raise Conflict(
                "this content already exists as a version of the document",
                version_id=str(duplicate.id),
                content_hash=spec.content_hash,
            )

        row = DocumentVersionRow(
            id=uuid.uuid4(),
            document_id=spec.document_id,
            content_ref=spec.content_ref,
            content_hash=spec.content_hash,
            source_type=spec.source_type.value,
            author=spec.author,
            change_summary=spec.change_summary,
            idp_report_ref=spec.idp_report_ref,
            # Fails closed: nothing is publishable until the PII gate clears it (INV-7, M4).
            pii_status="pending",
            effective_from=spec.effective_from,
            effective_to=spec.effective_to,
            is_canonical=False,
            retention_until=self.retention_until(DocClass(document.doc_class)),
            created_at=repo.now(),
        )
        repo.add_version(self._session, row)
        self._record(
            AuditAction.VERSION_CREATE,
            actor,
            {"document_id": str(spec.document_id), "version_id": str(row.id)},
            {"content_hash": spec.content_hash, "source_type": spec.source_type.value},
        )
        log.info(
            "version_created",
            extra={"document_id": str(spec.document_id), "version_id": str(row.id)},
        )
        return row

    def retention_until(self, doc_class: DocClass, *, today: date | None = None) -> date:
        """[OPEN]-4 — Legal owes the real schedule; the config is conservative until then."""
        retention = self._settings.retention
        years = {
            DocClass.REGULATORY: retention.regulatory_years,
            DocClass.INTERNAL_NORMATIVE: retention.internal_normative_years,
            DocClass.OPERATIONAL: retention.operational_years,
            DocClass.CUSTOMER_FACING: retention.customer_facing_years,
        }.get(doc_class, retention.default_years)
        reference = today or date.today()
        return date(reference.year + years, 12, 31)

    # ------------------------------------------------------------------------ pii gate

    def set_pii_status(
        self,
        version_id: uuid.UUID,
        status: PiiStatus,
        *,
        actor: str,
        detail: dict[str, object] | None = None,
    ) -> DocumentVersionRow:
        """Record the gate's verdict on a version (INV-7).

        `pii_status` is one of the two columns a version may change after creation — the gate
        has to be able to write its result, and the immutability trigger allows exactly this
        and the canonical flag.
        """
        version = repo.get_version(self._session, version_id)
        if version is None:
            raise NotFound("version not found", version_id=str(version_id))
        if (
            PiiStatus(version.pii_status) is PiiStatus.OVERRIDDEN
            and status is not PiiStatus.BLOCKED
        ):
            # An override is a human decision with a justification behind it. A later
            # automated scan must not quietly discard it — nor silently re-clear it.
            raise Conflict(
                "version has a human PII override; re-scanning cannot replace it",
                version_id=str(version_id),
            )

        version.pii_status = status.value
        self._session.flush()
        self._record(
            AuditAction.PII_BLOCK if status is PiiStatus.BLOCKED else "pii_scan",
            actor,
            {"version_id": str(version_id), "document_id": str(version.document_id)},
            {"pii_status": status.value, **(detail or {})},
        )
        log.info(
            "pii_status_set",
            extra={"version_id": str(version_id), "pii_status": status.value},
        )
        return version

    def override_pii(
        self,
        version_id: uuid.UUID,
        *,
        actor: str,
        justification: str,
        findings: dict[str, object] | None = None,
    ) -> DocumentVersionRow:
        """Override a blocked PII verdict (INV-7).

        Three things are required and none is optional: a human, a justification in their own
        words, and an audit record. The role check belongs to the caller — this refuses to
        record an override without a reason, which is the part that must never be skippable.
        """
        version = repo.get_version(self._session, version_id)
        if version is None:
            raise NotFound("version not found", version_id=str(version_id))
        if PiiStatus(version.pii_status) is not PiiStatus.BLOCKED:
            raise Conflict(
                "only a blocked version can be overridden",
                version_id=str(version_id),
                pii_status=version.pii_status,
            )
        if len(justification.strip()) < MIN_OVERRIDE_JUSTIFICATION:
            raise ValidationError(
                "an override needs a written justification",
                minimum_characters=MIN_OVERRIDE_JUSTIFICATION,
            )

        version.pii_status = PiiStatus.OVERRIDDEN.value
        self._session.flush()
        self._record(
            AuditAction.PII_OVERRIDE,
            actor,
            {"version_id": str(version_id), "document_id": str(version.document_id)},
            {
                "justification": justification.strip(),
                "findings": findings or {},
                "previous_status": PiiStatus.BLOCKED.value,
            },
        )
        log.warning(
            "pii_override",
            extra={"version_id": str(version_id), "actor": actor},
        )
        return version

    # --------------------------------------------------------------------------- ingest

    def ingest(
        self,
        kbdoc: KBDoc,
        *,
        spec: DocumentCreate,
        content_ref: str,
        content_hash: str,
        idp_report_ref: str | None,
        actor: str,
    ) -> IngestResult:
        """Create (or match) a document and attach a new version for an upload.

        The M1 happy path is a new document. When the legal number already exists the upload
        is a revision of that instrument, so we attach the version to it and let the workflow
        open a review task — silently creating a second document for the same instrument would
        split its history, which is precisely what the registry exists to prevent.
        """
        legal_number = spec.legal_number or kbdoc.doc_meta.legal_number
        existing = repo.find_by_legal_number(self._session, legal_number) if legal_number else None

        if existing is not None:
            document = existing
            created_document = False
            duplicate = repo.find_version_by_hash(self._session, document.id, content_hash)
            if duplicate is not None:
                # Identical bytes for an instrument we already hold: nothing to ingest.
                return IngestResult(
                    document_id=document.id,
                    version_id=duplicate.id,
                    created_document=False,
                    matched_existing=True,
                    duplicate_of_version_id=duplicate.id,
                )
        else:
            document = self.create_document(
                spec.model_copy(update={"legal_number": legal_number}), actor=actor
            )
            created_document = True

        version = self.create_version(
            VersionCreate(
                document_id=document.id,
                content_ref=content_ref,
                content_hash=content_hash,
                author=actor,
                change_summary=("Initial ingest" if created_document else "Revision from upload"),
                idp_report_ref=idp_report_ref,
                # What the instrument says about itself, subject to the reviewer's correction.
                # A wrong date is visible on the review screen; a missing one is invisible and
                # means "always in effect" (ADR-0029).
                effective_from=_as_date(kbdoc.doc_meta.effective_from),
            ),
            actor=actor,
        )
        return IngestResult(
            document_id=document.id,
            version_id=version.id,
            created_document=created_document,
            matched_existing=existing is not None,
        )

    # ------------------------------------------------------------------------ reference

    def resolve_pending_refs(self, document_id: uuid.UUID) -> list[uuid.UUID]:
        """Promote every parked reference that names this document into a real edge.

        Called when a document is registered, which is the moment a legal number becomes
        resolvable. Without it the graph depends on ingest order: an amending decree loaded
        before the instrument it amends would never link, and nothing about the corpus would
        say so — the supersession warning, the consolidation task and the impact traversal all
        read edges, so a missing one is silence rather than an error (ADR-0028).
        """
        document = repo.get_document(self._session, document_id)
        if document is None or not document.legal_number:
            return []

        key = normalize_legal_number(document.legal_number)
        created: list[uuid.UUID] = []
        for pending in repo.pending_refs_for(self._session, key):
            if pending.src_document_id == document_id:
                repo.drop_pending_ref(self._session, pending)
                continue
            if repo.get_edge(self._session, pending.src_document_id, document_id, pending.ref_type):
                repo.drop_pending_ref(self._session, pending)
                continue
            row = repo.add_edge(
                self._session,
                DocumentRefRow(
                    id=uuid.uuid4(),
                    src_document_id=pending.src_document_id,
                    dst_document_id=document_id,
                    ref_type=pending.ref_type,
                    articles=pending.articles,
                    # Promoted unchanged: the precision was read from the citing text when it
                    # was ingested, and re-deriving it here would need that text again.
                    anchors=pending.anchors,
                    detected_by=pending.detected_by,
                    confirmed_by=None,
                    created_at=repo.now(),
                ),
            )
            repo.drop_pending_ref(self._session, pending)
            created.append(row.id)

        if created:
            # The serving projection is rebuilt by the publish transaction of the *document
            # being published*; these edges attach to documents published long ago, so their
            # copy is refreshed here or graph expansion never sees them (INV-10).
            repo.refresh_graph_serving_for(self._session, document_id)
            log.info(
                "pending_refs_resolved",
                extra={"document_id": str(document_id), "edges": len(created)},
            )
        return created

    def link_detected_refs(
        self, document_id: uuid.UUID, refs: list[DetectedRefIn]
    ) -> tuple[list[uuid.UUID], list[str]]:
        """Create edges for references whose target we hold; report the rest.

        Edges target documents, never versions (INV-10), and stay unconfirmed until a human
        agrees — `confirmed_by` is what makes an edge authoritative for consolidation (M5).
        """
        created: list[uuid.UUID] = []
        unresolved: list[str] = []
        for ref in refs:
            target = repo.find_by_legal_number(self._session, ref.legal_number)
            if target is None:
                # The target is not in the registry *yet*. In a corpus digitised in arbitrary
                # order that is normal, not exceptional — so the reference is parked rather
                # than reported once and forgotten, and becomes an edge when the instrument it
                # names is registered (ADR-0028).
                repo.add_pending_ref(
                    self._session,
                    PendingDocumentRefRow(
                        id=uuid.uuid4(),
                        src_document_id=document_id,
                        target_legal_number=ref.legal_number,
                        target_key=normalize_legal_number(ref.legal_number),
                        ref_type=ref.ref_type.value,
                        articles=_articles_for(ref) or None,
                        anchors=ref.anchors or None,
                        detected_by=ref.detected_by,
                        created_at=repo.now(),
                    ),
                )
                unresolved.append(ref.legal_number)
                continue
            if target.id == document_id:
                continue
            if repo.get_edge(self._session, document_id, target.id, ref.ref_type.value):
                continue
            row = repo.add_edge(
                self._session,
                DocumentRefRow(
                    id=uuid.uuid4(),
                    src_document_id=document_id,
                    dst_document_id=target.id,
                    ref_type=ref.ref_type.value,
                    articles=_articles_for(ref) or None,
                    anchors=ref.anchors or None,
                    detected_by=ref.detected_by,
                    confirmed_by=None,
                    created_at=repo.now(),
                ),
            )
            created.append(row.id)
        return created, unresolved

    # ---------------------------------------------------------------------- review task

    def open_review_task(
        self,
        version_id: uuid.UUID,
        task_type: ReviewTaskType,
        *,
        assignee_group: str | None,
        payload: dict[str, object] | None = None,
    ) -> ReviewTaskRow:
        row = ReviewTaskRow(
            id=uuid.uuid4(),
            version_id=version_id,
            task_type=task_type.value,
            state=ReviewTaskState.OPEN.value,
            assignee_group=assignee_group,
            payload=payload or {},
            created_at=repo.now(),
        )
        repo.add_review_task(self._session, row)
        log.info(
            "review_task_opened",
            extra={
                "task_id": str(row.id),
                "task_type": task_type.value,
                "assignee_group": assignee_group,
            },
        )
        return row

    def steward_group_for(self, category_path: str) -> str | None:
        category = repo.get_category(self._session, category_path)
        if category is not None and category.steward_group:
            return str(category.steward_group)
        # Walk up the tree: a leaf without its own steward inherits its parent's.
        parts = category_path.split(".")
        for depth in range(len(parts) - 1, 0, -1):
            parent = repo.get_category(self._session, ".".join(parts[:depth]))
            if parent is not None and parent.steward_group:
                return str(parent.steward_group)
        return None

    # ---------------------------------------------------------------------------- misc

    def _require_document(self, document_id: uuid.UUID) -> DocumentRow:
        row = repo.get_document(self._session, document_id)
        if row is None:
            raise NotFound("document not found", document_id=str(document_id))
        return row

    def _record(
        self,
        action: str,
        actor: str,
        object_ref: dict[str, object],
        detail: dict[str, object] | None = None,
    ) -> None:
        if self._audit is None:
            return
        self._audit.write(
            AuditRecord(action=action, actor=actor, object_ref=object_ref, detail=detail or {})
        )


__all__ = ["TOPIC_VERSION_CREATED", "IngestResult", "RefType", "RegistryService"]
