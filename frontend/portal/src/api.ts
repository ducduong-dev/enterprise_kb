/**
 * portal-api client.
 *
 * Every call carries the signed-in user's bearer token. The browser holds no ACL logic and
 * never talks to an index or the registry directly — portal-api and retrieval-api decide what
 * this user may see, server-side (INV-1, INV-2).
 */
import { getAccessToken } from "./auth";

const BASE = import.meta.env.VITE_API_BASE ?? "/api";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await getAccessToken();
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      ...(init.headers ?? {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      body.error ?? "error",
      body.message ?? response.statusText,
    );
  }
  return (await response.json()) as T;
}

export interface Category {
  path: string;
  label: string;
  default_visibility: string;
  steward_group: string | null;
}

export interface DocumentSummary {
  id: string;
  title: string;
  legal_number: string | null;
  doc_class: string;
  category_path: string;
  visibility: string;
  status: string;
  updated_at: string;
}

export interface UploadAccepted {
  upload_id: string;
  workflow_id: string;
  document_hash: string;
  already_stored: boolean;
}

export interface UploadStatus {
  workflow_id: string;
  status: string;
  outcome: {
    status: string;
    document_id?: string;
    version_id?: string;
    review_task_id?: string;
    detail?: string;
  } | null;
}

export interface RetrievedChunk {
  chunk_id: string;
  version_id: string;
  document_id: string;
  citation_label: string | null;
  section_path: string | null;
  text: string;
  score: number;
  highlights: string[];
  supersession_flag: boolean;
}

export interface GraphExpansion {
  document_id: string;
  ref_type: string;
  summary: string | null;
  citation_label: string | null;
}

export interface RetrieveResponse {
  chunks: RetrievedChunk[];
  expansions: GraphExpansion[];
  /** Identifies the ACL filter the server applied; joins this page to its audit record. */
  resolved_filter_id: string;
}

export interface SearchParams {
  query: string;
  top_k?: number;
  /** Facets narrow only. There is deliberately no visibility or group parameter (INV-2). */
  category?: string;
  department?: string;
  doc_class?: string;
  expand_graph?: boolean;
}

export interface KBDocBlock {
  id: string;
  type: string;
  section_path: string[];
  text: string;
  table: { rows: string[][]; header_rows: number } | null;
  page: number;
  bbox: [number, number, number, number] | null;
  confidence: number;
  engine: string;
}

export interface ReviewTaskDetail {
  task: {
    id: string;
    task_type: string;
    state: string;
    assignee_group: string | null;
    payload: Record<string, unknown>;
  };
  document: {
    id: string;
    title: string;
    legal_number: string | null;
    doc_class: string;
    category_path: string;
    department: string | null;
    visibility: string;
    allowed_groups: string[];
    status: string;
  };
  version: {
    id: string;
    author: string | null;
    pii_status: string;
    source_type: string;
    /** What the parser read from the document's own text; null when it did not say plainly. */
    effective_from: string | null;
    /** The sentence that date was read from, so the reviewer can check it in one glance. */
    effective_evidence: string | null;
  };
  kbdoc: { blocks: KBDocBlock[] } | null;
  page_refs: string[];
  page_scores: number[];
  page_sizes: { width: number; height: number }[];
  escalated_pages: number[];
}

export interface ReviewSubmission {
  decision: "approve" | "reject";
  corrections: { block_id: string; text?: string; drop?: boolean }[];
  classification?: {
    title?: string;
    category_path?: string;
    visibility?: string;
    allowed_groups?: string[];
    department?: string | null;
    /** ISO date, or "" to clear a detected date the reviewer disagrees with. */
    effective_from?: string;
  };
  confirmed_refs?: [string, string][];
  note?: string;
}

export interface ReviewTask {
  id: string;
  version_id: string;
  task_type: string;
  state: string;
  assignee_group: string | null;
  payload: Record<string, unknown>;
}

export interface ChatCitation {
  marker: number;
  label: string;
  document_id: string;
  version_id: string;
  chunk_id: string;
  section_path: string | null;
  quote: string | null;
  supersession_flag: boolean;
}

