/**
 * Search (M2).
 *
 * Three things this screen must get right, because they are what makes a result usable in a
 * bank rather than merely relevant:
 *
 *  1. **Every result is citable.** The citation label is shown as the result's identity, not
 *     as a footnote — "Điều 12.2, TT 41/2016/TT-NHNN" is what a compliance officer quotes.
 *  2. **Superseded text is marked.** The canonical text of an amended regulation is still the
 *     canonical text; presenting it silently would be presenting something the bank already
 *     knows has been amended.
 *  3. **Facets narrow, never widen.** The controls here can only ever remove results. There
 *     is no visibility or group control, because the server would refuse one anyway (INV-2).
 */
import { FormEvent, useState } from "react";
import { ApiError, Category, RetrieveResponse, search } from "../api";

const DOC_CLASSES = [
  { value: "", label: "Mọi loại văn bản" },
  { value: "regulatory", label: "Văn bản pháp quy" },
  { value: "internal_normative", label: "Văn bản nội bộ" },
  { value: "operational", label: "Vận hành" },
  { value: "customer_facing", label: "Khách hàng" },
];

interface Props {
  categories: Category[];
}

export function Search({ categories }: Props) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [docClass, setDocClass] = useState("");
  const [expandGraph, setExpandGraph] = useState(true);
  const [results, setResults] = useState<RetrieveResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setResults(
        await search({
          query,
          top_k: 10,
          category: category || undefined,
          doc_class: docClass || undefined,
          expand_graph: expandGraph,
        }),
      );
    } catch (err) {
      // A 403 here means the funnel refused the request — usually a facet that would have
      // widened the filter. Say so plainly rather than showing an empty result list.
      setError(
        err instanceof ApiError && err.status === 403
          ? "Yêu cầu bị từ chối: bộ lọc không hợp lệ."
          : "Không thực hiện được tìm kiếm.",
      );
      setResults(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page page--wide">
      <h1>Tìm kiếm</h1>

      <form onSubmit={submit} className="search-form">
        <input
          className="search-input"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Tỷ lệ an toàn vốn tối thiểu là bao nhiêu?"
          aria-label="Nội dung tìm kiếm"
        />
        <button type="submit" disabled={busy}>
          {busy ? "Đang tìm…" : "Tìm"}
        </button>

        <div className="facets">
          <label>
            Danh mục
            <select value={category} onChange={(event) => setCategory(event.target.value)}>
              <option value="">Tất cả</option>
              {categories.map((item) => (
                <option key={item.path} value={item.path}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>

          <label>
            Loại văn bản
            <select value={docClass} onChange={(event) => setDocClass(event.target.value)}>
              {DOC_CLASSES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>

          <label className="checkbox">
            <input
              type="checkbox"
              checked={expandGraph}
              onChange={(event) => setExpandGraph(event.target.checked)}
            />
            Hiển thị văn bản liên quan
          </label>
        </div>
      </form>

      {error && <p className="error">{error}</p>}

      {results && results.chunks.length === 0 && !error && (
        <p className="hint">
          Không tìm thấy nội dung phù hợp trong phạm vi bạn được phép truy cập.
        </p>
      )}

      <ol className="results">
        {results?.chunks.map((chunk) => (
          <li key={chunk.chunk_id} className="result">
            <div className="result__citation">
              {chunk.citation_label ?? chunk.section_path ?? "Không có trích dẫn"}
            </div>
            {chunk.supersession_flag && (
              <p className="warn">
                Văn bản này đang bị sửa đổi bởi một văn bản khác chưa được hợp nhất — hãy
                kiểm tra trước khi trích dẫn.
              </p>
            )}
            {chunk.highlights.length > 0 ? (
              chunk.highlights.map((fragment, index) => (
                <p
                  key={index}
                  className="result__snippet"
                  // The server produced this markup: it is `ts_headline` output over the
                  // document's own text, with only <mark> tags. No user input reaches it.
                  dangerouslySetInnerHTML={{ __html: fragment }}
                />
              ))
            ) : (
              <p className="result__snippet">{chunk.text.slice(0, 400)}</p>
            )}
            <div className="result__meta">
              điểm {chunk.score.toFixed(3)}
              {chunk.section_path ? ` · ${chunk.section_path}` : ""}
            </div>
          </li>
        ))}
      </ol>

      {results && results.expansions.length > 0 && (
        <aside className="expansions">
          <h2>Văn bản liên quan</h2>
          <ul>
            {results.expansions.map((expansion) => (
              <li key={`${expansion.document_id}-${expansion.ref_type}`}>
                <strong>{expansion.citation_label ?? expansion.summary}</strong>
                <span className="hint"> · {expansion.ref_type}</span>
              </li>
            ))}
          </ul>
        </aside>
      )}

      {results && (
        <p className="hint filter-id">
          Bộ lọc đã áp dụng: <code>{results.resolved_filter_id}</code>
        </p>
      )}
    </section>
  );
}
