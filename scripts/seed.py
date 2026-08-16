#!/usr/bin/env python
"""Seed a development database.

Loads the category tree, a small bilingual corpus, and the three ACL canaries the sweep
depends on. Idempotent: re-running replaces the seeded rows (identified by fixed UUIDs) and
leaves anything else alone.

The canaries are restricted to `restricted/board`, a group no fixture principal holds. Any
principal that can retrieve a canary through search, chat or graph expansion has escaped the
server-side filter (INV-2/3/4/10) — that is the whole point of them existing.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from kb_common.config import get_settings
from kb_common.db import create_db_engine
from kb_common.logging import configure_logging, get_logger
from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_schemas.enums import DocClass, DocStatus, PiiStatus, RefType, SourceType, Visibility
from kb_schemas.orm import (
    CategoryRow,
    ChunkRow,
    DocumentRow,
    DocumentVersionRow,
    GraphServingRow,
)
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

log = get_logger("seed")

EMBEDDING_DIM = 1024
SEED_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-00000000d0c5")


def sid(name: str) -> uuid.UUID:
    """Deterministic IDs so seeding is idempotent and fixtures can hard-code references."""
    return uuid.uuid5(SEED_NAMESPACE, name)


#: The same deterministic adapter the dev/CI retrieval path uses, so seeded chunks live in
#: the same vector space as anything published locally. Production seeds run with BGE-M3.
_embedder = HashedEmbeddingAdapter(EMBEDDING_DIM)


def fake_embedding(text_value: str) -> list[float]:
    return _embedder.embed_documents([text_value])[0]


CATEGORIES: list[tuple[str, str, Visibility, list[str], str | None]] = [
    ("regulations", "Văn bản pháp quy", Visibility.INTERNAL_ALL, [], "dept/legal"),
    ("regulations.sbv", "Ngân hàng Nhà nước", Visibility.INTERNAL_ALL, [], "dept/legal"),
    ("regulations.sbv.capital", "An toàn vốn", Visibility.INTERNAL_ALL, [], "dept/legal"),
    ("regulations.sbv.aml", "Phòng chống rửa tiền", Visibility.INTERNAL_ALL, [], "dept/compliance"),
    ("internal", "Văn bản nội bộ", Visibility.INTERNAL_ALL, [], "dept/operations"),
    ("internal.policies", "Chính sách", Visibility.INTERNAL_ALL, [], "dept/operations"),
    ("internal.procedures", "Quy trình", Visibility.INTERNAL_ALL, [], "dept/operations"),
    (
        "internal.board",
        "Hồ sơ Hội đồng quản trị",
        Visibility.RESTRICTED,
        ["restricted/board"],
        "dept/legal",
    ),
    ("products", "Sản phẩm & biểu phí", Visibility.EXTERNAL, [], "dept/retail"),
    ("products.retail", "Khách hàng cá nhân", Visibility.EXTERNAL, [], "dept/retail"),
]


@dataclass(frozen=True)
class SeedDoc:
    key: str
    title: str
    legal_number: str | None
    doc_class: DocClass
    category: str
    department: str | None
    visibility: Visibility
    allowed_groups: tuple[str, ...]
    effective_from: date
    sections: tuple[tuple[str, str, str], ...]  # (section_path, citation_label, text)


DOCS: tuple[SeedDoc, ...] = (
    SeedDoc(
        key="tt41-capital",
        title="Thông tư 41/2016/TT-NHNN quy định tỷ lệ an toàn vốn",
        legal_number="41/2016/TT-NHNN",
        doc_class=DocClass.REGULATORY,
        category="regulations.sbv.capital",
        department=None,
        visibility=Visibility.INTERNAL_ALL,
        allowed_groups=(),
        effective_from=date(2020, 1, 1),
        sections=(
            (
                "Chương II > Điều 6 > Khoản 1",
                "Điều 6.1, TT 41/2016/TT-NHNN",
                "Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu 8% tính theo quy định "
                "tại Thông tư này.",
            ),
            (
                "Chương II > Điều 12 > Khoản 2",
                "Điều 12.2, TT 41/2016/TT-NHNN",
                "Tài sản có rủi ro tín dụng được xác định theo phương pháp tiêu chuẩn, có "
                "tính đến hệ số rủi ro của từng loại tài sản.",
            ),
        ),
    ),
    SeedDoc(
        key="policy-capital-internal",
        title="Chính sách quản lý vốn nội bộ / Internal capital management policy",
        legal_number="QD-2023-114",
        doc_class=DocClass.INTERNAL_NORMATIVE,
        category="internal.policies",
        department="risk",
        visibility=Visibility.INTERNAL_ALL,
        allowed_groups=(),
        effective_from=date(2023, 6, 1),
        sections=(
            (
                "Phần 2 > Mục 3",
                "Mục 3, QĐ 114/2023",
                "Bộ phận Quản lý rủi ro thực hiện tính toán tỷ lệ an toàn vốn hằng tháng và "
                "báo cáo Ủy ban ALCO. The internal buffer is set 150 bps above the regulatory "
                "minimum.",
            ),
        ),
    ),
    SeedDoc(
        key="aml-procedure",
        title="Quy trình nhận biết khách hàng (KYC) / Customer due diligence procedure",
        legal_number="QT-2024-007",
        doc_class=DocClass.OPERATIONAL,
        category="internal.procedures",
        department="compliance",
        visibility=Visibility.RESTRICTED,
        allowed_groups=("dept/compliance", "dept/legal"),
        effective_from=date(2024, 3, 1),
        sections=(
            (
                "Bước 3",
                "Bước 3, QT 07/2024",
                "Giao dịch viên đối chiếu giấy tờ tùy thân với dữ liệu CCCD gắn chip trước "
                "khi mở tài khoản cho khách hàng cá nhân.",
            ),
        ),
    ),
    SeedDoc(
        key="retail-fees",
        title="Biểu phí dịch vụ khách hàng cá nhân / Retail service fee schedule",
        legal_number=None,
        doc_class=DocClass.CUSTOMER_FACING,
        category="products.retail",
        department="retail",
        visibility=Visibility.EXTERNAL,
        allowed_groups=(),
        effective_from=date(2026, 1, 1),
        sections=(
            # Written the way the portal's structured rate schedule writes them (M8): the
            # effective date travels with the figure, because the chunk carrying the figure is
            # what a public answer quotes, and an undated fee is a fee the bank may not
            # currently charge.
            (
                "Phí tài khoản",
                "Biểu phí 2026, mục 1",
                "Phí duy trì tài khoản thanh toán: miễn phí đối với số dư bình quân từ "
                "2.000.000 VND, hiệu lực từ ngày 01/01/2026. Account maintenance is free "
                "above an average balance of VND 2,000,000, effective 01 January 2026.",
            ),
            (
                "Phí rút tiền",
                "Biểu phí 2026, mục 2",
                "Phí rút tiền mặt tại ATM ngoài hệ thống: 3.300 VND/giao dịch, đã bao gồm "
                "VAT, hiệu lực từ ngày 01/01/2026. Biểu phí đầy đủ được công bố trên "
                "website ngân hàng.",
            ),
        ),
    ),
    SeedDoc(
        key="branch-ops",
        title="Hướng dẫn vận hành quầy giao dịch chi nhánh",
        legal_number=None,
        doc_class=DocClass.OPERATIONAL,
        category="internal.procedures",
        department="retail",
        visibility=Visibility.RESTRICTED,
        allowed_groups=("dept/retail",),
        effective_from=date(2025, 9, 1),
        sections=(
            (
                "Mục 4",
                "Mục 4, HD vận hành quầy",
                "Cuối ngày, kiểm quỹ và đối chiếu tiền mặt tại quầy với hệ thống core banking.",
            ),
        ),
    ),
)

#: Restricted to a group nobody holds. Text carries a unique token the sweep greps for.
CANARIES: tuple[SeedDoc, ...] = tuple(
    SeedDoc(
        key=f"canary-{i}",
        title=f"Canary document {i} — board restricted",
        legal_number=None,
        doc_class=DocClass.INTERNAL_NORMATIVE,
        category="internal.board",
        department="board",
        visibility=Visibility.RESTRICTED,
        allowed_groups=("restricted/board",),
        effective_from=date(2025, 1, 1),
        sections=(
            (
                f"Section {i}",
                f"Canary {i}",
                f"CANARY-{token} Nội dung chỉ dành cho Hội đồng quản trị: kế hoạch sáp nhập "
                f"và tỷ lệ an toàn vốn dự kiến. Board-only merger plan and capital ratio.",
            ),
        ),
    )
    for i, token in enumerate(("ALPHA-7F3D", "BRAVO-2C91", "CHARLIE-E5A8"), start=1)
)

#: (src, dst, ref_type) — the amendment chain the M5 fixture builds on.
EDGES: tuple[tuple[str, str, RefType], ...] = (
    ("policy-capital-internal", "tt41-capital", RefType.IMPLEMENTS),
    ("aml-procedure", "tt41-capital", RefType.CITES),
)


def seed_categories(session: Session) -> None:
    for path, label, visibility, groups, steward in CATEGORIES:
        session.merge(
            CategoryRow(
                path=path,
                label=label,
                default_visibility=visibility.value,
                default_allowed_groups=list(groups),
                steward_group=steward,
                existence_disclosure=False,
            )
        )


def seed_document(session: Session, doc: SeedDoc, retention_years: int) -> None:
    doc_id = sid(f"doc:{doc.key}")
    version_id = sid(f"ver:{doc.key}:1")
    now = datetime.now(UTC)

    # Idempotence: drop the seeded chunks first, then re-insert. Versions are immutable
    # (INV-9) but the seed owns these rows entirely.
    session.execute(delete(ChunkRow).where(ChunkRow.version_id == version_id))

    session.merge(
        DocumentRow(
            id=doc_id,
            title=doc.title,
            legal_number=doc.legal_number,
            doc_class=doc.doc_class.value,
            category_path=doc.category,
            department=doc.department,
            visibility=doc.visibility.value,
            allowed_groups=list(doc.allowed_groups),
            status=DocStatus.PUBLISHED.value,
            canonical_version_id=None,  # set after the version exists
            review_by=date(doc.effective_from.year + 2, doc.effective_from.month, 1),
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()

    # Versions are immutable (INV-9): re-seeding must not touch an existing one, not even to
    # rewrite `created_at`. Insert once, then leave it alone.
    if session.get(DocumentVersionRow, version_id) is None:
        session.merge(
            DocumentVersionRow(
                id=version_id,
                document_id=doc_id,
                content_ref=f"kb-originals/seed/{doc.key}.json",
                content_hash=hashlib.sha256(doc.key.encode()).hexdigest(),
                source_type=SourceType.UPLOAD.value,
                author="seed",
                change_summary="Seeded fixture",
                idp_report_ref=None,
                pii_status=PiiStatus.CLEAR.value,
                effective_from=doc.effective_from,
                effective_to=None,
                is_canonical=True,
                retention_until=date(doc.effective_from.year + retention_years, 12, 31),
                created_at=now,
                published_at=now,
            )
        )
    session.flush()
    session.execute(
        text("UPDATE documents SET canonical_version_id = :v WHERE id = :d"),
        {"v": version_id, "d": doc_id},
    )

    for index, (section_path, citation, body) in enumerate(doc.sections):
        session.add(
            ChunkRow(
                id=sid(f"chunk:{doc.key}:{index}"),
                document_id=doc_id,
                version_id=version_id,
                section_path=section_path,
                citation_label=citation,
                text=body,
                embedding=fake_embedding(body),
                visibility=doc.visibility.value,
                allowed_groups=list(doc.allowed_groups),
                department=doc.department,
                category_path=doc.category,
                doc_class=doc.doc_class.value,
                doc_status=DocStatus.PUBLISHED.value,
                effective_from=doc.effective_from,
                effective_to=None,
                tombstoned=False,
                ordinal=index,
                page=1,
            )
        )


def fixture_document_ids() -> frozenset[uuid.UUID]:
    """Every document this script owns.

    The quality gates measure retrieval against a *known* corpus, and a dev database is not a
    clean one — real documents get uploaded through the portal into the same categories the
    fixtures live in, and they then compete for the top of every result list. Purging by
    category prefix cannot separate them, because they share the categories.

    Derivable rather than recorded because `sid` is deterministic: this is the same set the
    seeder writes, computed the same way, so the two cannot drift.
    """
    return frozenset(sid(f"doc:{doc.key}") for doc in DOCS + CANARIES)


def seed_edges(session: Session) -> None:
    for src_key, dst_key, ref_type in EDGES:
        src, dst = sid(f"doc:{src_key}"), sid(f"doc:{dst_key}")
        # Upsert on the *natural* key, not the surrogate one. `session.merge` matches by
        # primary key, and an edge between the same two documents can already exist under a
        # different id — ingest mints `uuid4`, and `resolve_pending_refs` promotes a parked
        # reference the same way. Seeding then died on `uq_refs_edge` and the corpus could
        # only be restored by dropping the volume, which is why these fixtures drifted.
        #
        # DO UPDATE rather than DO NOTHING: seeding is a declaration of what the fixture
        # corpus *is*, so it takes ownership of an edge ingest guessed at.
        session.execute(
            text(
                """
                INSERT INTO document_refs (id, src_document_id, dst_document_id, ref_type,
                    articles, anchors, detected_by, confirmed_by, created_at)
                VALUES (:id, :src, :dst, CAST(:ref_type AS ref_type),
                    NULL, NULL, 'seed', 'seed', :created_at)
                ON CONFLICT (src_document_id, dst_document_id, ref_type) DO UPDATE
                SET detected_by = EXCLUDED.detected_by,
                    confirmed_by = EXCLUDED.confirmed_by
                """
            ),
            {
                "id": sid(f"edge:{src_key}:{dst_key}:{ref_type.value}"),
                "src": src,
                "dst": dst,
                "ref_type": ref_type.value,
                "created_at": datetime.now(UTC),
            },
        )
        target = next(d for d in DOCS + CANARIES if d.key == dst_key)
        # graph_serving carries the target's ACL so expansion filters identically (INV-10).
        session.merge(
            GraphServingRow(
                src_document_id=src,
                dst_document_id=dst,
                ref_type=ref_type.value,
                dst_canonical_version_id=sid(f"ver:{dst_key}:1"),
                dst_summary=target.title,
                dst_visibility=target.visibility.value,
                dst_allowed_groups=list(target.allowed_groups),
                dst_effective_from=target.effective_from,
                articles=None,
            )
        )


def main() -> int:
    settings = get_settings()
    configure_logging("kb-seed", settings.log_level, settings.log_format)
    if settings.is_production:
        raise SystemExit("refusing to seed a production database")

    engine = create_db_engine(settings.db)
    with Session(engine) as session:
        seed_categories(session)
        session.flush()
        for doc in DOCS + CANARIES:
            seed_document(session, doc, settings.retention.default_years)
        session.flush()
        seed_edges(session)
        session.commit()

    log.info(
        "seed_complete",
        extra={
            "documents": len(DOCS) + len(CANARIES),
            "canaries": len(CANARIES),
            "categories": len(CATEGORIES),
        },
    )
    print(
        f"seeded {len(DOCS)} documents + {len(CANARIES)} canaries "
        f"across {len(CATEGORIES)} categories"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
