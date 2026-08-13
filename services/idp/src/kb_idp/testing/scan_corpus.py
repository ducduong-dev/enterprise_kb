"""Ground truth for the twenty scanned fixtures (M3 acceptance criterion).

These are what the bank's archive actually holds: circulars with Chương/Điều/Khoản structure,
internal decisions, fee tables, branch notices, forms, and a few pages degraded past the point
where OCR alone can be trusted. The text here is the *truth* — every assertion about the
scanned pipeline compares its output against these strings, byte for byte where diacritics are
concerned.

Each spec also declares how the page was damaged on its way into the archive (`skew`, `noise`,
`blur`, `border`) and which pages are bad enough that recognition should escalate to the VLM.
Those two together are what make the fixture set a test of the *decision* the pipeline makes,
not only of its plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScanSpec:
    name: str
    #: One list of lines per page. An empty string is a paragraph break.
    pages: list[list[str]]
    #: Degradations applied when rendering, so the fixture looks like a photocopy.
    skew_degrees: float = 0.0
    noise: float = 0.0
    blur: int = 0
    border_px: int = 0
    #: Pages the simulated OCR should fail badly enough to trigger VLM escalation.
    escalate_pages: tuple[int, ...] = ()
    #: Simulated OCR quality for the pages that are not escalated.
    base_confidence: float = 0.96
    language: str = "vi"
    legal_number: str | None = None
    has_table: bool = False
    notes: str = ""

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def text(self, page: int) -> list[str]:
        return [line for line in self.pages[page - 1] if line.strip()]

    @property
    def full_text(self) -> str:
        return "\n".join(line for page in self.pages for line in page if line.strip())


def _circular(number: str, title: str, articles: list[tuple[str, list[str]]]) -> list[list[str]]:
    header = [
        "NGÂN HÀNG NHÀ NƯỚC VIỆT NAM",
        "CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM",
        f"Số: {number}",
        "Độc lập - Tự do - Hạnh phúc",
        "",
        "THÔNG TƯ",
        title,
        "",
    ]
    body: list[str] = []
    for heading, clauses in articles:
        body.append(heading)
        body.extend(clauses)
        body.append("")
    return [header + body]


SCANS: tuple[ScanSpec, ...] = (
    ScanSpec(
        name="tt36_gioi_han",
        legal_number="36/2014/TT-NHNN",
        pages=_circular(
            "36/2014/TT-NHNN",
            "Quy định các giới hạn, tỷ lệ bảo đảm an toàn trong hoạt động của tổ chức tín dụng",
            [
                (
                    "Chương I",
                    ["QUY ĐỊNH CHUNG"],
                ),
                (
                    "Điều 1. Phạm vi điều chỉnh",
                    [
                        "1. Thông tư này quy định về các giới hạn, tỷ lệ bảo đảm an toàn trong "
                        "hoạt động của tổ chức tín dụng.",
                        "2. Chi nhánh ngân hàng nước ngoài thực hiện theo quy định tại Điều 2.",
                    ],
                ),
                (
                    "Điều 2. Đối tượng áp dụng",
                    [
                        "1. Ngân hàng thương mại cổ phần, ngân hàng liên doanh.",
                        "2. Chi nhánh ngân hàng nước ngoài hoạt động tại Việt Nam.",
                    ],
                ),
            ],
        ),
        skew_degrees=1.4,
        noise=0.02,
    ),
    ScanSpec(
        name="tt41_an_toan_von",
        legal_number="41/2016/TT-NHNN",
        pages=_circular(
            "41/2016/TT-NHNN",
            "Quy định tỷ lệ an toàn vốn đối với ngân hàng, chi nhánh ngân hàng nước ngoài",
            [
                (
                    "Điều 6. Tỷ lệ an toàn vốn",
                    [
                        "1. Ngân hàng phải duy trì tỷ lệ an toàn vốn tối thiểu là 8%.",
                        "2. Tỷ lệ an toàn vốn được xác định theo công thức tại Phụ lục 1.",
                    ],
                ),
                (
                    "Điều 12. Tài sản có rủi ro tín dụng",
                    [
                        "1. Tài sản có rủi ro tín dụng được xác định theo phương pháp tiêu chuẩn.",
                        "2. Hệ số rủi ro đối với khoản phải đòi Chính phủ Việt Nam bằng 0%.",
                    ],
                ),
            ],
        ),
        skew_degrees=-0.9,
        noise=0.015,
    ),
    ScanSpec(
        name="nd88_xu_phat",
        legal_number="88/2019/NĐ-CP",
        pages=[
            [
                "CHÍNH PHỦ",
                "Số: 88/2019/NĐ-CP",
                "",
                "NGHỊ ĐỊNH",
                "Quy định về xử phạt vi phạm hành chính trong lĩnh vực tiền tệ và ngân hàng",
                "",
                "Điều 1. Phạm vi điều chỉnh",
                "Nghị định này quy định hành vi vi phạm hành chính, hình thức xử phạt, "
                "mức xử phạt trong lĩnh vực tiền tệ và ngân hàng.",
                "",
                "Điều 14. Vi phạm quy định về tỷ lệ bảo đảm an toàn",
                "1. Phạt tiền từ 80.000.000 đồng đến 120.000.000 đồng đối với hành vi không "
                "duy trì tỷ lệ an toàn vốn tối thiểu.",
                "2. Biện pháp khắc phục hậu quả: buộc thực hiện đúng tỷ lệ bảo đảm an toàn.",
            ]
        ],
        skew_degrees=2.1,
        noise=0.03,
        blur=3,
    ),
    ScanSpec(
        name="qd_hdqt_chinh_sach_von",
        legal_number="114/2023/QĐ-HĐQT",
        pages=[
            [
                "HỘI ĐỒNG QUẢN TRỊ",
                "Số: 114/2023/QĐ-HĐQT",
                "",
                "QUYẾT ĐỊNH",
                "Ban hành Chính sách quản lý vốn nội bộ",
                "",
                "Điều 1. Ban hành kèm theo Quyết định này Chính sách quản lý vốn nội bộ.",
                "Điều 2. Đệm vốn nội bộ",
                "1. Ngân hàng duy trì đệm vốn nội bộ cao hơn mức tối thiểu theo quy định tại "
                "Thông tư 41/2016/TT-NHNN là 150 điểm cơ bản.",
                "2. Khối Quản lý rủi ro báo cáo Ủy ban ALCO hằng tháng.",
                "Điều 3. Hiệu lực thi hành",
                "Quyết định này có hiệu lực kể từ ngày ký.",
            ]
        ],
        skew_degrees=0.6,
        noise=0.01,
    ),
    ScanSpec(
        name="qt_kyc_quy_trinh",
        legal_number="QT-2024-007",
        pages=[
            [
                "QUY TRÌNH NỘI BỘ",
                "Số: QT-2024-007",
                "",
                "QUY TRÌNH NHẬN BIẾT KHÁCH HÀNG",
                "",
                "Bước 1. Tiếp nhận hồ sơ",
                "Giao dịch viên tiếp nhận giấy tờ tùy thân của khách hàng cá nhân.",
                "Bước 2. Đối chiếu thông tin",
                "Đối chiếu thông tin trên căn cước công dân gắn chip với dữ liệu quốc gia.",
                "Bước 3. Phê duyệt và lưu hồ sơ",
                "Kiểm soát viên phê duyệt trước khi mở tài khoản thanh toán.",
            ]
        ],
        skew_degrees=-1.8,
        noise=0.04,
        border_px=12,
    ),
    ScanSpec(
        name="bieu_phi_ca_nhan",
        pages=[
            [
                "BIỂU PHÍ DỊCH VỤ KHÁCH HÀNG CÁ NHÂN",
                "Áp dụng từ ngày 01/01/2026",
                "",
                "Dịch vụ | Mức phí | Ghi chú",
                "Duy trì tài khoản thanh toán | Miễn phí | Số dư từ 2.000.000 VND",
                "Chuyển khoản trong hệ thống | Miễn phí | Không giới hạn",
                "Chuyển khoản liên ngân hàng | 11.000 VND | Mỗi giao dịch",
                "Phát hành thẻ ghi nợ nội địa | 50.000 VND | Lần đầu",
            ]
        ],
        has_table=True,
        skew_degrees=0.4,
        noise=0.02,
    ),
    ScanSpec(
        name="thong_bao_chi_nhanh",
        pages=[
            [
                "THÔNG BÁO",
                "Về việc điều chỉnh giờ giao dịch tại quầy",
                "",
                "Kính gửi: Quý khách hàng",
                "Từ ngày 01 tháng 3 năm 2026, chi nhánh điều chỉnh giờ giao dịch như sau:",
                "Buổi sáng từ 08 giờ 00 đến 11 giờ 30.",
                "Buổi chiều từ 13 giờ 30 đến 16 giờ 30.",
                "Trân trọng thông báo.",
            ]
        ],
        skew_degrees=3.2,
        noise=0.05,
        blur=3,
        border_px=20,
    ),
    ScanSpec(
        name="cong_van_ttgsnh",
        legal_number="1234/NHNN-TTGSNH",
        pages=[
            [
                "NGÂN HÀNG NHÀ NƯỚC VIỆT NAM",
                "Số: 1234/NHNN-TTGSNH",
                "V/v báo cáo tỷ lệ bảo đảm an toàn",
                "",
                "Kính gửi: Các tổ chức tín dụng",
                "Thực hiện quy định tại Thông tư 36/2014/TT-NHNN, Ngân hàng Nhà nước yêu cầu "
                "các tổ chức tín dụng báo cáo tỷ lệ bảo đảm an toàn định kỳ hằng quý.",
                "Báo cáo gửi về Cơ quan Thanh tra, giám sát ngân hàng trước ngày 15 của tháng "
                "đầu quý tiếp theo.",
            ]
        ],
        skew_degrees=-2.4,
        noise=0.035,
    ),
    ScanSpec(
        name="quy_che_tin_dung",
        legal_number="QC-2022-045",
        pages=[
            [
                "QUY CHẾ CHO VAY",
                "Số: QC-2022-045",
                "",
                "Chương II",
                "ĐIỀU KIỆN VAY VỐN",
                "Điều 5. Điều kiện chung",
                "1. Khách hàng có năng lực pháp luật dân sự đầy đủ.",
                "2. Mục đích sử dụng vốn vay hợp pháp.",
                "3. Có phương án sử dụng vốn khả thi.",
            ],
            [
                "Điều 6. Thời hạn cho vay",
                "1. Thời hạn cho vay ngắn hạn tối đa 12 tháng.",
                "2. Thời hạn cho vay trung hạn từ trên 12 tháng đến 60 tháng.",
                "Điều 7. Lãi suất",
                "Lãi suất cho vay do ngân hàng và khách hàng thỏa thuận theo quy định.",
            ],
        ],
        skew_degrees=1.1,
        noise=0.02,
    ),
    ScanSpec(
        name="huong_dan_quay",
        pages=[
            [
                "HƯỚNG DẪN VẬN HÀNH QUẦY GIAO DỊCH",
                "",
                "Mục 1. Đầu ngày",
                "Kiểm tra tiền mặt tại quầy và đối chiếu với hệ thống core banking.",
                "Mục 2. Trong ngày",
                "Thực hiện giao dịch theo hạn mức được phân quyền.",
                "Mục 3. Cuối ngày",
                "Kiểm quỹ, đối chiếu và niêm phong tiền mặt tồn quỹ.",
            ]
        ],
        skew_degrees=-0.5,
        noise=0.015,
    ),
    # --- pages bad enough that OCR alone cannot be trusted ------------------------------
    ScanSpec(
        name="tt02_co_cau_no_mo",
        legal_number="02/2023/TT-NHNN",
        pages=[
            [
                "NGÂN HÀNG NHÀ NƯỚC VIỆT NAM",
                "Số: 02/2023/TT-NHNN",
                "",
                "THÔNG TƯ",
                "Quy định về việc cơ cấu lại thời hạn trả nợ",
                "",
                "Điều 4. Cơ cấu lại thời hạn trả nợ",
                "1. Tổ chức tín dụng xem xét quyết định cơ cấu lại thời hạn trả nợ đối với "
                "số dư nợ gốc và lãi.",
                "2. Thời gian cơ cấu lại không vượt quá 12 tháng kể từ ngày đến hạn.",
            ]
        ],
        skew_degrees=4.5,
        noise=0.09,
        blur=5,
        escalate_pages=(1,),
        base_confidence=0.62,
        notes="heavy photocopy degradation",
    ),
    ScanSpec(
        name="quyet_dinh_dau_moc",
        legal_number="1627/2001/QĐ-NHNN",
        pages=[
            [
                "NGÂN HÀNG NHÀ NƯỚC VIỆT NAM",
                "Số: 1627/2001/QĐ-NHNN",
                "",
                "QUYẾT ĐỊNH",
                "Về việc ban hành Quy chế cho vay của tổ chức tín dụng đối với khách hàng",
                "",
                "Điều 1. Ban hành kèm theo Quyết định này Quy chế cho vay.",
                "Điều 2. Quyết định này có hiệu lực sau 15 ngày kể từ ngày ký.",
            ]
        ],
        skew_degrees=-3.8,
        noise=0.11,
        blur=5,
        border_px=25,
        escalate_pages=(1,),
        base_confidence=0.58,
        notes="stamped and faded",
    ),
    ScanSpec(
        name="bien_ban_hop_alco",
        pages=[
            [
                "BIÊN BẢN HỌP ỦY BAN ALCO",
                "Phiên họp quý IV năm 2025",
                "",
                "Nội dung: đánh giá tỷ lệ an toàn vốn và kế hoạch vốn năm 2026.",
                "Kết luận: duy trì đệm vốn nội bộ ở mức 150 điểm cơ bản.",
                "Giao Khối Quản lý rủi ro theo dõi và báo cáo hằng tháng.",
            ]
        ],
        skew_degrees=2.8,
        noise=0.10,
        blur=3,
        escalate_pages=(1,),
        base_confidence=0.64,
        notes="handwritten annotations",
    ),
    # --- ordinary documents, lighter damage -------------------------------------------
    ScanSpec(
        name="mau_don_mo_tai_khoan",
        pages=[
            [
                "GIẤY ĐỀ NGHỊ MỞ TÀI KHOẢN THANH TOÁN",
                "",
                "Họ và tên khách hàng: ................................",
                "Số căn cước công dân: ................................",
                "Ngày cấp: ............ Nơi cấp: ......................",
                "Địa chỉ thường trú: .................................",
                "Tôi cam kết các thông tin trên là đúng sự thật.",
            ]
        ],
        skew_degrees=0.8,
        noise=0.02,
    ),
    ScanSpec(
        name="chinh_sach_bao_mat",
        pages=[
            [
                "CHÍNH SÁCH BẢO MẬT THÔNG TIN KHÁCH HÀNG",
                "",
                "Điều 1. Nguyên tắc chung",
                "Ngân hàng bảo mật thông tin khách hàng theo quy định của pháp luật.",
                "Điều 2. Phạm vi cung cấp thông tin",
                "Chỉ cung cấp thông tin khách hàng theo yêu cầu của cơ quan nhà nước "
                "có thẩm quyền.",
            ]
        ],
        skew_degrees=-1.2,
        noise=0.025,
    ),
    ScanSpec(
        name="bao_cao_thanh_khoan",
        pages=[
            [
                "BÁO CÁO TỶ LỆ KHẢ NĂNG CHI TRẢ",
                "Kỳ báo cáo: quý IV năm 2025",
                "",
                "Chỉ tiêu | Quý III | Quý IV",
                "Tỷ lệ dự trữ thanh khoản | 12,4% | 13,1%",
                "Tỷ lệ khả năng chi trả 30 ngày | 52,0% | 55,6%",
                "Nhận xét: các tỷ lệ đều cao hơn mức tối thiểu theo quy định.",
            ]
        ],
        has_table=True,
        skew_degrees=1.6,
        noise=0.03,
    ),
    ScanSpec(
        name="thong_bao_lai_suat",
        pages=[
            [
                "THÔNG BÁO LÃI SUẤT HUY ĐỘNG",
                "Áp dụng từ ngày 15/02/2026",
                "",
                "Kỳ hạn | Lãi suất (%/năm)",
                "Không kỳ hạn | 0,20",
                "01 tháng | 3,10",
                "06 tháng | 4,60",
                "12 tháng | 5,40",
            ]
        ],
        has_table=True,
        skew_degrees=-0.7,
        noise=0.02,
    ),
    ScanSpec(
        name="quy_dinh_uy_quyen",
        pages=[
            [
                "QUY ĐỊNH VỀ ỦY QUYỀN PHÊ DUYỆT",
                "",
                "Điều 3. Hạn mức phê duyệt",
                "1. Giám đốc chi nhánh phê duyệt đến 5 tỷ đồng.",
                "2. Trên 5 tỷ đồng trình Hội sở chính.",
                "Điều 4. Trách nhiệm",
                "Người được ủy quyền chịu trách nhiệm về quyết định của mình.",
            ]
        ],
        skew_degrees=2.0,
        noise=0.03,
    ),
    ScanSpec(
        name="ke_hoach_kinh_doanh",
        pages=[
            [
                "KẾ HOẠCH KINH DOANH NĂM 2026",
                "",
                "Mục tiêu tăng trưởng tín dụng: 14%.",
                "Mục tiêu huy động vốn: 16%.",
                "Kiểm soát tỷ lệ nợ xấu dưới 1,5%.",
                "Duy trì tỷ lệ an toàn vốn trên 11%.",
            ],
            [
                "Giải pháp thực hiện",
                "1. Mở rộng mạng lưới khách hàng cá nhân.",
                "2. Tăng cường số hóa quy trình tín dụng.",
                "3. Kiểm soát chặt chẽ chất lượng tài sản.",
            ],
        ],
        skew_degrees=-1.5,
        noise=0.025,
    ),
    ScanSpec(
        name="huong_dan_song_ngu",
        language="mixed",
        pages=[
            [
                "HƯỚNG DẪN SỬ DỤNG DỊCH VỤ NGÂN HÀNG SỐ",
                "DIGITAL BANKING USER GUIDE",
                "",
                "Điều 1. Đăng ký dịch vụ",
                "Khách hàng đăng ký tại quầy hoặc trên ứng dụng di động.",
                "Article 1. Registration",
                "Customers may register at a branch or through the mobile application.",
                "Điều 2. Bảo mật",
                "Khách hàng không cung cấp mã OTP cho bất kỳ ai.",
                "Article 2. Security",
                "Customers must never share the OTP code with anyone.",
            ]
        ],
        skew_degrees=0.9,
        noise=0.02,
    ),
)

SCAN_NAMES: tuple[str, ...] = tuple(spec.name for spec in SCANS)
BY_NAME: dict[str, ScanSpec] = {spec.name: spec for spec in SCANS}

#: Fixtures whose pages are damaged badly enough to require the VLM.
ESCALATING: tuple[str, ...] = tuple(spec.name for spec in SCANS if spec.escalate_pages)


def spec(name: str) -> ScanSpec:
    return BY_NAME[name]


__all__ = ["BY_NAME", "ESCALATING", "SCANS", "SCAN_NAMES", "ScanSpec", "spec"]
