"""The clause anchor, on the chunk.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-16

ADR-0036 says a reference resolves by "an equality join on `(dst_document_id, article)`".
That is exact for an anchor of `"12"` — every chunk of Điều 12, in `ordinal` order — and not
exact for `"12.2"`, because every clause of Điều 12 carries `article = 12`. The ADR's own
worked example ("`"12.2"` resolves to one") needs clause granularity on the chunk, and 0006
only put the article there.

So the chunk carries its full dotted address as well. `article` stays: it answers the
article-shaped question the supersession flag and the impact traversal ask, and deriving one
from the other keeps both honest — the same reasoning ADR-0036 gives for keeping `articles`
beside `anchors` on the edge.

Both columns are pure functions of `section_path`, which is already stored, so
`scripts/backfill_chunk_article.py` fills this one too — no rechunk, no re-embedding.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("anchor", sa.Text(), nullable=True))
    # The clause-precise resolution join. Ordinal is in the index because an article that
    # spans several chunks resolves to several anchors and must come back in document order.
    op.create_index(
        "ix_chunks_document_anchor",
        "chunks",
        ["document_id", "anchor", "ordinal"],
        postgresql_where=sa.text("anchor IS NOT NULL AND NOT tombstoned"),
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_document_anchor", table_name="chunks")
    op.drop_column("chunks", "anchor")
