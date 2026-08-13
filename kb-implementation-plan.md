# Knowledge Base Platform — Software Development Implementation Plan

**Version:** 1.0 · **Derived from:** Architecture Design v1.1 (banking profile) · **Date:** August 2026

> **HOW TO USE THIS FILE (instructions for the AI assistant or engineer loading it):**
> This is the authoritative starting context for implementing the enterprise knowledge base platform for a Vietnamese bank. It contains locked decisions, the repository layout, service contracts, database schema, coding invariants, and a milestone-ordered task breakdown. When starting development: (1) read the Locked Decisions and Invariants sections first — they are non-negotiable constraints, not suggestions; (2) build in milestone order — each milestone has acceptance criteria that must pass before the next begins; (3) items marked `[OPEN]` are pending business decisions — implement behind interfaces so either outcome is a configuration/adapter change; (4) when a detail is unspecified, prefer the simplest option consistent with the invariants and record the choice in `docs/decisions/` as an ADR.

---

## 1. Locked decisions

| Area | Decision |
|---|---|
| Deployment | On-premises. Docker Compose for dev and first production; Kubernetes migration deferred. |
| Core database | PostgreSQL 16 + pgvector (HNSW). Registry, ACLs, reference graph edges, chunks+embeddings in one DB. |
| Object storage | MinIO, S3 API, content-hash-addressed keys. |
| Keyword search | OpenSearch (single node) — provisional pending bake-off vs ParadeDB pg_search. Put behind `KeywordIndexPort` interface. |
| Workflow engine | Temporal (self-hosted) for ingestion, review, merge, and approval workflows. |
| IDP (intelligent document processing) | Digital-native: PyMuPDF, python-docx/mammoth, openpyxl, python-pptx. Scanned: OpenCV preprocess → PaddleOCR + PP-Structure (layout, tables) → Qwen2.5-VL 7B (vLLM) escalation for low-confidence pages. All local. |
| Embeddings / rerank | BGE-M3 embeddings, BGE reranker, served on GPU node behind `ModelGateway`. |
| Chat LLM | `[OPEN]` local Qwen2.5-32B via vLLM vs API model — pending compliance ruling. Behind `GenerationPort`; both adapters implemented. |
| Identity | Keycloak brokering AD/LDAP. OIDC; token exchange for internal-bot on-behalf-of; client-credentials for service accounts. |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 + Alembic migrations, Pydantic v2. |
| Frontend | React 18 + TypeScript, TipTap editor, Vite. |
| Observability | Prometheus + Grafana + Loki; structured JSON logs; audit log is a DB table, not just logs. |
| Languages | Corpus is Vietnamese + English. All analyzers, prompts, and test sets must cover both. |

## 2. System invariants — enforce in code, verify in CI

These are the guarantees of the platform. Every PR is reviewed against them; each has automated tests (section 10).

- **INV-1 Single retrieval funnel.** All read paths (search, internal bot, external bot, citation clicks, graph expansion) call `retrieval-api`. No service queries the indexes directly. Lint rule: only `retrieval-api` may import index adapters.
- **INV-2 Server-side mandatory ACL filter.** The filter is constructed inside `retrieval-api` from the verified token/service account. Request parameters can narrow, never widen. Filters are applied inside the index query (WHERE clause / OpenSearch filter context), never post-hoc.
- **INV-3 On-behalf-of internal bot.** The internal bot forwards the end user's token (OIDC token exchange). Its own service account grants zero document visibility.
- **INV-4 Hard-scoped external bot.** External scope (`visibility='external' AND status='published'`) is bound to the service account server-side, not passed by the caller.
- **INV-5 Atomic canonical publish.** Canonical-flag flip, old-chunk tombstoning, new-chunk insert, edge updates, and outbox event commit in one Postgres transaction. Index visibility ≤ 10 s.
- **INV-6 Canonical-only serving.** Indexes contain only canonical-version chunks (plus point-in-time archive lookup via registry, never via the default search path).
- **INV-7 PII gate fails closed.** Publication is impossible while `pii_status != 'clear'`. Overrides require a human with role, justification text, and an audit record. Same detector runs as chatbot output filter.
- **INV-8 No automation for regulated classes.** Document classes `regulatory` and `customer_facing` can never auto-publish. Enforced as a code-level guard in the publish service, not configuration.
- **INV-9 Immutable versions, retention hold.** Versions are never updated or deleted. Purge operations check `retention_until`; access to archived versions is audit-logged.
- **INV-10 Reference edges target Document IDs**, never version IDs. Graph expansion applies the same ACL filter as the main query (existence disclosure per category flag).
- **INV-11 Full answer reconstructability.** Every retrieval logs principal, on-behalf-of user, resolved filter, chunk+version IDs, expansion edges, answer ID.
- **INV-12 Model calls behind ports.** OCR, VLM, embedding, rerank, generation, PII detection each have a port interface with at least a local adapter; no service imports model clients directly.

