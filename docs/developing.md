# Developing on this platform

A guide for the engineer who has just cloned the repository and has a ticket to work on. The
[README](../README.md) says what the system does; this says how to change it without breaking
the guarantees it makes.

Read [`kb-implementation-plan.md`](../kb-implementation-plan.md) sections 1 and 2 first. They
are twenty minutes and they are the constraints — everything below assumes you know what the
twelve invariants are.

---

## 1. Get it running

```bash
make install                     # sync the uv workspace (Python 3.12)
cp .env.example .env             # never commit a real one
make dev                         # postgres, minio, temporal, keycloak, migrations, keyword engine
make seed                        # categories, fixture corpus, the three ACL canaries
make check                       # lint, types, invariants, unit tests
make test-all                    # add the integration tests — closer to what CI runs
```

`make dev-all` adds the services and the portal; `make observability` adds Prometheus, Grafana
and Loki; `make gpu` starts the model servers and needs an NVIDIA runtime. `make help` lists
everything.

| What | Where |
|---|---|
| Portal | http://localhost:5173 |
| Keycloak | http://localhost:8080 — `admin`/`admin` |
| MinIO console | http://localhost:9001 |
| Temporal UI | http://localhost:8088 |
| LiteLLM proxy | http://localhost:4000 — `make models` |
| retrieval-api / chat-api / portal-api | :8006 / :8007 / :8008 |

Fixture users live in `ops/keycloak-realm/kb-realm.json` (password `dev`) and mirror
`libs/authz/src/kb_authz/fixtures.py`. Use them; do not invent principals in tests.

**The database image is ParadeDB** (Postgres 16 + pgvector + pg_search). That is deliberate:
the keyword engine CI exercises is the one production runs (ADR-0021). A plain
`pgvector/pgvector:pg16` also works if you set `KB_KEYWORD_BACKEND=postgres_fts`; the
`pg_search` tests then skip and say so.

---

### Running it without a GPU

`make dev-all` brings up the whole stack against the deterministic model adapters if `.env`
sets `KB_MODEL_DETERMINISTIC_FALLBACK=true`. That flag is the *only* thing you should change
for a laptop: `KB_ENV=test` looks like it does the same job and does not — it also redirects
object storage to a local directory, so the workflow worker and the API stop agreeing about
where the bytes are, and an upload dies in `parsing` with "object not found".

The browser only ever talks to the portal's origin: nginx serves the bundle, proxies `/api` to
portal-api and `/realms` to Keycloak. So the bundle carries no hostname (`VITE_API_BASE=/api`,
`VITE_OIDC_ISSUER=/realms/kb`, resolved against `window.location.origin`), and the stack works
through a single forwarded port. `KB_CORS_ORIGINS` on portal-api remains for the `npm run dev`
workflow, where the Vite server *is* a second origin.

`KB_PUBLIC_ORIGIN` is the one knob: it sets Keycloak's `KC_HOSTNAME` and every service's
`KB_OIDC_ISSUER`, while the services reach Keycloak in-network for keys and token exchange
(`KB_OIDC_JWKS_URL`, `KB_OIDC_TOKEN_ENDPOINT`). Getting that split wrong is what makes the
login button appear dead — see `ops/keycloak-realm/README.md`.

---

## 2. How the repository is laid out

The end-to-end journey of a document — which module owns which step, and why each boundary is
where it is — is [`data-flow.md`](data-flow.md). Read it before changing anything on the
ingest or retrieval path; this section is only the map of *where things live*.

```
libs/       common    config, logging, errors, audit, db, metrics, app factory
            schemas   domain enums, KBDoc, ORM models, wire contracts
            authz     principals, token verification and exchange, FilterBuilder, compilers
            ports     the interfaces + their adapters (index, model, storage)
            clients   the retrieval funnel client — the only way to read documents
            vntext    legal numbers, Chương/Điều/Khoản structure, language detection
services/   registry · idp · pii-gate · identity-merge · indexer
            retrieval-api · chat-api · portal-api · workflows
frontend/   portal (React + TypeScript)
ops/        alembic · docker · keycloak-realm · pg_search · dmz · backup · loadtest
            prometheus · grafana-dashboards · loki
eval/       golden_set · answers · pii_redteam · chat_redteam · external_redteam
            harness (the runners) · bakeoff
docs/       decisions (ADRs) · signoff · this file
tests/      cross-service acceptance flows, one file per milestone criterion
```

Each `libs/*` and `services/*` is a uv workspace member with its own `pyproject.toml`. Add a
dependency in the member that needs it, then `uv sync --all-packages`.

