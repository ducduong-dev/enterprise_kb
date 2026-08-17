"""The funnel — the thing that actually runs the gates over a corpus (M9d, ADR-0033).

Gates 1 to 5 each answer one question well and none of them knows when to run. This does: it
decides which pairs are worth forming at all, drives them through `find_candidates` and
`ClauseAdjudicator`, records what came back, and turns the one verdict with a consequence into
a proposal a steward can see.

**Three paths, by what evidence already exists**, and they are per *document pair* rather than
per document — a document routinely has an edge naming articles to one instrument and no edge
at all to another, and those two neighbours must be treated differently in the same run:

* **Path A — the edge names the articles.** Nothing to detect: the corpus already said which
  articles moved and ADR-0032 flags exactly those chunks. Detection is skipped for that
  neighbour entirely. Running it anyway would mean a machine second-guessing a confirmed edge,
  and the machine losing is the *good* outcome there.
* **Path B — an edge with no article list.** The two documents are known to be related, so the
  search space is those two documents and the subject gate relaxes. Much cheaper than C, and a
  far higher prior that a matching pair is real.
* **Path C — no edge.** The corpus, the strict subject gate, and the case the whole funnel was
  built for: a later instrument re-states a rule and never mentions the earlier one.

**Direction never comes from the run.** The funnel is invoked with a document, but which clause
of a pair is the replaced one is decided inside `adjudicate` from the legal dates. Running it
over a 2023 circular ingested today will happily propose that a 2026 clause already in the index
replaced one of *its* clauses — which is the case the archive's ingest order makes routine and
which a one-directional detector would silently never find.

**Nothing here decides anything a user can see.** Every supersession lands `proposed` with a
`clause_review` task beside it, and the other three verdicts land in `clause_pair_verdicts`,
which no serving path reads. The funnel's output is a queue.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

from kb_common.audit import AuditSink
from kb_common.logging import get_logger
from kb_schemas.enums import ReviewTaskType, SupersessionBasis, SupersessionVerdict
from kb_schemas.orm import ClausePairVerdictRow
from kb_vntext.legal_numbers import extract_legal_numbers
from sqlalchemy import text
from sqlalchemy.orm import Session

from kb_registry import repository as repo
from kb_registry.adjudicate import Adjudication, Clause, ClauseAdjudicator
from kb_registry.candidates import Candidate, find_candidates
from kb_registry.demand import DemandReport, rank
from kb_registry.gates import Window
from kb_registry.service import RegistryService
from kb_registry.supersession import ClauseRef, ClauseSupersessions

log = get_logger(__name__)

#: Who a row says detected it. Distinguishes the funnel's proposals from a steward's own and
#: from the declared path's, which matters when judging the false-supersession rate.
DETECTED_BY = "m9d_funnel"


@dataclass(frozen=True, slots=True)
class PairOutcome:
    """One pair, and what became of it."""

    left: ClauseRef
    right: ClauseRef
    path: str
    adjudication: Adjudication
    #: The `clause_supersessions` row, when the verdict had a consequence.
    proposal_id: uuid.UUID | None = None
    #: True when a stored verdict answered it and nothing was recomputed.
    from_cache: bool = False


@dataclass(slots=True)
class FunnelReport:
    """What a run did, in the terms ADR-0033 asks the eval to report."""

    document_id: uuid.UUID
    clauses: int = 0
    #: Neighbours skipped because their edge already names articles (Path A).
    skipped_neighbours: set[uuid.UUID] = field(default_factory=set)
    outcomes: list[PairOutcome] = field(default_factory=list)

    @property
    def pairs(self) -> int:
        return len(self.outcomes)

    @property
    def proposals(self) -> int:
        return sum(1 for item in self.outcomes if item.proposal_id is not None)

    @property
    def by_gate(self) -> int:
        """Pairs the cheap gates settled. Half of ADR-0033's split."""
        return sum(
            1
            for item in self.outcomes
            if not item.from_cache and not item.adjudication.called_model
        )

    @property
    def by_model(self) -> int:
        """Pairs the model settled. The other half, and what gate 5 costs."""
        return sum(
            1 for item in self.outcomes if not item.from_cache and item.adjudication.called_model
        )

    @property
    def unresolved(self) -> int:
        """Pairs nothing concluded about — a model that was down, a reply nobody could read.
        Nothing is stored for these, so the next run retries them."""
        return sum(
            1 for item in self.outcomes if item.adjudication.settled_by == "model_unavailable"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "document_id": str(self.document_id),
            "clauses": self.clauses,
            "pairs": self.pairs,
            "by_gate": self.by_gate,
            "by_model": self.by_model,
            "unresolved": self.unresolved,
            "proposals": self.proposals,
            "skipped_neighbours": len(self.skipped_neighbours),
        }


