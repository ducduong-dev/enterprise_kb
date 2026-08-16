"""Clause anchors on a reference.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-16

`document_refs.articles` is `INT[]`, which is coarser than both ends it sits between.
Vietnamese instruments cite clauses and points — *"khoản 2 Điều 12"*, *"điểm a khoản 3 Điều 8"*
— and since migration 0007 the chunk carries its own clause-precise address. The column between
them was the only coarse thing in the chain, so a reference to one clause of Điều 12 resolved to
all four (ADR-0036).

`articles` stays, and stays authoritative for the question it answers: the impact traversal asks
"does this policy implement anything that moved?", which is genuinely article-shaped. It is
derived from the anchors from now on, so the two cannot disagree.

`pending_document_refs` gets the column too. A reference waiting for a document the bank does not
hold yet must not lose its precision while it waits — it is promoted to a real edge unchanged,
and an anchor dropped at parking time is one nobody can recover afterwards (ADR-0028).

Existing rows keep NULL. NULL means "the whole document", which is today's behaviour, so nothing
regresses while `scripts/backfill_ref_anchors.py` re-reads the corpus.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_refs", sa.Column("anchors", postgresql.ARRAY(sa.Text()), nullable=True)
    )
    op.add_column(
        "pending_document_refs", sa.Column("anchors", postgresql.ARRAY(sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("pending_document_refs", "anchors")
    op.drop_column("document_refs", "anchors")
