/**
 * The clause-review screen: two clauses, one decision.
 *
 * The steward-facing end of M9d. A funnel run concluded that one clause replaced another and
 * proposed it; nothing it proposed reaches a reader until somebody here confirms it.
 *
 * The layout has one job — make the *comparison* easy — so the change leads and the machinery
 * follows. The quantity delta sits at the top as the change it is ("11.000 đồng → 15.000 đồng"),
 * the two texts sit side by side and whole, and the model's rationale sits under them. The
 * similarity score is in the footnote with the model name, deliberately: it is the one number
 * that invites a reviewer to defer to the machine rather than read two paragraphs, and it is on
 * the record for the eval either way.
 *
 * Confirming and rejecting are equally weighted buttons, and a rejection needs a reason. That
 * asymmetry with the merge screen is on purpose: there the reviewer is approving text the bank
 * wrote, here they are judging a machine's conclusion, and "why was this not a supersession" is
 * the only signal the detector's false-positive rate can ever be measured from.
 */
import { useEffect, useState } from "react";
import {
  ApiError,
  ClausePane,
  ClauseScreen,
  getClauseScreen,
  submitClauseDecision,
} from "../api";

const STATE_LABEL: Record<string, string> = {
  proposed: "Đề xuất — chưa áp dụng",
  confirmed: "Đã xác nhận",
  revoked: "Đã bác bỏ",
};

/** What the funnel concluded, for a reader who has not read ADR-0033. */
const VERDICT_LABEL: Record<string, string> = {
  superseded: "Điều khoản đã bị thay thế",
  same_rule_restated: "Cùng một quy định, chỉ khác cách diễn đạt",
  different_scope: "Hai quy định khác nhau về đối tượng áp dụng",
  conflicting_unresolved: "Mâu thuẫn, chưa xác định được chiều thay thế",
};

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const [year, month, day] = iso.split("-");
  return `${day}/${month}/${year}`;
}

function Pane({ pane, role }: { pane: ClausePane | null; role: "old" | "new" }) {
  const heading = role === "old" ? "Điều khoản bị thay thế" : "Điều khoản thay thế";
  if (!pane) {
    return (
      <div className="pane">
        <h4>{heading}</h4>
        {/* A pure abrogation: the clause ended and nothing took its place. Saying so is not
            the same as showing an empty pane. */}
        <p className="warn">Không có điều khoản thay thế — đề xuất này là bãi bỏ.</p>
      </div>
    );
  }
  return (
    <div className="pane">
      <h4>{heading}</h4>
      <p className="hint">
        {pane.document_title ?? "—"}
        {pane.legal_number && ` · ${pane.legal_number}`}
        {" · "}
        {pane.citation_label ?? pane.section_path}
        {pane.effective_from && ` · hiệu lực từ ${formatDate(pane.effective_from)}`}
      </p>
      {pane.missing ? (
        <p className="warn">
          Không tìm thấy nội dung của điều khoản này — có thể văn bản đã được chia lại khối sau
          khi đề xuất được tạo. Hãy mở tài liệu để đối chiếu trước khi quyết định.
        </p>
      ) : (
        <p>{pane.text}</p>
      )}
    </div>
  );
}

export function ClauseReview({ taskId, onDone }: { taskId: string; onDone: () => void }) {
  const [screen, setScreen] = useState<ClauseScreen | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    getClauseScreen(taskId)
      .then(setScreen)
      .catch((err: ApiError) => setError(err.message));
  }, [taskId]);

  if (error) return <p className="error page">{error}</p>;
  if (!screen) return <p className="page">Đang tải…</p>;

  async function decide(decision: "confirm" | "reject") {
    setBusy(true);
    setMessage(null);
    try {
      const result = await submitClauseDecision(taskId, { decision, note });
      setMessage(result.detail);
      onDone();
    } catch (err) {
      // Refusals from the ledger land here — four-eyes, a missing reason, a row somebody else
      // already decided. All are the server's word, shown verbatim rather than reinterpreted.
      setMessage((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  const scope = screen.scope_facets ?? {};
  const mismatched = Object.entries(scope.mismatched ?? {});
  const matched = Object.entries(scope.matched ?? {});

  return (
    <section className="page clause-review">
      <h1>Duyệt đề xuất thay thế điều khoản</h1>
      <p className="hint">
        <span className={`tag state-${screen.state}`}>
          {STATE_LABEL[screen.state] ?? screen.state}
        </span>
        {screen.verdict && (
          <span className="tag"> {VERDICT_LABEL[screen.verdict] ?? screen.verdict}</span>
        )}
        {" · áp dụng từ "}
        {formatDate(screen.supersedes_from)}
        {screen.assignee_group && ` · ${screen.assignee_group}`}
      </p>

      {/* The change, first and largest. This is what makes the queue workable. */}
      {screen.quantity_delta.length > 0 && (
        <div className="clause-delta">
          <h2>Thay đổi về số liệu</h2>
          <ul>
            {screen.quantity_delta.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="merge-panes">
        <Pane pane={screen.old} role="old" />
        <Pane pane={screen.new} role="new" />
      </div>

      <div className="clause-evidence">
        <h2>Căn cứ của đề xuất</h2>
        {screen.rationale && <p>{screen.rationale}</p>}
        {screen.settled_by_label && <p className="hint">{screen.settled_by_label}</p>}
        {mismatched.length > 0 && (
          <p className="warn">
            Đối tượng áp dụng khác nhau:{" "}
            {mismatched.map(([facet, values]) => `${facet} (${values.join(", ")})`).join("; ")}
          </p>
        )}
        {matched.length > 0 && (
          <p className="hint">
            Đối tượng áp dụng trùng nhau:{" "}
            {matched.map(([facet, values]) => `${facet} (${values.join(", ")})`).join("; ")}
          </p>
        )}
        {/* Provenance, in the footnote where it belongs. */}
        <p className="hint">
          Nguồn phát hiện: {screen.detected_by}
          {screen.model && ` · mô hình ${screen.model}`}
          {screen.prompt_version && ` · prompt v${screen.prompt_version}`}
          {typeof screen.score === "number" && ` · độ tin cậy ${screen.score.toFixed(2)}`}
        </p>
      </div>

      <div className="merge-approval">
        <h2>Quyết định</h2>
        {screen.decided ? (
          <p className="hint">Việc này đã được quyết định.</p>
        ) : (
          <p className="hint">
            Xác nhận sẽ gắn nhãn điều khoản cũ và trỏ tới điều khoản thay thế trong câu trả lời.
            Điều khoản cũ <strong>vẫn</strong> hiển thị trên trang tài liệu của nó và vẫn tra cứu
            được theo mốc thời gian — đây là suy luận của hệ thống, không phải văn bản tuyên bố
            hết hiệu lực.
          </p>
        )}
        <textarea
          value={note}
          placeholder="Lý do bác bỏ (bắt buộc khi bác bỏ), hoặc ghi chú khi xác nhận"
          onChange={(event) => setNote(event.target.value)}
        />
        <div className="actions">
          <button disabled={busy || screen.decided} onClick={() => void decide("confirm")}>
            Xác nhận thay thế
          </button>
          <button
            className="secondary"
            disabled={busy || screen.decided}
            onClick={() => void decide("reject")}
          >
            Bác bỏ đề xuất
          </button>
        </div>
        {message && <p className="hint">{message}</p>}
      </div>
    </section>
  );
}
