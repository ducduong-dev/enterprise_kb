"""Change-impact traversal: who else has to look at this?

When a circular changes, the bank's own documents that implement it are now potentially wrong.
Nobody notices — the policy still reads correctly, it just no longer matches the regulation it
was written against. That gap is what this traversal closes: a consolidated regulation opens a
review task on every internal document that implements it.

Direction matters. Edges point from the implementing document to the instrument it implements
(`policy --implements--> circular`), so impact runs **upstream to downstream**: find the
documents whose edges point *at* the changed one.

Two things keep the result useful rather than noise:

* **Article filtering.** An edge records which articles it implements (INV-10's `articles`
  column). A policy implementing Điều 12 is not affected by a change to Điều 40, and telling
  its owner otherwise is how impact tasks get ignored.
* **Depth.** A policy implements a circular; a procedure implements the policy. Both are
  affected, and the second-order case is exactly the one people miss — but three hops out the
  signal is gone, so the traversal stops.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol, TypedDict

from kb_common.logging import get_logger
from kb_registry import repository as repo
from kb_schemas.enums import DocClass, ReviewTaskType
from kb_schemas.orm import ReviewTaskRow
from sqlalchemy import text
from sqlalchemy.orm import Session

log = get_logger(__name__)

#: How far downstream to walk. One hop is the policy, two is the procedure implementing it;
#: beyond that the connection is too weak to justify interrupting someone.
MAX_DEPTH = 2
#: Edge types that mean "this document depends on that one".
DEPENDENT_REF_TYPES = ("implements", "cites")
#: Only `implements` carries a real obligation. A citation is worth flagging at depth 1 and
#: not worth chasing further.
STRONG_REF_TYPE = "implements"


class DependentRow(TypedDict):
    """One row of the dependents query, named so the traversal below stays readable."""

    id: uuid.UUID
    title: str
    doc_class: str
    department: str | None
    category_path: str
    canonical_version_id: uuid.UUID | None
    ref_type: str
    articles: list[int] | None


class StewardLookup(Protocol):
    """Just enough of `RegistryService` to route a task to the right queue."""

    def steward_group_for(self, category_path: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class ImpactedDocument:
    document_id: uuid.UUID
    title: str
    doc_class: str
    department: str | None
    category_path: str
    ref_type: str
    depth: int
    #: Articles the dependent document implements that the change actually touched. Empty
    #: means the edge records no article detail, so the whole document is flagged.
    matched_articles: tuple[int, ...] = ()
    canonical_version_id: uuid.UUID | None = None

    @property
    def is_obligation(self) -> bool:
        return self.ref_type == STRONG_REF_TYPE

    def as_dict(self) -> dict[str, object]:
        return {
            "document_id": str(self.document_id),
            "title": self.title,
            "doc_class": self.doc_class,
            "department": self.department,
            "ref_type": self.ref_type,
            "depth": self.depth,
            "matched_articles": list(self.matched_articles),
        }


@dataclass
class ImpactAssessment:
    source_document_id: uuid.UUID
    touched_articles: tuple[int, ...]
    impacted: list[ImpactedDocument] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "source_document_id": str(self.source_document_id),
            "touched_articles": list(self.touched_articles),
            "impacted": [item.as_dict() for item in self.impacted],
        }


def assess(
    session: Session,
    document_id: uuid.UUID,
    *,
    touched_articles: list[int] | None = None,
    max_depth: int = MAX_DEPTH,
) -> ImpactAssessment:
    """Find the documents a change to `document_id` puts at risk."""
    articles = tuple(sorted(touched_articles or ()))
    seen: set[uuid.UUID] = {document_id}
    impacted: list[ImpactedDocument] = []
    frontier = [document_id]

    for depth in range(1, max_depth + 1):
        next_frontier: list[uuid.UUID] = []
        for target in frontier:
            for row in _dependents(session, target):
                if row["id"] in seen:
                    continue
                # Beyond the first hop, only obligations propagate: a document that merely
                # cites something that cites the change is not impacted in any useful sense.
                if depth > 1 and row["ref_type"] != STRONG_REF_TYPE:
                    continue

                edge_articles: tuple[int, ...] = tuple(row["articles"] or ())
                if articles and edge_articles and not set(edge_articles) & set(articles):
                    # This document implements articles the amendment did not touch.
                    continue

                seen.add(row["id"])
                next_frontier.append(row["id"])
                impacted.append(
                    ImpactedDocument(
                        document_id=row["id"],
                        title=row["title"],
                        doc_class=row["doc_class"],
                        department=row["department"],
                        category_path=row["category_path"],
                        ref_type=row["ref_type"],
                        depth=depth,
                        matched_articles=tuple(sorted(set(edge_articles) & set(articles)))
                        if articles and edge_articles
                        else (),
                        canonical_version_id=row["canonical_version_id"],
                    )
                )
        frontier = next_frontier
        if not frontier:
            break

    log.info(
        "impact_assessed",
        extra={
            "document_id": str(document_id),
            "touched_articles": list(articles),
            "impacted": len(impacted),
        },
    )
    return ImpactAssessment(
        source_document_id=document_id, touched_articles=articles, impacted=impacted
    )


def _dependents(session: Session, document_id: uuid.UUID) -> list[DependentRow]:
    """Documents whose edges point at this one — the ones that depend on it."""
    rows = (
        session.execute(
            text(
                """
                SELECT d.id, d.title, d.doc_class, d.department,
                       d.category_path::text AS category_path, d.canonical_version_id,
                       r.ref_type, r.articles
                FROM document_refs r
                JOIN documents d ON d.id = r.src_document_id
                WHERE r.dst_document_id = :document_id
                  AND r.ref_type = ANY(CAST(:ref_types AS ref_type[]))
                  AND d.status = 'published'
                ORDER BY d.title
                """
            ),
            {"document_id": document_id, "ref_types": list(DEPENDENT_REF_TYPES)},
        )
        .mappings()
        .all()
    )
    return [DependentRow(**row) for row in rows]  # type: ignore[typeddict-item]


def open_impact_tasks(
    session: Session,
    assessment: ImpactAssessment,
    *,
    source_title: str,
    steward_for: StewardLookup | None = None,
) -> list[uuid.UUID]:
    """Open one `impact_review` task per impacted document.

    Assigned to the *impacted* document's steward, not to whoever consolidated the regulation:
    the person who has to decide whether a policy still says the right thing is the person who
    owns that policy.
    """
    task_ids: list[uuid.UUID] = []

    for item in assessment.impacted:
        if item.canonical_version_id is None:
            # A document with no canonical version has nothing to review yet.
            continue
        group = (
            steward_for.steward_group_for(item.category_path) if steward_for is not None else None
        )
        task = repo.add_review_task(
            session,
            _impact_task(item, assessment, source_title, group),
        )
        task_ids.append(task.id)

    log.info(
        "impact_tasks_opened",
        extra={"source": str(assessment.source_document_id), "tasks": len(task_ids)},
    )
    return task_ids


def _impact_task(
    item: ImpactedDocument,
    assessment: ImpactAssessment,
    source_title: str,
    group: str | None,
) -> ReviewTaskRow:
    return ReviewTaskRow(
        id=uuid.uuid4(),
        version_id=item.canonical_version_id,
        task_type=ReviewTaskType.IMPACT_REVIEW.value,
        state="open",
        assignee_group=group,
        payload={
            "source_document_id": str(assessment.source_document_id),
            "source_title": source_title,
            "touched_articles": list(assessment.touched_articles),
            "matched_articles": list(item.matched_articles),
            "ref_type": item.ref_type,
            "depth": item.depth,
            "reason": (
                f"{source_title} đã được hợp nhất; văn bản này {_relationship(item)} và có thể "
                "cần cập nhật."
            ),
        },
        created_at=repo.now(),
    )


def _relationship(item: ImpactedDocument) -> str:
    return "hướng dẫn thi hành văn bản đó" if item.is_obligation else "có dẫn chiếu tới văn bản đó"


def is_regulated(doc_class: str) -> bool:
    """Whether the impacted document itself needs the four-eyes treatment when updated."""
    from kb_schemas.enums import NO_AUTOMATION_CLASSES

    try:
        return DocClass(doc_class) in NO_AUTOMATION_CLASSES
    except ValueError:  # pragma: no cover - the column is an enum
        return True