## 3. Repository layout (monorepo)

```
kb-platform/
  docker-compose.yml            # postgres, minio, opensearch, temporal, keycloak, vllm, services
  Makefile                      # make dev / test / migrate / seed / e2e
  libs/
    common/                     # config, logging, errors, audit client
    schemas/                    # Pydantic models shared across services (Document, Version, Chunk, Filter)
    authz/                      # token verification, principal resolution, FilterBuilder (INV-2)
    ports/                      # KeywordIndexPort, VectorIndexPort, GenerationPort, EmbeddingPort,
                                # RerankPort, OcrPort, VlmPort, PiiDetectorPort, StoragePort
  services/
    registry/                   # documents, versions, categories, edges, ACL resolution, publish tx (INV-5)
    idp/                        # parsers, ocr pipeline, confidence scoring, normalization to KBDoc JSON
    pii-gate/                   # pattern rules + LLM detector; ingestion gate + output filter endpoints
    identity-merge/             # legal-number extraction, matching layers, diff, LLM merge draft
    indexer/                    # chunker (article/clause), embedder, index writers, outbox consumer
    retrieval-api/              # policy engine, hybrid search, RRF, rerank, graph expansion, citation lookup
    chat-api/                   # query condensation, context assembly, generation, output filter, citations
    portal-api/                 # upload, review tasks, merge screens, approvals, admin
    workflows/                  # Temporal workflow + activity definitions (ingest, review, merge, publish)
  frontend/
    portal/                     # React app: upload, review editor, merge 3-pane, admin, search UI
  ops/
    grafana-dashboards/  prometheus/  loki/  keycloak-realm/  alembic/
  eval/
    golden_set/                 # queries + graded labels (yaml)
    harness/                    # retrieval metrics (recall@k, nDCG), latency, ACL sweep, canaries
    bakeoff/                    # pg_search vs OpenSearch protocol runner
  docs/
    architecture.md  implementation-plan.md  decisions/ (ADRs)
```

## 4. Database schema (initial Alembic migration)

Types: `visibility ENUM('external','internal_all','restricted')`, `doc_status ENUM('draft','published','archived','expired')`, `doc_class ENUM('regulatory','internal_normative','operational','customer_facing')`, `ref_type ENUM('cites','amends','abrogates','implements','consolidates')`.

