/**
 * Internal chatbot (M6).
 *
 * The screen's whole job is to make an answer checkable. A fluent paragraph about capital
 * adequacy is worth nothing to a bank employee who cannot see where it came from — and worse
 * than nothing if they quote it. So:
 *
 *  1. **Citations are inline and clickable.** `[1]` in the text is the same `[1]` in the list
 *     below, which carries the passage the claim was drawn from and a link to the document.
 *  2. **A refusal looks like a refusal.** No spinner that ends in silence, no empty answer
 *     bubble — the bot says it has no basis, and that is a legitimate outcome, not an error.
 *  3. **Warnings are not decoration.** A cited article with an unconsolidated amendment is
 *     called out above the answer, where it is read before the number is copied.
 *  4. **The answer id is on screen.** It is what an auditor or a support ticket needs to pull
 *     the exact retrieval that produced this text (INV-11).
 *
 * The conversation is client-side state. Nothing is stored here, and every request carries the
 * whole (bounded) history, because the server condenses it into a query and must see it.
 */
import { FormEvent, Fragment, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, Category, ChatAnswer, ChatTurn, chat } from "../api";

interface Exchange {
  question: string;
  answer: ChatAnswer | null;
  error?: string;
}

/** Split an answer on its `[n]` markers so each one can be rendered as a link. */
function withCitations(text: string, onJump: (marker: number) => void) {
  const parts = text.split(/(\[\d{1,2}\])/g);
  return parts.map((part, index) => {
    const match = /^\[(\d{1,2})\]$/.exec(part);
    if (!match) return <Fragment key={index}>{part}</Fragment>;
    const marker = Number(match[1]);
    return (
      <button
        key={index}
        className="citation-marker"
        onClick={() => onJump(marker)}
        title="Xem đoạn trích được dẫn"
      >
        [{marker}]
      </button>
    );
  });
}

export function Chat({ categories }: { categories: Category[] }) {
  const [history, setHistory] = useState<Exchange[]>([]);
  const [question, setQuestion] = useState("");
  const [category, setCategory] = useState("");
  const [busy, setBusy] = useState(false);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [history, busy]);

  function turns(nextQuestion: string): ChatTurn[] {
    const messages: ChatTurn[] = [];
    for (const exchange of history) {
      messages.push({ role: "user", content: exchange.question });
      if (exchange.answer) messages.push({ role: "assistant", content: exchange.answer.answer });
    }
    messages.push({ role: "user", content: nextQuestion });
    return messages;
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const asked = question.trim();
    if (!asked || busy) return;

    const messages = turns(asked);
    setHistory((current) => [...current, { question: asked, answer: null }]);
    setQuestion("");
    setBusy(true);
    try {
      const answer = await chat(messages, { category: category || undefined });
      setHistory((current) =>
        current.map((item, index) => (index === current.length - 1 ? { ...item, answer } : item)),
      );
    } catch (err) {
      const message =
        err instanceof ApiError && err.status === 429
          ? "Bạn đang hỏi quá nhanh, vui lòng thử lại sau ít phút."
          : err instanceof ApiError
            ? err.message
            : "Không nhận được câu trả lời.";
      setHistory((current) =>
        current.map((item, index) =>
          index === current.length - 1 ? { ...item, error: message } : item,
        ),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page page--wide chat">
      <h1>Hỏi đáp nội bộ</h1>
      <p className="hint">
        Trợ lý chỉ trả lời dựa trên văn bản bạn được phép truy cập, và luôn dẫn nguồn. Nếu không
        có căn cứ, trợ lý sẽ nói là không có — đó là câu trả lời đúng, không phải lỗi.
      </p>

      <div className="chat__log">
        {history.map((exchange, index) => (
          <article key={index} className="chat__exchange">
            <p className="chat__question">{exchange.question}</p>

            {exchange.error && <p className="error">{exchange.error}</p>}
            {!exchange.answer && !exchange.error && <p className="hint">Đang tra cứu…</p>}

            {exchange.answer && (
              <div className={`chat__answer${exchange.answer.refused ? " chat__answer--refused" : ""}`}>
                {exchange.answer.warnings.map((warning) => (
                  <p key={warning} className="warn">
                    {warning}
                  </p>
                ))}
                {exchange.answer.redacted && (
                  <p className="warn">
                    Một số thông tin cá nhân đã được che trong câu trả lời.
                  </p>
                )}

                <p className="chat__text">
                  {withCitations(exchange.answer.answer, (marker) => {
                    const citation = exchange.answer?.citations.find((c) => c.marker === marker);
                    setHighlighted(citation ? `${index}-${citation.marker}` : null);
                  })}
                </p>

                {exchange.answer.citations.length > 0 && (
                  <ol className="chat__citations">
                    {exchange.answer.citations.map((citation) => (
                      <li
                        key={citation.chunk_id}
                        id={`${index}-${citation.marker}`}
                        className={
                          highlighted === `${index}-${citation.marker}` ? "is-highlighted" : ""
                        }
                      >
                        <Link to={`/documents?document=${citation.document_id}`}>
                          [{citation.marker}] {citation.label}
                        </Link>
                        {citation.supersession_flag && (
                          <span className="warn"> · đang có văn bản sửa đổi chưa hợp nhất</span>
                        )}
                        {citation.quote && <blockquote>{citation.quote}</blockquote>}
                      </li>
                    ))}
                  </ol>
                )}

                <p className="chat__meta">
                  Mã câu trả lời: <code>{exchange.answer.answer_id}</code>
                  {exchange.answer.resolved_filter_id && (
                    <>
                      {" "}· bộ lọc: <code>{exchange.answer.resolved_filter_id}</code>
                    </>
                  )}
                </p>
              </div>
            )}
          </article>
        ))}
        <div ref={bottom} />
      </div>

      <form className="chat__form" onSubmit={submit}>
        <input
          className="search-input"
          value={question}
          placeholder="Ví dụ: tỷ lệ an toàn vốn tối thiểu là bao nhiêu?"
          onChange={(event) => setQuestion(event.target.value)}
        />
        <button type="submit" disabled={busy}>
          Hỏi
        </button>
        <label className="chat__facet">
          Phạm vi
          <select value={category} onChange={(event) => setCategory(event.target.value)}>
            <option value="">Toàn bộ kho tài liệu</option>
            {categories.map((item) => (
              <option key={item.path} value={item.path}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
      </form>
    </section>
  );
}