_CLAUSES = """
    SELECT c.id, c.document_id, c.section_path, c.text, c.effective_from, c.effective_to,
           c.citation_label
    FROM chunks c
    WHERE c.document_id = :document_id
      AND NOT c.tombstoned
      AND c.section_path IS NOT NULL
    ORDER BY c.ordinal
"""

#: Every neighbour this document has an edge to, in either direction, and whether that edge
#: already says which articles moved. `anchors` is the clause-precise form and `articles` is
#: derived from it (ADR-0036), so either being present means Path A.
_NEIGHBOURS = """
    SELECT CASE WHEN src_document_id = :document_id THEN dst_document_id
                ELSE src_document_id END AS neighbour,
           bool_or(coalesce(array_length(articles, 1), 0) > 0
                   OR coalesce(array_length(anchors, 1), 0) > 0) AS names_articles
    FROM document_refs
    WHERE src_document_id = :document_id OR dst_document_id = :document_id
    GROUP BY neighbour
"""


class ClauseFunnel:
    """Runs M9d's detection over one document's clauses."""

    def __init__(
        self,
        session: Session,
        adjudicator: ClauseAdjudicator,
        *,
        audit: AuditSink | None = None,
        actor: str = DETECTED_BY,
    ) -> None:
        self._session = session
        self._adjudicator = adjudicator
        self._registry = RegistryService(session, audit=audit)
        self._supersessions = ClauseSupersessions(session, audit=audit)
        self._actor = actor
        self._instruments: dict[uuid.UUID, str | None] = {}

    def run(
        self,
        document_id: uuid.UUID,
        *,
        demand: DemandReport | None = None,
        limit: int | None = None,
    ) -> FunnelReport:
        """Detect over one document, cheapest neighbours first.

        `demand` orders the clauses by how often each has actually been served (ADR-0033): a
        backfill over 3,000 documents produces more work than anyone can do, and the clauses
        being served today are the ones doing real harm. `limit` caps a single run, which is
        what makes the backfill resumable in bounded chunks rather than one transaction nobody
        dares restart.
        """
        report = FunnelReport(document_id=document_id)
        clauses = self._clauses_of(document_id)
        if not clauses:
            return report

        ordered = self._order(clauses, demand)
        report.clauses = len(ordered)

        neighbours = self._neighbours(document_id)
        report.skipped_neighbours = {
            other for other, names_articles in neighbours.items() if names_articles
        }
        path_b = [other for other, names_articles in neighbours.items() if not names_articles]

        seen: set[tuple[ClauseRef, ClauseRef]] = set()
        for chunk_id, source in ordered:
            if limit is not None and report.pairs >= limit:
                break
            for candidate, path in self._candidates(chunk_id, path_b, report.skipped_neighbours):
                pair = _normalize(
                    source.ref, ClauseRef(candidate.document_id, candidate.section_path or "")
                )
                if pair in seen:
                    continue
                seen.add(pair)
                outcome = self._pair(source, candidate, path)
                if outcome is not None:
                    report.outcomes.append(outcome)
                if limit is not None and report.pairs >= limit:
                    break

        log.info("clause_funnel_ran", extra=report.as_dict())
        return report

    # ------------------------------------------------------------------------- candidates

    def _candidates(
        self, chunk_id: uuid.UUID, path_b: list[uuid.UUID], skipped: set[uuid.UUID]
    ) -> list[tuple[Candidate, str]]:
        """Path B first, then Path C over everything neither path has covered.

        Path B runs per neighbour because `find_candidates(within_document=…)` is what relaxes
        the subject gate, and relaxing it corpus-wide would flood gate 5. Path C's results are
        then filtered rather than re-queried: a candidate in a Path A document must not be
        adjudicated at all, and one in a Path B document has already been found with the better
        gate.
        """
        found: list[tuple[Candidate, str]] = []
        covered = set(path_b) | skipped
        for other in path_b:
            found.extend(
                (candidate, "B")
                for candidate in find_candidates(self._session, chunk_id, within_document=other)
            )
        found.extend(
            (candidate, "C")
            for candidate in find_candidates(self._session, chunk_id)
            if candidate.document_id not in covered
        )
        return found

    # ------------------------------------------------------------------------ one pair

    def _pair(self, source: Clause, candidate: Candidate, path: str) -> PairOutcome | None:
        if not candidate.section_path:
            # Nothing to anchor a verdict on. A chunk with no structural path cannot be
            # addressed after a rechunk, so recording a decision about it would be recording a
            # pointer that dangles by design.
            return None

        other = Clause(
            ref=ClauseRef(candidate.document_id, candidate.section_path),
            text=candidate.text,
            window=Window(candidate.effective_from, candidate.effective_to),
            instrument=self._instrument_of(candidate.document_id),
            label=None,
        )
        digest = _digest(source.text, other.text, self._adjudicator.prompt_version)
        stored = self._stored(source.ref, other.ref, digest)
        if stored is not None:
            return PairOutcome(
                left=source.ref,
                right=other.ref,
                path=path,
                adjudication=stored,
                from_cache=True,
            )

        adjudication = self._adjudicator.adjudicate(source, other)
        if adjudication.verdict is None:
            # Either not a pair (gate 2) or nothing concluded (the model was unreachable).
            # Neither is a decision, so neither is stored, and the next run tries again.
            return PairOutcome(
                left=source.ref, right=other.ref, path=path, adjudication=adjudication
            )

        self._record(source.ref, other.ref, adjudication, digest)
        proposal_id = (
            self._propose(adjudication, path) if adjudication.records_supersession else None
        )
        return PairOutcome(
            left=source.ref,
            right=other.ref,
            path=path,
            adjudication=adjudication,
            proposal_id=proposal_id,
        )

    def _propose(self, adjudication: Adjudication, path: str) -> uuid.UUID | None:
        """The one verdict with a consequence, and the queue entry that makes it visible.

        `basis` is always `detected`, on both paths. It was tempting to call Path B
        `edge_article`, and that would have been wrong: `edge_article` means the corpus named
        the articles, which is Path A and is skipped here. On Path B the edge said only that
        two documents are related — the clause pairing is still our inference, and labelling it
        as the corpus's would overstate the evidence on exactly the rows a steward is deciding.
        The path is recorded on the task instead, where it is context rather than provenance.

        Neither path writes the expiry ledger and neither hides anything: an inference is our
        conclusion about two texts, and only a declaration is the corpus stating that a rule
        ended (ADR-0040).
        """
        assert adjudication.old is not None and adjudication.supersedes_from is not None
        existing = self._supersessions.open_row(adjudication.old)
        if existing is not None:
            # Something already answers "what replaced this clause" — a declaration, a steward,
            # or an earlier run. A detection must not overwrite it: the declared path is
            # stronger evidence by construction, and re-proposing would close a confirmed row.
            return None

        decision = self._supersessions.propose(
            adjudication.old,
            new=adjudication.new,
            supersedes_from=adjudication.supersedes_from,
            basis=SupersessionBasis.DETECTED,
            detected_by=DETECTED_BY,
            actor=self._actor,
            verdict=SupersessionVerdict.SUPERSEDED,
            evidence=adjudication.rationale or None,
            quantity_delta=adjudication.quantity_delta,
            scope_facets=adjudication.scope_facets,
            score=adjudication.score,
            model=adjudication.model,
            prompt_version=adjudication.prompt_version,
        )
        self._open_task(adjudication, decision.row_id, path)
        return decision.row_id

    def _open_task(self, adjudication: Adjudication, row_id: uuid.UUID, path: str) -> None:
        """A `clause_review` task on the *older* clause's document.

        That document is the one whose serving changes if the proposal is confirmed, so it is
        the one a steward wants open. Silently skipped when the document has no canonical
        version, which means it is not published and nothing is being served from it anyway.
        """
        assert adjudication.old is not None
        document = repo.get_document(self._session, adjudication.old.document_id)
        if document is None or document.canonical_version_id is None:
            return
        self._registry.open_review_task(
            document.canonical_version_id,
            ReviewTaskType.CLAUSE_REVIEW,
            assignee_group=self._registry.steward_group_for(str(document.category_path)),
            payload={
                "supersession_id": str(row_id),
                "old": _ref_payload(adjudication.old),
                "new": _ref_payload(adjudication.new),
                # What makes the queue workable: "8%/năm → 10%/năm" rather than a score.
                "quantity_delta": adjudication.quantity_delta,
                "rationale": adjudication.rationale,
                "settled_by": adjudication.settled_by,
                "path": path,
            },
        )

    # ------------------------------------------------------------------------ persistence

    def _stored(self, left: ClauseRef, right: ClauseRef, digest: str) -> Adjudication | None:
        """A verdict already reached about this pair, if it is still about this text."""
        first, second = _normalize(left, right)
        row = (
            self._session.query(ClausePairVerdictRow)
            .filter(
                ClausePairVerdictRow.left_document_id == first.document_id,
                ClausePairVerdictRow.left_section_path == first.section_path,
                ClausePairVerdictRow.right_document_id == second.document_id,
                ClausePairVerdictRow.right_section_path == second.section_path,
            )
            .one_or_none()
        )
        if row is None or row.text_digest != digest:
            return None
        return Adjudication(
            verdict=SupersessionVerdict(row.verdict),
            settled_by=row.settled_by,  # type: ignore[arg-type]
            rationale=row.rationale or "",
            quantity_delta=row.quantity_delta,
            scope_facets=row.scope_facets,
            score=row.score,
            model=row.model,
            prompt_version=row.prompt_version,
        )

    def _record(
        self, left: ClauseRef, right: ClauseRef, adjudication: Adjudication, digest: str
    ) -> None:
        """Store the conclusion, replacing any stale one for the same pair.

        A replace and not an append: this is a record of work rather than a belief anyone
        reasons about historically, and `text_digest` already says whether it is current. The
        ledgers next door are append-only for the opposite reason — there, "what would we have
        answered on date D" is a question somebody asks.
        """
        assert adjudication.verdict is not None
        first, second = _normalize(left, right)
        self._session.query(ClausePairVerdictRow).filter(
            ClausePairVerdictRow.left_document_id == first.document_id,
            ClausePairVerdictRow.left_section_path == first.section_path,
            ClausePairVerdictRow.right_document_id == second.document_id,
            ClausePairVerdictRow.right_section_path == second.section_path,
        ).delete(synchronize_session=False)
        self._session.add(
            ClausePairVerdictRow(
                id=uuid.uuid4(),
                left_document_id=first.document_id,
                left_section_path=first.section_path,
                right_document_id=second.document_id,
                right_section_path=second.section_path,
                verdict=adjudication.verdict.value,
                settled_by=adjudication.settled_by,
                rationale=adjudication.rationale or None,
                quantity_delta=adjudication.quantity_delta,
                scope_facets=adjudication.scope_facets,
                score=adjudication.score,
                model=adjudication.model,
                prompt_version=adjudication.prompt_version,
                text_digest=digest,
                detected_by=DETECTED_BY,
                created_at=repo.now(),
            )
        )
        self._session.flush()

    # ------------------------------------------------------------------------- reading

    def _clauses_of(self, document_id: uuid.UUID) -> list[tuple[uuid.UUID, Clause]]:
        instrument = self._instrument_of(document_id)
        rows = (
            self._session.execute(text(_CLAUSES), {"document_id": str(document_id)})
            .mappings()
            .all()
        )
        return [
            (
                row["id"],
                Clause(
                    ref=ClauseRef(row["document_id"], row["section_path"]),
                    text=row["text"],
                    window=Window(row["effective_from"], row["effective_to"]),
                    instrument=instrument,
                    label=row["citation_label"],
                ),
            )
            for row in rows
        ]

    def _neighbours(self, document_id: uuid.UUID) -> dict[uuid.UUID, bool]:
        rows = self._session.execute(text(_NEIGHBOURS), {"document_id": str(document_id)}).all()
        return {row.neighbour: bool(row.names_articles) for row in rows}

    def _instrument_of(self, document_id: uuid.UUID) -> str | None:
        """The instrument code (`"ND"`, `"TT"`), for `older_first`'s same-day tie-break.

        Read from the legal number rather than stored: it is already there, and a second column
        holding a fact derivable from the first is a second thing to keep in step.
        """
        if document_id in self._instruments:
            return self._instruments[document_id]
        document = repo.get_document(self._session, document_id)
        found = (
            extract_legal_numbers(document.legal_number)
            if document and document.legal_number
            else []
        )
        self._instruments[document_id] = found[0].instrument_type if found else None
        return self._instruments[document_id]

    def _order(
        self, clauses: list[tuple[uuid.UUID, Clause]], demand: DemandReport | None
    ) -> list[tuple[uuid.UUID, Clause]]:
        if demand is None:
            return clauses
        wanted = rank(
            demand, [(item.ref.document_id, item.ref.section_path) for _, item in clauses]
        )
        position = {pair: index for index, pair in enumerate(wanted)}
        return sorted(
            clauses,
            key=lambda item: position.get(
                (item[1].ref.document_id, item[1].ref.section_path), len(position)
            ),
        )


