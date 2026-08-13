"""Access to the scanned fixture corpus and its recordings.

Imported by the IDP tests, the workflow tests and the review-editor tests, so all three run
against the same twenty documents and the same recorded recognition.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from kb_ports.adapters.ocr_recorded import RecordedOcrAdapter
from kb_ports.adapters.vlm import RecordedVlmAdapter

from kb_idp.testing.scan_corpus import BY_NAME, ESCALATING, SCAN_NAMES, ScanSpec

SCAN_DIR = Path(__file__).parent / "scans"

__all__ = [
    "ESCALATING",
    "SCAN_NAMES",
    "ScanSpec",
    "ground_truth",
    "manifest",
    "recorded_ocr",
    "recorded_vlm",
    "scan_bytes",
    "spec",
]


def scan_bytes(name: str) -> bytes:
    path = SCAN_DIR / f"{name}.pdf"
    if not path.is_file():
        raise FileNotFoundError(
            f"scanned fixture {name!r} is missing — regenerate with "
            f"`python services/idp/src/kb_idp/testing/make_scans.py`"
        )
    return path.read_bytes()


def spec(name: str) -> ScanSpec:
    return BY_NAME[name]


def ground_truth(name: str) -> str:
    """The text the scan was rendered from — what OCR output is compared against."""
    return BY_NAME[name].full_text


@lru_cache(maxsize=1)
def manifest() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SCAN_DIR / "manifest.json").read_text(encoding="utf-8"))
    return data


@lru_cache(maxsize=1)
def recorded_ocr() -> RecordedOcrAdapter:
    """Strict by default: an unrecognized page image is a fixture drift, not a blank page."""
    return RecordedOcrAdapter.from_directory(SCAN_DIR)


@lru_cache(maxsize=1)
def recorded_vlm() -> RecordedVlmAdapter:
    return RecordedVlmAdapter.from_directory(SCAN_DIR)
