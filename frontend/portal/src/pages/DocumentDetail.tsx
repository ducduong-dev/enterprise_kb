/**
 * One document, as the machine indexed it (ADR-0026).
 *
 * Three things a steward cannot otherwise see: the chunks retrieval will quote, the edges the
 * pipeline detected, and where this document sits in an amendment chain. The graph is drawn
 * ego-centrically — this document in the middle, what points at it on the left, what it points
 * at on the right — because every question worth asking here is local and typed. There is no
 * whole-corpus picture on purpose.
 *
 * Chunks are shown and never edited: they are derived from an immutable version (INV-9) and
 * rewritten whole by the publish transaction (INV-5). The one write offered is *re-chunk*,
 * which runs the same text through chunking again; wrong text is fixed by a new version.
 */
import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  ChunkView,
  DocumentInspection,
  EdgeView,
  ExpiryPanel,
  ExpiryView,
  RechunkResult,
  decideEdge,
  decideExpiry,
  inspectDocument,
  listDocumentChunks,
  proposeExpiry,
  rechunkDocument,
} from "../api";

const STATUS_LABEL: Record<string, string> = {
  draft: "Bản nháp",
  published: "Đã ban hành",
  archived: "Lưu trữ",
  expired: "Hết hiệu lực",
};

const REF_LABEL: Record<string, string> = {
  amends: "Sửa đổi",
  abrogates: "Bãi bỏ",
  replaces: "Thay thế",
  implements: "Hướng dẫn thi hành",
  cites: "Trích dẫn",
  consolidates: "Hợp nhất",
  related: "Liên quan",
};

const EXPIRY_STATE_LABEL: Record<string, string> = {
  proposed: "Đề xuất",
  confirmed: "Đã xác nhận",
  revoked: "Đã thu hồi",
};

const EXPIRY_BASIS_LABEL: Record<string, string> = {
  self_stated: "Văn bản tự quy định",
  abrogated_by: "Bị văn bản khác bãi bỏ",
  declared_by: "Văn bản khác tuyên bố thay thế",
  steward: "Do cán bộ quản lý xác định",
};

const PII_LABEL: Record<string, string> = {
  pending: "Chờ quét",
  clear: "Sạch",
  flagged: "Có dấu hiệu PII",
  overridden: "Đã phê duyệt ngoại lệ",
};

function refLabel(refType: string): string {
  return REF_LABEL[refType] ?? refType;
}

/** Edges of one type in one direction — the unit the map and the lists both group by. */
interface EdgeGroup {
  refType: string;
  edges: EdgeView[];
}

function group(edges: EdgeView[], direction: "incoming" | "outgoing"): EdgeGroup[] {
  const byType = new Map<string, EdgeView[]>();
  for (const edge of edges.filter((item) => item.direction === direction)) {
    byType.set(edge.ref_type, [...(byType.get(edge.ref_type) ?? []), edge]);
  }
  return [...byType.entries()]
    .map(([refType, items]) => ({ refType, edges: items }))
    .sort((a, b) => a.refType.localeCompare(b.refType));
}

/**
 * The ego map: a fixed layout, computed, not simulated.
 *
 * Deterministic on purpose — the same document draws the same picture every time, so two
 * reviewers looking at the same screen see the same shape and can talk about it.
 */