export interface ChatAnswer {
  answer: string;
  citations: ChatCitation[];
  answer_id: string;
  refused: boolean;
  refusal_reason: string | null;
  /** Joins this answer to its audit record (INV-11); shown so a user can quote it in a ticket. */
  resolved_filter_id: string | null;
  warnings: string[];
  redacted: boolean;
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface MergeSection {
  section_path: string;
  article: number | null;
  kind: "amended" | "added" | "removed" | "rewritten" | "unchanged";
  similarity: number;
  old_text: string;
  new_text: string;
  spans: { op: "equal" | "insert" | "delete"; text: string }[];
  bucket: "unchanged_in_substance" | "amended" | "new_or_abrogated" | null;
  impact: string;
  /** True when the model did not classify this section and the diff's verdict was used. */
  inferred: boolean;
  consolidated_text: string;
  drafted: boolean;
  note: string;
}

export interface MergeScreen {
  task: {
    id: string;
    task_type: string;
    state: string;
    assignee_group: string | null;
    consolidation: boolean;
    payload: Record<string, unknown>;
  };
  document: {
    id: string;
    title: string;
    legal_number: string | null;
    doc_class: string;
    category_path: string;
  };
  amending_document: { id: string; title: string; legal_number: string | null } | null;
  current_canonical: { version_id: string; author: string | null; blocks: KBDocBlock[] } | null;
  new_version: { version_id: string; author: string | null; blocks: KBDocBlock[] };
  llm_draft: {
    draft_ref: string | null;
    available: boolean;
    complete: boolean;
    model: string;
    prompt_version: string;
    substantive_changes: number;
  };
  section_classifications: MergeSection[];
  summary: Record<string, number>;
  touched_articles: number[];
  approvals: {
    required: number;
    received: number;
    approvers: string[];
    satisfied: boolean;
    rejected_by: string | null;
    rejection_reason: string;
    decided: boolean;
    prepared_by: string | null;
    doc_class: string | null;
  };
}

export interface MergeDecisionResult {
  task_id: string;
  decision: string;
  required: number;
  received: number;
  satisfied: boolean;
  /** False when the workflow could not be told; the decision is stored either way. */
  signalled: boolean;
  detail: string;
}

export interface UploadFields {
  file: File;
  title: string;
  docClass: string;
  categoryPath: string;
  legalNumber: string;
  department: string;
  visibility: string;
  allowedGroups: string;
}

export function listCategories(): Promise<Category[]> {
  return request<Category[]>("/v1/categories");
}

export function listDocuments(category?: string): Promise<DocumentSummary[]> {
  const query = category ? `?category=${encodeURIComponent(category)}` : "";
  return request<DocumentSummary[]>(`/v1/documents${query}`);
}

export function listReviewTasks(): Promise<ReviewTask[]> {
  return request<ReviewTask[]>("/v1/review-tasks");
}

export function search(params: SearchParams): Promise<RetrieveResponse> {
  return request<RetrieveResponse>("/v1/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
}

export function citationLookup(citation: string): Promise<RetrieveResponse> {
  return request<RetrieveResponse>("/v1/citation-lookup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ citation }),
  });
}

export function getReviewTask(taskId: string): Promise<ReviewTaskDetail> {
  return request<ReviewTaskDetail>(`/v1/review-tasks/${encodeURIComponent(taskId)}`);
}

/**
 * Page images are proxied by portal-api rather than linked from object storage: access
 * follows the review task, so a URL cannot outlive the reviewer's assignment.
 */
export function reviewPageUrl(taskId: string, page: number): string {
  return `${BASE}/v1/review-tasks/${encodeURIComponent(taskId)}/pages/${page}`;
}

export function submitReview(taskId: string, body: ReviewSubmission): Promise<unknown> {
  return request(`/v1/review-tasks/${encodeURIComponent(taskId)}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function chat(
  messages: ChatTurn[],
  options: { category?: string } = {},
): Promise<ChatAnswer> {
  return request<ChatAnswer>("/v1/chat/internal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      messages,
      ...(options.category ? { facets: { category: options.category } } : {}),
    }),
  });
}

export function getMergeScreen(taskId: string): Promise<MergeScreen> {
  return request<MergeScreen>(`/v1/merge-tasks/${encodeURIComponent(taskId)}`);
}

export function submitMergeDecision(
  taskId: string,
  body: { decision: "approve" | "reject"; note: string; draft_ref: string },
): Promise<MergeDecisionResult> {
  return request<MergeDecisionResult>(
    `/v1/merge-tasks/${encodeURIComponent(taskId)}/decision`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}

export function uploadDocument(fields: UploadFields): Promise<UploadAccepted> {
  const form = new FormData();
  form.append("file", fields.file);
  form.append("title", fields.title);
  form.append("doc_class", fields.docClass);
  form.append("category_path", fields.categoryPath);
  form.append("legal_number", fields.legalNumber);
  form.append("department", fields.department);
  form.append("visibility", fields.visibility);
  form.append("allowed_groups", fields.allowedGroups);
  // No Content-Type header: the browser must set the multipart boundary itself.
  return request<UploadAccepted>("/v1/uploads", { method: "POST", body: form });
}

export function getUploadStatus(workflowId: string): Promise<UploadStatus> {
  return request<UploadStatus>(`/v1/uploads/${encodeURIComponent(workflowId)}`);
}

// --------------------------------------------------------------- document inspection

export interface ChunkView {
  chunk_id: string;
  ordinal: number;
  section_path: string | null;
  citation_label: string | null;
  text: string;
  page: number;
  characters: number;
  tombstoned: boolean;
  embedded: boolean;
}

export interface EdgeView {
  direction: "incoming" | "outgoing";
  ref_type: string;
  document_id: string;
  title: string;
  legal_number: string | null;
  status: string;
  articles: number[];
  detected_by: string | null;
  confirmed_by: string | null;
  confirmed: boolean;
  readable: boolean;
}

export interface LineageEntry {
  document_id: string;
  title: string;
  legal_number: string | null;
  ref_type: string;
  effective_from: string | null;
  status: string;
  consolidated: boolean;
  confirmed: boolean;
}

export interface VersionView {
  version_id: string;
  author: string;
  source_type: string;
  pii_status: string;
  created_at: string;
  effective_from: string | null;
  canonical: boolean;
  change_summary: string | null;
}

/** One row of the expiry ledger. Nothing is ever updated, so this list only grows. */
export interface ExpiryView {
  row_id: string;
  /** When it stopped applying in the world. */
  effective_to: string;
  basis: string;
  state: "proposed" | "confirmed" | "revoked";
  anchors: string[];
  /** True when the row names clauses rather than the whole document. Recorded, not applied. */
  partial: boolean;
  evidence: string | null;
  source_document_id: string | null;
  source_title: string | null;
  detected_by: string;
  decided_by: string | null;
  /** When this platform started believing it — the second clock (ADR-0030). */
  created_at: string;
  /** …and stopped. Null means this row is the current belief. */
  closed_at: string | null;
  open: boolean;
}

/** One thing this document says it does to another instrument (ADR-0039). */
export interface DeclarationView {
  declaration_id: string;
  kind: "abrogates" | "replaces" | "amends";
  state: "waiting" | "open" | "applied" | "rejected";
  target_legal_number: string;
  target_document_id: string | null;
  /** Null when the caller may not read the target: its title is somebody else's content. */
  target_title: string | null;
  target_readable: boolean;
  target_anchors: string[];
  replacement_anchors: string[];
  effective_from: string | null;
  /** The sentence it was read from — the whole reason confirming one is a glance. */
  evidence: string;
  confidence: number | null;
  decided_by: string | null;
  /** The target is in the registry and nobody has decided yet. */
  actionable: boolean;
  whole_instrument: boolean;
}

export interface DeclarationDecisionResult {
  applied: {
    declaration_id: string;
    state: string;
    ended: string[];
    pointers: number;
    chunks_projected: number;
    note: string;
  }[];
  /** Rows the ledger's guards turned down. Reported rather than failing the batch. */
  refused: { declaration_id: string; reason: string }[];
}

export interface ExpiryPanel {
  /** The date retrieval actually evaluates. Null means the document is in force. */
  in_force: string | null;
  in_force_source: "ledger" | "version" | null;
  version_effective_to: string | null;
  current: ExpiryView[];
  history: ExpiryView[];
}

export interface ExpiryDecisionResult {
  row_id: string;
  document_id: string;
  effective_to: string;
  state: string;
  chunks_projected: number;
  /** False when the row was recorded but nothing was projected — a partial expiry, today. */
  applied: boolean;
  note: string;
}

export interface PendingRef {
  legal_number: string;
  ref_type: string;
  detected_by: string;
}

export interface DocumentInspection {
  document: {
    id: string;
    title: string;
    legal_number: string | null;
    doc_class: string;
    category_path: string;
    department: string | null;
    visibility: string;
    status: string;
    canonical_version_id: string | null;
    review_by: string | null;
  };
  versions: VersionView[];
  chunks: ChunkView[];
  edges: EdgeView[];
  lineage: LineageEntry[];
  expiry: ExpiryPanel;
  declarations: DeclarationView[];
  pending: PendingRef[];
  unreadable_edges: number;
  warnings: string[];
  map_node_limit: number;
}

export interface RechunkResult {
  document_id: string;
  version_id: string;
  chunks_before: number;
  chunks_written: number;
  edges_rebuilt: number;
}

export function inspectDocument(documentId: string): Promise<DocumentInspection> {
  return request<DocumentInspection>(
    `/v1/documents/${encodeURIComponent(documentId)}/inspect`,
  );
}

export function listDocumentChunks(
  documentId: string,
  options: { includeTombstoned?: boolean } = {},
): Promise<ChunkView[]> {
  const query = options.includeTombstoned ? "?include_tombstoned=true" : "";
  return request<ChunkView[]>(
    `/v1/documents/${encodeURIComponent(documentId)}/chunks${query}`,
  );
}

/** Rebuild the canonical version's chunks. Not a publish: the text is unchanged (ADR-0026). */
export function rechunkDocument(documentId: string): Promise<RechunkResult> {
  return request<RechunkResult>(
    `/v1/documents/${encodeURIComponent(documentId)}/rechunk`,
    { method: "POST" },
  );
}

/** Propose that a document has stopped applying. Changes nothing until confirmed. */
export function proposeExpiry(
  documentId: string,
  body: { effective_to: string; evidence: string; anchors?: string[] },
): Promise<ExpiryDecisionResult> {
  return request<ExpiryDecisionResult>(
    `/v1/documents/${encodeURIComponent(documentId)}/expiry`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}

/** Confirm a proposed expiry, or withdraw one. Confirming removes it from every default answer. */
export function decideExpiry(
  documentId: string,
  rowId: string,
  body: { confirm: boolean; reason?: string },
): Promise<ExpiryDecisionResult> {
  return request<ExpiryDecisionResult>(
    `/v1/documents/${encodeURIComponent(documentId)}/expiry/${encodeURIComponent(rowId)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}

/**
 * Confirm or reject what this document declares — several at once.
 *
 * Batch because that is the unit the corpus produces: one closing article declares a dozen
 * changes from one paragraph, and a steward satisfied about the paragraph is satisfied about
 * all of them (ADR-0039).
 */
export function decideDeclarations(
  documentId: string,
  body: { declaration_ids: string[]; confirm: boolean; reason?: string },
): Promise<DeclarationDecisionResult> {
  return request<DeclarationDecisionResult>(
    `/v1/documents/${encodeURIComponent(documentId)}/declarations`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}

export function decideEdge(
  documentId: string,
  body: { other_document_id: string; ref_type: string; confirm: boolean },
): Promise<{ status: string }> {
  return request<{ status: string }>(
    `/v1/documents/${encodeURIComponent(documentId)}/edges`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}
