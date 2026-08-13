#!/usr/bin/env python
"""Regenerate the two committed PDF fixtures.

Run manually when the fixtures need to change; the output is committed so tests never depend
on a Vietnamese-capable font being installed. Requires DejaVuSans (Debian: fonts-dejavu-core).

    python services/idp/src/kb_idp/testing/make_pdfs.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

FIXTURE_DIR = Path(__file__).parent
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
)

CIRCULAR_PAGES = [
    [
        ("NGÂN HÀNG NHÀ NƯỚC VIỆT NAM", 11),
        ("Số: 36/2014/TT-NHNN", 11),
        ("THÔNG TƯ", 18),
        ("Quy định các giới hạn, tỷ lệ bảo đảm an toàn trong hoạt động", 11),
        ("", 11),
        ("Chương I", 14),
        ("QUY ĐỊNH CHUNG", 14),
        ("", 11),
        ("Điều 1. Phạm vi điều chỉnh", 13),
        ("Thông tư này quy định về các giới hạn, tỷ lệ bảo đảm an toàn", 11),
        ("trong hoạt động của tổ chức tín dụng, chi nhánh ngân hàng nước ngoài.", 11),
        ("", 11),
        ("Điều 2. Đối tượng áp dụng", 13),
        ("1. Tổ chức tín dụng, bao gồm ngân hàng thương mại.", 11),
        ("2. Chi nhánh ngân hàng nước ngoài hoạt động tại Việt Nam.", 11),
    ],
    [
        ("Chương II", 14),
        ("TỶ LỆ AN TOÀN VỐN", 14),
        ("", 11),
        ("Điều 9. Tỷ lệ an toàn vốn tối thiểu", 13),
        ("1. Tổ chức tín dụng phải duy trì tỷ lệ an toàn vốn tối thiểu 9%.", 11),
        ("2. Cách xác định thực hiện theo Thông tư 41/2016/TT-NHNN.", 11),
        ("", 11),
        ("Điều 10. Báo cáo", 13),
        ("Tổ chức tín dụng báo cáo Ngân hàng Nhà nước theo định kỳ hằng quý.", 11),
    ],
]

BILINGUAL_PAGE = [
    ("Internal Capital Adequacy Assessment", 16),
    ("Đánh giá mức đủ vốn nội bộ", 13),
    ("", 11),
    ("Article 1. Scope", 13),
    ("This document applies to all business units of the bank.", 11),
    ("Tài liệu này áp dụng cho toàn bộ các đơn vị kinh doanh của ngân hàng.", 11),
    ("", 11),
    ("Article 2. Minimum ratio", 13),
    ("The bank maintains a capital adequacy ratio above the regulatory", 11),
    ("minimum defined in Thông tư 41/2016/TT-NHNN.", 11),
]


def find_font() -> str:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit(
        "no Unicode font found — install fonts-dejavu-core, or add a path to FONT_CANDIDATES"
    )


def write_pdf(path: Path, pages: list[list[tuple[str, int]]], font_path: str) -> None:
    document = pymupdf.open()
    for lines in pages:
        page = document.new_page()
        page.insert_font(fontname="vn", fontfile=font_path)
        y = 72.0
        for text, size in lines:
            if text:
                page.insert_text((72, y), text, fontname="vn", fontsize=size)
            y += size * 1.6
    # Subset the embedded font: the fixtures live in the repository, and a full DejaVu
    # embed is ~800 kB per file for a page of text.
    document.subset_fonts()
    document.save(path, garbage=4, deflate=True)
    document.close()


def write_scanned_pdf(path: Path) -> None:
    """A PDF with no usable text layer — the OCR routing fixture (M3 boundary).

    Rendered as an image so there is genuinely nothing to extract, exactly like the scans the
    bank's archive actually holds.
    """
    source = pymupdf.open(FIXTURE_DIR / "circular_native.pdf")
    document = pymupdf.open()
    for page_index in range(source.page_count):
        source_page = source[page_index]
        pixmap = source_page.get_pixmap(dpi=72, colorspace=pymupdf.csGRAY)
        page = document.new_page(width=source_page.rect.width, height=source_page.rect.height)
        page.insert_image(page.rect, stream=pixmap.tobytes("jpeg", jpg_quality=60))
    document.save(path, garbage=4, deflate=True)
    document.close()
    source.close()


def main() -> int:
    font_path = find_font()
    write_pdf(FIXTURE_DIR / "circular_native.pdf", CIRCULAR_PAGES, font_path)
    write_pdf(FIXTURE_DIR / "bilingual_report.pdf", [BILINGUAL_PAGE], font_path)
    write_scanned_pdf(FIXTURE_DIR / "scanned_notice.pdf")
    print(f"wrote 3 PDF fixtures to {FIXTURE_DIR} using {font_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
