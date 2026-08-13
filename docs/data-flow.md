# How a document gets in, and how it comes back out

A map of the two halves of the platform, with the file and line where each step actually
happens. The write path turns bytes into retrievable, ACL-tagged chunks and a reference graph;
the read path turns a question into passages the caller is allowed to see.

Everything here is one-way: nothing on the read path writes, and nothing on the write path
decides who may read. The single seam between them is the `chunks` table, which carries a copy
of the ACL so the filter can live inside the query (INV-2).

```
     upload ──► IngestWorkflow ──► KBDoc ──► registry ──► PII gate ──► review
                                                                          │
                                             chunk + embed ◄──────────────┘
                                                    │
                                    ONE transaction ├── canonical flip
                                                    ├── old chunks tombstoned
                                                    ├── new chunks + vectors written
                                                    ├── graph_serving rebuilt
                                                    ├── audit record
                                                    └── outbox event ──► keyword index

     question ──► ResolvedFilter ──► keyword ∥ vector ──► RRF ──► rerank ──► cap
                                                                              │
                        audit ◄── graph expansion ◄── supersession flags ◄─────┘
```

---

## Part 1 — The write path

### 1. Upload

`services/portal-api/src/kb_portal_api/main.py:150` — `create_upload`.

The bytes are hashed and written to object storage **before** the workflow starts. The key is
the content hash, so re-submitting the same file is a no-op rather than a second copy, and a
Temporal outage loses the trigger but never the document. The response carries a
`workflow_id`, which the upload screen polls.

Storage is behind `StoragePort` (`libs/ports/src/kb_ports/storage.py`) with two adapters:
`storage_s3.py` (MinIO/S3, two buckets — `kb-originals` versioned, `kb-derived` not) and
`storage_local.py` for tests.

### 2. The workflow

`services/workflows/src/kb_workflows/ingest.py:59` — `IngestWorkflow.run`.

Five activities, each with its own timeout and retry policy: `run_idp` → `register_ingest` →
`scan_pii` → `link_detected_refs` → `open_review_task`. A document waiting three weeks for a
reviewer costs nothing while it waits, and a worker restart mid-ingest resumes rather than
re-uploading.

Branch logic lives in `routing.py:decide` so it is testable without a Temporal server.
`ValidationError` and `UnsupportedFormat` are non-retryable — a malformed document fails
identically on every attempt, and burning the queue on it only delays everything behind it.

### 3. Parse → KBDoc

`services/idp/src/kb_idp/service.py:49` — `process(data, filename, ocr=…, vlm=…)`. One entry
point for the activity, the HTTP surface and the tests.

- **Format detection** (`detect.py`) reads magic bytes first and the filename only as a
  tiebreaker. A JPEG named `scan.pdf` must not reach the PDF parser and produce plausible
  garbage.
- **Digital-native route** — `parsers/__init__.py` maps the format to `parse_docx`,
  `parse_xlsx`, `parse_pptx`, `parse_pdf_native`, `parse_html`, `parse_txt`.
- **Scanned route** — `ocr_pipeline.py`: rasterize at 300 dpi → `preprocess.py` (trim, deskew,
  denoise, binarize) → PaddleOCR → `confidence.py:score_page` → escalate the pages that fail
  the score to the vision model. The score multiplies engine confidence by **diacritic
  plausibility**, because a page whose Vietnamese tone marks were lost comes back
  *confidently wrong* (ADR-0025).
- **Assembly** — `builder.py` emits `KBDoc` (`libs/schemas/src/kb_schemas/kbdoc.py`): blocks
  with ids, a structural path (Chương/Điều/Khoản), page, bbox, per-block confidence, and the
  `IdpReport`. It also reads what the instrument says about itself — the issue date, and the
  effective date from its effectivity clause (`libs/vntext/src/kb_vntext/dates.py`, ADR-0029).
  A date it cannot read plainly is left empty for the reviewer rather than guessed.

The KBDoc is written to `kb-derived` and only its **reference** is returned
(`activities.py:120`) — a large document's structure has no business in workflow history.
That stored `idp_report_ref` is also what makes re-chunking possible later.

### 4. Register

`services/workflows/src/kb_workflows/activities.py:193` — `register_ingest`.

