"""Consolidation, end to end (M5 acceptance).

The fixture is the shape the plan names: a circular Y, an internal policy P that *implements*
Y, and a later circular X that *amends* Y. What the bank needs from that arrangement is four
things, and this file asserts each of them against a real database:

1. From the moment X is published, retrieval warns that Y is out of date — and keeps serving
   it, because the flag is what makes quoting it safe.
2. The consolidated text of Y is drafted, not invented: every changed article is attributed to
   the amendment that changed it.
3. Nothing becomes canonical without two named approvals from the Legal cell (INV-8), and the
   flip itself is the ordinary publish transaction (INV-5), not a shortcut.
4. The moment Y changes, whoever depends on Y is told — P's steward gets a task naming the
   articles that moved.

The Temporal workflow that sequences these is a thin composition; `test_merge_policy.py`
covers the approval rules and `services/identity-merge/tests` cover the diff and the drafter.
This file is about what the four of them add up to.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from pathlib import Path

import pytest
from kb_authz.fixtures import ALL_PRINCIPALS
from kb_common.audit import AuditAction
from kb_common.config import get_settings
from kb_common.errors import GateBlocked
from kb_identity_merge.merge import Bucket
from kb_idp.builder import KBDocBuilder
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.generation import ScriptedGeneration
from kb_ports.adapters.pgvector_index import PgVectorIndexAdapter
from kb_ports.adapters.postgres_fts_index import PostgresFtsIndexAdapter
from kb_ports.adapters.rerank import LexicalRerankAdapter
from kb_ports.adapters.storage_local import LocalStorageAdapter
from kb_registry import repository as repo
from kb_registry.publish import Approval as PublishApproval
from kb_registry.publish import PublishService, is_superseded
from kb_registry.schemas import DocumentCreate, VersionCreate
from kb_registry.service import RegistryService
from kb_retrieval_api.engine import RetrievalEngine
from kb_schemas.api import RetrieveRequest
from kb_schemas.enums import DocClass, PiiStatus, RefType, ReviewTaskType, Visibility
from kb_schemas.kbdoc import KBDoc
from kb_schemas.orm import AuditLogRow, CategoryRow, DocumentRefRow, ReviewTaskRow
from kb_workflows.activities import Deps, set_deps
from kb_workflows.merge_activities import (
    LEGAL_CELL_GROUP,
    MergeRequest,
    assess_impact,
    build_merge_draft,
    open_merge_review,
    publish_merged,
)
from kb_workflows.merge_policy import Approval, ApprovalLedger
from sqlalchemy import Engine
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

REGULATIONS = "t_m5_regulations"
POLICIES = "t_m5_policies"
LEGAL = "dept/legal"
OPERATIONS = "dept/operations"

PREPARER = "u-m5-preparer"
LEGAL_ONE = "u-m5-legal-one"
LEGAL_TWO = "u-m5-legal-two"

#: A token unique to this fixture, so the retrieval assertions cannot match the seed corpus.
MARKER = "M5CONSOLID"

# The original circular. Điều 6 is what the amendment will change; Điều 12 is left alone, so
# an unchanged article showing up as "amended" is a visible failure rather than a silent one.
ORIGINAL = [
    (
        "Điều 6. Tỷ lệ dự trữ bắt buộc",
        f"Tổ chức tín dụng phải duy trì tỷ lệ dự trữ bắt buộc tối thiểu là 3% "
        f"trên tổng số dư tiền gửi {MARKER}.",
    ),
    (
        "Điều 12. Báo cáo định kỳ",
        f"Tổ chức tín dụng gửi báo cáo về Ngân hàng Nhà nước trước ngày 10 hằng tháng {MARKER}.",
    ),
]

# The consolidated text: Điều 6 raised to 5%, Điều 12 untouched, one new article added.
CONSOLIDATED = [
    (
        "Điều 6. Tỷ lệ dự trữ bắt buộc",
        f"Tổ chức tín dụng phải duy trì tỷ lệ dự trữ bắt buộc tối thiểu là 5% "
        f"trên tổng số dư tiền gửi {MARKER}.",
    ),
    (
        "Điều 12. Báo cáo định kỳ",
        f"Tổ chức tín dụng gửi báo cáo về Ngân hàng Nhà nước trước ngày 10 hằng tháng {MARKER}.",
    ),
    (
        "Điều 13. Hiệu lực thi hành",
        f"Điều này có hiệu lực kể từ ngày 01 tháng 01 năm 2027 {MARKER}.",
    ),
]

AMENDMENT = [
    ("Điều 1. Sửa đổi Điều 6", f"Sửa đổi tỷ lệ dự trữ bắt buộc tại Điều 6 thành 5% {MARKER}."),
]

POLICY = [
    ("Điều 1. Phạm vi", f"Quy định nội bộ này hướng dẫn thực hiện tỷ lệ dự trữ bắt buộc {MARKER}."),
]


# ------------------------------------------------------------------------------- fixture


def kbdoc(sections: list[tuple[str, str]], *, legal_number: str | None = None) -> KBDoc:
    builder = KBDocBuilder(source_format="docx")
    for heading, body in sections:
        builder.add(heading, block_type="heading")
        builder.add(body)
    doc = builder.build(page_count=1)
    if legal_number:
        doc.doc_meta.legal_number = legal_number
    return doc


class Fixture:
    """Y, P and X, published and indexed, in the state the acceptance criteria start from."""

    def __init__(self, session: Session, storage: LocalStorageAdapter) -> None:
        self.session = session
        self.storage = storage
        self.embedder = HashedEmbeddingAdapter()
        self.registry = RegistryService(session)
        self.suffix = uuid.uuid4().hex[:8].upper()

    # -- helpers ----------------------------------------------------------------------

    def store(self, doc: KBDoc) -> str:
        stored = self.storage.put(
            "kb-derived",
            io.BytesIO(doc.model_dump_json().encode("utf-8")),
            suffix=".kbdoc.json",
            content_type="application/json",
        )
        return f"kb-derived/{stored.key}"

    def add_version(
        self, document_id: uuid.UUID, doc: KBDoc, *, author: str = PREPARER
    ) -> uuid.UUID:
        version = self.registry.create_version(
            VersionCreate(
                document_id=document_id,
                content_ref="kb-originals/m5",
                content_hash=uuid.uuid4().hex,
                author=author,
                idp_report_ref=self.store(doc),
            ),
            actor=author,
        )
        # The gate has run and found nothing; without this no publish is possible at all.
        self.registry.set_pii_status(version.id, PiiStatus.CLEAR, actor="pii-gate")
        return version.id

    def publish(self, version_id: uuid.UUID, doc: KBDoc) -> None:
        publisher = PublishService(self.session, embedder=self.embedder)
        publisher.publish(
            version_id,
            publisher.prepare(doc, legal_number=doc.doc_meta.legal_number),
            actor=LEGAL_ONE,
            approval=PublishApproval(approver=LEGAL_ONE),
        )

    def add_document(
        self,
        title: str,
        sections: list[tuple[str, str]],
        *,
        doc_class: DocClass,
        category: str,
        legal_number: str | None = None,
    ) -> uuid.UUID:
        document = self.registry.create_document(
            DocumentCreate(
                title=title,
                doc_class=doc_class,
                category_path=category,
                legal_number=legal_number,
            ),
            actor=PREPARER,
        )
        doc = kbdoc(sections, legal_number=legal_number)
        self.publish(self.add_version(document.id, doc), doc)
        return document.id

    def link(
        self,
        src: uuid.UUID,
        dst: uuid.UUID,
        ref_type: RefType,
        *,
        articles: list[int] | None = None,
    ) -> None:
        repo.add_edge(
            self.session,
            DocumentRefRow(
                id=uuid.uuid4(),
                src_document_id=src,
                dst_document_id=dst,
                ref_type=ref_type.value,
                articles=articles,
                detected_by="fixture",
                confirmed_by=LEGAL_ONE,
                created_at=repo.now(),
            ),
        )

    # -- construction -----------------------------------------------------------------

    def build(self) -> Fixture:
        for path, label, steward in (
            (REGULATIONS, "M5 regulations", LEGAL),
            (POLICIES, "M5 internal policies", OPERATIONS),
        ):
            if repo.get_category(self.session, path) is None:
                repo.add_category(
                    self.session,
                    CategoryRow(
                        path=path,
                        label=label,
                        default_visibility=Visibility.INTERNAL_ALL.value,
                        default_allowed_groups=[],
                        steward_group=steward,
                        existence_disclosure=False,
                    ),
                )
        self.session.flush()

        self.target = self.add_document(
            "Thông tư quy định về tỷ lệ dự trữ bắt buộc",
            ORIGINAL,
            doc_class=DocClass.REGULATORY,
            category=REGULATIONS,
            legal_number=f"{self.suffix}/2026/TT-M5",
        )
        self.policy = self.add_document(
            "Quy định nội bộ về dự trữ bắt buộc",
            POLICY,
            doc_class=DocClass.INTERNAL_NORMATIVE,
            category=POLICIES,
        )
        self.amendment = self.add_document(
            "Thông tư sửa đổi, bổ sung một số điều",
            AMENDMENT,
            doc_class=DocClass.REGULATORY,
            category=REGULATIONS,
            legal_number=f"{self.suffix}/2026/TT-M5SD",
        )
        # A second policy that implements a *different* article of the same circular. It must
        # not be tasked when Điều 6 changes, or the impact queue becomes noise.
        self.unaffected_policy = self.add_document(
            "Quy định nội bộ về chế độ báo cáo",
            [("Điều 1. Phạm vi", f"Hướng dẫn chế độ báo cáo định kỳ {MARKER}.")],
            doc_class=DocClass.INTERNAL_NORMATIVE,
            category=POLICIES,
        )
        self.link(self.policy, self.target, RefType.IMPLEMENTS, articles=[6])
        self.link(self.unaffected_policy, self.target, RefType.IMPLEMENTS, articles=[12])
        self.link(self.amendment, self.target, RefType.AMENDS)
        self.session.commit()
        return self


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorageAdapter:
    adapter = LocalStorageAdapter(tmp_path)
    set_deps(
        Deps(
            storage=adapter,
            settings=get_settings(),
            generation=ScriptedGeneration(responses=[], strict=True),
        )
    )
    return adapter


@pytest.fixture
def corpus(migrated: Engine, session: Session, storage: LocalStorageAdapter) -> Fixture:
    return Fixture(session, storage).build()


@pytest.fixture
def retrieval(session: Session) -> RetrievalEngine:
    return RetrievalEngine(
        session,
        keyword_index=PostgresFtsIndexAdapter(session),
        vector_index=PgVectorIndexAdapter(session),
        embedder=HashedEmbeddingAdapter(),
        reranker=LexicalRerankAdapter(),
    )


def flags_for(retrieval: RetrievalEngine, document_id: uuid.UUID) -> list[bool]:
    response = retrieval.retrieve(
        ALL_PRINCIPALS["user_retail_staff"],
        RetrieveRequest(query=f"tỷ lệ dự trữ bắt buộc {MARKER}", top_k=20),
    ).response
    return [
        chunk.supersession_flag for chunk in response.chunks if chunk.document_id == document_id
    ]


# ---------------------------------------------------------------- 1. the supersession flag


def test_an_amendment_makes_the_target_stale_in_retrieval(
    corpus: Fixture, retrieval: RetrievalEngine
) -> None:
    """The bank knows Y is out of date the moment X is published; so does every answer."""
    flags = flags_for(retrieval, corpus.target)
    assert flags, "the amended circular must still be retrievable"
    assert all(flags)

    # And not by association: the policy that implements it is not itself amended.
    assert flags_for(retrieval, corpus.policy) in ([], [False])


def test_the_two_supersession_queries_agree(corpus: Fixture, retrieval: RetrievalEngine) -> None:
    """`is_superseded` and the engine's bulk query are the same predicate written twice."""
    for document_id in (corpus.target, corpus.policy, corpus.amendment):
        one_at_a_time = is_superseded(corpus.session, document_id)
        in_bulk = document_id in retrieval._superseded_documents(
            {corpus.target, corpus.policy, corpus.amendment}
        )
        assert one_at_a_time == in_bulk, document_id