```sql
categories(path LTREE PK, label TEXT, default_visibility visibility,
  default_allowed_groups TEXT[], steward_group TEXT, existence_disclosure BOOL DEFAULT false);

documents(id UUID PK, title TEXT, legal_number TEXT UNIQUE NULLS NOT DISTINCT,
  doc_class doc_class, category_path LTREE REFERENCES categories,
  department TEXT, visibility visibility, allowed_groups TEXT[],
  status doc_status, canonical_version_id UUID, review_by DATE,
  created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ);

document_versions(id UUID PK, document_id UUID REFERENCES documents,
  content_ref TEXT, content_hash TEXT, source_type TEXT,           -- upload | portal_edit | consolidation
  author TEXT, change_summary TEXT, idp_report_ref TEXT,
  pii_status TEXT CHECK (pii_status IN ('pending','clear','blocked','overridden')),
  effective_from DATE, effective_to DATE, is_canonical BOOL DEFAULT false,
  retention_until DATE, created_at TIMESTAMPTZ);
  -- partial unique index: one canonical per document
  CREATE UNIQUE INDEX ON document_versions(document_id) WHERE is_canonical;

document_refs(id UUID PK, src_document_id UUID, dst_document_id UUID,
  ref_type ref_type, articles INT[],                                -- which articles an amendment touches
  detected_by TEXT, confirmed_by TEXT, created_at TIMESTAMPTZ);

chunks(id UUID PK, document_id UUID, version_id UUID,
  section_path TEXT,                                                -- "Chương II > Điều 12 > Khoản 2"
  citation_label TEXT,                                              -- "Điều 12.2, TT 41/2016/TT-NHNN"
  text TEXT, embedding VECTOR(1024),
  visibility visibility, allowed_groups TEXT[], department TEXT,
  doc_status doc_status, effective_from DATE, effective_to DATE, tombstoned BOOL DEFAULT false);
  CREATE INDEX ON chunks USING hnsw(embedding vector_cosine_ops);
  -- btree indexes on (visibility, doc_status), department, effective dates

review_tasks(id UUID PK, version_id UUID, task_type TEXT,           -- idp_review|identity_review|merge_review|impact_review|pii_override
  state TEXT, assignee_group TEXT, payload JSONB,
  decision TEXT, decided_by TEXT, decided_at TIMESTAMPTZ);

graph_serving(src_document_id UUID, dst_document_id UUID, ref_type ref_type,
  dst_canonical_version_id UUID, dst_summary TEXT, dst_visibility visibility,
  dst_allowed_groups TEXT[], dst_effective_from DATE, articles INT[],
  PRIMARY KEY(src_document_id, dst_document_id, ref_type));         -- rebuilt on publish

audit_log(id BIGSERIAL PK, ts TIMESTAMPTZ, actor TEXT, on_behalf_of TEXT,
  action TEXT, object_ref JSONB, resolved_filter JSONB, detail JSONB);

outbox(id BIGSERIAL PK, ts TIMESTAMPTZ, topic TEXT, payload JSONB, processed_at TIMESTAMPTZ);
```

## 5. Normalized document format (`KBDoc` JSON) — IDP output contract

Every parser/OCR path emits the same structure; downstream services depend only on this.

```json
{
  "doc_meta": {"detected_title": "...", "legal_number": "41/2016/TT-NHNN|null",
               "language": "vi|en|mixed", "source_format": "pdf_scanned", "page_count": 42},
  "blocks": [
    {"id": "b001", "type": "heading|paragraph|table|figure|stamp",
     "section_path": ["Chương II","Điều 12","Khoản 2"],
     "text": "...", "table": {"rows": [["..."]]},
     "page": 3, "bbox": [x0,y0,x1,y1], "confidence": 0.97, "engine": "paddle|vlm|native"}
  ],
  "detected_refs": [{"raw": "Thông tư 41/2016/TT-NHNN", "legal_number": "41/2016/TT-NHNN",
                     "ref_type_guess": "cites", "block_id": "b017", "confidence": 0.99}],
  "idp_report": {"page_confidences": [...], "escalated_pages": [7,8], "warnings": [...]}
}
```

## 6. Key API contracts

**retrieval-api** (internal only; the single funnel, INV-1/2):
- `POST /v1/retrieve` — headers: user token or service token. Body: `{query, top_k, mode: "current"|"as_of", as_of_date?, facets?: {category?, department?, date_range?}, expand_graph: bool}`. Response: `{chunks: [{chunk_id, version_id, document_id, citation_label, text, score, supersession_flag?}], expansions: [{document_id, summary, citation_label}], resolved_filter_id}`. Facets may only narrow (INV-2).
- `POST /v1/citation-lookup` — `{citation: "Điều 12 Thông tư 41/2016/TT-NHNN"}` → article chunks via registry, same filter.
- `GET /v1/documents/{id}` — render-time access re-check (gate 3); logs archived-version access.

