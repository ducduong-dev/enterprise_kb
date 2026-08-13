/**
 * Upload screen (M1).
 *
 * Classification is collected up front because it decides the document's ACL: the category
 * supplies the defaults, and anything the uploader sets here narrows them. The screen then
 * follows the ingest workflow until it lands on a review queue, so the uploader sees where
 * the document went instead of watching a spinner and hoping.
 */
import { FormEvent, useEffect, useRef, useState } from "react";
import {
  ApiError,
  Category,
  UploadStatus,
  getUploadStatus,
  listCategories,
  uploadDocument,
} from "../api";

const DOC_CLASSES = [
  { value: "regulatory", label: "Văn bản pháp quy (regulatory)" },
  { value: "internal_normative", label: "Văn bản nội bộ (internal normative)" },
  { value: "operational", label: "Vận hành (operational)" },
  { value: "customer_facing", label: "Khách hàng (customer facing)" },
];

const VISIBILITIES = [
  { value: "", label: "Theo mặc định của danh mục" },
  { value: "external", label: "Công khai (external)" },
  { value: "internal_all", label: "Nội bộ toàn hàng (internal_all)" },
  { value: "restricted", label: "Hạn chế (restricted)" },
];

const TERMINAL_STATUSES = ["awaiting_review", "awaiting_ocr", "needs_identity_review", "duplicate"];

const STATUS_TEXT: Record<string, string> = {
  parsing: "Đang bóc tách nội dung…",
  registering: "Đang ghi vào sổ đăng ký…",
  awaiting_review: "Đã bóc tách xong — đang chờ người duyệt.",
  awaiting_ocr: "Tài liệu là bản quét, cần chạy OCR trước khi duyệt.",
  needs_identity_review: "Trùng số hiệu với văn bản đã có — cần quyết định hợp nhất.",
  duplicate: "Nội dung này đã tồn tại, không tạo phiên bản mới.",
};

export function Upload() {
  const [categories, setCategories] = useState<Category[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [docClass, setDocClass] = useState("operational");
  const [categoryPath, setCategoryPath] = useState("");
  const [legalNumber, setLegalNumber] = useState("");
  const [department, setDepartment] = useState("");
  const [visibility, setVisibility] = useState("");
  const [allowedGroups, setAllowedGroups] = useState("");

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<UploadStatus | null>(null);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    listCategories()
      .then((rows) => {
        setCategories(rows);
        if (rows.length > 0) setCategoryPath((current) => current || rows[0].path);
      })
      .catch((err: ApiError) => setError(err.message));
  }, []);

  useEffect(() => () => stopPolling(), []);

  function stopPolling() {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  function followWorkflow(workflowId: string) {
    stopPolling();
    pollRef.current = window.setInterval(async () => {
      try {
        const next = await getUploadStatus(workflowId);
        setStatus(next);
        if (TERMINAL_STATUSES.includes(next.status)) stopPolling();
      } catch {
        // A transient failure while the workflow starts is expected; keep polling.
      }
    }, 1500);
  }

  const restricted = visibility === "restricted";

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!file) {
      setError("Chọn tệp cần tải lên.");
      return;
    }
    if (restricted && allowedGroups.trim() === "") {
      // The server enforces this too; catching it here saves a round trip.
      setError("Tài liệu hạn chế phải có ít nhất một nhóm được phép truy cập.");
      return;
    }

    setBusy(true);
    setError(null);
    setStatus(null);
    try {
      const accepted = await uploadDocument({
        file,
        title,
        docClass,
        categoryPath,
        legalNumber,
        department,
        visibility,
        allowedGroups,
      });
      setStatus({ workflow_id: accepted.workflow_id, status: "parsing", outcome: null });
      followWorkflow(accepted.workflow_id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Tải lên thất bại.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page">
      <h1>Tải tài liệu lên</h1>
      <p className="hint">
        Tài liệu sẽ được bóc tách, ghi vào sổ đăng ký và chuyển tới hàng đợi duyệt. Tài liệu
        chưa được duyệt sẽ không xuất hiện trong tìm kiếm hay trợ lý hỏi đáp.
      </p>

      <form onSubmit={submit} className="form">
        <label>
          Tệp
          <input
            type="file"
            accept=".pdf,.docx,.xlsx,.pptx,.txt,.html,.htm,.md"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            required
          />
        </label>

        <label>
          Tiêu đề <span className="optional">(để trống sẽ lấy từ nội dung tài liệu)</span>
          <input value={title} onChange={(event) => setTitle(event.target.value)} />
        </label>

        <label>
          Danh mục
          <select
            value={categoryPath}
            onChange={(event) => setCategoryPath(event.target.value)}
            required
          >
            {categories.map((category) => (
              <option key={category.path} value={category.path}>
                {category.path} — {category.label}
              </option>
            ))}
          </select>
        </label>

        <label>
          Loại văn bản
          <select value={docClass} onChange={(event) => setDocClass(event.target.value)}>
            {DOC_CLASSES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        <label>
          Số hiệu <span className="optional">(để trống sẽ nhận dạng tự động)</span>
          <input
            value={legalNumber}
            onChange={(event) => setLegalNumber(event.target.value)}
            placeholder="41/2016/TT-NHNN"
          />
        </label>

        <label>
          Đơn vị
          <input value={department} onChange={(event) => setDepartment(event.target.value)} />
        </label>

        <label>
          Phạm vi truy cập
          <select value={visibility} onChange={(event) => setVisibility(event.target.value)}>
            {VISIBILITIES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        {restricted && (
          <label>
            Nhóm được phép <span className="required">bắt buộc</span>
            <input
              value={allowedGroups}
              onChange={(event) => setAllowedGroups(event.target.value)}
              placeholder="dept/legal, dept/compliance"
              required
            />
          </label>
        )}

        <button type="submit" disabled={busy}>
          {busy ? "Đang tải lên…" : "Tải lên"}
        </button>
      </form>

      {error && <p className="error">{error}</p>}

      {status && (
        <div className="status" role="status">
          <h2>Trạng thái xử lý</h2>
          <p>{STATUS_TEXT[status.status] ?? status.status}</p>
          {status.outcome?.review_task_id && (
            <p className="hint">Mã việc cần duyệt: {status.outcome.review_task_id}</p>
          )}
          {status.outcome?.document_id && (
            <p className="hint">Mã tài liệu: {status.outcome.document_id}</p>
          )}
        </div>
      )}
    </section>
  );
}
