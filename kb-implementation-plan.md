# Knowledge Base Platform — Software Development Implementation Plan

**Version:** 1.1 · **Derived from:** Architecture Design v1.1 (banking profile) · **Date:** August 2026

> **v1.1 changes:** two requirement changes are folded in. (a) An answer must cover a fact across *every* document that states it, with a link per source — INV-13, ADR-0037/0038, work split between M6 and M10. (b) Clause expiry inside a partially expired document has two shapes, and the common one is *declared* in the replacing text rather than inferred — M9 is re-staged around that, ADR-0039/0040.

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
- **INV-13 Answers are source-complete and linkable.** Every document that contributed a passage to a chat context appears in the response's `sources` with a link built from `(document_id, version_id, section_path)` — never a chunk id, which does not survive a rechunk. A context cut for budget is counted and stated to the user; a context narrowed by the caller's filter is counted in the answer log and never stated (ADR-0023/0037/0038).

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

Later migrations add the M9 tables (`document_expiry`, `clause_supersessions`, the prompt cache) and the columns they need (`chunks.article`, `chunks.subject_key`, `document_refs.anchors`, `document_expiry.anchors`). Each is described with the milestone that introduces it and the ADR that decided it; none of them belongs in the initial migration.

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
  "detected_refs": [{"raw": "khoản 2 Điều 12 Thông tư 41/2016/TT-NHNN", "legal_number": "41/2016/TT-NHNN",
                     "ref_type_guess": "cites", "anchors": ["12.2"],
                     "block_id": "b017", "confidence": 0.99}],
  "declarations": [{"kind": "replaces", "target": "10/2022/TT-NHNN",
                    "target_anchors": ["12.2"], "replacement_anchors": ["7"],
                    "effective_from": "2026-01-01", "block_id": "b402",
                    "evidence": "Điều 7 Thông tư này thay thế khoản 2 Điều 12 Thông tư 10/2022/TT-NHNN.",
                    "confidence": 0.95}],
  "idp_report": {"page_confidences": [...], "escalated_pages": [7,8], "warnings": [...]}
}
```

`detected_refs[].anchors` and the `declarations` array are added in M9b/M9c (ADR-0036, ADR-0039). Both are read from the citing document's own text and are correctable on the reference-confirmation step of the review screen, where a human is already looking at the edge.

## 6. Key API contracts

**retrieval-api** (internal only; the single funnel, INV-1/2):
- `POST /v1/retrieve` — headers: user token or service token. Body: `{query, top_k, mode: "current"|"as_of", as_of_date?, facets?: {category?, department?, date_range?}, expand_graph: bool, coverage: bool}`. Response: `{chunks: [{chunk_id, version_id, document_id, citation_label, text, score, supersession_flag?, superseded_by?, fact_set_id?}], expansions: [{document_id, summary, citation_label, anchors[]}], coverage: {documents, seeds, withheld_by_filter}, resolved_filter_id}`. Facets may only narrow (INV-2); `coverage` widens *nothing* — it runs a second query under the identical compiled filter and adds the passages that state the same fact (ADR-0037). `withheld_by_filter` goes to the audit record, never to the caller.
- `POST /v1/citation-lookup` — `{citation: "Điều 12 Thông tư 41/2016/TT-NHNN"}` → article chunks via registry, same filter.
- `POST /v1/resolve-anchor` — `{document_id, anchor: "12.2", as_of_date?}` → the target's chunks by equality join under the caller's full chunk predicate; unresolved anchors are reported, not dropped (ADR-0036). Distinct from `citation-lookup`, which trigram-matches text a human typed.
- `GET /v1/documents/{id}` — render-time access re-check (gate 3); logs archived-version access. Also the resolver behind every citation link (INV-13).

**chat-api**:
- `POST /v1/chat/{surface}` (`surface ∈ internal|external`) — `{messages[]}` → condensation → `/v1/retrieve` (with coverage) → generation (GenerationPort) → output filter (PiiDetectorPort) → `{answer, citations[], sources[], coverage_note?, answer_id}`. `citations[]` resolves the `[n]` markers the model claimed and each is verified against its passage (ADR-0018); `sources[]` is every document that contributed a passage, each with a link, and is not the model's to curate (INV-13, ADR-0038). `coverage_note` states a budget truncation and never an ACL narrowing. External surface: conduct-rule system prompt, tighter rate limits, links only to public document views.

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

**M2 — Indexing + retrieval + search UI (week 5–8).** Article/clause chunker; embedding port + indexer; pgvector + OpenSearch adapters behind ports; retrieval-api with policy engine, RRF, reranker, per-document cap, citation-lookup; publish transaction (INV-5) with outbox→indexer; search UI (facets, highlights). One constraint from INV-13 lands here rather than being retrofitted: every read path addresses a passage by `(document_id, version_id, section_path)`, and `chunk_id` appears only in the audit record — chunk ids are deleted and re-inserted on every publish and rechunk, so anything that stores one across that boundary is broken by design (ADR-0038). *AC: ACL sweep test (14 principals × query set, zero violations incl. 3 canaries); publish-to-searchable ≤ 10 s; recall@10 ≥ 0.85 on the seed golden set; a rechunk of a cited version leaves every stored citation still resolvable.*

**M3 — Scanned IDP + review editor (week 8–11).** OpenCV preprocess, PaddleOCR+PP-Structure adapter, confidence scoring, VLM escalation adapter; review editor UI (page image ↔ blocks, bbox jump, low-confidence highlights, table editing, category/visibility form, ref confirmation). *AC: 20-document scanned fixture set processes end-to-end; reviewer can correct and publish; Vietnamese diacritics preserved through the whole chain (byte-level fixture test).*

**M4 — PII gate (week 10–12, overlaps M3).** Pattern rules (accounts, CCCD, PAN+Luhn, name+balance heuristics); LLM detector prompt + adapter; gate in IngestWorkflow (fail closed, INV-7); override task type with justification; output-filter endpoint for chat-api. *AC: red-team fixture set — 40 seeded PII docs all blocked, 40 clean docs pass; override path audited.*

**M5 — Identity + merge + consolidation (week 12–16).** Legal-number extractor (regex library for VN instruments + bank decision formats); matcher layers with thresholds; section diff engine; LLM merge-draft prompt with 3-bucket classification; 3-pane merge UI; four-eyes approval; class guards (INV-8); ConsolidationWorkflow; change-impact traversal + impact_review tasks; `graph_serving` rebuild. *AC: amendment fixture (Circular X amends Y, policy implements Y) → consolidation draft produced → Legal approval → canonical flips → impact task opened on the policy; supersession flag appears in retrieval until consolidation approved.*

**M6 — Chatbot internal (week 16–19).** chat-api with condensation, context assembly with citation labels, GenerationPort (vLLM adapter + API adapter), grounded-answer + refusal prompt, citation rendering, answer logging (INV-11), on-behalf-of token exchange (INV-3).

Plus the first half of fact coverage (ADR-0037/0038), which is what makes a multi-document answer possible before M9b's subject key and anchors exist: a **coverage round** after rerank, seeded by the top passages rather than by the question — a second embedding query against each seed's own vector, unioned with the best-matching passages of the documents on the seed's confirmed reference edges (document granularity is all M5 offers; M9b makes it clause-precise), run through the same compiled filter as a second query and never as a post-filter (INV-1/2); **coverage-before-depth** budgeting in `assemble`, one passage per document across the fact set before any document gets a second; `sources[]` with a link per document (INV-13) and a rendered sources panel in the portal; a spoken note when the budget cut documents and a silent audit counter when the filter did (ADR-0023). Because a wider context now routinely holds two documents stating the same rule differently, and M9 cannot yet say which one replaced which, `grounded_answer_internal` gains one rule for the interim: where two passages in a set state different quantities, name each one's instrument and effective date and present the conflict — never resolve it silently. *AC: eval harness answer-faithfulness ≥ threshold on 60-question subset; canary extraction attempts fail across 20 red-team prompts; on the coverage-labelled golden subset, fact coverage ≥ 0.9 of the documents that state the answer's rule and every document in the context appears in `sources` with a link that resolves under the caller's own filter and under a colleague's (a link is not a capability); a question whose fact set spans a 2023 and a 2026 statement of the same rate produces an answer naming both instruments and both dates.*

**M7 — Bake-off + hardening (week 18–21, parallel).** Implement pg_search adapter for KeywordIndexPort; run eval/bakeoff per protocol; decide and remove the losing adapter; load test to 50 concurrent; backup/restore drill scripted. *AC: bake-off decision memo committed as ADR; p95 targets met.*

**M8 — External bot (week 21–24).** DMZ compose profile; hard-scoped service account (INV-4); conduct-rule prompts + red-team suite; output filter mandatory; rate limits; structured rates/fees content type in portal. *AC: full external red-team suite passes; Compliance/Legal sign-off checklist exported.*

**M9 — Expiry and clause-level supersession (post-M8).** Closes the gap between "the platform can hide a rule that ceased to apply" and "the platform ever decides one has". ADR-0030 to ADR-0036 and ADR-0039/0040 hold the decisions. Graphiti and LightRAG were evaluated as alternatives and both fail the invariant gate (`eval/graphrag/protocol.md`); four ideas are borrowed from Graphiti and named in the ADRs that take them.

The shape that drives the staging is that **a clause expiring inside a still-live document arrives in two forms, and they are not equally hard.** In roughly four cases out of five the replacing text says so — *"bãi bỏ khoản 2 Điều 12 Thông tư 10/2022"*, *"Điều 7 Thông tư này thay thế Điều 5 Thông tư 10/2022"* — usually in the closing *Điều khoản thi hành* article, in a handful of formulaic shapes. That case is a **reading** problem: patterns, an equality join, and a person confirming a sentence, with no model, no embeddings and no thresholds anywhere in the chain. The remaining fifth is genuinely implicit — a later instrument re-states a rule and never mentions the earlier one — and only that fifth is the search-and-adjudicate problem the funnel was designed for. So the declared path is built first and shipped alone (M9c), and the inference funnel (M9d) runs only over what it did not account for. Four stages, strictly ordered.

- **M9a — Full expiry.** `document_expiry` ledger (append-only, `basis`/`evidence`/`state`, plus the `created_at`/`closed_at` system-time pair so "what would we have answered on date D" is a query, ADR-0030); sunset-clause detector in `kb_vntext.dates` (`find_effective_to`, same read-don't-guess rule as ADR-0029); `abrogates` edge proposes an expiry dated from the abrogating instrument's `effective_from`; steward "mark expired" action on the inspection screen; confirmation projects the dates onto the chunk copies inside the same transaction; daily `expiry_sweep` Temporal Schedule for the status flip, T-30 warnings and `review_by` attestations (ADR-0031); expired matches returned to chat as a named refusal ("that rule ceased on …; the current provision is …") rather than silence, never as a citation (INV/ADR-0018 unchanged). The ledger's `anchors`/`articles` columns are written from the start and a row carrying them is a *partial* expiry: M9a records it, shows it on the inspection screen, and applies nothing — the projection onto individual clauses is M9c's, because it needs M9b's anchor resolution to know which chunks it means. *AC: a fixture where A abrogates B → expiry proposed with the sentence it was read from → confirmed → B unretrievable on the effective date **with the sweep never run** → `documents.status='expired'` after one sweep → an archive-reader's `as_of` query before that date still returns B; a partial row is recorded, is visible, changes no serving, and never flips a document's status.*
- **M9b — Article-scoped supersession and reference resolution.** `chunks.article` plus the subject key written by the chunker, on one migration and one corpus rechunk; `_superseded_documents` → `_superseded_articles`, both copies of the predicate moving together with the drift test asserting agreement at article granularity (ADR-0032); clause-precise `document_refs.anchors` (`"12"`, `"12.2"`, `"12.2a"`, the form `build_citation_label` already emits) written by a re-detection pass over stored KBDocs, resolved to the target's chunks by equality join under the caller's chunk-level filter, returned as anchors on `GraphExpansion`, with the inbound direction on the inspection screen and unresolved anchors reported rather than dropped (ADR-0036). Note that the anchor extractor is new work, not a widening of something that exists: `document_refs.articles` is in the ORM and read by the impact traversal, but no extractor anywhere populates it — it is filled by a human on the review screen or not at all, which is why the declared case currently arrives with no article list. Everything in M9c and half of M10 stands on this stage, so it lands whole rather than partially. *AC: an amendment touching Điều 12 of a sixty-article circular flags the chunks of Điều 12 and no others, and an edge with no article list still flags the whole document; a reference to "khoản 2 Điều 12" resolves to that one clause while a reference to "Điều 12" resolves to all of its clauses in document order; resolution returns nothing for a principal who may not read the target and nothing on the default path when the clause is no longer in effect; under `as_of` it resolves into the version canonical at that date, not today's; and an anchor is marked stale when the target's canonical version changed after the reference was detected.*
- **M9c — Declared clause supersession and partial expiry (scenario 1, the ~80% case).** A declaration extractor in `kb_vntext.supersession` beside the date and legal-number extractors — `find_declarations(text) -> list[Declaration]`, patterns only on ADR-0013's terms, emitting kind (`abrogates`|`replaces`|`amends`), the target instrument, `target_anchors` and `replacement_anchors` in `build_citation_label`'s dotted form, the stated effective date, and the sentence as evidence (ADR-0039). The declaring sentence names both ends and the direction, so nothing is inferred from dates or publish order on this path, and the granularity is whatever the text said — *bãi bỏ khoản 2 Điều 12* ends one clause and leaves the other three standing. The same pass writes M9b's `document_refs.anchors`; reading the corpus twice for two consumers costs twice and produces two answers that can disagree. A declaration whose target is not yet ingested is parked as a pending declaration and resolved when it arrives (ADR-0028's machinery, unchanged — an archive digitised in yield order routinely produces the amending instrument first); one whose target is the declaring document itself is routed to the M5 merge flow, not here. `declarations[]` on `KBDoc`, correctable on the reference-confirmation step where a human already looks at the edge. Confirmation is a batch screen per declaring document — one closing article typically declares a dozen changes from one paragraph — and on confirm, in one transaction: the anchors resolve to the target's canonical chunks by M9b's equality join, `effective_to` is written onto **those chunks only** (last day the rule applied, i.e. the replacing instrument's `effective_from` minus one), the ledger row is written with `basis='declared_by'` and its anchors, and where a replacement was named a `clause_supersessions` row carries the pointer so the refusal can say what took over (ADR-0040). Retrieval needs no new code: the effectivity predicate already in `kb_authz.compile` does the rest, so the clause leaves the default path on its date with nothing running. `expiry_sweep` skips rows with anchors; a partially expired document stays `published`. `PublishService.rechunk` re-projects from the ledger, which is the authority. *AC: on a fixture whose closing article declares four changes — a whole-instrument replacement, a clause abrogation with no replacement, an article replacement, and a clause replacement naming both ends — all four are extracted with their evidence sentences and confirmed from one screen; the abrogated clause is unretrievable on the default path from its date **with no model called anywhere in the flow and the sweep never run**, while every other article of its document is unaffected and the document is still `published`; a question about the replaced clause returns the named refusal pointing at the replacing article; an `as_of` query before that date returns the old clause unflagged; a `revoked` ledger row restores it; a rechunk of either document leaves all of the above true; and the extractor reports recall against a fixture set of real closing articles across instrument types in both languages.*
- **M9d — Inferred clause supersession (scenario 2, the implicit remainder).** Runs only over the pairs M9c did not account for. Three paths by evidence — the edge names articles (nothing to detect, M9b already flags it), the edge names none (search bounded to the two documents), no edge at all (the full funnel) — and for the last, four gates before any model call: subject candidates from the lexical and vector channels together, effectivity overlap, scope-facet match, quantity delta; then four-bucket adjudication on what survives. Quantity extractor in `kb_vntext`, patterns only (ADR-0013). Direction decided by effective date and never by publish order; the record carries `supersedes_from` so `as_of` before that date is unaffected. `clause_supersessions` anchored on section path; `clause_review` task type; `RetrievedChunk.superseded_by` pointer; fusion drops a confirmed-superseded chunk when its replacement is in the candidate set; corpus backfill landing as `proposed`, queue ranked by retrieval frequency from `audit_log` (ADR-0033). **An inferred supersession never writes the expiry ledger and never hides a clause** — it flags, points and drops in fusion, exactly as ADR-0033 specified. The dividing line against M9c is not a confidence threshold that a detection could one day cross: a declaration is the corpus stating what the law now is, an inference is our conclusion about two texts, and only the first is evidence that a rule *ended* (ADR-0040). Depends on two cross-cutting changes that land first: index-based model I/O with range validation, which also fixes `MergeDraft.complete` in the M5 merge classifier (ADR-0034), and the prompt-output cache that makes the backfill re-runnable (ADR-0035). *AC: the two-schedule fixture (same rate item in a 2023 and a 2026 document, no edge between them) is detected, proposed, and changes nothing until confirmed; after confirmation the answer cites only the 2026 clause and names it as the replacement, while an `as_of` query dated before the 2026 clause took effect still returns the 2023 clause unflagged; a same-subject pair differing only in customer segment is recorded `different_scope` by gate 3 with no model call; the confirmed inference flags and drops the 2023 clause but leaves it retrievable on its own document page, where a declared expiry would have ended it; and the eval harness reports stale-answer rate, false-supersession rate, the split between pairs resolved by the gates and pairs resolved by the model, and the measured share of supersessions that arrived declared rather than inferred — the number the whole M9 staging rests on, and the one to re-check against the corpus rather than the estimate.*

**M10 — Fact-scoped answers across documents (post-M9).** Upgrades M6's coverage round from an approximation to the thing INV-13 promises, using the two exact channels M9 built. Membership in a fact set becomes, in descending order of confidence: the resolved reference anchors from M9b (a procedure that names *khoản 2 Điều 12* is about that rule by the bank's own statement, an equality join, nothing to tune), then the normalized subject key written onto `chunks` by M9b's rechunk and first consumed by M9d (heading chain with boilerplate stripped, plus `RateItem.label` for structured content — indexed, exact), then M6's passage-seeded embedding round, which stays because it is the only channel that reaches a rule phrased in words nothing else shares (ADR-0037). Fact sets are surfaced in the search UI as grouped results, not only in chat — "four documents state this" is the answer to a search as much as to a question. Confirmed supersessions from M9c/M9d resolve within the set before assembly, so the conflict-presentation rule M6 shipped as an interim narrows to the pairs nobody has adjudicated yet. The golden set is re-labelled per *fact* rather than per query→passage, which is the real cost of this milestone and is work on the eval set rather than the code. *AC: fact coverage ≥ 0.95 on the re-labelled golden set with no regression in recall@10 or in p95 latency; a fact stated in five documents produces five sources with five resolving links; a member the caller may not read is absent from the answer, absent from the source count, and present in the audit record; a member ended by a confirmed declaration is absent from the default path and present under `as_of`; a member superseded by a confirmed inference is present, labelled, and dropped from fusion when its replacement is in the set; and the answer states how many documents the budget cut without ever stating how many the filter did.*

Backfill of the 3,000-document corpus runs as an operational track from M3 onward, prioritized: effective regulations → implementing internal normative docs → operational → customer-facing.

## 9. Prompt assets to author (in `services/*/prompts/`, versioned, with eval cases)

`merge_classifier` (3-bucket section classification, vi+en), `merge_drafter` (consolidated text), `pii_detector` (semantic PII judgment), `query_condenser`, `grounded_answer_internal`, `grounded_answer_external` (+conduct rules), `doc_summarizer` (graph_serving summaries). Added by the v1.1 requirements: `clause_adjudicator` (M9d's four-bucket verdict, answering with indices per ADR-0034), and `declaration_reader` (M9c residue only — sentences the patterns matched partially and drafting that does not follow the template; it proposes and never decides, ADR-0039). `grounded_answer_internal` and `_external` both change for multi-source answers: cite every document the context drew on, present rather than resolve a quantity conflict between two unadjudicated passages, and state a budget truncation. Each prompt file pairs with a fixture-based eval in `eval/`; a prompt version change invalidates exactly its own cache entries (ADR-0035).

## 10. Testing strategy

- **Unit:** FilterBuilder (every principal × every widen attempt), chunker section-paths, legal-number extractor (fixture list of real VN citation formats), Luhn/CCCD validators, declaration extractor (real closing articles, vi+en, measured on recall), expiry boundary dates (the day before, the day of, and the day after a replacement takes effect — the inclusive predicate makes this an off-by-one with a day of wrong answers on either side).
- **ACL sweep (CI, blocking):** golden queries × all fixture principals; assert zero filter violations; 3 canary documents never retrievable by unauthorized principals through search, chat, or graph expansion (INV-2/3/4/10).
- **Publish consistency (CI):** publish revision fixture → old-version text unreachable, new reachable ≤ 10 s; kill indexer mid-publish → outbox recovery leaves no divergence (INV-5/6).
- **PII red-team set:** maintained fixture corpus, both gate and output filter.
- **Retrieval quality:** eval harness = the bake-off harness; golden set grows with real queries; runs nightly, regression threshold blocks merge to main. From M6 it also reports **fact coverage** (share of the documents stating the answer's rule that reached the context), and from M9 **repealed-clause serve rate** (answers citing a clause a confirmed declaration ended — the hardest failure of the set, because the corpus told us and we served it anyway), **stale-answer rate** and **false-supersession rate**.
- **Citation stability (CI):** build an answer, rechunk every cited version, re-resolve every link and every `sources` entry. Nothing may address a passage by chunk id outside the audit record (INV-13); this is the one failure mode here that no reviewer catches by reading the diff.
- **Coverage ACL (CI, blocking):** the fact-set expansion is a second query and therefore a second chance to widen. The ACL sweep runs with `coverage: true` as well as without, over the same principals and canaries, and additionally asserts that a fact set narrowed by the filter is identical from the caller's side to one that was never narrowed.
- **E2E:** Playwright flows — upload→review→publish→search→chat with citations.

## 11. `[OPEN]` items — build behind interfaces, do not block

1. **Cloud vs local generation LLM** — pending Compliance ruling on processing internal documents via external API. Both GenerationPort adapters ship; default local.
2. **Keyword engine** — OpenSearch vs pg_search, resolved by M7 bake-off (Vietnamese tokenization gate is the likely decider).
3. **Existence-disclosure defaults per category** — Compliance to specify; default false.
4. **Retention periods per doc_class** — Legal to provide the schedule; column exists, values seeded conservatively (10 years).
5. **SBV IT-classification obligations** — Compliance mapping may add logging/DR requirements; keep ops config declarative.
6. **Auto-confirmation of expiry per doc_class** — `regulatory` and `customer_facing` clearly need a human (INV-8). Whether an `abrogates` edge may expire an internal or operational document unattended is Legal's call; the ledger's `proposed` state supports either answer (ADR-0030).
7. **Which date ends the abrogated instrument** — the abrogating document's effective date (assumed) or its issue date. Legal to confirm once in writing; it is baked into every row the abrogation detector writes.
8. **Does "newer wins" ever apply automatically to regulatory clauses** — lex specialis means a specific older rule can survive a general newer one. Sets the confirmation threshold for M9d and, in practice, decides whether the inferred review queue is workable (ADR-0033). Note it does *not* govern M9c: a declaration states which rule ended, so lex specialis is the drafter's problem rather than ours.
9. **Retention after expiry** — an expired document is not a deletable one. `retention_until` is set at version creation from the class schedule; whether expiry starts or resets a clock is part of item 4's schedule.
10. **What the external bot says about an expired fee** — "no longer applies, see X" is more useful and discloses that the document exists, which runs into existence disclosure (item 3, ADR-0023). Needs a ruling per category.
11. **May a declared supersession auto-confirm for the unregulated classes** — distinct from item 6 and easier, because the evidence is an explicit sentence rather than an inference, and M9c's batch screen makes confirming cheap enough that automation may not be worth its risk. `regulatory` and `customer_facing` still need a human either way (INV-8). Legal's call; the ledger's `proposed` state supports either answer.
12. **How many sources an answer may cite before it stops being read** — a product decision, not a technical one. The cap bounds ADR-0037's "every document that states the fact"; too low and coverage is theatre, too high and nobody reads the sources panel. Set it from observed answers after M6 rather than guessing now, and keep it configurable per surface.
13. **What an answer says when the caller's filter cut the fact set** — nothing, today, and deliberately: a spoken "some sources are not available to you" is an existence disclosure by another route (item 3, ADR-0023). Whether a *steward* reviewing an answer in the portal should see the withheld count, which the audit record already holds, is a separate and easier question.

## 12. Glossary

**IDP** — intelligent document processing: parsing, OCR, layout, tables, confidence, normalization to KBDoc. **Canonical version** — the single referred source per document; for regulations, the consolidated text (văn bản hợp nhất). **Consolidation** — applying amendment instruments to produce the canonical regulatory text. **Tombstone** — marking old-version chunks unretrievable within the publish transaction. **Four-eyes** — preparer + approver separation. **On-behalf-of** — internal bot retrieves with the end user's identity via token exchange. **Serving projection (`graph_serving`)** — derived read-optimized copy of reference edges with summaries and ACL data. **Golden set** — human-graded query/relevance fixtures powering evals and the bake-off. **Fact set** — the passages across all documents that state the same rule, gathered under one filter and answered together (ADR-0037). **Anchor** — a clause-precise pointer in `build_citation_label`'s dotted form (`"12"`, `"12.2"`, `"12.2a"`), resolved to chunks by equality join. **Declaration** — a sentence in a replacing instrument that states which earlier clause ends and what replaces it; read, not inferred (ADR-0039). **Partial expiry** — a ledger row carrying anchors: the named clauses stop being served, the rest of the document stays live and `published` (ADR-0040).