function EgoMap({
  title,
  incoming,
  outgoing,
}: {
  title: string;
  incoming: EdgeGroup[];
  outgoing: EdgeGroup[];
}) {
  const rows = Math.max(incoming.length, outgoing.length, 1);
  const rowHeight = 46;
  const height = rows * rowHeight + 40;
  const middle = height / 2;
  const width = 640;

  const side = (groups: EdgeGroup[], x: number, anchor: "start" | "end") =>
    groups.map((g, index) => {
      const y = height / 2 - ((groups.length - 1) * rowHeight) / 2 + index * rowHeight;
      const confirmed = g.edges.filter((edge) => edge.confirmed).length;
      return (
        <g key={`${anchor}-${g.refType}`}>
          <path
            d={`M ${anchor === "end" ? x + 10 : x - 10} ${y} C ${width / 2} ${y}, ${
              width / 2
            } ${middle}, ${width / 2 + (anchor === "end" ? -70 : 70)} ${middle}`}
            className={confirmed === g.edges.length ? "egomap__link" : "egomap__link--unconfirmed"}
            fill="none"
          />
          <text x={x} y={y - 4} textAnchor={anchor} className="egomap__type">
            {refLabel(g.refType)}
          </text>
          <text x={x} y={y + 12} textAnchor={anchor} className="egomap__count">
            {g.edges.length} văn bản · {confirmed} đã xác nhận
          </text>
        </g>
      );
    });

  return (
    <svg
      className="egomap"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`Sơ đồ liên kết của ${title}`}
    >
      {side(incoming, 150, "end")}
      {side(outgoing, width - 150, "start")}
      <rect
        x={width / 2 - 70}
        y={middle - 22}
        width={140}
        height={44}
        rx={6}
        className="egomap__self"
      />
      <text x={width / 2} y={middle + 4} textAnchor="middle" className="egomap__self-label">
        Văn bản này
      </text>
      <text x={150} y={16} textAnchor="end" className="egomap__axis">
        Được trỏ tới bởi
      </text>
      <text x={width - 150} y={16} textAnchor="start" className="egomap__axis">
        Trỏ tới
      </text>
    </svg>
  );
}

/**
 * The expiry ledger for one document.
 *
 * Built around the two questions a steward actually has. *What is in force* — one date, and
 * where it came from, because the ledger and the version can disagree and the ledger wins.
 * *How did it get that way* — the whole sequence, since nothing is ever deleted and "why did
 * this vanish from search in May" is answered by reading down it (ADR-0030).
 *
 * A proposal is visibly inert: it says so, because the gap between proposing and confirming is
 * the entire safety property and a screen that blurs it would undo the design.
 */