Dependencies point one way: `services/*` may import `libs/*`; `libs/*` may import each other
(`ports` uses `authz` and `vntext`); nothing in `libs/` imports a service. `portal-api` imports
`kb_registry` as a library rather than calling it over HTTP (ADR-0006), which is the one
service-to-service exception and it is deliberate.

---

## 3. The rules you cannot break

These are enforced mechanically. If you find yourself working around one, you have found
either a design gap worth an ADR or a bug worth a test — not a rule to loosen.

**INV-1 — one retrieval funnel.** Every read path goes through `retrieval-api`. Only
`retrieval-api` and `indexer` may import an index adapter or write a vector-distance query.
`scripts/check_invariants.py` fails the build otherwise. From any other service, use
`kb_clients.retrieval`.

**INV-2 — the filter is built server-side.** `FilterBuilder` builds a `ResolvedFilter` from a
*verified* principal; `kb_authz.compile` turns it into SQL. Request models have no field that
could widen it — facets narrow by set intersection (ADR-0004). Never filter after the fact:
returning rows and then dropping them leaks existence through counts.

**INV-5 — publish is one transaction.** Canonical flip, chunk tombstoning, new chunks,
`graph_serving`, audit record and outbox event all commit together. Chunking and embedding
happen *outside* it (ADR-0009), because holding a transaction open across a GPU call turns a
model hiccup into a registry-wide lock.

**INV-7 — the PII gate fails closed.** Nothing publishes while `pii_status != clear`
(or `overridden`, which required a human, a justification and an audit record). The same
detector filters chat output.

**INV-8 — regulated classes never auto-publish.** `regulatory` and `customer_facing` need a
human who is not the author.

**INV-11 — answers are reconstructable.** Every retrieval and every chat answer *and refusal*
writes an audit record naming principal, delegate, resolved filter, chunks, versions and the
answer id.

**INV-12 — models behind ports.** No `openai`, `paddleocr`, `transformers` import outside
`libs/ports/adapters/`. Same lint as INV-1.

Run them: `make invariants`.

---

## 4. Working on a change

### Everyday loop

```bash
make test                        # unit tests, no infrastructure
make test-all                    # everything, needs `make dev`
uv run pytest services/chat-api -q          # one service
uv run pytest tests/test_dmz.py -q -k role  # one test
make lint typecheck invariants   # the fast gates
make acl-sweep                   # the blocking ACL gate
```

Tests are marked: `integration` needs a live Postgres (and object storage for a few),
`acl_sweep` is the blocking invariant sweep and is a subset of the others rather than a
separate suite. `make test` excludes integration so it runs anywhere; CI runs everything, then
runs the ACL sweep again on its own so its failure is unmissable in the log.

### Test conventions that matter here

* **Name the behaviour, not the function.** `test_a_bot_with_no_delegation_cannot_use_the_internal_surface`, not `test_check_principal`.
* **Use the fixture principals.** `ALL_PRINCIPALS` in `kb_authz.fixtures` covers 14 kinds
  including the zero-visibility bot and the external bot. A test that invents a principal is
  testing a world that does not exist.
* **Integration tests use `t_` categories.** Anything under a category starting with `t_` is
  disposable; `conftest.py` purges it through the *production purge path* (INV-9), so the
  cleanup exercises the guard rather than bypassing it.
* **If your test commits, expect company.** Several tests must commit (publish consistency is
  about what other connections see). The quality gates use the `pristine_corpus` fixture,
  which purges test data at the moment they run — otherwise a fixture from an earlier test
  sits in the index competing for the top of every result list.
* **Assert on the invariant, not the implementation.** `assert exc.value.detail["invariant"] == "INV-7"` survives a refactor; asserting a message does not.

### Migrations

```bash
make revision M="add whatever"   # autogenerate
make migrate                     # apply
make check-drift                 # fail if the ORM and the migrations disagree
```

ORM models live in `libs/schemas/src/kb_schemas/orm.py` (ADR-0002). Declare every constraint
in `__table_args__` — `alembic check` compares against the ORM, and a constraint that exists
only in a migration reads as drift forever.

Objects the *database* owns rather than the model — the pg_search BM25 index, its generated
columns, PostGIS tables — are listed in `ops/alembic/env.py` so `alembic check` ignores them.
Without that list, a ParadeDB node reports the production keyword index as drift, and the first
person to believe it drops it.

### Model calls