Creates the `documents` row or attaches to an existing one. Identity matching is
`services/identity-merge/src/kb_identity_merge/matching.py:103`: exact legal number →
normalized number → fuzzy number + title support (thresholds `0.85` / title support), and
anything below that becomes an `identity_review` task rather than a guess. An upload whose
number already exists **attaches a version**, so an instrument's history stays one lineage.

Category defaults (visibility, allowed groups, steward group) come from the category tree in
`services/registry/src/kb_registry/service.py`; retention is set here, and `pii_status` starts
at `pending`.

### 5. The PII gate

`services/workflows/src/kb_workflows/activities.py:253` — `scan_pii`, over
`services/pii-gate/src/kb_pii_gate/detector.py:222` (`scan_document`).

Pattern rules (`patterns.py`: CCCD, CMND, PAN+Luhn, account, tax, phone, email, address, DOB)
are the floor; a model may only *add* findings (ADR-0013). The verdict is written onto the
version: `clear`, `blocked`, or `pending`. `pending` is not clean — a scan that could not
complete leaves the version unpublishable by any path (INV-7), enforced both in
`publish.check_publishable` and by a database trigger.

### 6. Reference edges (graph build, part 1)

Detection happens during parsing: `services/idp/src/kb_idp/builder.py:181` runs
`extract_legal_numbers` and `guess_ref_type` from
`libs/vntext/src/kb_vntext/legal_numbers.py:141` over every block, producing `detected_refs`
with the block id and a confidence.

`activities.py:306` — `link_detected_refs` — resolves those numbers against the registry and
writes `document_refs` rows with `detected_by="idp"` and `confirmed_by=NULL`.

A number that resolves to nothing is **parked in `pending_document_refs`**, not dropped
(ADR-0028): the corpus is digitised in arbitrary order, so an amending decree routinely
arrives before the instrument it amends. The moment a document with that legal number is
registered — `RegistryService.create_document` → `resolve_pending_refs` — the parked rows
become edges and `graph_serving` is refreshed for them. The graph therefore converges on the
same shape whatever order documents arrive in, and the inspection screen lists what a document
is still waiting for.

Edges point at **documents, never versions** (INV-10) — a reference to "Thông tư 41" means the
instrument, and it must survive that instrument being amended.

### 7. Review

`services/portal-api/src/kb_portal_api/review.py:188` — `ReviewService.submit`.

The reviewer approves, rejects, or corrects. Corrections create a **new version** (ADR-0012)
rather than editing text in place, because the version is the audit unit. Approval is what
calls publish, at `review.py:250`.

### 8. Chunking

`services/indexer/src/kb_indexer/chunker.py:62` — `chunk_document(kbdoc, legal_number=…)`.

The boundary is structural, not a token window: **one clause (Khoản), or one article (Điều)
when it has no clauses**. That is how Vietnamese instruments are written and cited, and it is
what makes `citation_label` a real citation ("Điều 12.2, TT 41/2016/TT-NHNN") instead of
"chunk 47".

| Constant | Value | Why |
|---|---|---|
| `TARGET_CHARS` | 1200 | a chunk answering two questions ranks well for neither |
| `MAX_CHARS` | 2000 | hard ceiling before a clause is split |
| `MIN_CHARS` | 200 | below this, merge with the next sibling or it retrieves on nothing |
| `SPLIT_OVERLAP_CHARS` | 150 | a sentence spanning a cut stays findable |

Rules that fall out of the choice: a split clause keeps **one** citation across its parts (a
split is an embedding concern, never a citation concern); a table is never split, and a
heading immediately above one is kept as its caption; every chunk carries its ancestor
headings as a prefix, so a clause reading "tỷ lệ này" still matches a query about "tỷ lệ an
toàn vốn".

### 9. Embedding

`services/registry/src/kb_registry/publish.py:108` — `PublishService.prepare` — chunks and
embeds **outside** the transaction, because a GPU round trip must not hold a row lock.

`EmbeddingPort` (`libs/ports/src/kb_ports/models.py`) has two methods, not one:
`embed_documents` and `embed_query`. BGE-M3 is asymmetric, and mixing the two costs recall
silently rather than failing. Adapters:

