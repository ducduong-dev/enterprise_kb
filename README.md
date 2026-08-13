# Knowledge Base Platform

Enterprise knowledge base for a Vietnamese bank: ingestion of scanned and digital documents,
a versioned document registry with consolidation of amended regulations, ACL-aware hybrid
retrieval, and grounded chatbots for internal and external surfaces.

Authoritative plan: [`kb-implementation-plan.md`](kb-implementation-plan.md) — locked
decisions, invariants, milestones. Read sections 1 and 2 before writing code; they are
constraints, not suggestions. Decisions taken while building are recorded in
[`docs/decisions/`](docs/decisions/).

How a document travels through the system — upload, parsing, chunking, embedding, the graph,
the publish transaction, and every step of retrieval, with the code that does each — is
[`docs/data-flow.md`](docs/data-flow.md). Working on the code itself:
[`docs/developing.md`](docs/developing.md).

**Current state: M0–M8 complete.** Every milestone in the plan has been built and its
acceptance criteria met; what remains is the operational backfill of the 3,000-document corpus
and the `[OPEN]` decisions that need a business ruling.

## Ingest pipeline (M1)

```
portal upload → object storage (content-hash key) → IngestWorkflow
   → IDP (docx/xlsx/pptx/pdf-native/html/txt → KBDoc)
   → registry (document + version, retention set, pii_status=pending)
   → reference edges for instruments we already hold
   → review task on the category steward's queue
```

An upload whose legal number already exists attaches to that document and opens an
identity-review task rather than forking the instrument's history.

## Scanned documents (M3)

```
image-only PDF → rasterize (300 dpi) → OpenCV: trim border, deskew, denoise, binarize
   → PaddleOCR (vi) + PP-Structure
   → score the page: engine confidence × diacritic plausibility × garbage ratio × coverage
   → below threshold? re-read the page with Qwen2.5-VL
   → KBDoc: same contract a DOCX produces, plus page, bbox, confidence and engine per block
   → review editor: page image beside the text, low-confidence blocks highlighted
   → corrections create a new version (ADR-0012) → publish
```

The confidence score exists because engine confidence alone lies in the way that matters here:
a page whose Vietnamese tone marks were lost comes back *confidently wrong*, producing
plausible words with different meanings. A low diacritic ratio on a page claiming to be
Vietnamese is therefore treated as a page-level failure and escalated, whatever the engine
reports.

Scanned text is provisional until a human has looked at it. The review editor shows what the
machine read next to the page it read it from, highlights what it was unsure of, and records
who decided what.

## The PII gate (M4)

```
parsed document → pattern rules (CCCD, CMND, PAN+Luhn, account, tax, phone, email,
                                 address, date of birth, name+balance)
                → LLM judgement for PII that has no shape
                → clear | blocked | pending   →  written onto the version
blocked → Compliance queue → override needs the role, a written justification, an audit record
```

Patterns are the floor and the model may only add findings (ADR-0013): a prompt-injected
document cannot talk its way past a Luhn-valid card number, and a model that times out leaves
the scan *incomplete*, which is not the same as clear. A `pending` version cannot be published
by any path.

The same detector serves the chatbot's output filter, so an answer cannot disclose what
ingestion refused to publish. It redacts rather than refusing — an answer with an identifier
removed is still an answer, and refusing outright sends users somewhere the data leaks anyway.

## Grounded chatbot (M6)

```
messages → condense to a standalone query (rejected if it invents an entity)
   → /v1/retrieve as the *end user* (the bot's own account sees nothing, INV-3)
   → assemble numbered passages with document title + citation label
   → relevance floor: no passage about the question → refuse, do not answer
   → generate (GenerationPort) → verify every [n] against its passage
   → PII output filter (the ingestion detector, INV-7)
   → {answer, citations[], answer_id} + audit record naming both parties (INV-11)
```

A citation that points at nothing is stripped, one whose passage does not support its sentence
is dropped, and an answer left with none becomes the surface's refusal (ADR-0018). Numbers are
checked separately from words: "tối thiểu là 8%" and "tối thiểu là 12%" share every word that
matters, and in a bank the figure is the claim.

An internal connector authenticates with its own service account and names the user it is
acting for; chat-api exchanges that token (RFC 8693) before retrieving anything, and a refused
exchange fails the request rather than answering as the bot (ADR-0020). Both surfaces run one
pipeline — `services/chat-api/src/kb_chat_api/surfaces.py` holds every difference between them,
so "did we harden the public surface?" has an answer you can read in one file.