def _normalize(left: ClauseRef, right: ClauseRef) -> tuple[ClauseRef, ClauseRef]:
    """One identity per pair, so A-against-B and B-against-A are the same row.

    Ordered on `(document_id, section_path)`, which is what the table's `ck_pair_normalized`
    check enforces. Direction is deliberately not part of it: it lives in
    `clause_supersessions` where it means something, and encoding it here would give one pair
    two identities and let a backfill do the work twice.
    """
    return (
        (left, right)
        if (str(left.document_id), left.section_path) < (str(right.document_id), right.section_path)
        else (right, left)
    )


def _digest(left_text: str, right_text: str, prompt_version: str | None) -> str:
    """What the verdict was about, hashed.

    Both texts and the prompt version, order-independent so it matches whichever way the pair
    was formed. A clause that was reworded no longer matches its stored verdict and is
    adjudicated again — the same rule as ADR-0035's cache key, and for the same reason: an
    identifier survives an edit that changes the answer, and the text does not.
    """
    first, second = sorted((left_text, right_text))
    joined = "\x1f".join((first, second, prompt_version or ""))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _ref_payload(ref: ClauseRef | None) -> dict[str, str] | None:
    return (
        {"document_id": str(ref.document_id), "section_path": ref.section_path}
        if ref is not None
        else None
    )


__all__ = ["DETECTED_BY", "ClauseFunnel", "FunnelReport", "PairOutcome"]
