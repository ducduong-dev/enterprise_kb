"""Clause-level supersession: what replaced a rule that ended.

`ExpiryLedger` records that a clause stopped applying. This records what took its place, and
the difference is what a reader gets back: *"khoản 2 Điều 12 hết hiệu lực từ 01/01/2026"* is a
dead end, *"…; quy định hiện hành là Điều 7 Thông tư 15/2025"* is an answer.

Deliberately the same shape as the expiry ledger (ADR-0030): append-only, both clocks, one open
row per scope, only `confirmed` is served. They are the same kind of record — a decision about
a rule, made by somebody, on evidence, at a time — and a steward should not need two mental
models to read them side by side.

**What this module never does is hide a clause.** A detected supersession flags the older text
and points at its replacement; the clause is still in the document, and a page that omitted it
would lie about what the document says (ADR-0033). Only a *declared* supersession also ends the
clause, and it does that by writing an expiry ledger row — which is `ExpiryLedger`'s job, not
this one's. The split is not a confidence threshold a detection could one day cross: a
declaration is the corpus stating what the law now is, an inference is our reading of two
texts, and only the first is evidence that a rule ended (ADR-0040).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from kb_common.audit import AuditAction, AuditRecord, AuditSink
from kb_common.errors import Conflict, NotFound, ValidationError
from kb_common.logging import get_logger
from kb_schemas.enums import (
    NO_AUTOMATION_CLASSES,
    DocClass,
    ExpiryState,
    SupersessionBasis,
    SupersessionVerdict,
)
from kb_schemas.orm import ClauseSupersessionRow
from sqlalchemy import select
from sqlalchemy.orm import Session

from kb_registry import repository as repo

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ClauseRef:
    """One clause, addressed the way it survives a rechunk."""

    document_id: uuid.UUID
    section_path: str


@dataclass(frozen=True, slots=True)
class SupersessionDecision:
    row_id: uuid.UUID
    old: ClauseRef
    new: ClauseRef | None
    state: str
    supersedes_from: date
    note: str = ""

    @property
    def is_abrogation(self) -> bool:
        """No replacement. The clause ended and nothing took its place."""
        return self.new is None


class ClauseSupersessions:
    def __init__(self, session: Session, *, audit: AuditSink | None = None) -> None:
        self._session = session
        self._audit = audit

    # ------------------------------------------------------------------------------ reads

    def open_row(self, old: ClauseRef) -> ClauseSupersessionRow | None:
        """The current belief about what replaced this clause, whatever its state."""
        stmt = select(ClauseSupersessionRow).where(
            ClauseSupersessionRow.old_document_id == old.document_id,
            ClauseSupersessionRow.old_section_path == old.section_path,
            ClauseSupersessionRow.closed_at.is_(None),
        )
        return self._session.execute(stmt).scalars().one_or_none()

    def history(self, document_id: uuid.UUID) -> list[ClauseSupersessionRow]:
        """Every row ever written about this document's clauses, oldest first."""
        stmt = (
            select(ClauseSupersessionRow)
            .where(ClauseSupersessionRow.old_document_id == document_id)
            .order_by(ClauseSupersessionRow.created_at, ClauseSupersessionRow.id)
        )
        return list(self._session.execute(stmt).scalars())

    def served(
        self, document_ids: set[uuid.UUID], *, on: date | None = None
    ) -> dict[tuple[uuid.UUID, str], ClauseSupersessionRow]:
        """Confirmed supersessions in force on a date, keyed by the clause they replace.

        The retrieval-side lookup. Bounded by `supersedes_from` rather than by "now": a
        confirmed supersession is true from the newer clause's own effective date, so an
        `as_of` query before that date must still show the older clause as current and
        unflagged — the property the whole expiry programme exists to establish (ADR-0033).
        """
        if not document_ids:
            return {}
        when = on or date.today()
        stmt = select(ClauseSupersessionRow).where(
            ClauseSupersessionRow.old_document_id.in_(document_ids),
            ClauseSupersessionRow.closed_at.is_(None),
            ClauseSupersessionRow.state == ExpiryState.CONFIRMED.value,
            ClauseSupersessionRow.supersedes_from <= when,
        )
        return {
            (row.old_document_id, row.old_section_path): row
            for row in self._session.execute(stmt).scalars()
        }

    # ----------------------------------------------------------------------------- writes

    def propose(
        self,
        old: ClauseRef,
        *,
        supersedes_from: date,
        basis: SupersessionBasis,
        detected_by: str,
        actor: str,
        new: ClauseRef | None = None,
        verdict: SupersessionVerdict | None = None,
        evidence: str | None = None,
        quantity_delta: dict[str, Any] | None = None,
        scope_facets: dict[str, Any] | None = None,
        score: float | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> SupersessionDecision:
        """Record that one clause replaced another. Changes nothing a user can see."""
        self._require_document(old.document_id)
        if not old.section_path.strip():
            raise ValidationError("a superseded clause needs a section path")
        if new is not None:
            self._require_document(new.document_id)
            if not new.section_path.strip():
                raise ValidationError("a replacing clause needs a section path")
            if new == old:
                raise ValidationError("a clause cannot replace itself")

        row = self._append(
            old=old,
            new=new,
            basis=basis.value,
            state=ExpiryState.PROPOSED,
            supersedes_from=supersedes_from,
            verdict=verdict.value if verdict else None,
            evidence=evidence,
            quantity_delta=quantity_delta,
            scope_facets=scope_facets,
            score=score,
            model=model,
            prompt_version=prompt_version,
            detected_by=detected_by,
            confirmed_by=None,
        )
        self._record(actor, row, "propose")
        return self._decision(row, "proposed; nothing is flagged until a steward confirms")

    def confirm(self, row_id: uuid.UUID, *, actor: str) -> SupersessionDecision:
        """Accept a proposal.

        Flags the older clause and points at its replacement. It does **not** hide anything:
        the clause is still in the document and a page that omitted it would lie about what
        the document says. Ending a clause is the expiry ledger's act, on declared evidence
        only (ADR-0040).
        """
        row = self._require_open(row_id)
        if row.state == ExpiryState.CONFIRMED.value:
            raise Conflict("already confirmed", row_id=str(row_id))
        self._check_four_eyes(row, actor)

        confirmed = self._copy(row, state=ExpiryState.CONFIRMED, confirmed_by=actor)
        self._record(actor, confirmed, "confirm")
        return self._decision(
            confirmed,
            "confirmed; the older clause is flagged with a pointer and fusion drops it when "
            "its replacement is in the candidate set",
        )

    def revoke(self, row_id: uuid.UUID, *, actor: str, reason: str) -> SupersessionDecision:
        """Withdraw a supersession — as a new row, never a deletion."""
        if not reason.strip():
            raise ValidationError("a revocation needs a reason")
        row = self._require_open(row_id)
        if row.state == ExpiryState.REVOKED.value:
            raise Conflict("already revoked", row_id=str(row_id))

        revoked = self._copy(row, state=ExpiryState.REVOKED, confirmed_by=actor, evidence=reason)
        self._record(actor, revoked, "revoke")
        return self._decision(revoked, "withdrawn; the clause is served unflagged again")

    # -------------------------------------------------------------------------- internals

    def _append(self, **fields: Any) -> ClauseSupersessionRow:
        """Write a new row and close whatever it replaces, in one transaction."""
        old: ClauseRef = fields.pop("old")
        new: ClauseRef | None = fields.pop("new")
        state: ExpiryState = fields.pop("state")
        now = datetime.now(UTC)

        previous = self.open_row(old)
        if previous is not None:
            previous.closed_at = now
            self._session.flush()

        row = ClauseSupersessionRow(
            id=uuid.uuid4(),
            old_document_id=old.document_id,
            old_section_path=old.section_path,
            new_document_id=new.document_id if new else None,
            new_section_path=new.section_path if new else None,
            state=state.value,
            decided_at=now if fields.get("confirmed_by") else None,
            created_at=now,
            **fields,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def _copy(
        self,
        row: ClauseSupersessionRow,
        *,
        state: ExpiryState,
        confirmed_by: str,
        evidence: str | None = None,
    ) -> ClauseSupersessionRow:
        return self._append(
            old=ClauseRef(row.old_document_id, row.old_section_path),
            new=(
                ClauseRef(row.new_document_id, row.new_section_path)
                if row.new_document_id and row.new_section_path
                else None
            ),
            basis=row.basis,
            state=state,
            supersedes_from=row.supersedes_from,
            verdict=row.verdict,
            evidence=evidence if evidence is not None else row.evidence,
            quantity_delta=row.quantity_delta,
            scope_facets=row.scope_facets,
            score=row.score,
            model=row.model,
            prompt_version=row.prompt_version,
            detected_by=row.detected_by,
            confirmed_by=confirmed_by,
        )

    def _check_four_eyes(self, row: ClauseSupersessionRow, actor: str) -> None:
        """A regulated clause is not withdrawn from an answer by one person acting alone.

        Same rule and same reasoning as the expiry ledger's: the person who proposed may not
        confirm. A machine-detected proposal needs exactly one human, because `detected_by` is
        a detector and can never collide with an actor.
        """
        document = repo.get_document(self._session, row.old_document_id)
        if document is None:  # pragma: no cover - the FK guarantees it
            return
        if DocClass(document.doc_class) not in NO_AUTOMATION_CLASSES:
            return
        if row.detected_by == actor:
            from kb_common.errors import GateBlocked

            raise GateBlocked(
                "four-eyes: the person who proposed this supersession may not confirm it",
                document_id=str(row.old_document_id),
                doc_class=document.doc_class,
                invariant="INV-8",
            )

    def _require_document(self, document_id: uuid.UUID) -> None:
        if repo.get_document(self._session, document_id) is None:
            raise NotFound("document not found", document_id=str(document_id))

    def _require_open(self, row_id: uuid.UUID) -> ClauseSupersessionRow:
        row = self._session.get(ClauseSupersessionRow, row_id)
        if row is None:
            raise NotFound("supersession row not found", row_id=str(row_id))
        if row.closed_at is not None:
            raise Conflict(
                "this row has been replaced by a later decision; act on the open one",
                row_id=str(row_id),
            )
        return row

    def _decision(self, row: ClauseSupersessionRow, note: str) -> SupersessionDecision:
        return SupersessionDecision(
            row_id=row.id,
            old=ClauseRef(row.old_document_id, row.old_section_path),
            new=(
                ClauseRef(row.new_document_id, row.new_section_path)
                if row.new_document_id and row.new_section_path
                else None
            ),
            state=row.state,
            supersedes_from=row.supersedes_from,
            note=note,
        )

    def _record(self, actor: str, row: ClauseSupersessionRow, verb: str) -> None:
        if self._audit is None:
            return
        self._audit.write(
            AuditRecord(
                action=AuditAction.CLAUSE_SUPERSESSION,
                actor=actor,
                object_ref={
                    "document_id": str(row.old_document_id),
                    "section_path": row.old_section_path,
                    "supersession_id": str(row.id),
                },
                detail={
                    "verb": verb,
                    "state": row.state,
                    "basis": row.basis,
                    "supersedes_from": row.supersedes_from.isoformat(),
                    "replaced_by": (
                        f"{row.new_document_id}:{row.new_section_path}"
                        if row.new_document_id
                        else None
                    ),
                    "verdict": row.verdict,
                },
            )
        )


__all__ = ["ClauseRef", "ClauseSupersessions", "SupersessionDecision"]
