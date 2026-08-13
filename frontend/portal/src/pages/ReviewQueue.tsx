/**
 * The reviewer's queue.
 *
 * Shows only the queues of the groups the signed-in user belongs to — enforced server-side in
 * portal-api, not by hiding rows here. The full review editor (page image ↔ blocks, bbox jump,
 * low-confidence highlights) lands in M3; M1 shows what is waiting and why.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, ReviewTask, listReviewTasks } from "../api";

const TASK_LABEL: Record<string, string> = {
  idp_review: "Duyệt bóc tách",
  identity_review: "Xác định trùng văn bản",
  merge_review: "Duyệt hợp nhất",
  impact_review: "Đánh giá tác động",
  pii_override: "Phê duyệt ngoại lệ dữ liệu cá nhân",
};

export function ReviewQueue() {
  const [tasks, setTasks] = useState<ReviewTask[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listReviewTasks()
      .then(setTasks)
      .catch((err: ApiError) => setError(err.message));
  }, []);

  if (error) return <p className="error">{error}</p>;

  return (
    <section className="page">
      <h1>Việc cần duyệt</h1>
      {tasks.length === 0 && <p className="hint">Không có việc nào đang chờ.</p>}
      <ul className="tasks">
        {tasks.map((task) => (
          <li key={task.id}>
            {/* A consolidation opens the three-pane merge screen, not the block editor. */}
            <Link
              to={
                task.task_type === "merge_review"
                  ? `/merge/${task.id}`
                  : `/review/${task.id}`
              }
            >
              <strong>{TASK_LABEL[task.task_type] ?? task.task_type}</strong>
            </Link>
            <span className="hint"> · {task.assignee_group ?? "chưa phân công"}</span>
            {typeof task.payload.detected_title === "string" && (
              <div>{task.payload.detected_title}</div>
            )}
            {task.task_type === "merge_review" && (
              <div className="hint">
                {typeof task.payload.sections_changed === "number" &&
                  `${task.payload.sections_changed} mục thay đổi`}
                {Array.isArray(task.payload.touched_articles) &&
                  (task.payload.touched_articles as number[]).length > 0 &&
                  ` · Điều: ${(task.payload.touched_articles as number[]).join(", ")}`}
                {task.payload.draft_complete === false && (
                  <span className="warn"> · dự thảo chưa hoàn chỉnh</span>
                )}
              </div>
            )}
            {task.payload.requires_ocr === true && (
              <div className="warn">Bản quét — cần OCR trước khi duyệt.</div>
            )}
            {task.payload.scanned === true && (
              <div className="hint">
                Bản quét đã nhận dạng
                {Array.isArray(task.payload.escalated_pages) &&
                  (task.payload.escalated_pages as number[]).length > 0 &&
                  ` · trang dùng VLM: ${(task.payload.escalated_pages as number[]).join(", ")}`}
                {typeof task.payload.low_confidence_blocks === "number" &&
                  ` · ${task.payload.low_confidence_blocks} khối cần kiểm tra`}
              </div>
            )}
            {Array.isArray(task.payload.unresolved_references) &&
              task.payload.unresolved_references.length > 0 && (
                <div className="warn">
                  Dẫn chiếu chưa có trong kho: {(task.payload.unresolved_references as string[]).join(", ")}
                </div>
              )}
          </li>
        ))}
      </ul>
    </section>
  );
}