**M6 acceptance** runs on every commit: 60 graded questions (`eval/answers`) scored for
faithfulness, and 20 red-team prompts (`eval/chat_redteam`) asked as authenticated employees.
Disclosure — a canary token, a document the principal may not read, a personal identifier —
fails the run. The CI generator quotes rather than predicts (ADR-0019), so the numbers measure
the pipeline; questions needing semantic matching or model judgement are reported as *not
measured* rather than quietly passing.

## Consolidation of amended regulations (M5)

```
new version of a known instrument
   → identity match (exact number → normalized number → fuzzy + title → review)
   → section diff, aligned on Chương/Điều/Khoản paths, not on position
   → classifier: unchanged in substance | amended | new or abrogated
   → drafter: one section at a time → văn bản hợp nhất (a draft, never a publication)
   → merge_review task for the Legal cell → approval signals
   → publish (the same transaction as every other publish) + `consolidates` edge
   → impact traversal → a task for every document that implements or cites what moved
```

The merge screen shows three panes per changed section — current canonical, incoming text, and
the drafted consolidation — with the word-level diff between the first two. Sections are
ordered by substance, not by article number, and anything the model did not actually decide is
marked as inferred (ADR-0016).

Four-eyes is a rule, not a button (INV-8). `ApprovalLedger` closes the three holes it fails
through in practice: the preparer approving their own work, one person approving twice, and an
approval arriving for a draft that has since been redrafted. The portal enforces the rules so
the approver gets a reason, the workflow enforces them again and owns the publish (ADR-0017).

Approving the consolidation is what clears the supersession warning, because the
`consolidates` edge is written in the same transaction as the canonical flip (ADR-0015).
Impact tasks go to the *impacted* document's steward — the person who owns the policy is the
one who can say whether it still means what it says — and are filtered by article, so a policy
implementing Điều 12 is not woken up because Điều 6 changed.

## Publish and retrieval (M2)

```
review approved → chunk + embed (outside the transaction)
   → ONE transaction: canonical flip · old chunks tombstoned · new chunks written
                      · graph_serving rebuilt · audit record · outbox event
   → indexer mirrors canonical chunks into the keyword index (≤ 10 s, measured)

query → FilterBuilder(principal) → keyword ∥ vector, both filtered inside the query
      → RRF → rerank → per-document cap → supersession flags → graph expansion
      → audit record naming principal, delegate, filter and every chunk returned
```

Chunks are cut at clause/article boundaries so every result carries a citation a compliance
officer can quote (ADR-0008). An amended regulation is **flagged, not hidden** — its text is
still canonical, and the warning clears when the consolidation is approved (ADR-0015).

Backends are selected by config, not code: `KB_KEYWORD_BACKEND` picks `pg_search` (the M7
bake-off winner, ADR-0021) or the Postgres FTS fallback for a node without the extension, and
the embedding/rerank ports fall back to deterministic local adapters when no GPU node is
present. Those fallbacks declare `semantic: false` so an eval report cannot be mistaken for a
production measurement.

## Checking the machine's work

`/documents/:id` in the portal shows what a steward otherwise cannot see: the chunks retrieval
will quote, the edges the pipeline detected, and the amendment chain this document sits in.

The graph is reviewed **one document at a time** — this document in the middle, what points at
it on the left, what it points at on the right, grouped by reference type (ADR-0026). There is
no whole-corpus picture: every question a reviewer actually has is local and typed, and a
force-directed hairball of three thousand instruments answers none of them. Each edge carries
its provenance separately — `detected_by` (the pipeline) and `confirmed_by` (a person) are
never merged into one "verified" flag — and the reviewer can confirm it or remove it. Edges
whose other end is outside the caller's ACL are counted, never named.

Chunks are shown and never edited: they are derived from an immutable version (INV-9) and
rewritten whole by the publish transaction (INV-5). The one write offered is **re-chunk**,
which runs the canonical version's text through chunking and embedding again in a single
transaction — for a chunker or embedding-model change, not for a correction. Wrong *text*
still means a new version (ADR-0012).

## Quick start

```bash
make install          # sync the uv workspace (Python 3.12)
make dev              # postgres (pgvector+pg_search), minio, temporal, keycloak, migrations
make seed             # categories, fixture corpus, the three ACL canaries
make check            # lint, types, architectural invariants, tests
```