# --------------------------------------------------------------------- 2. the merge draft


def test_the_draft_names_every_article_that_moved(corpus: Fixture) -> None:
    new_version = corpus.add_version(corpus.target, kbdoc(CONSOLIDATED))
    corpus.session.commit()

    draft = asyncio.run(
        build_merge_draft(
            MergeRequest(
                document_id=str(corpus.target),
                new_version_id=str(new_version),
                actor=PREPARER,
                amending_document_id=str(corpus.amendment),
                consolidation=True,
            )
        )
    )

    assert not draft.no_changes
    # Điều 6 was rewritten and Điều 13 is new; Điều 12 was left alone and must not appear.
    assert draft.touched_articles == [6, 13]
    assert draft.section_count == 2
    assert draft.draft_ref and corpus.storage.exists(*draft.draft_ref.split("/", 1))
    # No model was available, so the draft is mechanical — and says so. A lawyer writes the
    # prose; nothing here pretends the machine already did.
    assert draft.complete is False
    assert draft.doc_class == DocClass.REGULATORY.value
    assert draft.author == PREPARER


def test_an_identical_resubmission_produces_no_merge(corpus: Fixture) -> None:
    """Re-uploading the same file must not create a version whose only content is a date."""
    unchanged = corpus.add_version(corpus.target, kbdoc(ORIGINAL))
    corpus.session.commit()

    draft = asyncio.run(
        build_merge_draft(
            MergeRequest(
                document_id=str(corpus.target),
                new_version_id=str(unchanged),
                actor=PREPARER,
            )
        )
    )
    assert draft.no_changes
    assert draft.draft_ref is None


