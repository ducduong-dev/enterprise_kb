"""Image preparation before OCR.

Scans in this archive are photocopies of photocopies: skewed by a degree or two from a sheet
feeder, speckled, sometimes bordered by the black edge of the scanner lid. PaddleOCR handles
clean text well and degrades sharply on all three, so the cheapest accuracy gains available
are here, before any model runs.

Every step reports whether it fired and by how much. That matters twice over: a reviewer
looking at a bad page needs to know whether the image was rotated under them, and a page whose
preprocessing did nothing but still scored badly is a different problem from one that was
deskewed by 4° and still scored badly.

Deliberately conservative: each step is skipped when its own measurement says it is not needed,
because a deskew of 0.05° or an aggressive denoise on clean text costs accuracy rather than
buying it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import cv2
import numpy as np
from numpy.typing import NDArray

Image = NDArray[np.uint8]

#: Below this angle, rotating costs more (resampling blur) than it gains.
MIN_DESKEW_DEGREES = 0.3
#: Above this, the page is not skewed — it is rotated or misdetected. Rotating by a wrong
#: large angle destroys the page, so leave it and let the confidence score flag it.
MAX_DESKEW_DEGREES = 15.0
#: A scan below this is too coarse for reliable Vietnamese diacritics; upscale before OCR.
MIN_DPI = 200
#: Fraction of a border row/column that must be dark before it is treated as scanner edge.
BORDER_DARK_RATIO = 0.85


@dataclass
class PreprocessReport:
    """What was done to the image, in the reviewer's terms."""

    deskew_degrees: float = 0.0
    denoised: bool = False
    binarized: bool = False
    border_trimmed_px: tuple[int, int, int, int] = (0, 0, 0, 0)
    upscaled_from_dpi: int | None = None
    steps: list[str] = field(default_factory=list)

    @property
    def modified(self) -> bool:
        return bool(self.steps)

    def as_dict(self) -> dict[str, object]:
        return {
            "deskew_degrees": round(self.deskew_degrees, 2),
            "denoised": self.denoised,
            "binarized": self.binarized,
            "border_trimmed_px": list(self.border_trimmed_px),
            "upscaled_from_dpi": self.upscaled_from_dpi,
            "steps": self.steps,
        }


def prepare(
    image: Image, *, dpi: int = 300, binarize: bool = True
) -> tuple[Image, PreprocessReport]:
    """Return an OCR-ready image and a report of what changed.

    Order matters: trim the scanner border first (it dominates the skew estimate), then
    deskew (text lines must be horizontal for the line detector), then denoise, then binarize
    last so the threshold sees the corrected image.
    """
    report = PreprocessReport()
    working = to_grayscale(image)

    working, trimmed = trim_border(working)
    if any(trimmed):
        report.border_trimmed_px = trimmed
        report.steps.append("border_trim")

    if dpi < MIN_DPI:
        scale = MIN_DPI / dpi
        working = _as_u8(
            cv2.resize(working, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        )
        report.upscaled_from_dpi = dpi
        report.steps.append("upscale")

    angle = estimate_skew(working)
    if MIN_DESKEW_DEGREES <= abs(angle) <= MAX_DESKEW_DEGREES:
        working = rotate(working, -angle)
        report.deskew_degrees = angle
        report.steps.append("deskew")

    if is_noisy(working):
        # Median blur, not Gaussian: speckle from photocopying is impulse noise, and a
        # Gaussian blur smears the thin strokes Vietnamese tone marks are made of.
        working = _as_u8(cv2.medianBlur(working, 3))
        report.denoised = True
        report.steps.append("denoise")

    if binarize:
        working = adaptive_binarize(working)
        report.binarized = True
        report.steps.append("binarize")

    return working, report


def _as_u8(array: NDArray[Any]) -> Image:
    """OpenCV's stubs widen every return to a generic ndarray; every operation here keeps
    8-bit images, so one narrowing helper beats a cast at each call site."""
    return cast("Image", array)


def to_grayscale(image: Image) -> Image:
    if image.ndim == 3:
        return _as_u8(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
    return image


def estimate_skew(image: Image) -> float:
    """Skew angle in degrees, positive clockwise.

    Measured from the minimum-area rectangle around the dark pixels: on a page of text that
    rectangle's angle is the text baseline's angle. Cheaper and steadier on sparse pages than
    a Hough transform, which needs long straight lines the page may not have.
    """
    inverted = cv2.bitwise_not(image)
    _, mask = cv2.threshold(inverted, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    coordinates = cv2.findNonZero(mask)
    if coordinates is None or len(coordinates) < 50:
        return 0.0

    angle = cv2.minAreaRect(coordinates)[-1]
    # OpenCV returns the angle in (0, 90]; map it to the nearest small rotation.
    if angle > 45:
        angle -= 90
    return float(angle)


def rotate(image: Image, degrees: float) -> Image:
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), degrees, 1.0)
    return _as_u8(
        cv2.warpAffine(
            image,
            matrix,
            (width, height),
            flags=cv2.INTER_CUBIC,
            # White border: the page is white, and a black fill would look like content to
            # both the binarizer and the layout detector.
            borderMode=cv2.BORDER_REPLICATE,
        )
    )


def is_noisy(image: Image, threshold: float = 0.012) -> bool:
    """Estimate impulse noise as the share of pixels a median filter would move a lot."""
    filtered = cv2.medianBlur(image, 3)
    difference = cv2.absdiff(image, filtered)
    return bool(float(np.count_nonzero(difference > 40)) / difference.size > threshold)


def adaptive_binarize(image: Image) -> Image:
    """Local thresholding — a photocopy's illumination is uneven across the page, and one
    global threshold turns the dark side into a blob and the light side into blank paper."""
    return _as_u8(
        cv2.adaptiveThreshold(
            image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, blockSize=31, C=15
        )
    )


def trim_border(image: Image) -> tuple[Image, tuple[int, int, int, int]]:
    """Remove the black frame a scanner lid leaves around an undersized page."""
    height, width = image.shape[:2]
    dark = image < 64

    top = _leading_dark_lines(dark, axis=1)
    bottom = _leading_dark_lines(dark[::-1], axis=1)
    left = _leading_dark_lines(dark.T, axis=1)
    right = _leading_dark_lines(dark.T[::-1], axis=1)

    if not any((top, bottom, left, right)):
        return image, (0, 0, 0, 0)
    # Never trim away the page: a fully dark scan would otherwise become an empty image.
    if top + bottom >= height * 0.5 or left + right >= width * 0.5:
        return image, (0, 0, 0, 0)

    cropped = image[top : height - bottom, left : width - right]
    return cropped, (top, bottom, left, right)


def _leading_dark_lines(dark: NDArray[np.bool_], axis: int) -> int:
    count = 0
    for line in dark:
        if line.mean() < BORDER_DARK_RATIO:
            break
        count += 1
    return count


def decode_image(data: bytes) -> Image:
    """Decode image bytes with OpenCV, preserving the original bit depth."""
    buffer = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_UNCHANGED)
    if image is None:
        from kb_idp.detect import UnsupportedFormat

        raise UnsupportedFormat("image could not be decoded")
    decoded: Image = image.astype(np.uint8)
    return decoded


def encode_png(image: Image) -> bytes:
    """Encode losslessly. JPEG artefacts around thin strokes cost diacritic accuracy, and the
    page images are stored for the review editor to display next to the recognized text."""
    ok, buffer = cv2.imencode(".png", image)
    if not ok:  # pragma: no cover - only on an invalid array
        raise ValueError("failed to encode image")
    return bytes(buffer.tobytes())