**chat-api**:
- `POST /v1/chat/{surface}` (`surface ∈ internal|external`) — `{messages[]}` → condensation → `/v1/retrieve` → generation (GenerationPort) → output filter (PiiDetectorPort) → `{answer, citations[], answer_id}`. External surface: conduct-rule system prompt, tighter rate limits.

**portal-api** (selected):
- `POST /v1/uploads` (multipart) → starts Temporal `IngestWorkflow`, returns `workflow_id`.
- `GET/POST /v1/review-tasks/...` — list, claim, submit decisions; merge screen payload = `{current_canonical, new_version, llm_draft, section_classifications[]}`.
- `POST /v1/versions/{id}/publish` — guards: pii_status='clear' (INV-7), class automation guard (INV-8), four-eyes state; executes publish transaction (INV-5).
- `PUT /v1/documents/{id}/content` — portal direct edit → new version through the same pipeline.

## 7. Temporal workflows

- `IngestWorkflow(upload_id)`: store original → IDP activity (parser route or OCR chain) → PII scan → identity resolution → branch: `NewDocReview` | `MergeFlow` | `IdentityReviewTask`. Human steps are signals on the workflow.
- `MergeFlow(document_id, new_version_id)`: section diff → LLM merge draft → `merge_review` task → approval signal(s) per doc_class → `PublishActivity`.
- `PublishActivity`: single Postgres tx (INV-5) → outbox event → indexer consumes → chunk/embed/write + `graph_serving` rebuild for affected nodes → impact traversal (`implements` downward) → open `impact_review` tasks.
- `ConsolidationWorkflow(regulation_id, amending_id)`: same as MergeFlow but assignee_group = Legal cell; never auto (INV-8).

## 8. Milestone plan

Order is strict; acceptance criteria (AC) gate progression. Sizes assume 3–4 engineers + 1 frontend.

**M0 — Foundations (week 1–2).** Monorepo scaffold, docker-compose with all infra, Keycloak realm + test users/groups fixture, Alembic migration of section-4 schema, `libs/authz` FilterBuilder with unit tests, CI (lint, type-check, tests), seed script. *AC: `make dev` brings up the stack; FilterBuilder tests cover all principal types incl. widen-attempt rejection.*

**M1 — Registry + storage + digital-native IDP (week 3–5).** Registry service CRUD; MinIO storage port; parsers for docx/xlsx/pptx/pdf-native emitting KBDoc; upload endpoint + IngestWorkflow happy path; portal upload UI. *AC: upload a DOCX → KBDoc stored → document+version rows created; golden parser fixtures for 12 sample files pass.*

**M2 — Indexing + retrieval + search UI (week 5–8).** Article/clause chunker; embedding port + indexer; pgvector + OpenSearch adapters behind ports; retrieval-api with policy engine, RRF, reranker, citation-lookup; publish transaction (INV-5) with outbox→indexer; search UI (facets, highlights). *AC: ACL sweep test (14 principals × query set, zero violations incl. 3 canaries); publish-to-searchable ≤ 10 s; recall@10 ≥ 0.85 on the seed golden set.*

**M3 — Scanned IDP + review editor (week 8–11).** OpenCV preprocess, PaddleOCR+PP-Structure adapter, confidence scoring, VLM escalation adapter; review editor UI (page image ↔ blocks, bbox jump, low-confidence highlights, table editing, category/visibility form, ref confirmation). *AC: 20-document scanned fixture set processes end-to-end; reviewer can correct and publish; Vietnamese diacritics preserved through the whole chain (byte-level fixture test).*

**M4 — PII gate (week 10–12, overlaps M3).** Pattern rules (accounts, CCCD, PAN+Luhn, name+balance heuristics); LLM detector prompt + adapter; gate in IngestWorkflow (fail closed, INV-7); override task type with justification; output-filter endpoint for chat-api. *AC: red-team fixture set — 40 seeded PII docs all blocked, 40 clean docs pass; override path audited.*

**M5 — Identity + merge + consolidation (week 12–16).** Legal-number extractor (regex library for VN instruments + bank decision formats); matcher layers with thresholds; section diff engine; LLM merge-draft prompt with 3-bucket classification; 3-pane merge UI; four-eyes approval; class guards (INV-8); ConsolidationWorkflow; change-impact traversal + impact_review tasks; `graph_serving` rebuild. *AC: amendment fixture (Circular X amends Y, policy implements Y) → consolidation draft produced → Legal approval → canonical flips → impact task opened on the policy; supersession flag appears in retrieval until consolidation approved.*