`make dev-all` adds the services and the portal; `make observability` adds Prometheus,
Grafana and Loki; `make gpu` starts the model servers (needs an NVIDIA runtime).
`make help` lists everything.

One thing a first run needs on a machine with no GPU node: set
`KB_MODEL_DETERMINISTIC_FALLBACK=true` in `.env`: the embedding, rerank and generation ports
become the deterministic local adapters, which declare `semantic: false` / `real_model: false`
so nothing they produce can be mistaken for a measurement. Everything else — storage, auth,
the database, the workflows — stays real.

The portal is the whole stack's front door: its nginx serves the bundle, proxies the API at
`/api` and Keycloak at `/realms`, so **one forwarded port is enough** to use it from a laptop
(VS Code port forwarding, a jump host, a codespace). Browsing from another machine's own
address instead? Set `KB_PUBLIC_ORIGIN` in `.env` and rebuild the portal — the OIDC issuer is
stamped from it, and a token minted for one origin does not verify against another.

| Service | URL |
|---|---|
| Portal (and the API, and sign-in) | http://localhost:5173 |
| Keycloak admin console | http://localhost:8080 (`admin`/`admin`) |
| MinIO console | http://localhost:9001 |
| Temporal UI | http://localhost:8088 |
| LiteLLM proxy | http://localhost:4000 (`make models`) |

Fixture users live in `ops/keycloak-realm/kb-realm.json`, password `dev`, and mirror the
principals in `libs/authz/src/kb_authz/fixtures.py`.

## Layout

```
libs/       common (config, logging, errors, audit) · schemas (domain, KBDoc, ORM)
            authz (principals, tokens, token exchange, FilterBuilder)
            ports (index/model/storage interfaces) · clients (the retrieval funnel)
            vntext (legal numbers, Chương/Điều/Khoản structure, language detection)
services/   registry · idp · pii-gate · identity-merge · indexer
            retrieval-api · chat-api · portal-api · workflows
frontend/   portal (React + TypeScript + TipTap)
ops/        alembic · docker · keycloak-realm · pg_search (engine install) · litellm (model routes)
            dmz (role, gateway) · backup (+ drill) · loadtest · prometheus · grafana · loki
eval/       golden_set (graded queries) · pii_redteam (40 blocked + 40 clean)
            answers (60 graded answers) · chat_redteam (20 attacks)
            external_redteam (30 public attacks) · harness (metrics + runners) · bakeoff
docs/       decisions/ (ADRs)
```

## How the invariants are enforced

The plan's twelve invariants are the platform's guarantees. Each has a mechanism, not just a
convention:

| Invariant | Enforced by |
|---|---|
| INV-1 single retrieval funnel | `scripts/check_invariants.py` (blocking in CI) — only `retrieval-api` and `indexer` may import an index client or issue a vector-distance query |
| INV-2 mandatory server-side filter | `kb_authz.FilterBuilder` builds from the verified principal; `kb_authz.compile` puts it in the WHERE clause / filter context; widening is unrepresentable in `RetrieveRequest` |
| INV-3 on-behalf-of internal bot | the bot's own account raises `PolicyViolation`; chat-api exchanges its token before retrieving (ADR-0020); delegation comes from the RFC 8693 `act` claim |
| INV-4 hard-scoped external bot | scope bound to the service account, `external` + `published` only, archive access refused; plus a SELECT-only database role with row-level security, a process bound to the public surface, and a gateway routing one path (ADR-0022) |
| INV-5 atomic publish | one transaction with the document row locked; chunk write, tombstoning, graph rebuild and outbox insert all in the same commit (ADR-0009) |
| INV-6 canonical-only serving | superseded chunks tombstoned in the publish transaction and deleted from the keyword index; `as_of` needs the archive role and is audited |
| INV-7 PII gate fails closed | pattern rules the model cannot override (ADR-0013); an incomplete scan leaves the version `pending`; publish guard in code **and** a DB trigger on the canonical flag; overrides need the role, a justification and an audit record |
| INV-8 no automation for regulated classes | `NO_AUTOMATION_CLASSES` guard in the publish service; `ApprovalLedger` for merges — two approvers, never the preparer, never the same person twice, never a stale draft |
| INV-9 immutable versions, retention hold | triggers rejecting content updates and unauthorized deletes; reviewer corrections create a new version rather than editing one (ADR-0012) |
| INV-10 edges target documents | schema, plus `graph_serving` carrying the target's ACL |
| INV-11 reconstructable answers | `audit_log` table with principal, delegate, resolved filter, chunk/version IDs; every chat answer *and refusal* logs its answer id, condensed query, citations and pruned markers |
| INV-12 models behind ports | `libs/ports` + the same invariant lint |

