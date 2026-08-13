/**
 * Review editor (M3).
 *
 * The screen where a machine-read document becomes a human decision. Its shape follows what
 * the reviewer's eyes actually do: read a block, look at the same place on the page image,
 * fix it if it is wrong, move on.
 *
 *  - **The page image sits beside the text**, and selecting a block scrolls the image to that
 *    block's box and outlines it. Without that, checking a forty-block scan means hunting.
 *  - **Low-confidence blocks are highlighted and can be filtered to.** The reviewer's job is
 *    to find the lines OCR got wrong; making them findable is most of the value here.
 *  - **Escalated pages say so.** Text produced by the VLM has no coordinates, so the outline
 *    is absent and the reason is shown instead of leaving a reviewer wondering.
 *  - **Approve is not a button on its own.** Classification and reference confirmation are on
 *    the same screen, because they are part of the same decision.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  Category,
  ReviewTaskDetail,
  KBDocBlock,
  getReviewTask,
  reviewPageUrl,
  submitReview,
} from "../api";

const LOW_CONFIDENCE = 0.85;

interface Props {
  taskId: string;
  categories: Category[];
  onDone?: () => void;
}

export function ReviewEditor({ taskId, categories, onDone }: Props) {
  const [detail, setDetail] = useState<ReviewTaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [dropped, setDropped] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [onlyLowConfidence, setOnlyLowConfidence] = useState(false);
  const [confirmedRefs, setConfirmedRefs] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  const [categoryPath, setCategoryPath] = useState("");
  /** Effectivity, as the parser read it — the reviewer confirms or corrects before publish. */
  const [effectiveFrom, setEffectiveFrom] = useState("");
  const [visibility, setVisibility] = useState("");
  const [allowedGroups, setAllowedGroups] = useState("");
  const [department, setDepartment] = useState("");
  const [title, setTitle] = useState("");

  const imageRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    getReviewTask(taskId)
      .then((data) => {
        setDetail(data);
        setCategoryPath(data.document.category_path);
        setVisibility(data.document.visibility);
        setAllowedGroups(data.document.allowed_groups.join(", "));
        setDepartment(data.document.department ?? "");
        setTitle(data.document.title);
        setEffectiveFrom(data.version.effective_from ?? "");
      })
      .catch((err: ApiError) => setError(err.message));
  }, [taskId]);

  const blocks: KBDocBlock[] = detail?.kbdoc?.blocks ?? [];
  const selectedBlock = blocks.find((block) => block.id === selected) ?? null;

  const visibleBlocks = useMemo(
    () => (onlyLowConfidence ? blocks.filter((b) => b.confidence < LOW_CONFIDENCE) : blocks),
    [blocks, onlyLowConfidence],
  );
  const lowCount = blocks.filter((b) => b.confidence < LOW_CONFIDENCE).length;
  const page = selectedBlock?.page ?? 1;
  const escalated = detail?.escalated_pages.includes(page) ?? false;

  function textOf(block: KBDocBlock): string {
    return edits[block.id] ?? block.text;
  }

  async function submit(decision: "approve" | "reject") {
    if (!detail) return;
    setBusy(true);
    setError(null);
    try {
      const corrections = [
        ...Object.entries(edits)
          .filter(([id, text]) => !dropped.has(id) && text !== blocks.find((b) => b.id === id)?.text)
          .map(([block_id, text]) => ({ block_id, text })),
        ...[...dropped].map((block_id) => ({ block_id, drop: true })),
      ];
      await submitReview(taskId, {
        decision,
        corrections,
        classification: {
          title,
          category_path: categoryPath,
          visibility,
          allowed_groups: allowedGroups
            .split(",")
            .map((group) => group.trim())
            .filter(Boolean),
          department: department || null,
          // "" clears a detection the reviewer disagrees with; the server treats null as
          // "leave it alone" and only writes when the field is present.
          effective_from: effectiveFrom,
        },
        confirmed_refs: [...confirmedRefs].map((entry) => entry.split("|") as [string, string]),
        note,
      });
      onDone?.();
    } catch (err) {
      // A refusal here is usually an invariant doing its job: the PII gate is not clear, or
      // four-eyes forbids approving text you corrected yourself. Show it as-is.
      setError(err instanceof ApiError ? err.message : "Không gửi được quyết định.");
    } finally {
      setBusy(false);
    }
  }

  if (error && !detail) return <p className="error">{error}</p>;
  if (!detail) return <p className="page">Đang tải…</p>;

  return (
    <section className="page page--wide review">
      <header className="review__header">
        <h1>Duyệt tài liệu</h1>
        <p className="hint">
          {detail.document.legal_number ?? "chưa có số hiệu"} · {detail.document.doc_class} ·{" "}
          {blocks.length} khối · {lowCount} khối cần kiểm tra
          {detail.escalated_pages.length > 0 &&
            ` · trang phải dùng VLM: ${detail.escalated_pages.join(", ")}`}
        </p>
      </header>

      <div className="review__body">
        <div className="review__blocks">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={onlyLowConfidence}
              onChange={(event) => setOnlyLowConfidence(event.target.checked)}
            />
            Chỉ hiển thị khối có độ tin cậy thấp ({lowCount})
          </label>

          {visibleBlocks.map((block) => {
            const low = block.confidence < LOW_CONFIDENCE;
            const isDropped = dropped.has(block.id);
            return (
              <article
                key={block.id}
                className={[
                  "block",
                  low ? "block--low" : "",
                  selected === block.id ? "block--selected" : "",
                  isDropped ? "block--dropped" : "",
                ].join(" ")}
                onFocus={() => setSelected(block.id)}
                onClick={() => setSelected(block.id)}
              >
                <div className="block__meta">
                  trang {block.page} · {block.engine} · độ tin cậy{" "}
                  {block.confidence.toFixed(2)}
                  {block.section_path.length > 0 && ` · ${block.section_path.join(" > ")}`}
                </div>

                {block.table ? (
                  <table className="block__table">
                    <tbody>
                      {block.table.rows.map((row, rowIndex) => (
                        <tr key={rowIndex}>
                          {row.map((cell, cellIndex) => (
                            <td key={cellIndex}>{cell}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <textarea
                    className="block__text"
                    value={textOf(block)}
                    disabled={isDropped}
                    rows={Math.min(8, Math.ceil(textOf(block).length / 90) + 1)}
                    onChange={(event) =>
                      setEdits({ ...edits, [block.id]: event.target.value })
                    }
                  />
                )}

                <button
                  type="button"
                  className="link"
                  onClick={() => {
                    const next = new Set(dropped);
                    isDropped ? next.delete(block.id) : next.add(block.id);
                    setDropped(next);
                  }}
                >
                  {isDropped ? "Khôi phục khối" : "Xóa khối (nhiễu quét)"}
                </button>
              </article>
            );
          })}
        </div>

        <aside className="review__page" ref={imageRef}>
          <div className="review__page-header">
            Trang {page} / {detail.page_refs.length}
            {escalated && <span className="warn"> · đọc bằng VLM, không có tọa độ</span>}
          </div>
          <div className="review__page-frame">
            <img src={reviewPageUrl(taskId, page)} alt={`Trang ${page}`} />
            {selectedBlock?.bbox && (
              // The box the selected block came from, drawn over the page image.
              <span
                className="review__bbox"
                style={boxStyle(selectedBlock, detail.page_sizes?.[page - 1])}
              />
            )}
          </div>
        </aside>
      </div>

      <section className="review__decision">
        <h2>Phân loại</h2>
        <div className="form">
          <label>
            Tiêu đề
            <input value={title} onChange={(event) => setTitle(event.target.value)} />
          </label>
          <label>
            Danh mục
            <select
              value={categoryPath}
              onChange={(event) => setCategoryPath(event.target.value)}
            >
              {categories.map((category) => (
                <option key={category.path} value={category.path}>
                  {category.path} — {category.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Ngày có hiệu lực
            <input
              type="date"
              value={effectiveFrom}
              onChange={(event) => setEffectiveFrom(event.target.value)}
            />
            {detail.version.effective_evidence ? (
              <span className="hint">Máy đọc được: “{detail.version.effective_evidence}”</span>
            ) : (
              <span className="hint">
                Văn bản không nêu rõ ngày hiệu lực — để trống nghĩa là luôn có hiệu lực.
              </span>
            )}
          </label>
          <label>
            Phạm vi truy cập
            <select value={visibility} onChange={(event) => setVisibility(event.target.value)}>
              <option value="external">Công khai</option>
              <option value="internal_all">Nội bộ toàn hàng</option>
              <option value="restricted">Hạn chế</option>
            </select>
          </label>
          {visibility === "restricted" && (
            <label>
              Nhóm được phép
              <input
                value={allowedGroups}
                onChange={(event) => setAllowedGroups(event.target.value)}
                placeholder="dept/legal, dept/compliance"
              />
            </label>
          )}
          <label>
            Đơn vị
            <input value={department} onChange={(event) => setDepartment(event.target.value)} />
          </label>
        </div>

        {(detail.task.payload.detected_refs as DetectedRef[] | undefined)?.length ? (
          <>
            <h2>Dẫn chiếu được nhận dạng</h2>
            <p className="hint">
              Chỉ những dẫn chiếu được xác nhận mới dùng cho hợp nhất văn bản.
            </p>
            <ul className="refs">
              {(detail.task.payload.detected_refs as DetectedRef[]).map((ref) => {
                const key = `${ref.legal_number}|${ref.ref_type}`;
                return (
                  <li key={key}>
                    <label className="checkbox">
                      <input
                        type="checkbox"
                        checked={confirmedRefs.has(key)}
                        onChange={(event) => {
                          const next = new Set(confirmedRefs);
                          event.target.checked ? next.add(key) : next.delete(key);
                          setConfirmedRefs(next);
                        }}
                      />
                      {ref.legal_number} · {ref.ref_type}
                    </label>
                  </li>
                );
              })}
            </ul>
          </>
        ) : null}

        <label>
          Ghi chú
          <input value={note} onChange={(event) => setNote(event.target.value)} />
        </label>

        {error && <p className="error">{error}</p>}

        <div className="review__actions">
          <button type="button" disabled={busy} onClick={() => void submit("approve")}>
            Duyệt và ban hành
          </button>
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={() => void submit("reject")}
          >
            Từ chối
          </button>
        </div>
      </section>
    </section>
  );
}

interface DetectedRef {
  legal_number: string;
  ref_type: string;
}

function boxStyle(
  block: KBDocBlock,
  pageSize: { width: number; height: number } | undefined,
): React.CSSProperties {
  if (!block.bbox) return { display: "none" };
  const [x0, y0, x1, y1] = block.bbox;
  // Boxes are in the coordinates of the preprocessed page image, and the image is displayed
  // scaled to its container, so percentages are the only stable unit here.
  const width = pageSize?.width ?? 0;
  const height = pageSize?.height ?? 0;
  if (!width || !height) return { display: "none" };
  return {
    left: `${(x0 / width) * 100}%`,
    top: `${(y0 / height) * 100}%`,
    width: `${((x1 - x0) / width) * 100}%`,
    height: `${((y1 - y0) / height) * 100}%`,
  };
}
