"""Rates and fees as structured content (M8).

The public bot's most common question is "what does this cost?", and the honest answer needs
three things a paragraph of prose usually loses: the figure, what it applies to, and the date
it took effect. A fee schedule pasted in as a PDF gives the retriever one blob of text where a
customer needs one line.

So a schedule is published as *items*: each becomes its own block, its own chunk, and its own
citation — "Phí duy trì tài khoản: miễn phí với số dư bình quân từ 2.000.000 VND (áp dụng từ
01/01/2026)". A question about one fee retrieves that line and nothing else, and the effective
date travels with the number rather than sitting in a header the chunk no longer contains.

Two properties this does not change, deliberately:

* **It publishes through the ordinary path.** A rate schedule is `customer_facing`, which is a
  no-automation class (INV-8), so this creates a version and a review task — not a published
  document. Someone approves the numbers the bank is about to tell the public.
* **It goes through the PII gate** like anything else (INV-7). Fee schedules are the one
  customer-facing artefact most likely to arrive with an example account number in it.
"""

from __future__ import annotations

import io
import uuid
from datetime import date

from kb_common.errors import ValidationError
from kb_common.logging import get_logger
from kb_idp.builder import KBDocBuilder
from kb_ports.storage import StoragePort
from kb_registry.schemas import DocumentCreate, VersionCreate
from kb_registry.service import RegistryService
from kb_schemas.enums import DocClass, ReviewTaskType, SourceType, Visibility
from kb_schemas.kbdoc import KBDoc
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

log = get_logger(__name__)

#: A schedule with more rows than this is a document, not a schedule; splitting it is a
#: content decision the publisher should make rather than a limit we silently apply.
MAX_ITEMS = 200
#: Rows per table block. The chunker emits a table whole — splitting one would strand rows
#: from their header — so a 200-row schedule in a single table would be a single enormous
#: chunk that crowds everything else out of an answer's context. Emitting several tables, each
#: carrying the header, keeps every row attributable and every chunk a usable size.
TABLE_ROWS_PER_BLOCK = 15


class RateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: What is charged or paid for: "Phí duy trì tài khoản thanh toán".
    label: str = Field(min_length=1, max_length=300)
    #: The figure as published — "miễn phí", "0,5%/năm", "20.000". A string, not a number:
    #: "miễn phí với điều kiện" is a real value, and rounding a published fee is a way to
    #: publish a fee the bank did not approve.
    value: str = Field(min_length=1, max_length=200)
    #: "VND", "%/năm", "VND/giao dịch". Empty when the value carries its own unit.
    unit: str = Field(default="", max_length=50)
    #: When it applies: "số dư bình quân từ 2.000.000 VND", "khách hàng ưu tiên".
    condition: str = Field(default="", max_length=500)
    #: Anything a customer must be told alongside the number.
    note: str = Field(default="", max_length=500)

    def as_sentence(self, effective_from: date) -> str:
        parts = [f"{self.label}: {self.value}"]
        if self.unit:
            parts[0] = f"{parts[0]} {self.unit}"
        if self.condition:
            parts.append(f"áp dụng với {self.condition}")
        if self.note:
            parts.append(self.note.rstrip("."))
        # The date rides with the figure, because the chunk that carries the figure is what an
        # answer quotes — and a fee without its effective date is a fee the bank may not
        # currently charge.
        parts.append(f"hiệu lực từ ngày {effective_from.strftime('%d/%m/%Y')}")
        return ". ".join(parts) + "."


class RateScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    category_path: str
    effective_from: date
    items: list[RateItem] = Field(min_length=1, max_length=MAX_ITEMS)
    #: Published externally by default — that is what a rate schedule is for. Still a
    #: *request*: the visibility that takes effect is the one the reviewer approves.
    visibility: Visibility = Visibility.EXTERNAL
    department: str | None = None
    effective_to: date | None = None
    #: Free text under the table: scope, exclusions, where the full schedule lives.
    footnote: str = ""


class RateScheduleOut(BaseModel):
    document_id: uuid.UUID
    version_id: uuid.UUID
    review_task_id: uuid.UUID
    items: int
    #: Always false, and returned so a caller cannot mistake acceptance for publication.
    published: bool = False