The **ACL sweep** (`make acl-sweep`) runs every fixture principal against the seeded corpus
and fails on any retrieval of the three canary documents. It is blocking in CI. The canaries
are ACL'd to a group no principal holds, so a hit means the filter was escaped, not that a
grade was wrong.

The **parser goldens** (`services/idp/tests/goldens/`) pin the twelve fixture documents' block
structure, section paths, detected references and a SHA-256 of their text. The hash is the
diacritic guard: any normalization or re-encoding introduced anywhere in the parse chain
changes it. Regenerate deliberately, and read the diff:

```bash
KB_UPDATE_GOLDENS=1 uv run pytest services/idp/tests/test_parsers_golden.py
```

**The scanned corpus** (`services/idp/src/kb_idp/testing/scans/`) is twenty generated
documents with known ground truth, damaged the way the archive's documents are damaged, plus
OCR recordings keyed by the hash of each preprocessed page image (ADR-0011). CI runs the whole
scanned route against them without a GPU; a change to rasterization or preprocessing changes
the keys and fails loudly rather than replaying stale output. Regenerate deliberately:

```bash
python services/idp/src/kb_idp/testing/make_scans.py
```

**The PII red-team corpus** (`eval/pii_redteam/`) is forty documents that must be blocked and
forty that must pass. Both halves are the criterion (ADR-0014): a gate that blocks regulatory
text teaches reviewers that a block means nothing, and the one document that mattered goes
through with the rest. Compliance owns the corpus; a new false positive is an entry there
before it is a code change.

**Answer faithfulness and the chat red team** (`make chat-eval`) grade the pipeline rather
than the model: 60 questions declaring whether the corpus can answer them and which instrument
must be cited, and 20 attempts to talk the bot past its instructions. Both run in CI as
`tests/test_chat_quality.py`, and both distinguish what was measured from what the
deterministic adapters cannot exercise (ADR-0019).

**Retrieval quality** (`make eval`) runs the golden set through the funnel as each query's
fixture principal, scoring recall@10 and nDCG@10 *and* checking every result against that
query's forbidden list. Quality and access are one run because they are one question: the
right documents for the right person. The gate is recall@10 ≥ 0.85 with zero violations, and
it runs in CI as `tests/test_retrieval_quality.py`.

## The public surface (M8)

```
internet → nginx (one path, per-IP limit, 16 KB body, delegation header stripped)
        → chat-api  KB_SURFACE=dmz  — refuses the internal surface, whoever asks
        → retrieval-api (its own instance)
        → postgres as kb_external — SELECT only, row-level security: published + external
        → litellm (dmz-models) — the only other bridge out of the segment
```

Four controls, each failing independently (ADR-0022): the scope bound to the service account
(INV-4), a database role that cannot read an internal row at all, a process bound to the public
surface, and a gateway that routes one method on one path. `make dmz-check` asks all four
directly and writes its answers into the sign-off pack.

The public surface will not answer without a working PII filter — not "answers unfiltered", but
refuses to begin (INV-7). Rates and fees are published as structured items, so a fee answer
carries its own effective date rather than depending on a header the chunk no longer contains;
they are `customer_facing`, the class that can never publish itself (INV-8).

**M8 acceptance** — `make external-redteam`: thirty prompts asked as the public account,
scored as *disclosure* (internal content, a canary, a personal identifier) and *conduct* (a
commitment, advice, an undated figure, a competitor comparison). Both fail the build
(ADR-0023). `uv run python scripts/export_signoff.py` exports the Compliance and Legal pack:
every externally visible document with its approvals, the conduct prompt with its hash, the
surface configuration, each control with the file that implements it, and the red-team and
isolation evidence. It reports findings rather than hiding them — the current pack flags that
the seeded corpus has no approval trail, which is exactly what it is for.

## Models (one proxy, four roles)

```
generation · vision · embedding · rerank
        → LiteLLM proxy (ops/litellm/config.yaml) → vLLM · TEI · a public provider
```

The platform knows roles; the proxy knows providers (ADR-0024). Changing which model answers is
a line of YAML and a restart — no adapter changes, no new protocol, one credential store, one
place to see spend. `KB_MODEL_USE_PROXY=false` keeps the direct vLLM/TEI paths for a deployment
that has not adopted it.

