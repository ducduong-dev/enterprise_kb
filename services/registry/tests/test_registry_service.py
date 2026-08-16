"""Registry rules: classification defaults, retention, duplicate handling, edges."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from kb_common.audit import AuditAction, InMemoryAuditSink
from kb_common.errors import Conflict, NotFound, ValidationError
from kb_registry import repository as repo
from kb_registry.schemas import (
    DeclarationIn,
    DetectedRefIn,
    DocumentCreate,
    DocumentUpdate,
    VersionCreate,
)
from kb_registry.service import RegistryService
from kb_schemas.enums import DeclarationState, DocClass, DocStatus, PiiStatus, RefType, Visibility
from kb_schemas.kbdoc import DocMeta, KBDoc
from kb_schemas.orm import CategoryRow
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


@pytest.fixture
def audit() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def registry(session: Session, audit: InMemoryAuditSink) -> RegistryService:
    for path, label, visibility, groups, steward in [
        ("t_regulations", "Quy định", Visibility.INTERNAL_ALL, [], "dept/legal"),
        ("t_regulations.sbv", "NHNN", Visibility.INTERNAL_ALL, [], None),
        ("t_board", "Hội đồng quản trị", Visibility.RESTRICTED, ["restricted/board"], "dept/legal"),
        ("t_products", "Sản phẩm", Visibility.EXTERNAL, [], "dept/retail"),
    ]:
        if repo.get_category(session, path) is None:
            repo.add_category(
                session,
                CategoryRow(
                    path=path,
                    label=label,
                    default_visibility=visibility.value,
                    default_allowed_groups=groups,
                    steward_group=steward,
                    existence_disclosure=False,
                ),
            )
    session.flush()
    return RegistryService(session, audit=audit)


def spec(**overrides: object) -> DocumentCreate:
    base: dict[str, object] = {
        "title": "Quy định thử nghiệm",
        "doc_class": DocClass.OPERATIONAL,
        "category_path": "t_regulations.sbv",
    }
    base.update(overrides)
    return DocumentCreate(**base)


# ------------------------------------------------------------------------- classification


def test_visibility_defaults_come_from_the_category(registry: RegistryService) -> None:
    external = registry.create_document(spec(category_path="t_products"), actor="tester")
    assert external.visibility == Visibility.EXTERNAL.value

    restricted = registry.create_document(spec(category_path="t_board"), actor="tester")
    assert restricted.visibility == Visibility.RESTRICTED.value
    assert restricted.allowed_groups == ["restricted/board"]


def test_uploader_may_narrow_but_the_result_must_be_coherent(
    registry: RegistryService,
) -> None:
    document = registry.create_document(
        spec(visibility=Visibility.RESTRICTED, allowed_groups=["dept/legal"]), actor="tester"
    )
    assert document.allowed_groups == ["dept/legal"]

    with pytest.raises(ValueError):  # caught by the request model, before any write
        spec(visibility=Visibility.RESTRICTED, allowed_groups=[])


def test_restricted_category_without_groups_is_refused(
    registry: RegistryService, session: Session
) -> None:
    repo.add_category(
        session,
        CategoryRow(
            path="t_broken",
            label="Bad policy",
            default_visibility=Visibility.RESTRICTED.value,
            default_allowed_groups=[],
            steward_group=None,
            existence_disclosure=False,
        ),
    )
    session.flush()
    with pytest.raises(ValidationError):
        registry.create_document(spec(category_path="t_broken"), actor="tester")


def test_unknown_category_is_refused(registry: RegistryService) -> None:
    with pytest.raises(ValidationError):
        registry.create_document(spec(category_path="t_nope.missing"), actor="tester")


def test_new_documents_are_drafts_with_no_canonical_version(
    registry: RegistryService,
) -> None:
    """Nothing reaches an index by being created — publication is a separate, guarded act."""
    document = registry.create_document(spec(), actor="tester")
    assert document.status == DocStatus.DRAFT.value
    assert document.canonical_version_id is None


def test_duplicate_legal_numbers_are_refused(registry: RegistryService) -> None:
    registry.create_document(spec(legal_number="99/2026/TT-TEST"), actor="tester")
    with pytest.raises(Conflict):
        registry.create_document(
            spec(title="Another", legal_number="99/2026/TT-TEST"), actor="tester"
        )


def test_steward_group_is_inherited_from_the_parent_category(
    registry: RegistryService,
) -> None:
    assert registry.steward_group_for("t_regulations.sbv") == "dept/legal"
    assert registry.steward_group_for("t_products") == "dept/retail"


# -------------------------------------------------------------------------------- versions


def version_spec(document_id: uuid.UUID, **overrides: object) -> VersionCreate:
    base: dict[str, object] = {
        "document_id": document_id,
        "content_ref": "kb-originals/originals/aa/bb/aabbcc",
        "content_hash": "aabbcc",
        "author": "tester",
    }
    base.update(overrides)
    return VersionCreate(**base)


def test_new_versions_start_with_a_pending_pii_state(registry: RegistryService) -> None:
    """INV-7 fails closed: a version is unpublishable until the gate says otherwise."""
    document = registry.create_document(spec(), actor="tester")
    version = registry.create_version(version_spec(document.id), actor="tester")
    assert version.pii_status == PiiStatus.PENDING.value
    assert version.is_canonical is False


def test_retention_is_set_from_the_class_schedule(registry: RegistryService) -> None:
    document = registry.create_document(spec(doc_class=DocClass.REGULATORY), actor="tester")
    version = registry.create_version(version_spec(document.id), actor="tester")
    assert version.retention_until is not None
    assert version.retention_until.year >= date.today().year + 10


def test_reuploading_identical_bytes_is_a_conflict_not_a_new_version(
    registry: RegistryService,
) -> None:
    document = registry.create_document(spec(), actor="tester")
    registry.create_version(version_spec(document.id), actor="tester")
    with pytest.raises(Conflict):
        registry.create_version(version_spec(document.id), actor="tester")


def test_version_for_an_unknown_document_is_refused(registry: RegistryService) -> None:
    with pytest.raises(NotFound):
        registry.create_version(version_spec(uuid.uuid4()), actor="tester")


# ---------------------------------------------------------------------------------- audit


def test_writes_are_attributable(registry: RegistryService, audit: InMemoryAuditSink) -> None:
    document = registry.create_document(spec(), actor="u-steward")
    registry.create_version(version_spec(document.id), actor="u-steward")
    actions = {record.action for record in audit.records}
    assert AuditAction.DOCUMENT_CREATE in actions
    assert AuditAction.VERSION_CREATE in actions
    assert all(record.actor == "u-steward" for record in audit.records)


def test_acl_changes_are_audited_with_before_and_after(
    registry: RegistryService, audit: InMemoryAuditSink
) -> None:
    document = registry.create_document(spec(), actor="u-steward")
    registry.update_document(
        document.id,
        DocumentUpdate(visibility=Visibility.RESTRICTED, allowed_groups=["dept/legal"]),
        actor="u-steward",
    )
    (record,) = audit.by_action(AuditAction.ACL_CHANGE)
    assert record.detail["before"]["visibility"] == Visibility.INTERNAL_ALL.value
    assert record.detail["after"]["allowed_groups"] == ["dept/legal"]


def test_a_document_cannot_be_left_restricted_without_groups(
    registry: RegistryService,
) -> None:
    document = registry.create_document(spec(), actor="tester")
    with pytest.raises(ValidationError):
        registry.update_document(
            document.id, DocumentUpdate(visibility=Visibility.RESTRICTED), actor="tester"
        )


# ---------------------------------------------------------------------------------- ingest


def kbdoc(legal_number: str | None = None) -> KBDoc:
    return KBDoc(doc_meta=DocMeta(source_format="docx", legal_number=legal_number))


def test_ingest_creates_a_document_and_its_first_version(
    registry: RegistryService,
) -> None:
    result = registry.ingest(
        kbdoc("77/2026/TT-TEST"),
        spec=spec(legal_number=None),
        content_ref="kb-originals/originals/11/22/112233",
        content_hash="112233",
        idp_report_ref="kb-derived/reports/112233.json",
        actor="u-uploader",
    )
    assert result.created_document
    assert not result.matched_existing
    document = repo.get_document(registry._session, result.document_id)
    assert document is not None
    # The number found by the IDP becomes the document's identity when the uploader omits it.
    assert document.legal_number == "77/2026/TT-TEST"


def test_ingesting_a_known_instrument_attaches_to_the_existing_document(
    registry: RegistryService,
) -> None:
    """A revision of an instrument we already hold must not fork its history."""
    first = registry.ingest(
        kbdoc("78/2026/TT-TEST"),
        spec=spec(),
        content_ref="a",
        content_hash="h1",
        idp_report_ref=None,
        actor="u-uploader",
    )
    second = registry.ingest(
        kbdoc("78/2026/TT-TEST"),
        spec=spec(),
        content_ref="b",
        content_hash="h2",
        idp_report_ref=None,
        actor="u-uploader",
    )
    assert second.document_id == first.document_id
    assert second.matched_existing
    assert not second.created_document
    assert second.version_id != first.version_id


def test_ingesting_identical_bytes_twice_is_a_no_op(registry: RegistryService) -> None:
    first = registry.ingest(
        kbdoc("79/2026/TT-TEST"),
        spec=spec(),
        content_ref="a",
        content_hash="same",
        idp_report_ref=None,
        actor="u-uploader",
    )
    again = registry.ingest(
        kbdoc("79/2026/TT-TEST"),
        spec=spec(),
        content_ref="a",
        content_hash="same",
        idp_report_ref=None,
        actor="u-uploader",
    )
    assert again.version_id == first.version_id
    assert again.duplicate_of_version_id == first.version_id


# ----------------------------------------------------------------------------------- refs


def test_edges_are_created_for_known_targets_and_reported_for_unknown(
    registry: RegistryService,
) -> None:
    target = registry.create_document(
        spec(title="Thông tư gốc", legal_number="41/2016/TT-REF"), actor="tester"
    )
    source = registry.create_document(spec(title="Văn bản dẫn chiếu"), actor="tester")

    created, unresolved = registry.link_detected_refs(
        source.id,
        [
            DetectedRefIn(legal_number="41/2016/TT-REF", ref_type=RefType.AMENDS),
            DetectedRefIn(legal_number="00/1999/TT-UNKNOWN"),
        ],
    )
    assert len(created) == 1
    assert unresolved == ["00/1999/TT-UNKNOWN"]

    edge = repo.get_edge(registry._session, source.id, target.id, RefType.AMENDS.value)
    assert edge is not None
    assert edge.detected_by == "idp"
    # Unconfirmed: an amendment is not authoritative until a human says so (M5).
    assert edge.confirmed_by is None


def test_linking_the_same_reference_twice_does_not_duplicate_the_edge(
    registry: RegistryService,
) -> None:
    registry.create_document(spec(title="Đích", legal_number="42/2016/TT-REF"), actor="t")
    source = registry.create_document(spec(title="Nguồn"), actor="t")
    refs = [DetectedRefIn(legal_number="42/2016/TT-REF")]
    first, _ = registry.link_detected_refs(source.id, refs)
    second, _ = registry.link_detected_refs(source.id, refs)
    assert len(first) == 1
    assert second == []


# ------------------------------------------------------- reference anchors (M9b)


def test_an_edge_is_as_precise_as_the_citation_was(registry: RegistryService) -> None:
    """A reference to "khoản 2 Điều 12" must not arrive as "somewhere in this circular"."""
    target = registry.create_document(
        spec(title="Thông tư đích", legal_number="55/2016/TT-ANCHOR"), actor="t"
    )
    source = registry.create_document(spec(title="Văn bản dẫn chiếu điều khoản"), actor="t")

    registry.link_detected_refs(
        source.id,
        [DetectedRefIn(legal_number="55/2016/TT-ANCHOR", anchors=["12.2", "12.3"])],
    )

    edge = repo.get_edge(registry._session, source.id, target.id, RefType.CITES.value)
    assert edge is not None
    assert edge.anchors == ["12.2", "12.3"]
    # Derived, never supplied: the impact traversal reads `articles` and the resolver reads
    # `anchors`, and a reference meaning different things to the two is a policy owner told
    # the wrong thing (ADR-0036).
    assert edge.articles == [12]


def test_a_reference_to_a_whole_instrument_keeps_no_anchors(registry: RegistryService) -> None:
    target = registry.create_document(
        spec(title="Thông tư đích", legal_number="56/2016/TT-ANCHOR"), actor="t"
    )
    source = registry.create_document(spec(title="Văn bản dẫn chiếu chung"), actor="t")

    registry.link_detected_refs(source.id, [DetectedRefIn(legal_number="56/2016/TT-ANCHOR")])

    edge = repo.get_edge(registry._session, source.id, target.id, RefType.CITES.value)
    assert edge is not None
    assert edge.anchors is None, "NULL means the whole document, which is what was cited"


def test_a_parked_reference_keeps_its_anchors_until_the_target_arrives(
    registry: RegistryService,
) -> None:
    """An anchor dropped at parking time is one nobody can recover later — the citing text is
    not read again (ADR-0028/0036)."""
    source = registry.create_document(spec(title="Văn bản dẫn chiếu sớm"), actor="t")
    registry.link_detected_refs(
        source.id,
        [DetectedRefIn(legal_number="57/2016/TT-ANCHOR", anchors=["8.3a"])],
    )
    parked = list(repo.pending_refs_from(registry._session, source.id))
    assert [row.anchors for row in parked] == [["8.3a"]]

    target = registry.create_document(
        spec(title="Thông tư đến sau", legal_number="57/2016/TT-ANCHOR"), actor="t"
    )
    registry.resolve_pending_refs(target.id)

    edge = repo.get_edge(registry._session, source.id, target.id, RefType.CITES.value)
    assert edge is not None
    assert edge.anchors == ["8.3a"]


# -------------------------------------------------------- declarations (M9c, ADR-0039)


def _declaration(number: str, **kwargs: Any) -> DeclarationIn:
    return DeclarationIn(
        kind=kwargs.pop("kind", "abrogates"),
        target_legal_number=number,
        evidence=kwargs.pop("evidence", "Bãi bỏ khoản 2 Điều 12 của văn bản này."),
        **kwargs,
    )


def test_a_declaration_against_a_document_we_hold_is_ready_for_review(
    registry: RegistryService,
) -> None:
    target = registry.create_document(
        spec(title="Thông tư cũ", legal_number="70/2022/TT-DECL"), actor="t"
    )
    source = registry.create_document(spec(title="Thông tư mới"), actor="t")

    recorded, waiting = registry.record_declarations(
        source.id, [_declaration("70/2022/TT-DECL", target_anchors=["12.2"])]
    )

    assert (recorded, waiting) == (1, 0)
    row = repo.declarations_from(registry._session, source.id)[0]
    assert row.target_document_id == target.id
    assert row.state == DeclarationState.OPEN.value
    assert row.target_anchors == ["12.2"]
    assert row.evidence, "the sentence travels with the reading"


def test_a_declaration_against_a_document_we_do_not_hold_waits(
    registry: RegistryService,
) -> None:
    """An archive digitised in yield order routinely produces the amending instrument first,
    and the citing text is never read again — so this is parked, not dropped (ADR-0028)."""
    source = registry.create_document(spec(title="Thông tư đến trước"), actor="t")

    recorded, waiting = registry.record_declarations(source.id, [_declaration("71/2022/TT-DECL")])

    assert (recorded, waiting) == (1, 1)
    row = repo.declarations_from(registry._session, source.id)[0]
    assert row.target_document_id is None
    assert row.state == DeclarationState.WAITING.value


def test_a_waiting_declaration_attaches_when_its_target_arrives(
    registry: RegistryService,
) -> None:
    source = registry.create_document(spec(title="Thông tư đến trước"), actor="t")
    registry.record_declarations(source.id, [_declaration("72/2022/TT-DECL")])

    target = registry.create_document(
        spec(title="Thông tư đến sau", legal_number="72/2022/TT-DECL"), actor="t"
    )

    row = repo.declarations_from(registry._session, source.id)[0]
    assert row.target_document_id == target.id
    assert row.state == DeclarationState.OPEN.value


def test_a_document_declaring_against_itself_is_not_recorded(
    registry: RegistryService,
) -> None:
    """A renumbering is the merge flow's business; recording it here produces a document that
    supersedes itself (ADR-0039)."""
    source = registry.create_document(
        spec(title="Thông tư tự sửa", legal_number="73/2022/TT-DECL"), actor="t"
    )

    recorded, _ = registry.record_declarations(source.id, [_declaration("73/2022/TT-DECL")])

    assert recorded == 0
    assert repo.declarations_from(registry._session, source.id) == []


def test_re_ingesting_the_same_sentence_does_not_duplicate_it(
    registry: RegistryService,
) -> None:
    registry.create_document(spec(title="Đích", legal_number="74/2022/TT-DECL"), actor="t")
    source = registry.create_document(spec(title="Nguồn"), actor="t")
    declarations = [_declaration("74/2022/TT-DECL", target_anchors=["12.2"])]

    first, _ = registry.record_declarations(source.id, declarations)
    second, _ = registry.record_declarations(source.id, declarations)

    assert (first, second) == (1, 0)
    assert len(repo.declarations_from(registry._session, source.id)) == 1


def test_one_closing_article_may_declare_several_changes_against_one_instrument(
    registry: RegistryService,
) -> None:
    """ "bãi bỏ Điều 5" and "bãi bỏ khoản 2 Điều 12" of the same circular are two decisions, so
    the anchors are part of what makes a declaration distinct."""
    registry.create_document(spec(title="Đích", legal_number="75/2022/TT-DECL"), actor="t")
    source = registry.create_document(spec(title="Nguồn"), actor="t")

    recorded, _ = registry.record_declarations(
        source.id,
        [
            _declaration("75/2022/TT-DECL", target_anchors=["5"]),
            _declaration("75/2022/TT-DECL", target_anchors=["12.2"]),
        ],
    )

    assert recorded == 2


def test_recording_a_declaration_changes_nothing_that_is_served(
    registry: RegistryService,
) -> None:
    """The whole point of the state machine: a reading is not a decision (ADR-0039)."""
    target = registry.create_document(spec(title="Đích", legal_number="76/2022/TT-DECL"), actor="t")
    source = registry.create_document(spec(title="Nguồn"), actor="t")
    registry.record_declarations(source.id, [_declaration("76/2022/TT-DECL")])

    from kb_registry.expiry import ExpiryLedger

    assert ExpiryLedger(registry._session).open_row(target.id) is None, (
        "no expiry is proposed until a steward confirms the declaration"
    )