Four roles — generation, vision, embedding, rerank — all go through the LiteLLM proxy
(ADR-0024). An adapter never hardcodes an endpoint: it asks `kb_ports.proxy.route(role)` for a
base URL, a model alias, a credential and whether that route leaves the bank's network, and
records the last of those in its `info`. `ops/litellm/config.yaml` is where a role becomes a
provider; `KB_MODEL_USE_PROXY=false` restores the direct vLLM/TEI paths.

A route that leaves the network is refused at resolution unless
`KB_MODEL_ALLOW_EXTERNAL_PROCESSING=true` ([OPEN]-1) — so a misconfigured proxy fails when a
service starts, not when it meets its first document.

### Adding an adapter

Ports are Protocols in `libs/ports/`; adapters register themselves:

```python
@register_adapter(PortName.KEYWORD_INDEX, "my_engine")
def build_my_engine(session: Session) -> MyEngineAdapter:
    return MyEngineAdapter(session)
```

Then `KB_KEYWORD_BACKEND=my_engine` selects it. Three rules:

1. **The ACL predicate comes from `kb_authz.compile`.** Never hand-write it. If your engine
   cannot filter inside its query, it cannot be an adapter here. A *model* adapter takes its
   endpoint from `kb_ports.proxy.route` for the same reason: one place decides where a call
   goes and whether it may go there.
2. **Declare what you are in `info.extra`.** The deterministic CI adapters say
   `semantic: false`, `real_model: false`, `real_rules: true`. Downstream code and eval reports
   read those; a stand-in that claims to be real is how a green run comes to mean nothing.
3. **Adapters are imported for their registration side effect only.** Nothing outside
   `libs/ports/adapters/` imports one directly, except the two services allowed to.

### Adding a prompt

