# ADR-0022 — The public surface is contained four times over

**Status:** accepted · **Date:** 2026-08-11

## Context

INV-4 says the external bot's scope is bound to its service account server-side. That control
is real, tested by the ACL sweep, and single: one `FilterBuilder` branch decides what a
stranger on the internet may read. Every other control in the platform has at least two
implementations of its guarantee — the publish transaction has a row lock *and* a partial
unique index, the PII gate has a code guard *and* a database trigger.

The public surface is the one place where a mistake is unrecoverable: internal text quoted to
an anonymous caller cannot be un-quoted, and the bank finds out from the caller.

## Decision

Four controls, each of which fails independently, and none of which is sufficient alone:

1. **The filter** (`kb_authz.FilterBuilder`, INV-4). The scope comes from the verified account;
   the request has no field that could widen it.
2. **The database role** (`ops/dmz/external-role.sql`). The DMZ connects as `kb_external`:
   SELECT on four tables, INSERT on the audit log, and row-level security that shows it
   published external rows and nothing else. A filter bug is then a *bad public answer*, not a
   disclosure — the connection cannot read the row at all.
3. **The deployment surface** (`KB_SURFACE=dmz`). The public process refuses `/v1/chat/internal`
   whoever reaches it. Routing is not the control; the process is.
4. **The gateway** (`ops/dmz/nginx.conf`). One method, one path, a per-IP rate limit, a 16 KB
   body cap, and the delegation header stripped so a public caller cannot even attempt to act
   for a named user.

**Amended 2026-08-11 (ADR-0024).** The DMZ segment now has a second bridge besides the
database: `dmz-models`, reaching the LiteLLM proxy. A public answer needs a model, and routing
that call through the same proxy is what keeps one place deciding where model traffic goes —
the alternative was a second model client, with its own credential, inside the DMZ. The
segment still reaches nothing else: not the portal, not Temporal, not MinIO, not the internal
chat, and not the model servers directly. `scripts/dmz_check.py` asserts both halves — that the
proxy *is* reachable, and that the rest still is not.

`scripts/dmz_check.py` asks all four directly against the running profile, and its output is
evidence in the sign-off pack rather than a claim in it.

## Why row-level security rather than a separate database

A replica containing only public rows was the alternative: stronger isolation, and a second
copy of the corpus to keep in step. The copy is the problem — the moment a document's
visibility changes, two systems disagree about what is public, and the window is exactly the
one INV-6 exists to close. RLS keeps one copy, one publish path, and one moment at which a
document becomes public.

The cost is that the control lives in the same cluster it protects. That is a real limitation:
a Postgres superuser compromise defeats it, as does any RLS bypass. It is defence in depth,
not a boundary — the network segment is the boundary.

## Consequences

* The DMZ can write the audit trail and cannot read it (INV-11 still holds for public answers;
  a public endpoint that could enumerate the bank's audit records would be a reporting channel
  for an attacker). One consequence found while testing: `SqlAuditSink` must not use
  `RETURNING id`, because a column-level grant would also permit `count(*)`.
* `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` applies to `chunks`, `documents` and
  `document_refs` in every deployment, including internal ones. The platform's own role is
  unaffected by policy, but the setting is now part of the schema's behaviour and a migration
  that disabled it would silently remove control 2.
* Running the public surface needs one more deployment step (`make dmz-role`), and a
  deployment that skips it fails closed: the role does not exist, so the DMZ cannot connect.
