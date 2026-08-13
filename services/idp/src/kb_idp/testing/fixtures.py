"""The 12 golden parser fixtures (M1 acceptance criterion).

Office formats are generated at test time rather than committed: they are zip archives whose
bytes change with every timestamp, so a committed .docx would show as modified on every run
while its *content* stayed identical. The two PDFs are committed, because generating a PDF
with Vietnamese glyphs needs a font that may not exist on every machine, and a fixture that
silently skips is not a fixture.

Between them the twelve cover what the corpus actually contains: Vietnamese legal structure,
bilingual policies, English-only policies, fee tables, multi-sheet workbooks, hidden sheets,
slide decks with speaker notes, a scanned PDF that must route to OCR, and text/HTML exports.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent

# --------------------------------------------------------------------------------- content

TT41_ARTICLES = [
    (
        "Điều 6. Tỷ lệ an toàn vốn",
        [
            "1. Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 8% được xác định "
            "theo quy định tại Thông tư này.",
            "2. Tỷ lệ an toàn vốn được tính theo công thức quy định tại Phụ lục 1.",
        ],
    ),
    (
        "Điều 12. Tài sản có rủi ro tín dụng",
        [
            "1. Tài sản có rủi ro tín dụng được xác định theo phương pháp tiêu chuẩn.",
            "2. Hệ số rủi ro của từng loại tài sản được quy định tại Khoản 3 Điều này.",
            "a) Đối với khoản phải đòi của Chính phủ Việt Nam: hệ số rủi ro 0%.",
        ],
    ),
]


def _docx_tt41() -> bytes:
    from docx import Document

    document = Document()
    document.core_properties.title = "Thông tư 41/2016/TT-NHNN"
    document.add_paragraph("NGÂN HÀNG NHÀ NƯỚC VIỆT NAM")
    document.add_paragraph("Số: 41/2016/TT-NHNN")
    document.add_paragraph("THÔNG TƯ", style="Heading 1")
    document.add_paragraph("Quy định tỷ lệ an toàn vốn đối với ngân hàng thương mại")
    document.add_paragraph("Chương I. QUY ĐỊNH CHUNG", style="Heading 2")
    document.add_paragraph("Căn cứ Nghị định 88/2019/NĐ-CP quy định về xử phạt vi phạm hành chính.")
    document.add_paragraph("Chương II. TỶ LỆ AN TOÀN VỐN", style="Heading 2")
    for heading, paragraphs in TT41_ARTICLES:
        document.add_paragraph(heading, style="Heading 3")
        for paragraph in paragraphs:
            document.add_paragraph(paragraph)

    table = document.add_table(rows=3, cols=3)
    rows = [
        ["Loại tài sản", "Hệ số rủi ro", "Ghi chú"],
        ["Khoản phải đòi Chính phủ", "0%", "Đồng Việt Nam"],
        ["Khoản phải đòi doanh nghiệp", "100%", "Chưa xếp hạng"],
    ]
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            table.rows[row_index].cells[col_index].text = value

    return _save(document)


def _docx_amendment() -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph("Số: 22/2023/TT-NHNN")
    document.add_paragraph("THÔNG TƯ", style="Heading 1")
    document.add_paragraph(
        "Sửa đổi, bổ sung một số điều của Thông tư 41/2016/TT-NHNN quy định tỷ lệ "
        "an toàn vốn đối với ngân hàng thương mại."
    )
    document.add_paragraph("Điều 1. Sửa đổi, bổ sung Điều 12", style="Heading 2")
    document.add_paragraph(
        "1. Sửa đổi Khoản 2 Điều 12 của Thông tư 41/2016/TT-NHNN như sau: hệ số rủi ro "
        "đối với khoản phải đòi doanh nghiệp chưa xếp hạng là 150%."
    )
    document.add_paragraph("Điều 2. Hiệu lực thi hành", style="Heading 2")
    document.add_paragraph("1. Thông tư này có hiệu lực từ ngày 01 tháng 7 năm 2023.")
    document.add_paragraph("2. Bãi bỏ Quyết định 1627/2001/QĐ-NHNN.")
    return _save(document)


def _docx_bilingual_policy() -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph("QD-2023-114", style="Heading 1")
    document.add_paragraph("Chính sách quản lý vốn nội bộ / Internal capital management policy")
    document.add_paragraph("Phần I. PHẠM VI ÁP DỤNG", style="Heading 2")
    document.add_paragraph(
        "Chính sách này áp dụng cho toàn bộ các đơn vị của ngân hàng. This policy applies "
        "to all business units of the bank and to its subsidiaries."
    )
    document.add_paragraph("Điều 3. Đệm vốn nội bộ", style="Heading 3")
    document.add_paragraph(
        "1. Bộ phận Quản lý rủi ro tính toán tỷ lệ an toàn vốn hằng tháng. The internal "
        "buffer is set 150 basis points above the regulatory minimum defined in "
        "Thông tư 41/2016/TT-NHNN."
    )
    return _save(document)


def _docx_english_policy() -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph("Outsourcing Risk Policy", style="Heading 1")
    document.add_paragraph("Chapter I. Purpose", style="Heading 2")
    document.add_paragraph(
        "This policy establishes the requirements for assessing and monitoring outsourcing "
        "arrangements entered into by the bank."
    )
    document.add_paragraph("Article 4. Due diligence", style="Heading 3")
    document.add_paragraph(
        "1. The business owner shall complete a due diligence assessment before signing "
        "any outsourcing agreement."
    )
    document.add_paragraph("Section 2 Monitoring", style="Heading 2")
    document.add_paragraph(
        "The second line of defence shall review material arrangements at least annually."
    )
    return _save(document)


def _xlsx_fees() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Biểu phí cá nhân"
    for row in [
        ["Dịch vụ", "Phí (VND)", "Ghi chú"],
        ["Duy trì tài khoản thanh toán", "0", "Số dư bình quân từ 2.000.000"],
        ["Chuyển khoản trong hệ thống", "0", "Không giới hạn"],
        ["Chuyển khoản liên ngân hàng", "11.000", "Mỗi giao dịch"],
        ["Phát hành thẻ ghi nợ nội địa", "50.000", "Lần đầu"],
    ]:
        sheet.append(row)
    return _save_workbook(workbook)


def _xlsx_risk_limits() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    limits = workbook.active
    limits.title = "Hạn mức"
    for row in [
        ["Danh mục", "Hạn mức (tỷ VND)", "Cảnh báo"],
        ["Bất động sản", "12000", "80%"],
        ["Chứng khoán", "3000", "75%"],
    ]:
        limits.append(row)

    weights = workbook.create_sheet("Risk weights")
    for row in [
        ["Exposure class", "Risk weight", "Reference"],
        ["Sovereign VND", "0%", "41/2016/TT-NHNN"],
        ["Corporate unrated", "100%", "41/2016/TT-NHNN"],
    ]:
        weights.append(row)

    working = workbook.create_sheet("Tính toán")
    working.append(["tạm tính", "=1+1"])
    working.sheet_state = "hidden"
    return _save_workbook(workbook)


def _pptx_training() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Đào tạo: Nhận biết khách hàng (KYC)"
    slide.placeholders[
        1
    ].text = "Đối chiếu giấy tờ tùy thân với dữ liệu CCCD gắn chip\nLưu bản sao hồ sơ theo quy định"
    slide.notes_slide.notes_text_frame.text = (
        "Nhấn mạnh: quy trình QT-2024-007 yêu cầu đối chiếu trước khi mở tài khoản."
    )

    second = presentation.slides.add_slide(presentation.slide_layouts[5])
    second.shapes.title.text = "Escalation"
    box = second.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(2))
    box.text_frame.text = "Report suspicious transactions to Compliance within 24 hours."
    return _save_presentation(presentation)


def _pptx_committee() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "Tỷ lệ an toàn vốn quý IV"
    shape = slide.shapes.add_table(3, 3, Inches(1), Inches(2), Inches(8), Inches(2))
    values = [
        ["Chỉ tiêu", "Quý III", "Quý IV"],
        ["CAR", "11,2%", "11,8%"],
        ["Vốn tự có (tỷ VND)", "58.000", "61.400"],
    ]
    for row_index, row in enumerate(values):
        for col_index, value in enumerate(row):
            shape.table.cell(row_index, col_index).text = value
    return _save_presentation(presentation)


def _txt_notice() -> bytes:
    text = (
        "THÔNG BÁO\n\n"
        "Về việc điều chỉnh biểu phí dịch vụ khách hàng cá nhân\n\n"
        "Kể từ ngày 01/01/2026, ngân hàng áp dụng biểu phí mới đối với dịch vụ chuyển "
        "khoản liên ngân hàng.\n\n"
        "Chi tiết xem tại Quyết định 114/2023/QĐ-HĐQT.\n"
    )
    return text.encode("utf-8")


def _html_faq() -> bytes:
    html = """<!doctype html>
