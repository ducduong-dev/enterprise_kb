"""Every workflow payload survives Temporal's converter.

Activities are called directly in the other tests, which is the right way to test what they
*do* — and which never exercises the one thing that only a real worker does: reconstruct the
dataclass from JSON using its type hints. A hint the converter cannot satisfy fails at
`ReviewTaskRequest(payload=...)` in production and nowhere in CI, so it is checked here.

This is not hypothetical. `payload: dict[str, object]` decoded fine while empty and failed the
moment a real ingest put something in it — the document sat in `pii_scan` with
"Failed decoding arguments" in the worker log.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any

import pytest
from kb_workflows import types
from temporalio.converter import DataConverter

CONVERTER = DataConverter.default.payload_converter

#: One populated instance per payload type — populated on purpose, because an empty container
#: hides exactly the hint this test exists to catch.
SAMPLES: list[Any] = [
    types.Classification(
        title="Thông tư 41",
        doc_class="regulatory",
        category_path="regulations.sbv",
        legal_number="41/2016/TT-NHNN",
        allowed_groups=["dept/legal"],
    ),
    types.UploadRef(
        bucket="kb-originals",
        key="ab/cd/ef.txt",
        content_hash="ab" * 32,
        filename="qt-2026-031.txt",
        size=1024,
    ),
    types.IngestRequest(
        upload=types.UploadRef(
            bucket="kb-originals",
            key="ab/cd/ef.txt",
            content_hash="ab" * 32,
            filename="qt-2026-031.txt",
            size=1024,
        ),
        classification=types.Classification(
            title="Quy trình QT-2026-031",
            doc_class="operational",
            category_path="internal.procedures",
        ),
        actor="u-steward",
        upload_id="52b4faa5cfc8467bb646b14abd1e3e4f",
    ),
    types.DetectedRefOut(
        legal_number="41/2016/TT-NHNN", ref_type="cites", block_id="b-3", confidence=0.9
    ),
    types.IdpOutcome(
        requires_ocr=False,
        kbdoc_ref="kb-derived/x.kbdoc.json",
        detected_title="Quy trình QT-2026-031",
        legal_number="QT-2026-031",
        language="vi",
        source_format="txt",
        page_count=1,
        block_count=9,
        low_confidence_blocks=1,
        detected_refs=[types.DetectedRefOut(legal_number="41/2016/TT-NHNN", ref_type="cites")],
        warnings=["one block below threshold"],
        page_refs=["kb-derived/p1.png"],
        page_scores=[0.87],
        escalated_pages=[1],
        page_sizes=[(1240, 1754)],
    ),
    types.RegisterOutcome(
        document_id="8ee0a7b6-0a0d-4a2e-9e5f-2f4f0a5f0f6a",
        version_id="1a2b3c4d-0a0d-4a2e-9e5f-2f4f0a5f0f6a",
        created_document=True,
        matched_existing=False,
        duplicate=False,
        steward_group="dept/operations",
    ),
    types.PiiOutcome(
        status="clear",
        finding_count=2,
        kinds={"cccd": 1, "account_number": 1},
        blocked_blocks=["b-4"],
        detector="rules",
    ),
    types.IngestOutcome(
        status="awaiting_review",
        document_id="8ee0a7b6-0a0d-4a2e-9e5f-2f4f0a5f0f6a",
        version_id="1a2b3c4d-0a0d-4a2e-9e5f-2f4f0a5f0f6a",
        review_task_id="2b2b3c4d-0a0d-4a2e-9e5f-2f4f0a5f0f6a",
        detail="one block below threshold",
    ),
    types.ReviewTaskRequest(
        version_id="8ee0a7b6-0a0d-4a2e-9e5f-2f4f0a5f0f6a",
        task_type="idp_review",
        assignee_group="dept/legal",
        payload={"workflow_id": "ingest-1", "confidence": 0.42, "pages": [1, 2]},
    ),
]


def _round_trip(value: Any) -> Any:
    payload = CONVERTER.to_payloads([value])
    return CONVERTER.from_payloads(payload, [type(value)])[0]


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: type(s).__name__)
def test_a_populated_payload_decodes_back_into_its_type(sample: Any) -> None:
    assert _round_trip(sample) == sample


def test_every_payload_type_has_a_sample() -> None:
    """A new payload type with no sample here is a decoding bug waiting for production."""
    declared = {
        name
        for name, obj in vars(types).items()
        if inspect.isclass(obj)
        and dataclasses.is_dataclass(obj)
        and obj.__module__ == types.__name__
    }
    covered = {type(sample).__name__ for sample in SAMPLES}
    assert declared - covered == set(), f"no converter sample for: {sorted(declared - covered)}"