Two things stay on the platform's side of that line: whether a call may leave the bank, and
recording that it did. `kb_ports.proxy.route` refuses an external model until
`KB_MODEL_ALLOW_EXTERNAL_PROCESSING` is set ([OPEN]-1) and fails at start-up rather than on the
first document, and every adapter's `info` carries `leaves_network` into the IDP report and the
answer's audit record.

**Scanned pages** are read one of two ways (ADR-0025). `KB_MODEL_OCR_ENGINE=paddle` is the
default — OCR everything, escalate what the confidence scorer flags. `KB_MODEL_OCR_ENGINE=vlm`
transcribes every page with the vision model instead, which is what the degraded end of the
archive needs and what a public vision model is currently best at; the report then names the
model and says whether page images left the network.

## Performance and recovery (M7)

```
make dmz          # the public surface in its own segment, with its own database role
make dmz-check    # prove the isolation: role, process, gateway, network
make external-redteam  # thirty public attacks: disclosure and conduct
make signoff      # the Compliance/Legal pack, with the evidence attached
make bakeoff      # keyword engines: gates first, then quality, latency, freshness
make loadtest     # 50 concurrent users against the running stack, through the funnel
make backup       # postgres + object storage + a manifest to restore against
make backup-drill # restore into a scratch database and prove the corpus still works
```

Measured on the seeded corpus scaled to 3,600 chunks at 50 concurrent, with the connection
pool sized to the concurrency — the first bake-off run measured the pool rather than the
engines, which is a production sizing lesson as much as a benchmarking one:

| | pg_search | OpenSearch |
|---|---|---|
| publish → searchable | 14 ms | 838 ms |
| keyword p50 / p95 | 45 / 118 ms | 35 / 75 ms |
| hybrid p50 / p95 | 125 / 195 ms | 285 / 537 ms |
| recall@10 (vi / en) | 1.000 / 0.667 | 1.000 / 0.667 |

Two defects in the chosen keyword engine came out of this milestone, both now guarded: a
generic query plan segfaulted the Postgres backend on the eleventh execution of any
parameterised BM25 query (`plan_cache_mode = force_custom_plan`, with a regression test), and
a bulk delete left the BM25 index returning `item_pointer_is_valid` assertions until it was
rebuilt (`make keyword-maintenance`). Both are written up in ADR-0021 — the second reason for
running a bake-off is finding out what an engine does when you push it.

The backup drill is not "did the file appear": it restores into a scratch database and then
checks the row counts against the manifest, that exactly one version per document is canonical
(INV-5), that no live chunk belongs to a superseded version (INV-6), that retrieval answers,
and that the canaries are still unreachable (INV-2). It has already earned its place — the
first run failed because a ParadeDB dump restored into a database created from `template1`
collides with the preinstalled extension, which is the kind of thing one discovers either in a
drill or during an incident.

## Open decisions

Tracked as `[OPEN]` in the plan; all are behind interfaces, none block progress.

1. **Processing outside the bank** — local models vs a public provider, pending Compliance.
   Routing lives in the proxy (ADR-0024); the platform refuses any route that leaves the network
   until `KB_MODEL_ALLOW_EXTERNAL_PROCESSING` is set, and records `leaves_network` on every
   answer and every transcription. Default local. This now covers scanned pages as well as
   answers (ADR-0025) — a page image is the document itself.
2. ~~**Keyword engine**~~ — **resolved in M7: `pg_search`** (ADR-0021). Both engines passed
   the Vietnamese tokenization gate; freshness (14 ms vs 838 ms), hybrid p95 (195 ms vs 537 ms)
   and having one ACL surface instead of two decided it. The OpenSearch adapter is deleted;
   `KB_KEYWORD_BACKEND` still selects between `pg_search` and the FTS fallback.
3. **Existence disclosure per category** — column exists, defaults to `false` (deny by
   "not found").
4. **Retention periods per class** — `KB_RETENTION_*`, seeded conservatively at 10 years.
5. **SBV IT-classification obligations** — ops config stays declarative under `ops/`.

## Contributing

Start with [`docs/developing.md`](docs/developing.md) — how to run the stack, what the
mechanically-enforced rules are, how to add an adapter or a prompt, and the traps this codebase
has already found.

Every PR runs `make check`. A change touching retrieval, ACLs, publish or the PII gate needs
the relevant invariant test extended, and a structural choice needs an ADR.
