"""The expiry ledger: deciding that an instrument stopped applying, and applying it.

`effective_to` on `chunks` has been read by the ACL predicate since M2 — set it and the chunk
stops being retrievable that day, inside the query, with no post-filtering. Nothing ever wrote
it, so an instrument that ceased to apply was served as current forever. This module is the
half that decides (ADR-0030) and the half that applies (ADR-0031).

Three properties everything here is built to keep:

* **Append-only.** No row is ever updated. Confirming a proposal writes a *new* row and closes
  the old one; so does revoking. If `state` mutated in place, "what did we believe on 3 May"
  would return today's belief wearing a past date, which is the one question the
  `created_at`/`closed_at` pair exists to answer (INV-11).
* **Serving never waits for a job.** The dates are projected onto the chunk copies at
  *confirmation* time, not on the day the expiry lands. The predicate compares them against
  the query date, so a chunk whose end date was written in February stops being retrievable on
  1 May with nothing running at all. `expiry_sweep` does bookkeeping, never correctness.
* **Only `confirmed` is served.** A detector writes `proposed`, and a proposal changes nothing
  a user can see.

The projection writes `chunks` and never `document_versions`: a version is immutable (INV-9),
and its own `effective_to` is a different fact — a sunset the document stated about itself, at
publication. When both exist the ledger wins for serving and both stay visible on the
inspection screen, because a steward who typed a date into a form needs to see why a different
one is in force.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from kb_common.audit import AuditAction, AuditRecord, AuditSink
from kb_common.db import affected_rows
from kb_common.errors import Conflict, GateBlocked, NotFound, ValidationError
from kb_common.logging import get_logger
from kb_schemas.enums import NO_AUTOMATION_CLASSES, DocClass, ExpiryBasis, ExpiryState
from kb_schemas.orm import DocumentExpiryRow, DocumentRow
from kb_vntext.sections import anchor_families, anchor_matches
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from kb_registry import repository as repo

log = get_logger(__name__)


#: `[OPEN]`-7. An abrogated instrument applied through the day *before* the abrogating one took
#: effect — the predicate is inclusive, so this one line decides whether a repealed rule is
#: served for one extra day or a live rule is hidden for one. Legal has still to confirm in
#: writing whether the abrogating document's *effective* date or its *issue* date ends the
#: target; the assumption is baked in here and nowhere else, so the ruling is one edit.
def expiry_date_for_abrogation(source_effective_from: date) -> date:
    return source_effective_from - timedelta(days=1)


@dataclass(frozen=True, slots=True)
class ExpiryDecision:
    """What a ledger write did — including, deliberately, what it did *not* do."""

    row_id: uuid.UUID
    document_id: uuid.UUID
    effective_to: date
    state: str
    chunks_projected: int = 0
    #: False when the row was recorded but nothing was projected. True today only for a
    #: whole-document expiry: a partial one names clauses, and resolving an anchor to the
    #: chunks it means is M9c's (ADR-0040). A steward is told, rather than left believing a
    #: clause left service when it did not.
    applied: bool = True
    note: str = ""


class ExpiryLedger:
    def __init__(self, session: Session, *, audit: AuditSink | None = None) -> None:
        self._session = session
        self._audit = audit

    # ------------------------------------------------------------------------------ reads

    def open_row(
        self, document_id: uuid.UUID, *, anchors: Sequence[str] | None = None
    ) -> DocumentExpiryRow | None:
        """The current belief for one scope, whatever its state."""
        scope = _normalize_anchors(anchors)
        stmt = select(DocumentExpiryRow).where(
            DocumentExpiryRow.document_id == document_id,
            DocumentExpiryRow.closed_at.is_(None),
        )
        for row in self._session.execute(stmt).scalars():
            if _normalize_anchors(row.anchors) == scope:
                return row
        return None

    def history(self, document_id: uuid.UUID) -> list[DocumentExpiryRow]:
        """Every row, oldest first — the sequence the inspection screen shows."""
        stmt = (
            select(DocumentExpiryRow)
            .where(DocumentExpiryRow.document_id == document_id)
            .order_by(DocumentExpiryRow.created_at, DocumentExpiryRow.id)
        )
        return list(self._session.execute(stmt).scalars())

    def belief_at(self, document_id: uuid.UUID, when: datetime) -> list[DocumentExpiryRow]:
        """What this platform believed on a given day — the second clock.

        Distinct from `as_of`, which asks what was in force in the world. A reviewer
        investigating a past answer needs this one and will otherwise reach for the wrong one.
        """
        stmt = select(DocumentExpiryRow).where(
            DocumentExpiryRow.document_id == document_id,
            DocumentExpiryRow.created_at <= when,
            (DocumentExpiryRow.closed_at.is_(None)) | (DocumentExpiryRow.closed_at > when),
        )
        return list(self._session.execute(stmt).scalars())

    def effective_to_for(self, document_id: uuid.UUID) -> date | None:
        """The date retrieval evaluates: the open confirmed ledger row, else the version's own.

        The chunks already carry this value — that is what the predicate reads. This exists for
        callers reasoning about a document rather than a passage, and as the assertion the
        projection tests compare against.
        """
        row = self.open_row(document_id)
        if row is not None and row.state == ExpiryState.CONFIRMED.value and not row.anchors:
            return row.effective_to
        version = repo.get_canonical_version(self._session, document_id)
        return version.effective_to if version else None

    # ----------------------------------------------------------------------------- writes

    def propose(
        self,
        document_id: uuid.UUID,
        *,
        effective_to: date,
        basis: ExpiryBasis,
        detected_by: str,
        actor: str,
        source_document_id: uuid.UUID | None = None,
        anchors: Sequence[str] | None = None,
        evidence: str | None = None,
    ) -> ExpiryDecision:
        """Record that something ought to expire. Changes nothing a user can see."""
        self._require_document(document_id)
        if basis in _ATTRIBUTED_BASES and source_document_id is None:
            raise ValidationError(
                "an expiry attributed to another instrument must name it", basis=basis.value
            )
        if source_document_id is not None:
            if basis not in _ATTRIBUTED_BASES:
                raise ValidationError(
                    "only an attributed basis may name a source instrument", basis=basis.value
                )
            if source_document_id == document_id:
                raise ValidationError("a document cannot expire itself")
            self._require_document(source_document_id)

        row = self._append(
            document_id=document_id,
            effective_to=effective_to,
            basis=basis.value,
            state=ExpiryState.PROPOSED,
            detected_by=detected_by,
            source_document_id=source_document_id,
            anchors=anchors,
            evidence=evidence,
            decided_by=None,
        )
        self._record(actor, row, "propose", {"evidence": evidence})
        return ExpiryDecision(
            row_id=row.id,
            document_id=document_id,
            effective_to=effective_to,
            state=row.state,
            applied=False,
            note="proposed; nothing is served differently until a steward confirms",
        )

    def confirm(self, row_id: uuid.UUID, *, actor: str) -> ExpiryDecision:
        """Accept a proposal, and project it onto the chunks in the same transaction."""
        row = self._require_open(row_id)
        if row.state == ExpiryState.CONFIRMED.value:
            raise Conflict("already confirmed", row_id=str(row_id))
        self._check_four_eyes(row, actor)

        confirmed = self._append(
            document_id=row.document_id,
            effective_to=row.effective_to,
            basis=row.basis,
            state=ExpiryState.CONFIRMED,
            detected_by=row.detected_by,
            source_document_id=row.source_document_id,
            anchors=row.anchors,
            evidence=row.evidence,
            decided_by=actor,
        )

        projected = self.project(row.document_id)

        if confirmed.anchors:
            # A partial expiry ends the clauses its anchors name and leaves the rest of the
            # instrument in service — the ordinary shape in this corpus, because Vietnamese
            # practice abrogates in pieces (ADR-0040). An anchor that resolves to nothing is
            # reported rather than widened, and the steward is told the difference.
            resolved = self._chunks_for_anchors(row.document_id, confirmed.anchors)
            note = (
                f"{len(resolved)} đoạn hết hiệu lực; phần còn lại của văn bản vẫn có hiệu lực"
                if resolved
                else "không tìm thấy điều khoản nào khớp — chưa có gì bị gỡ khỏi tìm kiếm"
            )
            self._record(
                actor,
                confirmed,
                "confirm",
                {"applied": bool(resolved), "chunks": projected, "partial": True},
            )
            return ExpiryDecision(
                row_id=confirmed.id,
                document_id=row.document_id,
                effective_to=row.effective_to,
                state=confirmed.state,
                chunks_projected=projected,
                applied=bool(resolved),
                note=note,
            )

        self._record(actor, confirmed, "confirm", {"applied": True, "chunks": projected})
        return ExpiryDecision(
            row_id=confirmed.id,
            document_id=row.document_id,
            effective_to=row.effective_to,
            state=confirmed.state,
            chunks_projected=projected,
            applied=True,
        )

    def revoke(self, row_id: uuid.UUID, *, actor: str, reason: str) -> ExpiryDecision:
        """Withdraw an expiry — as a new row, never a deletion.

        "Why did this instrument disappear from search on 3 May, and who put it back?" has to
        be answerable from this table alone.
        """
        if not reason.strip():
            raise ValidationError("a revocation needs a reason")
        row = self._require_open(row_id)
        if row.state == ExpiryState.REVOKED.value:
            raise Conflict("already revoked", row_id=str(row_id))

        revoked = self._append(
            document_id=row.document_id,
            effective_to=row.effective_to,
            basis=row.basis,
            state=ExpiryState.REVOKED,
            detected_by=row.detected_by,
            source_document_id=row.source_document_id,
            anchors=row.anchors,
            evidence=reason,
            decided_by=actor,
        )
        # Only a confirmed row was ever projected, so only that one has to be undone; but
        # re-projecting unconditionally is cheap and idempotent, and it repairs a chunk whose
        # date drifted for any other reason.
        restored = self.project(row.document_id)
        self._record(actor, revoked, "revoke", {"reason": reason, "chunks": restored})
        return ExpiryDecision(
            row_id=revoked.id,
            document_id=row.document_id,
            effective_to=row.effective_to,
            state=revoked.state,
            chunks_projected=restored,
            applied=True,
            note="expiry withdrawn; the document is served again from its own dates",
        )

    # ------------------------------------------------------------------------- projection

    def project(self, document_id: uuid.UUID) -> int:
        """Write the ledger's answer onto this document's serving chunks.

        Idempotent and total. It resets every non-tombstoned chunk to the date its own version
        states, then applies each open confirmed ledger row on top: a whole-document row over
        all of them, a partial row over the clauses its anchors name (ADR-0040).

        Called at confirmation, at revocation, and after any publish or rechunk —
        `_insert_chunks` copies the *version's* date, so without this a republish would quietly
        resurrect an expired document or an expired clause.

        The earliest applicable date wins, so the order rows are applied in cannot matter. A
        clause repealed in March inside a document that ends in December ends in March; a
        document that ends in March takes its clauses with it whatever their own rows say.

        Tombstoned chunks are left alone: they belong to superseded versions, their dates are
        that version's history, and the default path cannot reach them anyway.
        """
        session = self._session
        rows = session.execute(
            text(
                """
                SELECT c.id, c.effective_to, v.effective_to AS version_to
                FROM chunks c
                JOIN document_versions v ON v.id = c.version_id
                WHERE c.document_id = :doc AND NOT c.tombstoned
                """
            ),
            {"doc": document_id},
        ).all()
        if not rows:
            return 0

        # The target for every chunk, computed before anything is written. Doing it this way
        # rather than reset-then-overwrite is what makes the function idempotent: a chunk that
        # is already correct is never touched, so the returned count is the number of rows that
        # actually needed changing — which is what makes calling this on every publish and
        # every rechunk free when nothing has moved.
        target: dict[uuid.UUID, date | None] = {row.id: row.version_to for row in rows}
        for ledger_row in self._open_confirmed(document_id):
            scope = (
                self._chunks_for_anchors(document_id, ledger_row.anchors)
                if ledger_row.anchors
                else list(target)
            )
            if ledger_row.anchors and not scope:
                # An anchor that resolves to nothing is a steward's problem, not a licence to
                # expire the whole document (ADR-0036). Reported, never widened.
                log.warning(
                    "partial_expiry_anchors_unresolved",
                    extra={"document_id": str(document_id), "anchors": list(ledger_row.anchors)},
                )
                continue
            for chunk_id in scope:
                current = target.get(chunk_id)
                target[chunk_id] = (
                    ledger_row.effective_to
                    if current is None
                    else min(current, ledger_row.effective_to)
                )

        # Grouped by date so a document with one expiry costs one statement.
        changes: dict[date | None, list[uuid.UUID]] = {}
        for row in rows:
            if row.effective_to != target[row.id]:
                changes.setdefault(target[row.id], []).append(row.id)

        touched = 0
        for when, chunk_ids in changes.items():
            touched += affected_rows(
                session.execute(
                    text(
                        "UPDATE chunks SET effective_to = CAST(:date AS DATE) "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"date": when, "ids": [str(item) for item in chunk_ids]},
                )
            )
        return touched

    def _open_confirmed(self, document_id: uuid.UUID) -> list[DocumentExpiryRow]:
        """Every current belief that is actually served. Only `confirmed` counts."""
        stmt = select(DocumentExpiryRow).where(
            DocumentExpiryRow.document_id == document_id,
            DocumentExpiryRow.closed_at.is_(None),
            DocumentExpiryRow.state == ExpiryState.CONFIRMED.value,
        )
        return list(self._session.execute(stmt).scalars())

    def _chunks_for_anchors(
        self, document_id: uuid.UUID, anchors: Sequence[str]
    ) -> list[uuid.UUID]:
        """The serving chunks a set of clause anchors names.

        The same matching the reference resolver applies, from the same two functions, because
        they ask one question of one column: a clause a reference resolves to and a clause a
        partial expiry ends must be the same clause (ADR-0036).

        Resolved in Python over `(id, anchor)` pairs rather than in SQL. A document has tens to
        hundreds of chunks, so the read is trivial, and it means the matching rule has exactly
        one implementation instead of one in each caller's WHERE clause.
        """
        families = anchor_families(anchors)
        if not families:
            return []
        rows = self._session.execute(
            text(
                "SELECT id, anchor FROM chunks "
                "WHERE document_id = :doc AND NOT tombstoned AND anchor IS NOT NULL"
            ),
            {"doc": document_id},
        ).all()

        under: dict[str, list[uuid.UUID]] = {}
        exact: dict[str, list[uuid.UUID]] = {}
        for row in rows:
            for family in families:
                if anchor_matches(row.anchor, family):
                    under.setdefault(family, []).append(row.id)
            exact.setdefault(str(row.anchor), []).append(row.id)

        found: list[uuid.UUID] = []
        for anchor in anchors:
            # Clause → article fallback, and only to a chunk anchored at the article exactly:
            # the merged-clause case. Never to the article's other clauses, which a reference
            # to khoản 2 does not name and an expiry of khoản 2 must not end.
            for chunk_id in under.get(anchor) or exact.get(anchor.split(".")[0]) or []:
                if chunk_id not in found:
                    found.append(chunk_id)
        return found

    # ---------------------------------------------------------------------- the detectors

    def propose_from_abrogations(
        self, document_id: uuid.UUID, *, actor: str
    ) -> list[ExpiryDecision]:
        """Every instrument this published document abrogates gets a proposed expiry.

        Runs on publish, because a draft that says it repeals something must not expire
        anything, and because `effective_from` is fixed by then. Idempotent: a scope that
        already has an open row from this same source is left alone — including a `revoked`
        one, since re-proposing what a steward has already turned down would put it back in the
        queue on every republish.

        Nothing here decides. The row is `proposed`, the sentence-level evidence is the edge,
        and a person confirms (INV-8 for the regulated classes, `[OPEN]`-6 for the rest).
        """
        source = self._require_document(document_id)
        if source.status != "published":
            return []
        if source.canonical_version_id is None:
            return []
        version = repo.get_canonical_version(self._session, document_id)
        if version is None or version.effective_from is None:
            # Without a date on the abrogating instrument there is nothing to date the expiry
            # from, and inventing one would be exactly what ADR-0029 refuses. The reviewer
            # supplies the effective date, and the next publish proposes.
            log.info(
                "abrogation_expiry_skipped_no_effective_date",
                extra={"document_id": str(document_id)},
            )
            return []

        ends_on = expiry_date_for_abrogation(version.effective_from)
        decisions: list[ExpiryDecision] = []
        for edge in repo.list_edges_from(self._session, document_id):
            if edge.ref_type != "abrogates":
                continue
            existing = self.open_row(edge.dst_document_id)
            if existing is not None and existing.source_document_id == document_id:
                continue
            decisions.append(
                self.propose(
                    edge.dst_document_id,
                    effective_to=ends_on,
                    basis=ExpiryBasis.ABROGATED_BY,
                    detected_by=f"abrogates_edge:{edge.id}",
                    actor=actor,
                    source_document_id=document_id,
                    evidence=(
                        f"{source.title} bãi bỏ văn bản này; có hiệu lực từ "
                        f"{version.effective_from.isoformat()}."
                    ),
                )
            )
        return decisions

    # ------------------------------------------------------------------------- internals

    def _append(
        self,
        *,
        document_id: uuid.UUID,
        effective_to: date,
        basis: str,
        state: ExpiryState,
        detected_by: str,
        source_document_id: uuid.UUID | None,
        anchors: Sequence[str] | None,
        evidence: str | None,
        decided_by: str | None,
    ) -> DocumentExpiryRow:
        """Write a new row and close whatever it replaces, in one transaction."""
        now = datetime.now(UTC)
        previous = self.open_row(document_id, anchors=anchors)
        if previous is not None:
            previous.closed_at = now
            self._session.flush()

        row = DocumentExpiryRow(
            id=uuid.uuid4(),
            document_id=document_id,
            effective_to=effective_to,
            basis=basis,
            source_document_id=source_document_id,
            anchors=_normalize_anchors(anchors),
            articles=_articles_from(anchors),
            evidence=evidence,
            state=state.value,
            detected_by=detected_by,
            decided_by=decided_by,
            decided_at=now if decided_by else None,
            created_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def _check_four_eyes(self, row: DocumentExpiryRow, actor: str) -> None:
        """A regulated document is not withdrawn from service by one person acting alone.

        Withdrawing what the bank tells people changes what the bank tells people as surely as
        publishing does, which is what INV-8 exists for. The rule is the same shape as the
        merge approval's: the person who *proposed* may not be the person who confirms.

        A machine-detected proposal therefore needs exactly one human — `detected_by` is a
        detector, never an actor, so it can never collide. That is the intended reading of
        ADR-0030's "a detector writes `proposed`, a human confirms", and it is why a steward's
        own proposal records them in the same column: so this check can see it.
        """
        document = repo.get_document(self._session, row.document_id)
        if document is None:  # pragma: no cover - the row's FK guarantees it
            return
        if DocClass(document.doc_class) not in NO_AUTOMATION_CLASSES:
            return
        if row.detected_by == actor:
            raise GateBlocked(
                "four-eyes: the person who proposed this expiry may not confirm it",
                document_id=str(row.document_id),
                doc_class=document.doc_class,
                proposed_by=row.detected_by,
                invariant="INV-8",
            )

    def _require_document(self, document_id: uuid.UUID) -> DocumentRow:
        row = repo.get_document(self._session, document_id)
        if row is None:
            raise NotFound("document not found", document_id=str(document_id))
        return row

    def _require_open(self, row_id: uuid.UUID) -> DocumentExpiryRow:
        row = self._session.get(DocumentExpiryRow, row_id)
        if row is None:
            raise NotFound("expiry row not found", row_id=str(row_id))
        if row.closed_at is not None:
            raise Conflict(
                "this row has been replaced by a later decision; act on the open one",
                row_id=str(row_id),
            )
        return row

    def _record(
        self,
        actor: str,
        row: DocumentExpiryRow,
        verb: str,
        detail: dict[str, object],
    ) -> None:
        if self._audit is None:
            return
        self._audit.write(
            AuditRecord(
                action=AuditAction.EXPIRY_DECISION,
                actor=actor,
                object_ref={"document_id": str(row.document_id), "expiry_id": str(row.id)},
                detail={
                    "verb": verb,
                    "state": row.state,
                    "basis": row.basis,
                    "effective_to": row.effective_to.isoformat(),
                    "anchors": list(row.anchors) if row.anchors else [],
                    "source_document_id": (
                        str(row.source_document_id) if row.source_document_id else None
                    ),
                    **detail,
                },
            )
        )


#: Bases that attribute the decision to another instrument, and therefore must name one.
_ATTRIBUTED_BASES = frozenset({ExpiryBasis.ABROGATED_BY, ExpiryBasis.DECLARED_BY})


def _normalize_anchors(anchors: Sequence[str] | None) -> list[str] | None:
    """NULL and `{}` both mean the whole document, and must not become two scopes."""
    if not anchors:
        return None
    return sorted({str(anchor).strip() for anchor in anchors if str(anchor).strip()}) or None


def _articles_from(anchors: Sequence[str] | None) -> list[int] | None:
    """The article numbers behind a set of anchors, kept alongside them.

    `anchors` is clause-precise ("12.2"); `articles` is what the impact traversal reads, which
    asks a genuinely article-shaped question. Derived rather than passed in, so the two cannot
    disagree (ADR-0036).
    """
    normalized = _normalize_anchors(anchors)
    if not normalized:
        return None
    articles = set()
    for anchor in normalized:
        head = anchor.split(".")[0].strip()
        if head.isdigit():
            articles.add(int(head))
    return sorted(articles) or None


__all__ = ["ExpiryDecision", "ExpiryLedger", "expiry_date_for_abrogation"]