**M6 — Chatbot internal (week 16–19).** chat-api with condensation, context assembly with citation labels, GenerationPort (vLLM adapter + API adapter), grounded-answer + refusal prompt, citation rendering, answer logging (INV-11), on-behalf-of token exchange (INV-3). *AC: eval harness answer-faithfulness ≥ threshold on 60-question subset; canary extraction attempts fail across 20 red-team prompts.*

**M7 — Bake-off + hardening (week 18–21, parallel).** Implement pg_search adapter for KeywordIndexPort; run eval/bakeoff per protocol; decide and remove the losing adapter; load test to 50 concurrent; backup/restore drill scripted. *AC: bake-off decision memo committed as ADR; p95 targets met.*

**M8 — External bot (week 21–24).** DMZ compose profile; hard-scoped service account (INV-4); conduct-rule prompts + red-team suite; output filter mandatory; rate limits; structured rates/fees content type in portal. *AC: full external red-team suite passes; Compliance/Legal sign-off checklist exported.*

Backfill of the 3,000-document corpus runs as an operational track from M3 onward, prioritized: effective regulations → implementing internal normative docs → operational → customer-facing.

## 9. Prompt assets to author (in `services/*/prompts/`, versioned, with eval cases)

`merge_classifier` (3-bucket section classification, vi+en), `merge_drafter` (consolidated text), `pii_detector` (semantic PII judgment), `query_condenser`, `grounded_answer_internal`, `grounded_answer_external` (+conduct rules), `doc_summarizer` (graph_serving summaries). Each prompt file pairs with a fixture-based eval in `eval/`.

## 10. Testing strategy

- **Unit:** FilterBuilder (every principal × every widen attempt), chunker section-paths, legal-number extractor (fixture list of real VN citation formats), Luhn/CCCD validators.
- **ACL sweep (CI, blocking):** golden queries × all fixture principals; assert zero filter violations; 3 canary documents never retrievable by unauthorized principals through search, chat, or graph expansion (INV-2/3/4/10).
- **Publish consistency (CI):** publish revision fixture → old-version text unreachable, new reachable ≤ 10 s; kill indexer mid-publish → outbox recovery leaves no divergence (INV-5/6).
- **PII red-team set:** maintained fixture corpus, both gate and output filter.
- **Retrieval quality:** eval harness = the bake-off harness; golden set grows with real queries; runs nightly, regression threshold blocks merge to main.
- **E2E:** Playwright flows — upload→review→publish→search→chat with citations.

## 11. `[OPEN]` items — build behind interfaces, do not block

1. **Cloud vs local generation LLM** — pending Compliance ruling on processing internal documents via external API. Both GenerationPort adapters ship; default local.
2. **Keyword engine** — OpenSearch vs pg_search, resolved by M7 bake-off (Vietnamese tokenization gate is the likely decider).
3. **Existence-disclosure defaults per category** — Compliance to specify; default false.
4. **Retention periods per doc_class** — Legal to provide the schedule; column exists, values seeded conservatively (10 years).
5. **SBV IT-classification obligations** — Compliance mapping may add logging/DR requirements; keep ops config declarative.

## 12. Glossary

**IDP** — intelligent document processing: parsing, OCR, layout, tables, confidence, normalization to KBDoc. **Canonical version** — the single referred source per document; for regulations, the consolidated text (văn bản hợp nhất). **Consolidation** — applying amendment instruments to produce the canonical regulatory text. **Tombstone** — marking old-version chunks unretrievable within the publish transaction. **Four-eyes** — preparer + approver separation. **On-behalf-of** — internal bot retrieves with the end user's identity via token exchange. **Serving projection (`graph_serving`)** — derived read-optimized copy of reference edges with summaries and ACL data. **Golden set** — human-graded query/relevance fixtures powering evals and the bake-off.