- `libs/ports/src/kb_ports/adapters/embedding_tei.py` — BGE-M3, 1024-dim, through the LiteLLM
  proxy (`/v1/embeddings`) or straight to TEI (`/embed`) (ADR-0024). It checks the returned
  dimension, because a configured model that has quietly diverged from the served one corrupts
  the index instead of failing a request.
- `embedding_hashed.py` — deterministic local stand-in, declares `semantic: false`
  (ADR-0027).

`prepare` fails loudly if the vector count does not match the chunk count.

### 10. The publish transaction

`services/registry/src/kb_registry/publish.py:166` — `PublishService.publish`. This is the
heart of INV-5, and everything in it commits together or not at all:

1. `SELECT id FROM documents WHERE id = :id FOR UPDATE` — serializes concurrent publishes of
   the same document, and nothing else (ADR-0009 explains why not `SERIALIZABLE`).
2. Re-read version and document **under the lock**, and re-check publishability — an approval
   or a PII override could have been revoked since the first check.
3. Previous canonical version: its chunks are `tombstoned = TRUE, doc_status = 'archived'`
   (`_tombstone`, line 378) and `is_canonical = FALSE`.
4. This version becomes canonical; `documents.status = 'published'`,
   `canonical_version_id` set.
5. `_insert_chunks` (line 388) writes the new chunks *with their vectors and a copy of the
   document's ACL*.
6. `_rebuild_graph_serving` (line 433) — graph build, part 2, below.
7. `enqueue(TOPIC_PUBLISHED)` — the outbox row.
8. Audit record naming actor, approver, PII status, and the counts.

A reader therefore never sees a half-swapped document: old chunks and new chunks are never
both live, and the keyword index is told only after the commit that made it true.

### 11. Graph build, part 2 — the serving projection

`publish.py:433` copies every edge touching this document into `graph_serving` with the
**target's** title, canonical version, visibility, allowed groups and effective date
(`libs/schemas/src/kb_schemas/orm.py:254`).

Two tables, two jobs: `document_refs` is the editorial truth (who detected it, who confirmed
it); `graph_serving` is the read-optimized copy that lets graph expansion be filtered by the
same compiled ACL predicate as the main query, with no join (INV-10). Rebuilding it on every
publish is what stops that copy going stale after a reclassification.

### 12. Outbox → keyword index

`services/indexer/src/kb_indexer/consumer.py:59` — `OutboxConsumer.run_once`, run by the
`indexer-consumer` container.