<html lang="vi"><head><title>Câu hỏi thường gặp</title>
<style>body{font-family:sans-serif}</style></head>
<body>
<h1>Câu hỏi thường gặp về tài khoản thanh toán</h1>
<p>Phí duy trì tài khoản được miễn nếu số dư bình quân đạt 2.000.000 VND.</p>
<h2>Biểu phí</h2>
<table>
  <tr><th>Dịch vụ</th><th>Phí</th></tr>
  <tr><td>Duy trì tài khoản</td><td>Miễn phí</td></tr>
  <tr><td>Chuyển khoản liên ngân hàng</td><td>11.000 VND</td></tr>
</table>
<script>console.log("not content");</script>
</body></html>
"""
    return html.encode("utf-8")


# ------------------------------------------------------------------------------- plumbing


def _save(document: object) -> bytes:
    buffer = BytesIO()
    document.save(buffer)  # type: ignore[attr-defined]
    return buffer.getvalue()


_save_workbook = _save
_save_presentation = _save


#: Fixtures generated on demand. Committed binaries are listed in COMMITTED_FIXTURES.
GENERATED_FIXTURES: dict[str, object] = {
    "tt41_capital.docx": _docx_tt41,
    "amendment.docx": _docx_amendment,
    "policy_bilingual.docx": _docx_bilingual_policy,
    "english_policy.docx": _docx_english_policy,
    "fees.xlsx": _xlsx_fees,
    "risk_limits.xlsx": _xlsx_risk_limits,
    "training.pptx": _pptx_training,
    "committee_pack.pptx": _pptx_committee,
    "notice.txt": _txt_notice,
    "faq.html": _html_faq,
}

#: Checked into the repository next to this file.
COMMITTED_FIXTURES: tuple[str, ...] = ("circular_native.pdf", "bilingual_report.pdf")

#: The twelve parser fixtures of the M1 acceptance criterion.
ALL_FIXTURES: tuple[str, ...] = tuple(GENERATED_FIXTURES) + COMMITTED_FIXTURES

#: Not one of the twelve: it has no text layer, and exists to prove the OCR route is taken
#: rather than a near-empty KBDoc being produced (M3 boundary).
OCR_ROUTING_FIXTURE = "scanned_notice.pdf"


def fixture_bytes(name: str) -> bytes:
    if name in GENERATED_FIXTURES:
        factory = GENERATED_FIXTURES[name]
        return factory()  # type: ignore[operator,no-any-return]
    path = FIXTURE_DIR / name
    if not path.is_file():
        raise FileNotFoundError(
            f"committed fixture {name} is missing — regenerate with "
            f"`python services/idp/src/kb_idp/testing/make_pdfs.py`"
        )
    return path.read_bytes()
