/**
 * Registry listing for stewards.
 *
 * Metadata only. Document text is never served here — it is served through retrieval-api,
 * which applies the mandatory server-side filter (INV-1). A title opens the inspection screen,
 * where the same filter decides what of the document's own chunks and edges come back.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, DocumentSummary, listDocuments } from "../api";

const STATUS_LABEL: Record<string, string> = {
  draft: "Bản nháp",
  published: "Đã ban hành",
  archived: "Lưu trữ",
  expired: "Hết hiệu lực",
};

export function Documents() {
  const [rows, setRows] = useState<DocumentSummary[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listDocuments()
      .then(setRows)
      .catch((err: ApiError) => setError(err.message));
  }, []);

  if (error) return <p className="error">{error}</p>;

  return (
    <section className="page">
      <h1>Sổ đăng ký tài liệu</h1>
      <table className="table">
        <thead>
          <tr>
            <th>Tiêu đề</th>
            <th>Số hiệu</th>
            <th>Danh mục</th>
            <th>Phạm vi</th>
            <th>Trạng thái</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td>
                <Link to={`/documents/${row.id}`}>{row.title}</Link>
              </td>
              <td>{row.legal_number ?? "—"}</td>
              <td>{row.category_path}</td>
              <td>{row.visibility}</td>
              <td>{STATUS_LABEL[row.status] ?? row.status}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={5}>Chưa có tài liệu nào.</td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