function ExpirySection({
  panel,
  busy,
  onPropose,
  onDecide,
}: {
  panel: ExpiryPanel;
  busy: boolean;
  onPropose: (effectiveTo: string, evidence: string) => void;
  onDecide: (row: ExpiryView, confirm: boolean) => void;
}) {
  const [effectiveTo, setEffectiveTo] = useState("");
  const [evidence, setEvidence] = useState("");
  const proposing = panel.current.filter((row) => row.state === "proposed");

  return (
    <section className="inspect__block">
      <h2>Hiệu lực</h2>

      <p className="inspect__meta">
        {panel.in_force ? (
          <>
            Hết hiệu lực từ sau <strong>{panel.in_force}</strong>{" "}
            {panel.in_force_source === "ledger"
              ? "(theo sổ quyết định)"
              : "(theo phiên bản văn bản)"}
          </>
        ) : (
          "Đang còn hiệu lực."
        )}
        {panel.version_effective_to && panel.version_effective_to !== panel.in_force && (
          <>
            {" · "}Ngày ghi trên phiên bản: {panel.version_effective_to}
          </>
        )}
      </p>

      {proposing.length > 0 && (
        <p className="hint">
          Đề xuất đang chờ xác nhận — chưa ảnh hưởng đến kết quả tìm kiếm.
        </p>
      )}

      {panel.history.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Hết hiệu lực</th>
              <th>Trạng thái</th>
              <th>Căn cứ</th>
              <th>Phạm vi</th>
              <th>Ghi nhận lúc</th>
              <th>Người quyết định</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {panel.history.map((row) => (
              <tr key={row.row_id} className={row.open ? "" : "row--closed"}>
                <td>{row.effective_to}</td>
                <td>{EXPIRY_STATE_LABEL[row.state] ?? row.state}</td>
                <td>
                  {EXPIRY_BASIS_LABEL[row.basis] ?? row.basis}
                  {row.source_title && <> — {row.source_title}</>}
                  {row.evidence && <div className="hint">{row.evidence}</div>}
                </td>
                <td>
                  {row.partial ? (
                    <>
                      {row.anchors.join(", ")}
                      <div className="warn">chưa được áp dụng</div>
                    </>
                  ) : (
                    "Toàn văn bản"
                  )}
                </td>
                {/* The second clock: when this platform believed it, not when it was true. */}
                <td>{row.created_at.slice(0, 10)}</td>
                <td>{row.decided_by ?? row.detected_by}</td>
                <td>
                  {row.open && row.state === "proposed" && (
                    <button disabled={busy} onClick={() => onDecide(row, true)}>
                      Xác nhận
                    </button>
                  )}
                  {row.open && row.state === "confirmed" && (
                    <button disabled={busy} onClick={() => onDecide(row, false)}>
                      Thu hồi
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <form
        className="inspect__expiry-form"
        onSubmit={(event) => {
          event.preventDefault();
          onPropose(effectiveTo, evidence);
          setEffectiveTo("");
          setEvidence("");
        }}
      >
        <label>
          Ngày cuối còn hiệu lực
          <input
            type="date"
            value={effectiveTo}
            required
            onChange={(event) => setEffectiveTo(event.target.value)}
          />
        </label>
        <label>
          Căn cứ
          <input
            type="text"
            value={evidence}
            required
            minLength={10}
            placeholder="Câu trong văn bản, hoặc lý do của cán bộ quản lý"
            onChange={(event) => setEvidence(event.target.value)}
          />
        </label>
        <button type="submit" disabled={busy}>
          Đề xuất hết hiệu lực
        </button>
      </form>
      <p className="hint">
        Đề xuất không thay đổi kết quả tìm kiếm. Chỉ khi được xác nhận, văn bản mới ngừng xuất
        hiện — kể từ ngày đã ghi, không cần chờ tác vụ nền.
      </p>
    </section>
  );
}

function EdgeList({
  heading,
  groups,
  onDecide,
  busy,
}: {
  heading: string;
  groups: EdgeGroup[];
  onDecide: (edge: EdgeView, confirm: boolean) => void;
  busy: boolean;
}) {
  if (groups.length === 0) return <p className="hint">{heading}: không có.</p>;
  return (
    <div className="edges">
      <h3>{heading}</h3>
      {groups.map((g) => (
        <section key={g.refType} className="edges__group">
          <h4>{refLabel(g.refType)}</h4>
          <ul className="edges__list">
            {g.edges.map((edge) => (
              <li key={`${edge.document_id}-${edge.ref_type}`} className="edge">
                <div className="edge__title">
                  {/* An edge whose target this user may not read is counted, never named. */}
                  {edge.readable ? edge.title : <em>Văn bản ngoài phạm vi truy cập</em>}
                  {edge.legal_number && edge.readable && (
                    <span className="edge__number"> · {edge.legal_number}</span>
                  )}
                </div>
                <div className="edge__meta">
                  {STATUS_LABEL[edge.status] ?? edge.status}
                  {edge.articles.length > 0 && ` · Điều ${edge.articles.join(", ")}`}
                  {" · "}
                  {edge.confirmed ? (
                    <span className="edge__confirmed">Đã xác nhận bởi {edge.confirmed_by}</span>
                  ) : (
                    <span className="edge__detected">
                      Máy phát hiện ({edge.detected_by ?? "không rõ"}) — chưa xác nhận
                    </span>
                  )}
                </div>
                <div className="edge__actions">
                  {!edge.confirmed && (
                    <button
                      type="button"
                      className="link"
                      disabled={busy}
                      onClick={() => onDecide(edge, true)}
                    >
                      Xác nhận
                    </button>
                  )}
                  <button
                    type="button"
                    className="link link--danger"
                    disabled={busy}
                    onClick={() => onDecide(edge, false)}
                  >
                    Gỡ liên kết
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

export function DocumentDetail({ documentId }: { documentId: string }) {
  const [data, setData] = useState<DocumentInspection | null>(null);
  const [chunks, setChunks] = useState<ChunkView[] | null>(null);
  const [showTombstoned, setShowTombstoned] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    inspectDocument(documentId)
      .then((result) => {
        setData(result);
        setChunks(result.chunks);
        setError(null);
      })
      .catch((err: ApiError) => setError(err.message));
  }, [documentId]);

  useEffect(load, [load]);

  useEffect(() => {
    if (!showTombstoned) return;
    listDocumentChunks(documentId, { includeTombstoned: true })
      .then(setChunks)
      .catch((err: ApiError) => setError(err.message));
  }, [documentId, showTombstoned]);

  async function onRechunk() {
    if (
      !window.confirm(
        "Lập chỉ mục lại các đoạn của bản hiện hành? Nội dung văn bản không thay đổi.",
      )
    )
      return;
    setBusy(true);
    try {
      const result: RechunkResult = await rechunkDocument(documentId);
      setNotice(
        `Đã lập chỉ mục lại: ${result.chunks_before} → ${result.chunks_written} đoạn, ` +
          `${result.edges_rebuilt} liên kết phục vụ tìm kiếm.`,
      );
      setShowTombstoned(false);
      load();
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  async function onProposeExpiry(effectiveTo: string, evidence: string) {
    setBusy(true);
    try {
      await proposeExpiry(documentId, { effective_to: effectiveTo, evidence });
      setNotice("Đã ghi nhận đề xuất hết hiệu lực. Chưa ảnh hưởng đến kết quả tìm kiếm.");
      load();
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  async function onDecideExpiry(row: ExpiryView, confirm: boolean) {
    // Confirming removes the document from every default answer from its date. Worth one
    // deliberate click, and worth naming the date in the question rather than "are you sure".
    const question = confirm
      ? `Xác nhận văn bản hết hiệu lực sau ngày ${row.effective_to}? Văn bản sẽ không còn xuất hiện trong tìm kiếm và trả lời.`
      : "Thu hồi quyết định hết hiệu lực? Văn bản sẽ được phục vụ trở lại.";
    if (!window.confirm(question)) return;
    const reason = confirm ? "" : window.prompt("Lý do thu hồi:") ?? "";
    if (!confirm && !reason.trim()) return;
    setBusy(true);
    try {
      const result = await decideExpiry(documentId, row.row_id, { confirm, reason });
      setNotice(
        result.applied
          ? confirm
            ? `Đã xác nhận. ${result.chunks_projected} đoạn được cập nhật ngày hết hiệu lực.`
            : "Đã thu hồi. Văn bản được phục vụ trở lại."
          : `Đã ghi nhận nhưng chưa áp dụng: ${result.note}`,
      );
      load();
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  async function onDecide(edge: EdgeView, confirm: boolean) {
    if (!confirm && !window.confirm("Gỡ liên kết này khỏi đồ thị?")) return;
    setBusy(true);
    try {
      await decideEdge(documentId, {
        other_document_id: edge.document_id,
        ref_type: edge.ref_type,
        confirm,
      });
      setNotice(confirm ? "Đã xác nhận liên kết." : "Đã gỡ liên kết.");
      load();
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  if (error && !data) return <p className="error">{error}</p>;
  if (!data) return <p className="page">Đang tải…</p>;

  const incoming = group(data.edges, "incoming");
  const outgoing = group(data.edges, "outgoing");
  const drawable = data.edges.length > 0 && data.edges.length <= data.map_node_limit;
  const shown = chunks ?? data.chunks;

  return (
    <section className="page page--wide inspect">
      <header className="inspect__header">
        <h1>{data.document.title}</h1>
        <p className="inspect__meta">
          {data.document.legal_number ?? "Không có số hiệu"} · {data.document.category_path} ·{" "}
          {data.document.visibility} · {STATUS_LABEL[data.document.status] ?? data.document.status}
        </p>
      </header>

      {error && <p className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}

      {data.warnings.length > 0 && (
        <ul className="inspect__warnings">
          {data.warnings.map((warning) => (
            <li key={warning} className="warn">
              {warning}
            </li>
          ))}
        </ul>
      )}

      <section className="inspect__block">
        <h2>Các bản</h2>
        <table className="table">
          <thead>
            <tr>
              <th>Tạo lúc</th>
              <th>Người soạn</th>
              <th>Nguồn</th>
              <th>PII</th>
              <th>Hiệu lực từ</th>
              <th>Hiện hành</th>
            </tr>
          </thead>
          <tbody>
            {data.versions.map((version) => (
              <tr key={version.version_id}>
                <td>{version.created_at.slice(0, 10)}</td>
                <td>{version.author}</td>
                <td>{version.source_type}</td>
                <td>{PII_LABEL[version.pii_status] ?? version.pii_status}</td>
                <td>{version.effective_from ?? "—"}</td>
                <td>{version.canonical ? "✓" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <ExpirySection
        panel={data.expiry}
        busy={busy}
        onPropose={onProposeExpiry}
        onDecide={onDecideExpiry}
      />

      <section className="inspect__block">
        <h2>Liên kết</h2>
        {drawable && <EgoMap title={data.document.title} incoming={incoming} outgoing={outgoing} />}
        {data.edges.length > data.map_node_limit && (
          <p className="hint">
            Quá {data.map_node_limit} liên kết — sơ đồ không còn đọc được, xem danh sách bên dưới.
          </p>
        )}
        <div className="inspect__edges">
          <EdgeList
            heading="Được trỏ tới bởi"
            groups={incoming}
            onDecide={onDecide}
            busy={busy}
          />
          <EdgeList heading="Trỏ tới" groups={outgoing} onDecide={onDecide} busy={busy} />
        </div>
        {data.pending.length > 0 && (
          <div className="pending">
            <h3>Chưa có trong hệ thống</h3>
            <p className="hint">
              Văn bản này dẫn chiếu tới {data.pending.length} văn bản mà kho tri thức chưa có.
              Liên kết sẽ tự động được tạo khi văn bản đó được nạp — không cần thao tác lại.
            </p>
            <ul className="pending__list">
              {data.pending.map((item) => (
                <li key={`${item.legal_number}-${item.ref_type}`}>
                  <span className="pending__number">{item.legal_number}</span>
                  <span className="edge__meta">
                    {refLabel(item.ref_type)} · máy phát hiện ({item.detected_by})
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {data.unreadable_edges > 0 && (
          <p className="hint">
            {data.unreadable_edges} liên kết tới văn bản ngoài phạm vi truy cập của bạn.
          </p>
        )}
      </section>

      {data.lineage.length > 0 && (
        <section className="inspect__block">
          <h2>Lịch sử sửa đổi</h2>
          <ol className="lineage">
            {data.lineage.map((item) => (
              <li key={item.document_id} className="lineage__item">
                <span className="lineage__date">{item.effective_from ?? "chưa rõ hiệu lực"}</span>
                <span className="lineage__title">
                  {refLabel(item.ref_type)}: {item.title}
                  {item.legal_number && ` (${item.legal_number})`}
                </span>
                <span className={item.consolidated ? "lineage__ok" : "warn"}>
                  {item.consolidated ? "đã hợp nhất" : "chưa hợp nhất"}
                </span>
              </li>
            ))}
          </ol>
        </section>
      )}

      <section className="inspect__block">
        <div className="inspect__chunks-header">
          <h2>Đoạn đã lập chỉ mục ({shown.filter((chunk) => !chunk.tombstoned).length})</h2>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={showTombstoned}
              onChange={(event) => {
                setShowTombstoned(event.target.checked);
                if (!event.target.checked) setChunks(data.chunks);
              }}
            />
            Hiện cả đoạn đã thu hồi
          </label>
          <button type="button" disabled={busy} onClick={onRechunk}>
            Lập chỉ mục lại
          </button>
        </div>
        <p className="hint">
          Đoạn là dữ liệu dẫn xuất từ bản hiện hành và không sửa trực tiếp được. Sai nội dung thì
          sửa văn bản rồi ban hành bản mới; sai cách cắt đoạn thì lập chỉ mục lại.
        </p>
        <ol className="chunks">
          {shown.map((chunk) => (
            <li
              key={chunk.chunk_id}
              className={chunk.tombstoned ? "chunk chunk--tombstoned" : "chunk"}
            >
              <div className="chunk__meta">
                #{chunk.ordinal} · {chunk.citation_label ?? chunk.section_path ?? "không có mục"} ·
                trang {chunk.page} · {chunk.characters} ký tự
                {!chunk.embedded && <span className="warn"> · chưa có vector</span>}
                {chunk.tombstoned && <span className="hint"> · đã thu hồi</span>}
              </div>
              <p className="chunk__text">{chunk.text}</p>
            </li>
          ))}
          {shown.length === 0 && <li className="hint">Chưa có đoạn nào.</li>}
        </ol>
      </section>
    </section>
  );
}
