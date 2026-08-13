"""References that point at a document the bank does not hold yet.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-12

A reference is detected when the *citing* document is ingested, and it can only become an edge
if its target is already in the registry. In a corpus digitised in arbitrary order that is the
minority case: an amending decree routinely arrives before the instrument it amends, and until
now the reference was reported once on a review task and then lost. The graph therefore
depended on ingest order, which nobody controls.

`pending_document_refs` is where such a reference waits. It is promoted to a real edge the
moment a document with that legal number is registered, and the wait is visible rather than
implicit — a steward can ask what the corpus is missing, which is the same question as "what
would this graph say if we finished the backfill".

Kept deliberately separate from `document_refs`: an edge has two endpoints, and a row with a
NULL endpoint would mean every consumer of the graph has to remember to exclude it (INV-10).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_document_refs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "src_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        #: As detected, for display.
        sa.Column("target_legal_number", sa.Text(), nullable=False),
        #: `normalize_legal_number` of the above — the column the arrival lookup matches on, so
        #: "41/2016/TT-NHNN" and "41 / 2016 / TT-NHNN." find each other.
        sa.Column("target_key", sa.Text(), nullable=False),
        sa.Column(
            "ref_type",
            postgresql.ENUM(name="ref_type", create_type=False),
            nullable=False,
        ),
        sa.Column("articles", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column("detected_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "src_document_id",
            "target_key",
            "ref_type",
            name="uq_pending_ref",
        ),
    )
    # The arrival lookup: one document is registered, which pending references does it satisfy?
    op.create_index("ix_pending_refs_target", "pending_document_refs", ["target_key"])

    # No data backfill here on purpose: `scripts/relink_references.py` re-parks references
    # from what ingest already recorded and promotes the ones whose target has since arrived.
    # A backfill you can only run once, inside a schema migration, is a backfill you cannot
    # re-run after fixing the corpus.


def downgrade() -> None:
    op.drop_index("ix_pending_refs_target", table_name="pending_document_refs")
    op.drop_table("pending_document_refs")