def build_rate_kbdoc(schedule: RateScheduleIn) -> KBDoc:
    """One block per item, then the table, then the footnote."""
    builder = KBDocBuilder(source_format="structured")
    builder.set_title_hint(schedule.title)
    builder.add_heading(schedule.title)
    builder.add(
        f"Biểu phí/lãi suất áp dụng từ ngày {schedule.effective_from.strftime('%d/%m/%Y')}"
        + (
            f" đến ngày {schedule.effective_to.strftime('%d/%m/%Y')}."
            if schedule.effective_to
            else "."
        )
    )

    for item in schedule.items:
        # One sentence per item, complete on its own: label, figure, condition and effective
        # date. The chunker groups short neighbours (that is right for prose and harmless
        # here), so a small schedule is one chunk of complete sentences and a long one splits
        # by size — either way the sentence an answer quotes carries its own date, which is
        # the property that matters when the quote reaches a customer.
        builder.add(item.as_sentence(schedule.effective_from))

    # The same content as a table, for the portal and for a reader who wants the whole
    # schedule at once. Retrieval will usually match the sentence above instead, which is the
    # point: a table row out of context is not an answer.
    header = ["Khoản mục", "Mức", "Điều kiện", "Ghi chú"]
    rows = [
        [item.label, f"{item.value} {item.unit}".strip(), item.condition, item.note]
        for item in schedule.items
    ]
    for start in range(0, len(rows), TABLE_ROWS_PER_BLOCK):
        builder.add_table([header, *rows[start : start + TABLE_ROWS_PER_BLOCK]], header_rows=1)
    if schedule.footnote:
        builder.add(schedule.footnote)

    return builder.build(page_count=1)


class RateScheduleService:
    def __init__(
        self,
        session: Session,
        *,
        storage: StoragePort,
        registry: RegistryService,
        derived_bucket: str = "kb-derived",
    ) -> None:
        self._session = session
        self._storage = storage
        self._registry = registry
        self._bucket = derived_bucket

    def create(self, schedule: RateScheduleIn, *, actor: str) -> RateScheduleOut:
        if schedule.effective_to and schedule.effective_to < schedule.effective_from:
            raise ValidationError("effective_to must not precede effective_from")

        kbdoc = build_rate_kbdoc(schedule)
        payload = kbdoc.model_dump_json().encode("utf-8")
        stored = self._storage.put(
            self._bucket, io.BytesIO(payload), suffix=".kbdoc.json", content_type="application/json"
        )

        document = self._registry.create_document(
            DocumentCreate(
                title=schedule.title,
                # Rates and fees are what the bank tells its customers, so they carry the
                # class that can never publish itself (INV-8).
                doc_class=DocClass.CUSTOMER_FACING,
                category_path=schedule.category_path,
                department=schedule.department,
                visibility=schedule.visibility,
            ),
            actor=actor,
        )
        version = self._registry.create_version(
            VersionCreate(
                document_id=document.id,
                content_ref=f"{self._bucket}/{stored.key}",
                content_hash=stored.content_hash,
                source_type=SourceType.PORTAL_EDIT,
                author=actor,
                change_summary=f"Biểu phí/lãi suất với {len(schedule.items)} khoản mục",
                idp_report_ref=f"{self._bucket}/{stored.key}",
                effective_from=schedule.effective_from,
                effective_to=schedule.effective_to,
            ),
            actor=actor,
        )
        task = self._registry.open_review_task(
            version.id,
            ReviewTaskType.IDP_REVIEW,
            assignee_group=self._registry.steward_group_for(schedule.category_path),
            payload={
                "kbdoc_ref": f"{self._bucket}/{stored.key}",
                "rate_schedule": True,
                "items": len(schedule.items),
                "effective_from": schedule.effective_from.isoformat(),
                "visibility_requested": schedule.visibility.value,
                # The reviewer is approving numbers that will be quoted to the public, so the
                # queue shows them without opening the document.
                "preview": [
                    item.as_sentence(schedule.effective_from) for item in schedule.items[:5]
                ],
            },
        )
        log.info(
            "rate_schedule_created",
            extra={
                "document_id": str(document.id),
                "version_id": str(version.id),
                "items": len(schedule.items),
                "visibility": schedule.visibility.value,
            },
        )
        return RateScheduleOut(
            document_id=document.id,
            version_id=version.id,
            review_task_id=task.id,
            items=len(schedule.items),
        )
