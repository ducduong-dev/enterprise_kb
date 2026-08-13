"""Structured rates and fees (M8).

What a customer asks the public bot is "how much is this, and does it apply to me?". This
tests that the published form can answer that: one citable statement per fee, each carrying
its own effective date, and none of it public until a human approved the numbers.
"""

from __future__ import annotations

from datetime import date

import pytest
from kb_common.audit import InMemoryAuditSink
from kb_common.errors import ValidationError
from kb_indexer.chunker import chunk_document
from kb_portal_api.rates import RateItem, RateScheduleIn, RateScheduleService, build_rate_kbdoc
from kb_ports.adapters.storage_local import LocalStorageAdapter
from kb_registry import repository as repo
from kb_registry.service import RegistryService
from kb_schemas.enums import DocClass, DocStatus, PiiStatus, ReviewTaskType, Visibility
from kb_schemas.orm import CategoryRow
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

CATEGORY = "t_rates"

ITEMS = [
    RateItem(
        label="Phí duy trì tài khoản thanh toán",
        value="miễn phí",
        condition="số dư bình quân từ 2.000.000 VND",
    ),
    RateItem(
        label="Phí rút tiền mặt tại ATM ngoài hệ thống",
        value="3.300",
        unit="VND/giao dịch",
        note="Đã bao gồm VAT",
    ),
    RateItem(
        label="Lãi suất tiền gửi kỳ hạn 12 tháng",
        value="5,2",
        unit="%/năm",
        condition="khách hàng cá nhân",
    ),
]


def schedule(**overrides: object) -> RateScheduleIn:
    base: dict[str, object] = {
        "title": "Biểu phí dịch vụ khách hàng cá nhân 2026",
        "category_path": CATEGORY,
        "effective_from": date(2026, 1, 1),
        "items": ITEMS,
        "footnote": "Biểu phí đầy đủ được công bố trên website ngân hàng.",
    }
    base.update(overrides)
    return RateScheduleIn(**base)


@pytest.fixture
def rates(session: Session) -> RateScheduleService:
    if repo.get_category(session, CATEGORY) is None:
        repo.add_category(
            session,
            CategoryRow(
                path=CATEGORY,
                label="Rate schedules",
                default_visibility=Visibility.EXTERNAL.value,
                default_allowed_groups=[],
                steward_group="dept/retail",
                existence_disclosure=False,
            ),
        )
    session.flush()
    return RateScheduleService(
        session,
        storage=LocalStorageAdapter("/tmp/kb-rates-test"),
        registry=RegistryService(session, audit=InMemoryAuditSink()),
    )


# ------------------------------------------------------------------------------- the shape


def test_every_fee_becomes_its_own_statement() -> None:
    """A question about one fee must retrieve that fee, not the whole schedule."""
    kbdoc = build_rate_kbdoc(schedule())
    texts = [block.text for block in kbdoc.blocks]

    fee = next(text for text in texts if "duy trì tài khoản" in text)
    assert "miễn phí" in fee
    assert "2.000.000 VND" in fee
    # The date travels with the number: the chunk that answers is the chunk that must date it.
    assert "01/01/2026" in fee


def test_the_unit_stays_attached_to_the_figure() -> None:
    kbdoc = build_rate_kbdoc(schedule())
    atm = next(block.text for block in kbdoc.blocks if "ATM" in block.text)
    assert "3.300 VND/giao dịch" in atm
    assert "Đã bao gồm VAT" in atm


def test_the_whole_schedule_is_also_a_table() -> None:
    """For the portal and for a reader who wants all of it at once."""
    kbdoc = build_rate_kbdoc(schedule())
    tables = [block for block in kbdoc.blocks if block.table is not None]
    assert len(tables) == 1
    assert tables[0].table is not None
    assert tables[0].table.header_rows == 1
    assert len(tables[0].table.rows) == len(ITEMS) + 1


def test_a_long_schedule_becomes_several_tables_each_with_its_header() -> None:
    """A table is chunked whole, so one 200-row table would be one unusable chunk."""
    many = [RateItem(label=f"Phí số {index}", value="1.000", unit="VND") for index in range(40)]
    kbdoc = build_rate_kbdoc(schedule(items=many))
    tables = [block.table for block in kbdoc.blocks if block.table is not None]
    assert len(tables) == 3
    for table in tables:
        assert table is not None
        assert table.header_rows == 1
        assert table.rows[0][0] == "Khoản mục"


