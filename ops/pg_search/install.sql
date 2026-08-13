-- pg_search (ParadeDB) keyword backend — installed only where that engine is the choice.
--
-- Deliberately *not* part of the Alembic chain while [OPEN]-2 is open. The bake-off candidate
-- that loses gets deleted, and a core migration adding a column and a BM25 index for a backend
-- nobody runs is a schema everyone pays for and one deployment uses.
--
-- Idempotent: safe to run on every deploy of a pg_search installation.

CREATE EXTENSION IF NOT EXISTS pg_search;

-- The diacritic-folded companion of `chunks.text`.
--
-- Vietnamese is searched both ways: "an toàn vốn" typed correctly, and "an toan von" typed on
-- a keyboard without tone marks. BM25 tokenizers match bytes, so folding has to exist as data.
-- A *generated* column rather than a trigger, so it cannot drift from the text it folds, and
-- `kb_unaccent` is the immutable wrapper the FTS backend already uses.
ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS text_folded text
    GENERATED ALWAYS AS (kb_unaccent(text)) STORED;

ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS citation_label_folded text
    GENERATED ALWAYS AS (kb_unaccent(coalesce(citation_label, ''))) STORED;

-- One BM25 index over the searchable fields. `key_field` is the row identity pg_search scores
-- and snippets against.
--
-- The ACL columns are deliberately absent: the filter is an ordinary SQL predicate in the same
-- query (INV-2), evaluated by the planner against the same rows, so duplicating it into the
-- search index would be a second copy of an authorization decision — exactly what the single
-- funnel exists to prevent.
CREATE INDEX IF NOT EXISTS chunks_bm25 ON chunks
    USING bm25 (id, text, text_folded, citation_label, citation_label_folded, section_path)
    WITH (key_field = 'id');

-- Tombstoned chunks are never served (INV-6). They stay in the table for the audit trail, and
-- the ACL predicate excludes them, so no partial index is needed here — this comment exists so
-- the next person does not add one and quietly change what "unreachable" means.

-- Maintenance note, learned the hard way (ADR-0021):
--
-- After a bulk delete — a retention purge, a corpus reload — `paradedb.score()` was observed
-- raising `assertion failed: item_pointer_is_valid(ctid)` on every query until the index was
-- rebuilt. `VACUUM (ANALYZE)` did not clear it; `VACUUM FULL` (heap rewrite + reindex) did.
-- Run `make keyword-maintenance` (REINDEX INDEX CONCURRENTLY chunks_bm25) after any bulk
-- delete, and prefer tombstoning to deleting, which is what the publish path already does.