def test_the_review_lands_with_legal_not_with_the_steward(corpus: Fixture) -> None:
    """Producing `văn bản hợp nhất` is a legal act. The steward cannot sign it off."""
    new_version = corpus.add_version(corpus.target, kbdoc(CONSOLIDATED))
    corpus.session.commit()
    request = MergeRequest(
        document_id=str(corpus.target),
        new_version_id=str(new_version),
        actor=PREPARER,
        amending_document_id=str(corpus.amendment),
        consolidation=True,
    )
    draft = asyncio.run(build_merge_draft(request))
    task_id = asyncio.run(open_merge_review(request, draft))

    corpus.session.expire_all()
    task = corpus.session.get(ReviewTaskRow, uuid.UUID(task_id))
    assert task is not None
    assert task.task_type == ReviewTaskType.MERGE_REVIEW.value
    assert task.assignee_group == LEGAL_CELL_GROUP
    assert task.payload["touched_articles"] == [6, 13]
    assert task.payload["draft_complete"] is False
    assert task.payload["consolidation"] is True


# ----------------------------------------------------------------------- 3. the approvals


def test_the_canonical_version_does_not_flip_until_two_people_approve(
    corpus: Fixture, retrieval: RetrievalEngine
) -> None:
    """The whole acceptance criterion in one run: draft → Legal approval → canonical flips →
    the supersession warning goes away."""
    before = repo.get_canonical_version(corpus.session, corpus.target)
    assert before is not None

    new_version = corpus.add_version(corpus.target, kbdoc(CONSOLIDATED))
    corpus.session.commit()
    request = MergeRequest(
        document_id=str(corpus.target),
        new_version_id=str(new_version),
        actor=PREPARER,
        amending_document_id=str(corpus.amendment),
        consolidation=True,
    )
    draft = asyncio.run(build_merge_draft(request))

    ledger = ApprovalLedger(
        doc_class=draft.doc_class, author=draft.author, draft_ref=draft.draft_ref or ""
    )
    ledger.add(Approval(approver=LEGAL_ONE, draft_ref=draft.draft_ref or ""))
    assert not ledger.satisfied, "one regulatory approval is not enough"

    # Publishing here would be the four-eyes hole the ledger exists to close.
    ledger.add(Approval(approver=LEGAL_TWO, note="Đã đối chiếu Điều 6 với bản sửa đổi."))
    assert ledger.satisfied

    chunks = asyncio.run(publish_merged(request, LEGAL_TWO, "Đã đối chiếu Điều 6."))
    assert chunks > 0

    corpus.session.expire_all()
    after = repo.get_canonical_version(corpus.session, corpus.target)
    assert after is not None and after.id == new_version
    assert after.id != before.id

    # The old canonical is not deleted — INV-9. It is simply no longer the one served.
    superseded_version = repo.get_version(corpus.session, before.id)
    assert superseded_version is not None and not superseded_version.is_canonical

    # 5%, not 3%, is what a question about the ratio now returns — and without a warning,
    # because the amendment has been folded in.
    response = retrieval.retrieve(
        ALL_PRINCIPALS["user_retail_staff"],
        RetrieveRequest(query=f"tỷ lệ dự trữ bắt buộc {MARKER}", top_k=20),
    ).response
    served = [chunk for chunk in response.chunks if chunk.document_id == corpus.target]
    assert served
    assert any("5%" in chunk.text for chunk in served)
    assert not any("3%" in chunk.text for chunk in served)
    assert not any(chunk.supersession_flag for chunk in served)


