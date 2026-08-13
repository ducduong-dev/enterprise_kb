/**
 * The merge screen: `văn bản hợp nhất`, three panes.
 *
 * Left is what is canonical today, right is what the amendment says, middle is what would be
 * published — with the word-level diff, the model's bucket, and a visible mark on anything the
 * model did not actually decide. The panes are aligned per section rather than per document:
 * a reviewer who has to scroll two columns in sync stops comparing and starts skimming, and a
 * skimmed consolidation is a bank publishing law nobody read.
 *
 * Approval is deliberately dull: one button, a mandatory note for a rejection, and a running
 * count of how many more approvals are needed. Four-eyes is enforced server-side (INV-8); this
 * screen only reports what the server decided, including its refusals.
 */
import { useEffect, useMemo, useState } from "react";
import {
  ApiError,
  MergeScreen,
  MergeSection,
  getMergeScreen,
  submitMergeDecision,
} from "../api";

const BUCKET_LABEL: Record<string, string> = {
  unchanged_in_substance: "Không thay đổi nội dung",
  amended: "Sửa đổi nội dung",
  new_or_abrogated: "Bổ sung / bãi bỏ",
};

const KIND_LABEL: Record<string, string> = {
  amended: "Sửa đổi",
  added: "Bổ sung",
  removed: "Bãi bỏ",
  rewritten: "Viết lại",
};

/** Buckets in the order a reviewer should read them: substance first, wording last. */
const BUCKET_ORDER = ["new_or_abrogated", "amended", null, "unchanged_in_substance"];

function bucketRank(section: MergeSection): number {
  const index = BUCKET_ORDER.indexOf(section.bucket);
  return index === -1 ? BUCKET_ORDER.length : index;
}

function WordDiff({ section }: { section: MergeSection }) {
  return (
    <p className="worddiff">
      {section.spans.map((span, index) => (
        <span key={index} className={`span-${span.op}`}>
          {span.text}{" "}
        </span>
      ))}
    </p>
  );
}

function SectionRow({ section }: { section: MergeSection }) {
  return (
    <article className="merge-row">
      <header>
        <strong>{section.section_path}</strong>
        <span className="tag">{KIND_LABEL[section.kind] ?? section.kind}</span>
        {section.bucket && (
          <span className={`tag bucket-${section.bucket}`}>
            {BUCKET_LABEL[section.bucket] ?? section.bucket}
          </span>
        )}
        {section.inferred && (
          <span className="tag warn" title="Mô hình không phân loại được; đây là kết luận của so sánh văn bản.">
            Suy ra từ so sánh
          </span>
        )}
      </header>
      {section.impact && <p className="hint">{section.impact}</p>}

      <div className="merge-panes">
        <div className="pane">
          <h4>Bản hiện hành</h4>
          <p>{section.old_text || <em className="hint">Chưa có điều khoản này.</em>}</p>
        </div>
        <div className="pane">
          <h4>Bản sửa đổi</h4>
          <p>{section.new_text || <em className="hint">Bị bãi bỏ.</em>}</p>
        </div>
        <div className="pane pane-draft">
          <h4>
            Dự thảo hợp nhất
            {!section.drafted && <span className="tag warn">Chưa soạn</span>}
          </h4>
          <p>{section.consolidated_text}</p>
          {section.note && <p className="hint">{section.note}</p>}
        </div>
      </div>

      <details>
        <summary>Khác biệt theo từ</summary>
        <WordDiff section={section} />
      </details>
    </article>
  );
}

export function MergeEditor({ taskId, onDone }: { taskId: string; onDone: () => void }) {
  const [screen, setScreen] = useState<MergeScreen | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    getMergeScreen(taskId)
      .then(setScreen)
      .catch((err: ApiError) => setError(err.message));
  }, [taskId]);

  const sections = useMemo(() => {
    if (!screen) return [];
    return [...screen.section_classifications].sort(
      (a, b) => bucketRank(a) - bucketRank(b) || a.section_path.localeCompare(b.section_path),
    );
  }, [screen]);

  if (error) return <p className="error">{error}</p>;
  if (!screen) return <p className="page">Đang tải…</p>;

  const { approvals, llm_draft: draft } = screen;
  const remaining = Math.max(approvals.required - approvals.received, 0);

  async function decide(decision: "approve" | "reject") {
    if (!screen) return;
    if (decision === "reject" && note.trim().length === 0) {
      setMessage("Cần nêu lý do từ chối để người soạn biết phải sửa gì.");
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const result = await submitMergeDecision(taskId, {
        decision,
        note,
        // Sent back so an approval given for text that has since been redrafted is refused.
        draft_ref: draft.draft_ref ?? "",
      });
      setMessage(
        result.signalled
          ? result.detail
          : `${result.detail} — chưa gửi được tới quy trình, quyết định đã được ghi nhận.`,
      );
      if (result.satisfied || decision === "reject") {
        onDone();
      } else {
        setScreen(await getMergeScreen(taskId));
      }
    } catch (err) {
      setMessage((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page merge">
      <h1>{screen.document.title}</h1>
      <p className="hint">
        {screen.document.legal_number ?? "chưa có số hiệu"} · {screen.document.doc_class}
        {screen.amending_document && (
          <>
            {" "}· hợp nhất theo{" "}
            <strong>
              {screen.amending_document.legal_number ?? screen.amending_document.title}
            </strong>
          </>
        )}
      </p>

      <div className="merge-summary">
        <span>{sections.length} mục thay đổi</span>
        {screen.touched_articles.length > 0 && (
          <span>Điều: {screen.touched_articles.join(", ")}</span>
        )}
        <span>{draft.substantive_changes} thay đổi về nội dung</span>
        {!draft.available && (
          <span className="warn">
            Chưa có dự thảo của mô hình — so sánh văn bản vẫn đầy đủ, phần soạn thảo do người
            duyệt thực hiện.
          </span>
        )}
        {draft.available && !draft.complete && (
          <span className="warn">Dự thảo chưa hoàn chỉnh — cần đọc kỹ từng mục.</span>
        )}
      </div>

      {sections.length === 0 && <p className="hint">Không có khác biệt nào giữa hai bản.</p>}
      {sections.map((section) => (
        <SectionRow key={section.section_path} section={section} />
      ))}

      <div className="merge-approval">
        <h2>Phê duyệt</h2>
        <p className="hint">
          Đã có {approvals.received}/{approvals.required} phê duyệt
          {approvals.approvers.length > 0 && `: ${approvals.approvers.join(", ")}`}
          {remaining > 0 && ` · còn thiếu ${remaining}`}
        </p>
        {approvals.prepared_by && (
          <p className="hint">
            Người soạn: {approvals.prepared_by} — người soạn không được tự phê duyệt.
          </p>
        )}
        {approvals.rejected_by && (
          <p className="warn">
            Đã bị từ chối bởi {approvals.rejected_by}: {approvals.rejection_reason}
          </p>
        )}
        <textarea
          value={note}
          placeholder="Ý kiến phê duyệt hoặc lý do từ chối"
          onChange={(event) => setNote(event.target.value)}
        />
        <div className="actions">
          <button disabled={busy || approvals.decided} onClick={() => void decide("approve")}>
            Phê duyệt hợp nhất
          </button>
          <button
            className="secondary"
            disabled={busy || approvals.decided}
            onClick={() => void decide("reject")}
          >
            Từ chối
          </button>
        </div>
        {message && <p className="hint">{message}</p>}
      </div>
    </section>
  );
}