def test_an_end_date_is_stated_when_there_is_one() -> None:
    kbdoc = build_rate_kbdoc(schedule(effective_to=date(2026, 12, 31)))
    assert any("31/12/2026" in block.text for block in kbdoc.blocks)


# ------------------------------------------------------------------------------ publishing


def test_a_schedule_is_created_for_review_not_published(
    rates: RateScheduleService, session: Session
) -> None:
    """Rates are `customer_facing`: the class that can never publish itself (INV-8)."""
    outcome = rates.create(schedule(), actor="u-retail-steward")
    assert outcome.published is False
    assert outcome.items == len(ITEMS)

    document = repo.get_document(session, outcome.document_id)
    assert document is not None
    assert document.doc_class == DocClass.CUSTOMER_FACING.value
    assert document.status == DocStatus.DRAFT.value
    assert document.canonical_version_id is None


def test_the_version_carries_the_effective_dates(
    rates: RateScheduleService, session: Session
) -> None:
    """The ACL filter is date-aware: a schedule that starts next year must not answer today."""
    outcome = rates.create(
        schedule(effective_from=date(2026, 3, 1), effective_to=date(2026, 12, 31)),
        actor="u-retail-steward",
    )
    version = repo.get_version(session, outcome.version_id)
    assert version is not None
    assert version.effective_from == date(2026, 3, 1)
    assert version.effective_to == date(2026, 12, 31)
    # And the PII gate still owns publishability (INV-7).
    assert version.pii_status == PiiStatus.PENDING.value


def test_the_reviewer_sees_the_numbers_without_opening_the_document(
    rates: RateScheduleService, session: Session
) -> None:
    outcome = rates.create(schedule(), actor="u-retail-steward")
    task = repo.get_review_task(session, outcome.review_task_id)
    assert task is not None
    assert task.task_type == ReviewTaskType.IDP_REVIEW.value
    assert task.assignee_group == "dept/retail"
    assert task.payload["rate_schedule"] is True
    assert task.payload["items"] == len(ITEMS)
    assert any("miễn phí" in line for line in task.payload["preview"])


def test_an_end_date_before_the_start_is_refused(rates: RateScheduleService) -> None:
    with pytest.raises(ValidationError):
        rates.create(
            schedule(effective_from=date(2026, 6, 1), effective_to=date(2026, 1, 1)),
            actor="u-retail-steward",
        )


def test_a_schedule_can_be_prepared_for_internal_eyes_only(
    rates: RateScheduleService, session: Session
) -> None:
    """Not every schedule is public — an internal transfer-pricing table is not."""
    outcome = rates.create(schedule(visibility=Visibility.INTERNAL_ALL), actor="u-retail-steward")
    document = repo.get_document(session, outcome.document_id)
    assert document is not None
    assert document.visibility == Visibility.INTERNAL_ALL.value


def test_every_fee_is_quotable_on_its_own_after_chunking() -> None:
    """Each sentence stands alone in the chunk, so quoting one quotes a complete fee.

    Short neighbours are merged by the chunker, which is right for prose and harmless here:
    what must survive is that the sentence an answer quotes carries its own figure, condition
    and effective date, rather than depending on a heading the chunk no longer contains.
    """
    chunks = chunk_document(build_rate_kbdoc(schedule()))
    text = "\n".join(chunk.text for chunk in chunks)
    for item in ITEMS:
        sentence = next(line for line in text.splitlines() if line.startswith(item.label))
        assert item.value in sentence
        assert "01/01/2026" in sentence


def test_a_long_schedule_splits_instead_of_growing_one_giant_chunk() -> None:
    many = [
        RateItem(
            label=f"Phí dịch vụ số {index}",
            value=str(1000 + index),
            unit="VND",
            condition="khách hàng cá nhân, số dư bình quân từ 2.000.000 VND mỗi tháng",
        )
        for index in range(40)
    ]
    chunks = chunk_document(build_rate_kbdoc(schedule(items=many)))
    assert len(chunks) > 3, "a 40-item schedule collapsed into one chunk"
    assert all(len(chunk.text) <= 2000 for chunk in chunks)
