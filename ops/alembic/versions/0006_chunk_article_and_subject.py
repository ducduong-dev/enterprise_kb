"""The article number and subject key, on the chunk.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-16

Two denormalized columns that several features have been re-deriving, or doing without.

**`article`.** An amendment usually touches two or three articles of a sixty-article circular,
and `document_refs.articles` has recorded which since M2 — the impact traversal already filters
on it. The supersession predicate ignored it and flagged every chunk of the target, so a reader
got a warning on all sixty, including the fifty-seven the amendment never mentions. The
predictable result is that the warning stops being read (ADR-0032).

`kb_vntext.sections` parses `Điều N` into the section path and `diff._article_number` recovers
the integer from a path; neither the SQL layer nor the retrieval hot path should be re-deriving
it with a regex over a text column, and the reference resolver in ADR-0036 needs to *join* on
it. So it becomes a column, written by the chunker.

**`subject_key`.** The normalized heading chain, boilerplate stripped — what two clauses about
the same rule share even when they are worded differently. Its consumers are ADR-0033's
detection funnel and ADR-0037's fact sets; it lands here rather than in a later migration
because both columns come from the same rechunk, and rechunking a 3,000-document corpus twice
to add one column each time is a waste of a day.

Populating them is a migration plus a corpus rechunk. `PublishService.rechunk` exists for
exactly this case (ADR-0026): the derived form changed and the text did not, so no four-eyes
and no PII re-run. Existing rows keep NULL until then, and NULL means "the whole document is
flagged" — today's behaviour, so nothing regresses while the backfill runs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("article", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("subject_key", sa.Text(), nullable=True))

    # The reference resolver's join: "the chunks of Điều 12 of that document, in order"
    # (ADR-0036). One composite index rather than a projection to keep in step.
    op.create_index(
        "ix_chunks_document_article",
        "chunks",
        ["document_id", "article", "ordinal"],
        postgresql_where=sa.text("NOT tombstoned"),
    )
    # The detection funnel's lexical channel and the fact-set lookup both start here.
    op.create_index(
        "ix_chunks_subject_key",
        "chunks",
        ["subject_key"],
        postgresql_where=sa.text("subject_key IS NOT NULL AND NOT tombstoned"),
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_subject_key", table_name="chunks")
    op.drop_index("ix_chunks_document_article", table_name="chunks")
    op.drop_column("chunks", "subject_key")
    op.drop_column("chunks", "article")
