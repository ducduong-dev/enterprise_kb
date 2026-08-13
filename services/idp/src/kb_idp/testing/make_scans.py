#!/usr/bin/env python
"""Generate the twenty scanned fixtures and their OCR recordings.

    python services/idp/src/kb_idp/testing/make_scans.py

Two artefacts per fixture, both committed:

* **the scan** — the ground truth rendered to a page, then damaged the way the archive's
  documents are damaged (skew from a sheet feeder, photocopy speckle, blur, scanner-lid
  border), and stored as an image-only PDF with no text layer;
* **the recording** — what the OCR engine "returns" for each page, keyed by the hash of the
  *preprocessed* page image.

Keying on the preprocessed image is the part that matters. Rasterization and preprocessing run
here exactly as they do in the pipeline, so if either changes, the key changes, the recording
misses, and the tests fail loudly. A recording that silently replayed stale output for an image
that no longer exists would be worse than no test at all.

The simulated recognition reproduces the error modes that matter for Vietnamese: tone marks
lost (the failure that produces plausible wrong words rather than obvious garbage), speckle
read as punctuation, and lines split. Escalating fixtures get output bad enough to fail the
confidence score, and a VLM recording that returns the ground truth — which is what makes the
escalation path's *benefit* observable rather than assumed.

This measures the pipeline, never PaddleOCR's accuracy. Real accuracy numbers come from the
GPU node.
"""

from __future__ import annotations

import json
import random
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kb_idp.preprocess import encode_png, prepare
from kb_idp.testing.scan_corpus import SCANS, ScanSpec

SCAN_DIR = Path(__file__).parent / "scans"
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)

#: The pipeline's rasterization DPI. Imported rather than repeated: if the pipeline changes
#: it, every recording key changes with it, and the fixtures must be regenerated.
from kb_idp.ocr_pipeline import RENDER_DPI  # noqa: E402

#: Scans are stored at a lower resolution than they are read at — which is also true of the
#: archive, and keeps twenty committed fixtures to a couple of megabytes rather than twenty.
SCAN_DPI = 150
JPEG_QUALITY = 50
PAGE_WIDTH, PAGE_HEIGHT = 595, 842  # A4 at 72 dpi
MARGIN = 60
LINE_HEIGHT = 22
FONT_SIZE = 11

#: Fixed seed: fixtures must be byte-identical on every machine and every run.
SEED = 20260811


@dataclass
class SimulatedLine:
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float


def find_font() -> str:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("no Unicode font found — install fonts-dejavu-core")


# ------------------------------------------------------------------------------- rendering


def render_clean_page(lines: list[str], font_path: str) -> np.ndarray:
    """Lay the ground truth out on an A4 page and rasterize it."""
    document = pymupdf.open()
    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page.insert_font(fontname="vn", fontfile=font_path)
    y = MARGIN
    for line in lines:
        if line.strip():
            page.insert_text((MARGIN, y), line, fontname="vn", fontsize=FONT_SIZE)
        y += LINE_HEIGHT
    pixmap = page.get_pixmap(dpi=SCAN_DPI)
    array = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width, pixmap.n
    )
    document.close()
    return cv2.cvtColor(array[:, :, :3], cv2.COLOR_RGB2GRAY)


def damage(image: np.ndarray, spec: ScanSpec, rng: random.Random) -> np.ndarray:
    """Make a clean render look like something that went through a photocopier twice."""
    result = image

    if spec.skew_degrees:
        height, width = result.shape[:2]
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), -spec.skew_degrees, 1.0)
        result = cv2.warpAffine(
            result,
            matrix,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=255,
        )

    if spec.blur:
        result = cv2.GaussianBlur(result, (spec.blur, spec.blur), 0)

    if spec.noise:
        # Salt-and-pepper: what photocopy speckle actually looks like, and what the OCR
        # engine turns into stray punctuation.
        noise = np.array(
            [[rng.random() for _ in range(result.shape[1])] for _ in range(result.shape[0])],
            dtype=np.float32,
        )
        result = result.copy()
        result[noise < spec.noise / 2] = 0
        result[noise > 1 - spec.noise / 2] = 255

    if spec.border_px:
        result = cv2.copyMakeBorder(result, *(spec.border_px,) * 4, cv2.BORDER_CONSTANT, value=0)

    return result


def to_image_pdf(pages: list[np.ndarray]) -> bytes:
    """Wrap the damaged page images in a PDF with no text layer — a scan, in other words."""
    document = pymupdf.open()
    for image in pages:
        ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:  # pragma: no cover - only on an invalid array
            raise RuntimeError("failed to encode page")
        height, width = image.shape[:2]
        page = document.new_page(width=width * 72 / SCAN_DPI, height=height * 72 / SCAN_DPI)
        page.insert_image(page.rect, stream=buffer.tobytes())
    data: bytes = document.tobytes(garbage=4, deflate=True)
    document.close()
    return data


