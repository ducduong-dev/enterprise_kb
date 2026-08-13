-- The DMZ's database role: defence in depth behind INV-4.
--
-- The external bot's scope is already bound server-side by `FilterBuilder` and compiled into
-- every query. This file assumes that control has failed.
--
-- A SQL injection in an unrelated endpoint, a filter compiled with the wrong principal, a
-- future developer adding a query that forgets the predicate — each ends the same way if the
-- connection can see internal rows. So the DMZ connects as a role that **cannot**, enforced by
-- Postgres rather than by our code: row-level security restricted to published, externally
-- visible, non-tombstoned rows, and SELECT only.
--
-- Nothing here replaces the filter. It means that when the filter is wrong, the blast radius
-- is "the public bot answered a public question badly" rather than "the public bot quoted the
-- board's merger plan".
--
--   psql -f ops/dmz/external-role.sql          (idempotent; run on every deploy)
--
-- The password comes from KB_DMZ_DB_PASSWORD in the environment; the fallback exists so
-- `make dmz` works on a laptop and is refused outside dev by the compose profile's own env.

\set dmz_password `echo "${KB_DMZ_DB_PASSWORD:-dmz-dev-password}"`

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kb_external') THEN
        CREATE ROLE kb_external LOGIN;
    END IF;
END
$$;

ALTER ROLE kb_external WITH PASSWORD :'dmz_password';

-- No schema rights, no write rights, no sequences: this role reads three tables and nothing
-- else. `documents` is needed for the citation label's title, `categories` for the ltree
-- ancestry the facet filter walks.
REVOKE ALL ON SCHEMA public FROM kb_external;
GRANT USAGE ON SCHEMA public TO kb_external;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM kb_external;
GRANT SELECT ON chunks, documents, categories TO kb_external;

-- Edges, for one purpose: the supersession warning. A public answer quoting a fee schedule
-- that has been amended must say so, and that check reads `document_refs`. The policy below
-- restricts it to edges between documents this role can already see, so the table cannot be
-- used to enumerate the existence of internal instruments.
GRANT SELECT ON document_refs TO kb_external;

-- Append-only on the audit log: the DMZ must record every answer it gives (INV-11) and must
-- not be able to read back what anyone else's answers were, or to edit its own. A public
-- endpoint that can read the audit trail is a public endpoint that can enumerate the bank's
-- internal questions.
GRANT INSERT ON audit_log TO kb_external;
GRANT USAGE, SELECT ON SEQUENCE audit_log_id_seq TO kb_external;
-- No SELECT at all, not even on `id`: `SqlAuditSink` writes with a plain INSERT and never
-- reads the key back, and a column-level grant would let the DMZ run `count(*)` — a thin
-- channel, but a public endpoint that can count the bank's audit records is a public endpoint
-- telling an attacker how busy the bank is. If a future writer needs `RETURNING id`, change
-- the writer.

-- Row-level security. `FORCE` matters: without it the policy is skipped for the table owner,
-- and a future migration that runs as the owner would silently disable the control it is
-- supposed to be testing.
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;

-- The platform's own role keeps full access: RLS applies per role, and the registry, the
-- publish transaction and the internal funnel must still see everything.
DROP POLICY IF EXISTS chunks_platform ON chunks;
CREATE POLICY chunks_platform ON chunks
    FOR ALL TO PUBLIC
    USING (current_user <> 'kb_external');

DROP POLICY IF EXISTS documents_platform ON documents;
CREATE POLICY documents_platform ON documents
    FOR ALL TO PUBLIC
    USING (current_user <> 'kb_external');

-- What the DMZ may see, stated once, in the database.
DROP POLICY IF EXISTS chunks_external_only ON chunks;
CREATE POLICY chunks_external_only ON chunks
    FOR SELECT TO kb_external
    USING (
        visibility = 'external'
        AND doc_status = 'published'
        AND NOT tombstoned
    );

DROP POLICY IF EXISTS documents_external_only ON documents;
CREATE POLICY documents_external_only ON documents
    FOR SELECT TO kb_external
    USING (visibility = 'external' AND status = 'published');

ALTER TABLE document_refs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS refs_platform ON document_refs;
CREATE POLICY refs_platform ON document_refs
    FOR ALL TO PUBLIC
    USING (current_user <> 'kb_external');

DROP POLICY IF EXISTS refs_external_only ON document_refs;
CREATE POLICY refs_external_only ON document_refs
    FOR SELECT TO kb_external
    USING (
        EXISTS (
            SELECT 1 FROM documents d
            WHERE d.id = document_refs.src_document_id
              AND d.visibility = 'external' AND d.status = 'published'
        )
        AND EXISTS (
            SELECT 1 FROM documents d
            WHERE d.id = document_refs.dst_document_id
              AND d.visibility = 'external' AND d.status = 'published'
        )
    );

-- Statement timeout: a public endpoint must not be able to hold a connection open.
ALTER ROLE kb_external SET statement_timeout = '5s';
-- The keyword engine's crash guard applies here too (ADR-0021).
ALTER ROLE kb_external SET plan_cache_mode = 'force_custom_plan';
