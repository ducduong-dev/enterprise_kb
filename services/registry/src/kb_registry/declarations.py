"""Deciding a declaration: one steward act, two records, one transaction.

`find_declarations` reads the sentence and `record_declarations` stores it. This is the third
step, and the only one that changes what a reader sees.

Confirming writes both halves of ADR-0040's answer:

* an **expiry ledger row**, which ends the clauses the declaration names — projected onto those
  chunks in this transaction, so they leave the default path on their date with no scheduled
  job involved (ADR-0031);
* a **`clause_supersessions` row** where the sentence named a replacement, which is what turns
  *"khoản 2 Điều 12 hết hiệu lực"* into *"…; quy định hiện hành là Điều 7 Thông tư 15/2025"*.

Both are written through their own services rather than by INSERT here, so a declaration and a
steward's manual act produce identical rows and go through identical guards — four-eyes on the
regulated classes, append-only, only `confirmed` served.

**Why this can end a clause when the inference funnel cannot.** A declaration is the corpus
stating what the law now is; a detection is our reading of two texts that never mention each
other. Only the first is evidence that a rule *ended*, and no accumulation of the second turns
into the first (ADR-0040). That is the whole reason this path exists separately.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from kb_common.audit import AuditSink
from kb_common.errors import Conflict, NotFound, ValidationError
from kb_common.logging import get_logger
from kb_schemas.enums import DeclarationState, ExpiryBasis, SupersessionBasis
from kb_schemas.orm import DocumentDeclarationRow
from sqlalchemy.orm import Session

from kb_registry import repository as repo
from kb_registry.anchors import resolve_clauses
from kb_registry.expiry import ExpiryLedger
from kb_registry.supersession import ClauseRef, ClauseSupersessions

log = get_logger(__name__)

#: Who the ledger records as having *found* the declaration. A detector string, never an actor,
#: which is what lets the four-eyes guard see that a machine proposed and one human confirmed
#: — the intended reading of ADR-0030's "a detector writes `proposed`, a human confirms".
DETECTOR = "declaration"


@dataclass
class DeclarationOutcome:
    declaration_id: uuid.UUID
    state: str
    #: Clauses of the target that stopped applying. Empty for a whole-instrument declaration,
    #: where the expiry covers the document rather than a list of clauses.
    ended: list[str] = field(default_factory=list)
    #: Supersession rows written — one per replaced clause that named a successor.
    pointers: int = 0
    chunks_projected: int = 0
    note: str = ""


class DeclarationReview:
    def __init__(self, session: Session, *, audit: AuditSink | None = None) -> None:
        self._session = session
        self._audit = audit
        self._expiry = ExpiryLedger(session, audit=audit)
        self._supersessions = ClauseSupersessions(session, audit=audit)

    # ------------------------------------------------------------------------------ reads

    def queue(self, document_id: uuid.UUID) -> list[DocumentDeclarationRow]:
        """Everything one document declares, which is what the batch screen shows.

        Per declaring document rather than per pair: a closing article typically declares a
        dozen changes from one paragraph, all read from the same sentence or its neighbours,
        so they are confirmed together with the evidence shown once (ADR-0039).
        """
        return list(repo.declarations_from(self._session, document_id))

    # ----------------------------------------------------------------------------- writes

    def confirm(self, declaration_id: uuid.UUID, *, actor: str) -> DeclarationOutcome:
        """Apply a declaration: end what it ends, and record what replaced it."""
        row = self._require_actionable(declaration_id)
        assert row.target_document_id is not None  # guaranteed by _require_actionable
        ends_on, takes_effect = self._dates(row)

        expiry = self._expiry.propose(
            row.target_document_id,
            effective_to=ends_on,
            basis=ExpiryBasis.DECLARED_BY,
            detected_by=f"{DETECTOR}:{row.id}",
            actor=actor,
            source_document_id=row.src_document_id,
            anchors=list(row.target_anchors or ()),
            evidence=row.evidence,
        )
        applied = self._expiry.confirm(expiry.row_id, actor=actor)

        pointers = self._write_pointers(row, takes_effect, actor=actor)

        row.state = DeclarationState.APPLIED.value
        row.decided_by = actor
        row.decided_at = datetime.now(UTC)
        self._session.flush()

        log.info(
            "declaration_confirmed",
            extra={
                "declaration_id": str(row.id),
                "target_document_id": str(row.target_document_id),
                "anchors": list(row.target_anchors or ()),
                "pointers": pointers,
                "chunks": applied.chunks_projected,
            },
        )
        return DeclarationOutcome(
            declaration_id=row.id,
            state=row.state,
            ended=list(row.target_anchors or ()),
            pointers=pointers,
            chunks_projected=applied.chunks_projected,
            note=applied.note,
        )

    def reject(self, declaration_id: uuid.UUID, *, actor: str, reason: str) -> DeclarationOutcome:
        """Record that a read declaration is not one.

        Kept rather than deleted, and for the same reason the ledger keeps a revoked row: the
        detector will read the same sentence again on the next re-detection pass, and a
        rejection nobody stored is a proposal that comes back every time.
        """
        if not reason.strip():
            raise ValidationError("rejecting a declaration needs a reason")
        row = self._session.get(DocumentDeclarationRow, declaration_id)
        if row is None:
            raise NotFound("declaration not found", declaration_id=str(declaration_id))
        if row.state == DeclarationState.APPLIED.value:
            raise Conflict(
                "this declaration was applied; withdraw the expiry instead",
                declaration_id=str(declaration_id),
            )

        row.state = DeclarationState.REJECTED.value
        row.decided_by = actor
        row.decided_at = datetime.now(UTC)
        row.evidence = f"{row.evidence}\n[bị từ chối] {reason.strip()}"
        self._session.flush()
        return DeclarationOutcome(
            declaration_id=row.id, state=row.state, note="rejected; nothing was applied"
        )

    # -------------------------------------------------------------------------- internals

    def _dates(self, row: DocumentDeclarationRow) -> tuple[date, date]:
        """`(last day the old rule applied, day the replacement took effect)`.

        The declaration's own date where the sentence stated one behind an effectivity cue,
        otherwise the declaring instrument's effective date — it is that instrument's coming
        into force that ends the old rule, so the two are the same fact read from two places.

        The old rule applied through the day *before*: the effectivity predicate is inclusive,
        and one day either way is a day of a repealed rule served or a day of a live rule
        hidden. This is the boundary `expiry_date_for_abrogation` states for the edge path,
        and it has to agree with it.

        Neither available is a refusal, not a guess. A date nobody read is exactly what
        ADR-0029 exists to prevent, and here it would decide when a rule stopped applying.
        """
        takes_effect = row.effective_from
        if takes_effect is None:
            version = repo.get_canonical_version(self._session, row.src_document_id)
            takes_effect = version.effective_from if version else None
        if takes_effect is None:
            raise ValidationError(
                "this declaration states no date and its instrument has no effective date; "
                "set the effective date before confirming",
                declaration_id=str(row.id),
            )
        return takes_effect - timedelta(days=1), takes_effect

    def _write_pointers(
        self, row: DocumentDeclarationRow, takes_effect: date, *, actor: str
    ) -> int:
        """A `clause_supersessions` row per replaced clause that named a successor.

        Anchors become section paths here and not at detection, because at detection the
        target may not have been published and may have had no chunks to resolve against.

        Pairing: one replacement for one clause is the ordinary shape and pairs directly. Where
        the counts match they pair in order. Where they do not — *"Điều 7 và Điều 8 thay thế
        Điều 5"* — every replaced clause points at the first replacement, which is the shape of
        the sentence rather than a guess about which half answers which.
        """
        if not row.replacement_anchors or not row.target_anchors:
            return 0
        assert row.target_document_id is not None

        replaced = resolve_clauses(self._session, row.target_document_id, row.target_anchors)
        replacing = resolve_clauses(self._session, row.src_document_id, row.replacement_anchors)
        if not replaced or not replacing:
            # The pointer is the useful half but not the safe half: the expiry above already
            # ended the clause. Reported so a steward can see the reference did not land.
            log.warning(
                "declaration_pointer_unresolved",
                extra={
                    "declaration_id": str(row.id),
                    "target_anchors": list(row.target_anchors),
                    "replacement_anchors": list(row.replacement_anchors),
                },
            )
            return 0

        written = 0
        for index, clause in enumerate(replaced):
            successor = replacing[index] if index < len(replacing) else replacing[0]
            proposal = self._supersessions.propose(
                ClauseRef(row.target_document_id, clause.section_path),
                new=ClauseRef(row.src_document_id, successor.section_path),
                supersedes_from=takes_effect,
                basis=SupersessionBasis.DECLARED,
                detected_by=f"{DETECTOR}:{row.id}",
                actor=actor,
                evidence=row.evidence,
            )
            self._supersessions.confirm(proposal.row_id, actor=actor)
            written += 1
        return written

    def _require_actionable(self, declaration_id: uuid.UUID) -> DocumentDeclarationRow:
        row = self._session.get(DocumentDeclarationRow, declaration_id)
        if row is None:
            raise NotFound("declaration not found", declaration_id=str(declaration_id))
        if row.target_document_id is None:
            raise Conflict(
                "this declaration is waiting for an instrument the registry does not hold",
                declaration_id=str(declaration_id),
                target=row.target_legal_number,
            )
        if row.state != DeclarationState.OPEN.value:
            raise Conflict(
                f"declaration is {row.state}, not open",
                declaration_id=str(declaration_id),
            )
        return row


__all__ = ["DETECTOR", "DeclarationOutcome", "DeclarationReview"]