# ---------------------------------------------------------------------- simulated reading


def strip_tone_marks(text: str) -> str:
    """The characteristic Vietnamese OCR failure: the word survives, its meaning does not."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.replace("đ", "d").replace("Đ", "D")


def simulate_page(
    spec: ScanSpec, page: int, image_height: int, image_width: int, rng: random.Random
) -> list[SimulatedLine]:
    """Derive "recognized" lines from the ground truth, with realistic damage."""
    escalating = page in spec.escalate_pages
    lines = spec.pages[page - 1]
    simulated: list[SimulatedLine] = []

    # Lay the boxes out the way the render did, scaled to the recognized image.
    scale = image_height / PAGE_HEIGHT
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        top = (MARGIN + index * LINE_HEIGHT - FONT_SIZE) * scale
        bottom = (MARGIN + index * LINE_HEIGHT + 4) * scale
        left = MARGIN * scale
        right = min(image_width, (MARGIN + len(line) * FONT_SIZE * 0.52) * scale)

        text = line
        confidence = spec.base_confidence

        if escalating:
            # Badly degraded pages lose their tone marks wholesale — the failure the
            # confidence scorer is built to catch.
            text = strip_tone_marks(text)
            confidence = rng.uniform(0.45, 0.70)
            if rng.random() < 0.30:
                text = f"{text} ,."  # speckle read as punctuation
        else:
            confidence = min(0.99, max(0.80, rng.gauss(spec.base_confidence, 0.02)))
            if rng.random() < 0.06:
                # One word in a clean page loses its marks: the reviewer's actual job.
                words = text.split()
                if len(words) > 3:
                    position = rng.randrange(len(words))
                    words[position] = strip_tone_marks(words[position])
                    text = " ".join(words)
                    confidence = min(confidence, 0.78)

        simulated.append(
            SimulatedLine(text=text, bbox=(left, top, right, bottom), confidence=confidence)
        )
    return simulated


# --------------------------------------------------------------------------------- output


def build() -> None:
    font_path = find_font()
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    ocr_recordings: dict[str, Any] = {}
    vlm_recordings: dict[str, str] = {}
    manifest: dict[str, Any] = {}

    for spec in SCANS:
        damaged_pages = [
            damage(render_clean_page(page, font_path), spec, rng) for page in spec.pages
        ]
        pdf_bytes = to_image_pdf(damaged_pages)
        (SCAN_DIR / f"{spec.name}.pdf").write_bytes(pdf_bytes)

        # Rasterize and preprocess exactly as the pipeline will, so the keys agree.
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        page_keys: list[str] = []
        for index in range(document.page_count):
            pixmap = document[index].get_pixmap(dpi=RENDER_DPI)
            array = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                pixmap.height, pixmap.width, pixmap.n
            )
            rgb = array[:, :, :3][:, :, ::-1].copy()
            prepared, _report = prepare(rgb, dpi=RENDER_DPI)
            png = encode_png(prepared)

            from kb_ports.adapters.ocr_recorded import image_key

            key = image_key(png)
            page_keys.append(key)

            lines = simulate_page(spec, index + 1, prepared.shape[0], prepared.shape[1], rng)
            ocr_recordings[key] = {
                "fixture": spec.name,
                "page": index + 1,
                "lines": [
                    {"text": line.text, "bbox": list(line.bbox), "confidence": line.confidence}
                    for line in lines
                ],
                "mean_confidence": (
                    sum(line.confidence for line in lines) / len(lines) if lines else 0.0
                ),
                "tables": _tables_for(spec, index + 1),
            }
            if index + 1 in spec.escalate_pages:
                # The VLM reads the page correctly — that is what escalation buys, and the
                # tests assert the improvement rather than assuming it.
                vlm_recordings[key] = "\n".join(spec.text(index + 1))

        document.close()
        manifest[spec.name] = {
            "pages": spec.page_count,
            "page_keys": page_keys,
            "escalate_pages": list(spec.escalate_pages),
            "legal_number": spec.legal_number,
            "language": spec.language,
            "notes": spec.notes,
        }

    (SCAN_DIR / "corpus.ocr.json").write_text(
        json.dumps(ocr_recordings, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )
    (SCAN_DIR / "corpus.vlm.json").write_text(
        json.dumps(vlm_recordings, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )
    (SCAN_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )
    print(
        f"wrote {len(SCANS)} scanned fixtures, {len(ocr_recordings)} OCR page recordings "
        f"and {len(vlm_recordings)} VLM recordings to {SCAN_DIR}"
    )


def _tables_for(spec: ScanSpec, page: int) -> list[list[list[str]]]:
    """PP-Structure output for the fixtures that contain a table."""
    if not spec.has_table:
        return []
    rows = [line.split(" | ") for line in spec.text(page) if line.count("|") >= 1]
    return [rows] if rows else []


if __name__ == "__main__":
    build()
