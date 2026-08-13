"""Golden parser fixtures — M1 acceptance criterion.

Each of the twelve fixtures is parsed and compared against a committed summary: block count
and type sequence, structural paths, table shapes, detected metadata and references, plus a
SHA-256 of the concatenated block text. The hash is the diacritic guard — any normalization,
re-encoding or "helpful" cleanup anywhere in the chain changes it, and the test says so.

Regenerate after an intentional parser change:

    KB_UPDATE_GOLDENS=1 uv run pytest services/idp/tests/test_parsers_golden.py

and read the diff before committing it. A golden that changes without a reason in the diff is
the bug this file exists to catch.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path

import pytest
from kb_idp.service import process
from kb_idp.testing.fixtures import ALL_FIXTURES, OCR_ROUTING_FIXTURE, fixture_bytes
from kb_schemas.kbdoc import KBDoc

GOLDEN_DIR = Path(__file__).parent / "goldens"
UPDATE = os.environ.get("KB_UPDATE_GOLDENS") == "1"


def summarize(kbdoc: KBDoc) -> dict[str, object]:
    """A stable, reviewable description of a parse result."""
    text = "\n".join(block.text for block in kbdoc.blocks)
    return {
        "doc_meta": {
            "detected_title": kbdoc.doc_meta.detected_title,
            "legal_number": kbdoc.doc_meta.legal_number,
            "language": kbdoc.doc_meta.language,
            "source_format": kbdoc.doc_meta.source_format,
            "page_count": kbdoc.doc_meta.page_count,
        },
        "block_count": len(kbdoc.blocks),
        "block_types": dict(sorted(Counter(b.type for b in kbdoc.blocks).items())),
        "section_paths": sorted(
            {" > ".join(b.section_path) for b in kbdoc.blocks if b.section_path}
        ),
        "tables": [
            {"rows": len(b.table.rows), "cols": max((len(r) for r in b.table.rows), default=0)}
            for b in kbdoc.blocks
            if b.table is not None
        ],
        "detected_refs": sorted(
            {(r.legal_number or r.raw, r.ref_type_guess) for r in kbdoc.detected_refs}
        ),
        "warnings": kbdoc.idp_report.warnings,
        # Byte-level: proves Vietnamese diacritics survive the whole parse chain unchanged.
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_chars": len(text),
    }


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_fixture_matches_its_golden(name: str) -> None:
    result = process(fixture_bytes(name), name)
    assert not result.requires_ocr, f"{name} unexpectedly routed to OCR"
    summary = summarize(result.kbdoc)

    golden_path = GOLDEN_DIR / f"{name}.json"
    if UPDATE or not golden_path.is_file():
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if not UPDATE:
            pytest.fail(f"golden for {name} was missing; it has been written — review it")
        return

    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    # Compare as JSON so tuples and lists agree; the assert message shows the whole diff.
    assert json.loads(json.dumps(summary)) == golden


def test_exactly_twelve_golden_fixtures() -> None:
    assert len(ALL_FIXTURES) == 12
    assert len(set(ALL_FIXTURES)) == 12


def test_scanned_pdf_routes_to_ocr_instead_of_producing_an_empty_document() -> None:
    """A scan must be a routing decision, not a document with no content in it."""
    result = process(fixture_bytes(OCR_ROUTING_FIXTURE), OCR_ROUTING_FIXTURE)
    assert result.requires_ocr
    assert result.reason and "text layer" in result.reason
    assert result.kbdoc.blocks == []