Prompts live in `services/*/prompts/*.md`, versioned in the file, with `{{placeholders}}` the
caller substitutes. Every prompt pairs with an eval case. If the model's output is parsed,
handle failure explicitly and *degrade with a flag* rather than raising — see
`kb_identity_merge.merge` (falls back to the mechanical diff, marks it `inferred`) and
`kb_chat_api.condense` (falls back to the user's own words).

### Adding an endpoint

Use the shared app factory (`kb_common.app.create_app`) so you get request-id logging, the
`PolicyViolation` handler and `/metrics`. Resolve the principal through the service's `auth`
dependency; never construct one from request data. If the endpoint reads documents, call the
funnel client with the *caller's* token — services do not assert identities of their own.

### Changing what a document is indexed as

Chunks are derived data. Nothing outside `PublishService` may write them, and nothing at all
may edit one in place: the version is immutable (INV-9) and the chunk set is replaced whole,
inside the publish transaction (INV-5). If your change alters chunking or embedding, existing
documents keep their old chunks until something re-runs the step — that something is
`PublishService.rechunk()`, exposed as `POST /v1/documents/{id}/rechunk` and as a button on
`/documents/:id`. It rebuilds the canonical version's chunks in one transaction, tombstones
nothing (the version is still canonical), needs the steward role rather than four eyes, and
emits the same `document.published` outbox event so downstream indexes converge (ADR-0026).

A change to the document's *text* is never this. It is a new version, through review and
approval, like every other publication (ADR-0012).

The screen that shows all of it — chunks, typed edges with provenance, the amendment lineage —
is `frontend/portal/src/pages/DocumentDetail.tsx` over `kb_portal_api.inspect`. The graph is
drawn ego-centrically, in a computed SVG with no physics: a deterministic picture two reviewers
can talk about, capped at `MAP_NODE_LIMIT` neighbours, after which the typed lists carry it.

---

## 5. The evaluation harnesses

These are gates, not reports. The first four run in CI through a pytest wrapper that fails the
build; the last four are operator-run and produce evidence (the bake-off's comparison table,
the load report, the drill's checks, the DMZ pack) — `tests/test_dmz.py` and
`tests/test_backup_drill.py` hold the parts of those that can run unattended.

| Harness | Command | What it holds |
|---|---|---|
| Retrieval quality + ACL sweep | `make eval` | recall@10 ≥ 0.85, zero canary hits |
| PII red team | `make redteam` | 40 dirty documents blocked, 40 clean ones pass |
| Answer faithfulness + chat red team | `make chat-eval` | ≥ 0.95 faithful, no disclosure |
| External conduct red team | `make external-redteam` | 30 public attacks: no disclosure, no conduct breach |
| Keyword bake-off | `make bakeoff` | the [OPEN]-2 comparison (decided; re-runnable) |
| Load | `make loadtest` | 50 concurrent users, p95 budgets |
| Backup | `make backup-drill` | a restore that still answers, with its ACL intact |
| DMZ isolation | `make dmz-check` | four independent controls on the public surface |

**They run against deterministic adapters.** Hashed embeddings, a lexical reranker, recorded
OCR, and a generator that quotes instead of predicting (ADR-0019). That is what makes them
reproducible; it is also why questions needing semantic matching or model judgement are
reported as *not measured* rather than scored. When you add a question that only a real model
can answer, mark it `requires: semantic` or `requires: model` — do not lower a threshold.

---

## 6. Things that will bite you

Learned the hard way; each has a comment in the code and usually an ADR.

**`paradedb.score()` segfaults under a generic query plan.** A parameterised BM25 query
survives ten executions and kills the backend on the eleventh, taking the cluster into
recovery. `create_db_engine` sets `plan_cache_mode = force_custom_plan` where pg_search is the
backend; `test_pg_search_survives_a_generic_plan` guards it (ADR-0021).

**A bulk delete leaves the BM25 index inconsistent.** `VACUUM (ANALYZE)` does not clear it;
`REINDEX` does. Run `make keyword-maintenance` after a retention purge or a corpus reload. The
publish path tombstones rather than deletes, so ordinary operation never hits this.

**`websearch_to_tsquery` ANDs everything.** The Postgres FTS adapter ORs terms deliberately; a
five-word Vietnamese question would otherwise match nothing.

**Diacritics are load-bearing.** Fold for *matching*, never for storage or display.
`kb_unaccent` is the immutable wrapper that makes folded matching indexable. The parser
goldens hash document text precisely so a stray normalization anywhere in the chain fails
loudly.

**`"khoản"` is a substring of `"tài khoản"`.** The PII detector matches whole words for exactly
this reason: substring matching once disqualified every account number by way of its own label
(ADR-0014).

**Logging `extra={"filename": ...}` used to crash the request.** `KBLogger` prefixes reserved
`LogRecord` attributes with `field_`. Log freely, but know why that exists.

**Test data pollution is real.** A committed fixture from an earlier test file changes what
retrieval returns. If a quality test passes alone and fails in the suite, that is what
happened — use `pristine_corpus`.

**`SET TRANSACTION ISOLATION LEVEL` must precede the first statement.** The publish path uses a
row lock instead (ADR-0009), which composes with callers that have already read.

---

## 7. When to write an ADR

Write one in `docs/decisions/` when a choice constrains someone else later: a new invariant
mechanism, a schema shape, a dependency direction, a threshold with a security consequence, a
deliberate trade. Number sequentially; state the context, the decision, what would reverse it,
and the consequences you accept. Twenty-nine exist — read a couple before writing your first
(0021 is a good model: it carries the measurements the decision rests on).

You do not need one for a bug fix, a refactor with no external effect, or a test.

---

## 8. Before you open a pull request

```bash
make check      # lint, typecheck, invariants, tests
make acl-sweep  # blocking
```

Then check the three things CI cannot:

1. **Does the change touch a read path?** Extend the ACL sweep with the principal that would
   have been wrong.
2. **Does it touch publish, the PII gate, or approvals?** Add the test for the state you are
   now able to reach — including the one you expect to be refused.
3. **Did you make a structural choice?** ADR.

CI additionally runs `alembic check`, a migrate/downgrade/migrate round trip, the eval
harnesses, the ACL sweep on its own, and the frontend's lint, typecheck and build. Nothing in
it is advisory. It runs one database configuration — ParadeDB with `pg_search` — so if you
change anything the `postgres_fts` fallback touches, run the suite once against a plain
`pgvector/pgvector:pg16` with `KB_KEYWORD_BACKEND=postgres_fts` before merging.

---

## 9. Where to look when something breaks

| Symptom | Look at |
|---|---|
| A search returns nothing | the resolved filter in the audit record (`resolved_filter.filter_id`), then the principal's groups |
| A published document is not searchable | `kb_outbox_unprocessed`, then `make index` |
| A chat answer is a refusal you did not expect | `chat_answer` audit detail: `unsupported_markers`, `refusal_reason`, the condensed query |
| An answer cites nothing | citation verification pruned it — the passage did not support the sentence, or the number was not in it (ADR-0018) |
| `alembic check` reports drift | a constraint declared in a migration but not in the ORM, or an engine-owned object missing from `ops/alembic/env.py` |
| The suite passes alone and fails together | committed test data; use `pristine_corpus` |
| Postgres restarts mid-test | the pg_search generic-plan crash — check `plan_cache_mode` |

Logs are JSON with a `request_id` per request; `make logs SVC=retrieval-api` tails one service.
Every answer carries an `answer_id` a user can quote, and the audit record it names contains
everything needed to reproduce the retrieval that produced it.