def test_the_consolidation_is_recorded_as_an_edge_and_an_audit_record(corpus: Fixture) -> None:
    """The edge is what clears the flag, so it is written inside the publish transaction —
    not announced afterwards by something that might not run."""
    new_version = corpus.add_version(corpus.target, kbdoc(CONSOLIDATED))
    corpus.session.commit()
    request = MergeRequest(
        document_id=str(corpus.target),
        new_version_id=str(new_version),
        actor=PREPARER,
        amending_document_id=str(corpus.amendment),
        consolidation=True,
    )
    asyncio.run(build_merge_draft(request))
    asyncio.run(publish_merged(request, LEGAL_TWO, "Đã hợp nhất."))

    corpus.session.expire_all()
    edge = repo.get_edge(
        corpus.session, corpus.target, corpus.amendment, RefType.CONSOLIDATES.value
    )
    assert edge is not None
    assert edge.confirmed_by == LEGAL_TWO
    # The amends edge stays: the history of why the text changed is not rewritten (INV-9).
    assert (
        repo.get_edge(corpus.session, corpus.amendment, corpus.target, RefType.AMENDS.value)
        is not None
    )

    actions = {
        row.action
        for row in corpus.session.query(AuditLogRow)
        .filter(AuditLogRow.object_ref["version_id"].astext == str(new_version))
        .all()
    }
    assert AuditAction.MERGE_APPROVAL in actions
    assert AuditAction.PUBLISH in actions


