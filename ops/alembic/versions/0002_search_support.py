"""Search support: full-text index, chunk ordering, publish bookkeeping.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-10

Adds what the M2 retrieval path needs on top of the section-4 schema:

* an immutable `kb_unaccent` wrapper so a diacritic-insensitive full-text index is possible at
  all — `unaccent()` is STABLE, not IMMUTABLE, and Postgres refuses it in an index expression;
* a GIN index over that expression, which is what makes the Postgres keyword adapter usable
  (the CI/dev keyword backend, and the pg_search bake-off's baseline);
* `ordinal` and `page` on chunks, so results can be read back in document order and linked to
  a page image in the review editor (M3);
* `published_at` on versions, which is what publish-to-searchable latency (INV-5, ≤ 10 s) is
  measured from.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # unaccent() is STABLE because its dictionary can be redefined. We pin the dictionary by
    # naming it explicitly and mark the wrapper IMMUTABLE, which is what lets it be indexed.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION kb_unaccent(text) RETURNS text AS $$
            SELECT public.unaccent('public.unaccent'::regdictionary, $1)
        $$ LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
        """
    )

    op.add_column("chunks", sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("chunks", sa.Column("page", sa.Integer(), nullable=False, server_default="1"))
    op.create_index("ix_chunks_order", "chunks", ["version_id", "ordinal"])

    # 'simple' rather than a language configuration: Vietnamese has no Postgres stemmer, and
    # stemming English inside a bilingual corpus would help one language and hurt the other.
    op.execute(
        """
        CREATE INDEX ix_chunks_fts ON chunks
        USING GIN (to_tsvector('simple', kb_unaccent(text)))
        """
    )
    # Citation lookup ("Điều 12 Thông tư 41/2016/TT-NHNN") matches on the label, fuzzily.
    op.execute(
        "CREATE INDEX ix_chunks_citation_trgm ON chunks USING GIN (citation_label gin_trgm_ops)"
    )

    op.add_column(
        "document_versions", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("document_versions", "published_at")
    op.execute("DROP INDEX IF EXISTS ix_chunks_citation_trgm")
    op.execute("DROP INDEX IF EXISTS ix_chunks_fts")
    op.drop_index("ix_chunks_order", table_name="chunks")
    op.drop_column("chunks", "page")
    op.drop_column("chunks", "ordinal")
    op.execute("DROP FUNCTION IF EXISTS kb_unaccent(text)")