Events are claimed `FOR UPDATE SKIP LOCKED` in batches of 20, ordered by id so a document's
publishes cannot overtake each other. The work is idempotent (index by chunk id, delete the
previous version's), so at-least-once delivery is enough: killing the process mid-publish and
restarting leaves the index correct. Failures keep their attempt count and last error;
`MAX_ATTEMPTS = 10`, then alert rather than retry forever. `publish_to_searchable` measures
the lag against the 10-second budget.

With `pg_search` there is no second store at all — the BM25 index is on the same rows — so
this loop is a no-op refresh rather than a copy. That was the deciding argument in the
bake-off (ADR-0021).

### 13. Re-chunk (maintenance)

`services/registry/src/kb_registry/publish.py:287` — `PublishService.rechunk`, exposed at
`POST /v1/documents/{id}/rechunk` and on the inspection screen.

Same document, same canonical version, same text — only the derived form is rebuilt, because
the chunker or the embedding model improved. It reads the KBDoc back from the version's
`idp_report_ref`, so a document registered without one cannot be re-chunked and says so. No
four-eyes (no new content), no tombstoning (the version is still canonical), one transaction,
audited, same outbox event so downstream indexes converge (ADR-0026).

---

## Part 2 — The read path, traced

One request, followed from the keystroke to the response, with every hop named. The example is
the portal's search box; the chat surfaces join at step 4 and the differences are noted where
they occur.

```
browser  Search.tsx  ──POST /api/v1/search──►  nginx ──►  portal-api
                                                             │  (verifies the token, adds nothing)
                                          ──POST /v1/retrieve──►  retrieval-api
                                                             │
                          FilterBuilder ──► compile_sql ──► keyword ∥ vector (one SQL each)
                                                             │
                                    RRF ──► rerank ──► cap ──► supersession ──► expansion
                                                             │
                                                     audit_log ◄── every chunk id
```

### 1. The browser sends a query and a token

`frontend/portal/src/pages/Search.tsx` collects the query and the facet selects, then calls
`search()` in `frontend/portal/src/api.ts`. Every request goes through one `request()` helper
that attaches `Authorization: Bearer <access token>` from session storage. The browser holds
**no** ACL logic: the facets it sends can only narrow, and the server decides whether even that
is allowed.

`POST /api/v1/search` is same-origin — nginx proxies `/api/` to portal-api (`frontend/portal/nginx.conf`).

### 2. portal-api verifies, and passes through

`services/portal-api/src/kb_portal_api/main.py:291` — `search()`. Three dependencies resolve
before the body runs:

* `current_principal` — verifies the JWT (signature against the realm JWKS, issuer, audience,
  `exp`/`iat`/`sub` required) and builds a `Principal` from its claims: subject, groups, roles,
  kind. Nothing in the request body contributes to identity.
* `bearer_token` — the raw token, because the next hop must carry the *caller's* identity, not
  the service's (INV-3).
* `retrieval` — the `HttpRetrievalClient`.

The body then does the only thing it is allowed to do: pack the facets into a `RetrieveRequest`
and hand it on. Any ranking or trimming here would be a second retrieval implementation, and
the platform's one guarantee is that there is exactly one (INV-1) — `scripts/check_invariants.py`
fails the build if any service outside retrieval-api imports an index client.

### 3. The funnel client refuses to soften a refusal

`libs/clients/src/kb_clients/retrieval.py` — `HttpRetrievalClient.retrieve()` POSTs to
`{KB_RETRIEVAL_URL}/v1/retrieve` with the caller's token and a 15-second timeout. A 401/403
comes back as `AuthzError`, deliberately **not** as an empty result: "no documents found" and
"you may not see this" are different sentences with different consequences.

The chat surfaces reach this same client from `services/chat-api/src/kb_chat_api/service.py:152`
after condensing the conversation into a standalone query — and the internal bot exchanges its
own token for the end user's first (RFC 8693, `libs/authz/src/kb_authz/exchange.py`), so what
arrives here is always a user's identity.

### 4. retrieval-api resolves the principal again

`services/retrieval-api/src/kb_retrieval_api/main.py:103` — `retrieve()`. It verifies the token
**itself** (`current_principal`, line 53) rather than trusting an upstream assertion, then
builds the engine through `engine()` (line 86), which wires the ports config chose:
`PgSearchIndexAdapter` or `PostgresFtsIndexAdapter` for keyword (`KB_KEYWORD_BACKEND`),
`PgVectorIndexAdapter` for vectors, BGE-M3 or the deterministic stand-in for embedding and
rerank, and `SqlAuditSink`.

Everything after this is `RetrievalEngine.retrieve()` at `engine.py:99`.

### 5. Principal → ResolvedFilter

`libs/authz/src/kb_authz/filters.py:205` — `FilterBuilder.build(principal, facets, mode, as_of_date)`.

`base()` (line 119) branches on *what kind of caller this is*:

| Principal kind | Visibilities | Groups | Notes |
|---|---|---|---|
| user | external + internal_all, plus restricted **iff** they hold groups | their groups | line 165 |
| internal bot | the end user's, via `on_behalf_of` | the user's | refuses without a delegate (INV-3) |
| external bot | external only | none | published only, refuses without the scope (INV-4) |
| service | external + internal_all | none | never restricted: no human to hold accountable |

Then `narrow()` (line 251) applies the request's facets. It is monotonic — category narrows to
a subtree of what the principal already had, department and doc-class intersect. A facet that
would *widen* raises `PolicyViolation` before any query runs, because silent clamping hides
probing. `as_of` mode additionally demands the `kb-archive-reader` role and is refused outright
for the external bot (INV-6/INV-4).

The result is frozen and carries `filter_id` — a SHA-256 over its canonical JSON, so the audit
record and the response can be joined months later (INV-11).

### 6. Compiled into SQL, never applied afterwards

`libs/authz/src/kb_authz/compile.py:19` — `compile_sql(filter)` returns a parameterised `WHERE`
fragment plus its bind parameters:

```sql
(c.tombstoned = FALSE)
AND (c.visibility = ANY(:acl_visibilities)
     OR (c.visibility = 'restricted' AND c.allowed_groups && :acl_group_scope))
AND (c.doc_status = ANY(:acl_statuses))
AND (c.effective_from IS NULL OR c.effective_from <= :acl_effective_on)
AND (c.effective_to   IS NULL OR c.effective_to   >= :acl_effective_on)
AND (c.category_path <@ :acl_category_0)      -- only when a facet narrowed it
```

Note `restricted` is *not* in `:acl_visibilities` — holding a group is what unlocks it, and
merging the two would grant every restricted document to anyone in any group.

### 7. Two retrievers, each filtered inside its own statement

`engine.py:323` — `_gather()`, `CANDIDATES_PER_RETRIEVER = 50` from each.

**Keyword** — `libs/ports/src/kb_ports/adapters/pg_search_index.py:85`. The ACL fragment and
the BM25 predicate are in one statement, so the planner evaluates both against the same rows;
there is no order of operations in which a chunk is scored, returned, then filtered. The query
is a boosted boolean over four fields — `text`, `text_folded`, `citation_label`,
`citation_label_folded` — plus a conjunction-mode phrase clause, which is what makes
"an toan von" find "an toàn vốn" while an exactly-typed query still wins. `paradedb.snippet()`
returns the `<mark>`ed highlight.

**Vector** — `pgvector_index.py:49`. `embedder.embed_query(query)` first (the *query* method:
BGE-M3 is asymmetric and mixing the two costs recall silently), then `SET LOCAL hnsw.ef_search
= 200` and a cosine nearest-neighbour scan with the same ACL fragment inline. `ef_search` is
raised on purpose: a filtered ANN walk visits its neighbours *before* the filter, so a
selective ACL under-returns at the default.

Both return `IndexHit` — chunk id, document id, version id, score, text, citation label,
section path, highlights.

### 8. Fuse

`services/retrieval-api/src/kb_retrieval_api/fusion.py:41` — `reciprocal_rank_fusion`,
`RRF_K = 60`. Score is `Σ 1/(k + rank)` across the lists that found the chunk, so agreement
between the two retrievers beats a strong score in one. A chunk found twice is **merged**, not
duplicated, keeping the richest payload (highlights only the keyword engine produces) and
recording which retriever ranked it where — that is what answers "why did this rank here" a
month later. Ties break on chunk id, because an eval run that reorders ties looks like a
regression.

### 9. Rerank, then cap

`engine.py:338` — `_rerank()` sends the top `RERANK_DEPTH = 30` texts to `RerankPort`
(bge-reranker-v2-m3 through the proxy, or the lexical stand-in). The reranked head is put back
in front of the untouched tail rather than replacing the list — dropping the tail would
silently cap recall at 30.

`cap_per_document(ranked, MAX_CHUNKS_PER_DOCUMENT = 3)` then `[:top_k]`. Without the cap one
long regulation fills the result set and the answer rests on a single source, which reads as
confident and is exactly when it is most likely wrong.

### 10. Flags and titles

`_superseded_documents()` (line 368) runs one query: is any *published* document pointing at
these with `amends`/`abrogates`, and is there no `consolidates` edge back? Those documents are
flagged, not hidden — the text is still canonical, and the flag clears when the Legal cell
approves the consolidation. `_titles()` (line 352) fetches display titles; they are not
denormalized onto chunks like the ACL columns, because a title changes without republishing and
a stale one in a citation is worse than an extra query.

Each surviving hit becomes a `RetrievedChunk`: chunk/version/document ids, citation label,
document title, section path, text, score, highlights, `supersession_flag`.

### 11. Graph expansion

`engine.py:406` — `_expand()`, only when the request asked for it. It reads `graph_serving`
(the projection the publish transaction maintains), excludes documents already in the result,
caps at `MAX_EXPANSIONS = 8`, and filters with `compile_sql_graph` — the *target's* ACL,
denormalized onto the edge, so following a reference can never disclose a document the caller
could not have searched for (INV-10).

### 12. Audit, then the response

`_audit_retrieval()` (line 442) writes one `audit_log` row: actor, `on_behalf_of`, every chunk,
version and document id returned, the expansion targets, the full `resolved_filter` including
its `filter_id`, the query text, mode and `top_k`. The query is user text, so it goes in the
access-controlled audit record and never into a metric label or an ordinary log line.

`RetrieveResponse{chunks, expansions, resolved_filter_id}` then returns up the same chain —
retrieval-api → client → portal-api → nginx → `Search.tsx`, which renders citation, snippet
with `<mark>` highlights, and the supersession warning. The `resolved_filter_id` shown at the
bottom of the results is the same string in the audit row.

### The chat flow, traced

The chatbot is the same read path with a model on either end of it: one call before retrieval
to turn a conversation into a query, one after to write the answer, and a verification step
whose job is to catch what the second one made up. `services/chat-api` holds no ACL logic at
all — it passes the caller's token to the funnel and receives whatever that principal is
entitled to.

```
POST /v1/chat/{surface}
   → deployment guard · surface policy · rate limit
   → verify token (+ RFC 8693 exchange when a service account acts for a user)
   → condense conversation → standalone query        [model call 1, validated]
   → retrieve as the end user                        [the whole trace above]
   → assemble numbered passages within a budget
   → refuse if empty · refuse if nothing is relevant
   → generate                                        [model call 2]
   → verify every [n] against its passage
   → refuse if nothing survived
   → PII filter the final text
   → audit: answer, citations, refusal, redaction kinds, model
```

#### 1. Which surface, and may this process serve it

`services/chat-api/src/kb_chat_api/main.py:208` — `chat()`. Three guards before any work:

* `check_deployment_surface()` (line 54) compares `KB_SURFACE` — a property of the
  *deployment* — against the path parameter, which is a property of the *request*. A `dmz`
  process refuses `/v1/chat/internal` however it is reached, so a misrouted request, an open
  port or a container escape on a neighbour still cannot ask the internal bot a question
  (INV-4). Routing is a second control, not the only one.
* `policy_for(surface)` returns the `SurfacePolicy` — the one place every difference between
  the two bots lives.
* The rate limiter (line 125) keys on surface + actor, at the policy's per-minute limit.

#### 2. Who is asking, and on whose behalf

`main.py:171` — `current_caller()`. The token is verified locally, then:

* a **user** token is used as-is;
* a **service account** must send `X-On-Behalf-Of`, and the token is exchanged for the end
  user's (RFC 8693, `libs/authz/src/kb_authz/exchange.py`). The exchanged token is re-resolved,
  and if it comes back without an `act` claim the request is refused — a delegation nothing
  records would attribute a user's reads to nobody (INV-3).

What travels on is a `Caller`: the principal to check policy against, and the token to retrieve
with. They are not always the same token, and that distinction is the whole of INV-3.

#### 3. The policy checks that precede the work

`service.py:95` — `ChatService.answer()` starts with two refusals rather than a computation:

* `policy.check_principal(kind)` — an internal bot on the external surface would answer a
  member of the public with an employee's visibility. Refused by kind, before retrieval, so a
  wrong caller never depends on the corpus happening to come back empty.
* `policy.check_output_filter(pii)` — the public surface will not *begin* an answer it could
  not filter. A detector that reports itself unhealthy, or a stand-in declaring
  `real_rules: false`, would let it answer with the filter present and doing nothing, which
  looks identical in every log and dashboard (INV-7).

#### 4. Condense — the first model call, and the one that is validated

`condense.py:92` — `QueryCondenser.condense(messages, history_turns=policy.history_turns)`.

"Vậy còn ngân hàng nhỏ thì sao?" retrieves nothing on its own. But this output goes straight
into a *search*, so a condenser that invents an entity sends the retriever after something
nobody asked about — and the answer is then perfectly cited to genuinely irrelevant documents.
So the candidate query is checked rather than trusted:

* every word must appear in the conversation, or be a function word from a fixed Vietnamese
  list — a query naming "Thông tư 41" when nobody said 41 is rejected;
* it is capped at 300 characters;
* anything that fails falls back to **the user's last message verbatim**, which is always a
  legal query and never a fabricated one.

Single-turn questions skip the model entirely. The result carries `condensed` and a reason,
both of which reach the audit record: a bad answer to a rewritten question is a different bug
from a bad answer to the question as asked.

#### 5. Retrieve, as the user

`service.py:152` — `_retrieve()` builds `RetrieveRequest(query=condensed, top_k=policy.top_k,
facets=request.facets, expand_graph=policy.expand_graph)` and calls the same
`HttpRetrievalClient` the portal uses, with the caller's token. **Steps 4–12 of the search
trace above run unchanged** — filter, compile, keyword ∥ vector, RRF, rerank, cap, flags,
expansion, audit. The chat service never sees an index and never builds a filter.

It keeps `resolved_filter_id`, the chunk ids and the version ids on the trace, so the answer's
own audit record can be joined to the retrieval that produced its evidence.

#### 6. Assemble the context

`context.py:163` — `assemble(chunks, max_tokens=3000)`. Passages are numbered in retrieval
order — the highest-scoring first, because that is where a model looks — and each one is put
behind its citation label, which is what maps `[3]` back to a version id later.

The budget is spent in **whole passages**: a passage that does not fit is dropped and counted,
never truncated, because half a clause presented as a clause is exactly the failure the
citation machinery exists to prevent. A non-zero `dropped` becomes a visible warning on the
answer.

#### 7. Two refusals, before the model is asked anything

* `context.empty` → refuse. Nothing was retrieved that this caller may see.
* `is_relevant(query, context)` (`context.py:322`) → refuse. Retrieval always returns its best
  guesses; for a question the corpus cannot answer, those guesses are simply the least-bad
  documents, and quoting one produces a fluent, citable answer to a question nobody asked. The
  floor is lexical and honest about it: at least two shared substantive terms and a best-passage
  match ratio ≥ 0.48, with question words absent from every passage counting against it.

The refusal text is **fixed policy text, not generated** — a model asked to explain why it
cannot answer will speculate about the answer.

#### 8. Generate — the second model call

`service.py:171` — `_generate()`. The prompt comes from `policy.prompt()`
(`services/chat-api/prompts/grounded_answer_{internal,external}.md`) with `{{context}}` and
`{{question}}` substituted, sent as **one user message** at `temperature=0.0` and the policy's
`max_answer_tokens`. Not a system message the document text could appear to close: the prompt
and the context arrive as a single block, and the instructions say plainly that context is data.

#### 9. Verify every citation

`context.py:195` — `verify(answer, context)`. Three failures, three treatments:

| The model did | What happens |
|---|---|
| cited `[7]` when six passages were supplied | marker stripped from the text |
| cited a passage that does not support the sentence | citation dropped, sentence keeps its text but loses the claim to authority |
| produced no surviving citation at all | the answer becomes a refusal |

Support is decided per sentence: shared substantive vocabulary above `SUPPORT_THRESHOLD =
0.18`, at least two shared terms for a sentence long enough to have them, and **numbers checked
separately from words** — "tối thiểu là 8%" and "tối thiểu là 12%" share every word that
matters. A marker is accepted if the sentence it sits in *or* the one it follows is supported,
because both are ordinary citation style.

None of this makes a model truthful. It makes an untruthful one visible.

#### 10. Filter, warn, record

`service.py:191` — `_finish()`:

* `pii.redact(text)` runs on the **final** text, after every other step has finished editing it
  (INV-7). Only the *kinds* removed are recorded — never the values.
* Warnings are attached: a supersession notice if any cited chunk carries the flag, and a
  partial-context notice if `assemble` dropped a passage.
* `_record()` (line 237) writes one `audit_log` row per answer *and per refusal*: actor and
  `on_behalf_of`, the answer id, surface, chunk and version ids, `filter_id`, the query as
  searched, whether it was condensed and why, the surviving citations, the unknown and
  unsupported markers, the refusal reason, the redaction kinds, the model and whether it
  leaves the network, and the latency. An audit trail that recorded only the answers that
  worked would be a record of the times nothing went wrong (INV-11).

The response is `{answer, citations[], answer_id, resolved_filter_id, warnings, refused}`.

#### Every difference between the two bots, in one table

`surfaces.py:100` — the policies are data, and nothing downstream branches on which surface it
is serving. A difference that is not a field here does not exist.

| | internal | external |
|---|---|---|
| allowed principals | user, internal bot | external bot only |
| `top_k` | 8 | 5 |
| answer length | 800 tokens | 400 |
| history kept | 6 turns | 4 (the rest dropped, not summarized) |
| rate limit | 30/min | 10/min |
| graph expansion | yes | no |
| PII filter required to start | no | **yes** |
| refusal text | "…kho tài liệu bạn được phép truy cập…" | "…thông tin công bố…" |

The access decisions are deliberately *not* here: who may see what is decided by
`FilterBuilder` from the verified principal (INV-2/3/4). A policy field that widened retrieval
would be a hole in the funnel. What lives here is conduct.

## Where the data lives

| Table | Holds | Written by |
|---|---|---|
| `documents` | identity, category, ACL, canonical pointer | registry service |
| `document_versions` | immutable versions, PII verdict, retention | registry service |
| `chunks` | text, **`embedding vector(1024)`**, denormalized ACL + facets | publish transaction only |
| `document_refs` | edges with provenance (`detected_by` / `confirmed_by`) | IDP, then stewards |
| `pending_document_refs` | references whose target is not in the registry yet | IDP; drained on arrival |
| `graph_serving` | edges + target ACL, for filtered expansion | publish transaction |
| `outbox` | published events | publish transaction |
| `audit_log` | every retrieval, publish, decision (INV-11) | all services |
| `review_tasks` | human queues | workflow activities |

Indexes that matter (`ops/alembic/versions/0001_initial_schema.py`,
`0002_search_support.py`, `ops/pg_search/install.sql`):

- `ix_chunks_embedding_hnsw` — HNSW, cosine, `m = 16`, `ef_construction = 64`
- `chunks_bm25` — pg_search BM25 over `text`, `text_folded`, `citation_label`,
  `citation_label_folded`, `section_path`
- `ix_chunks_fts` — GIN over `to_tsvector('simple', kb_unaccent(text))` for the fallback
- `ix_chunks_allowed_groups`, `ix_graph_serving_groups` — GIN, so the ACL predicate is indexed
- `ix_documents_legal_number_trgm`, `ix_chunks_citation_trgm` — trigram, for identity matching
  and citation lookup

`chunks.text_folded` and `citation_label_folded` are **generated columns** applying
`kb_unaccent`: Tantivy's tokenizers are byte-oriented and know nothing about Vietnamese
diacritics, so folding has to exist as data. That is what lets "an toan von" find "an toàn
vốn" while a correctly-typed query still ranks exact matches first.

---

## One real trace

The `QT-2026-032` document used to verify the platform on 2026-08-12:

```
POST /v1/uploads                     → workflow ingest-beae52dc…, hash beae52dc…
IngestWorkflow  parsing              → txt parser → KBDoc, 9 blocks, kb-derived/…kbdoc.json
                registering          → document 94090393…, version db3aec3d…, pii_status=pending
                pii_scan             → 6 blocks scanned, 0 findings → clear
                awaiting_review      → idp_review task on dept/operations
POST /v1/review-tasks/…/decision     → approve → prepare(): 4 chunks + 4 vectors
                                     → publish(): canonical flip, 4 chunks written, 0 tombstoned
                                     → outbox registry.published
POST /v1/documents/…/rechunk         → 4 → 4 chunks, same version, 0 tombstoned, audited
GET  /v1/documents/…/inspect         → versions 1 · chunks 4 · edges 0 · warnings 0
```

Chunk `#1` came back as `Điều 2 · 318 chars · vector=True`, `#2` as `Điều 12 · 261 chars` —
the structural boundary doing its job on a document whose articles are numbered 1, 2, 6, 12,
15.

---

## What breaks, and where you would see it

| Symptom | Where to look |
|---|---|
| document stuck in `parsing` | worker log; storage adapter mismatch between API and worker |
| published but nothing retrievable | `chunks` for the version; then the outbox backlog gauge |
| retrievable but never ranked | `embedding IS NULL` on the chunks — visible on the inspection screen |
| answer quotes the wrong article | chunk boundary; inspect the chunk list, then re-chunk |
| "cites" edge nobody expected | `document_refs.detected_by = 'idp'`, unconfirmed — confirm or remove it |
| search worse locally than in prod | `KB_MODEL_DETERMINISTIC_FALLBACK` is on (ADR-0027) |
| BM25 results stale after a purge | `make keyword-maintenance` — `VACUUM` does not clear it (ADR-0021) |