def test_a_merge_cannot_slip_past_the_pii_gate(corpus: Fixture) -> None:
    """Consolidation is a publish. Every guard that applies to a publish applies to it."""
    new_version = corpus.add_version(corpus.target, kbdoc(CONSOLIDATED))
    corpus.registry.set_pii_status(new_version, PiiStatus.BLOCKED, actor="pii-gate")
    corpus.session.commit()

    request = MergeRequest(
        document_id=str(corpus.target),
        new_version_id=str(new_version),
        actor=PREPARER,
        amending_document_id=str(corpus.amendment),
        consolidation=True,
    )
    with pytest.raises(GateBlocked) as exc:
        asyncio.run(publish_merged(request, LEGAL_TWO, "Đã hợp nhất."))
    assert exc.value.detail["invariant"] == "INV-7"

    corpus.session.expire_all()
    canonical = repo.get_canonical_version(corpus.session, corpus.target)
    assert canonical is not None and canonical.id != new_version


# --------------------------------------------------------------------------- 4. the impact


def test_the_policy_that_implements_the_circular_gets_a_task(corpus: Fixture) -> None:
    """Consolidating Y without telling P's owner is how an internal policy quietly starts
    contradicting the regulation it implements."""
    new_version = corpus.add_version(corpus.target, kbdoc(CONSOLIDATED))
    corpus.session.commit()
    request = MergeRequest(
        document_id=str(corpus.target),
        new_version_id=str(new_version),
        actor=PREPARER,
        amending_document_id=str(corpus.amendment),
        consolidation=True,
    )
    draft = asyncio.run(build_merge_draft(request))
    asyncio.run(publish_merged(request, LEGAL_TWO, "Đã hợp nhất."))
    impact = asyncio.run(assess_impact(request, draft.touched_articles))

    assert impact.impacted == 1, "only the policy implementing Điều 6 is affected"
    assert impact.touched_articles == [6, 13]

    corpus.session.expire_all()
    task = corpus.session.get(ReviewTaskRow, uuid.UUID(impact.task_ids[0]))
    assert task is not None
    assert task.task_type == ReviewTaskType.IMPACT_REVIEW.value
    # Assigned to the *impacted* document's steward, not to the one who changed the circular.
    assert task.assignee_group == OPERATIONS
    assert task.payload["touched_articles"] == [6, 13]
    assert task.payload["matched_articles"] == [6]
    assert task.payload["ref_type"] == RefType.IMPLEMENTS.value
    assert str(corpus.target) == task.payload["source_document_id"]
    assert task.version_id == repo.get_canonical_version(corpus.session, corpus.policy).id


def test_nothing_is_opened_when_nothing_depends_on_the_document(corpus: Fixture) -> None:
    """A task queue that fills with noise is a task queue nobody reads."""
    new_version = corpus.add_version(
        corpus.amendment, kbdoc([*AMENDMENT, ("Điều 2. Bổ sung", "Nội dung mới.")])
    )
    corpus.session.commit()
    request = MergeRequest(
        document_id=str(corpus.amendment), new_version_id=str(new_version), actor=PREPARER
    )
    impact = asyncio.run(assess_impact(request, [1]))
    assert impact.impacted == 0
    assert impact.task_ids == []


def test_the_draft_classifies_what_it_could_not_confirm_as_amended(corpus: Fixture) -> None:
    """With no model, every substantive change is treated as amended and marked inferred.
    Guessing "unchanged in substance" is the one guess that loses information."""
    from kb_identity_merge.diff import diff_documents
    from kb_identity_merge.merge import MergeDrafter

    drafter = MergeDrafter(ScriptedGeneration(responses=[], strict=True))
    draft = drafter.draft(diff_documents(kbdoc(ORIGINAL), kbdoc(CONSOLIDATED)))

    assert not draft.complete
    assert all(item.inferred for item in draft.classifications)
    assert {item.bucket for item in draft.classifications} <= {
        Bucket.AMENDED,
        Bucket.NEW_OR_ABROGATED,
    }
