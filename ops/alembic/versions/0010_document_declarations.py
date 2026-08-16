"""What a document says it does to another, between reading it and deciding on it.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-16

`find_declarations` reads a sentence; ADR-0040 says a *confirmed* declaration writes two
records — an expiry ledger row for the clauses that end, and a `clause_supersessions` row for
what replaced them. Between those two moments the declaration has to live somewhere, and it is
neither of the things it will become.

Same reasoning as `pending_document_refs`, which is a separate table because "an edge with one
endpoint is not an edge". A declaration nobody has confirmed is not an expiry and not a
supersession, and putting it in either table would mean every consumer of those tables
remembering to exclude it.

It also absorbs the waiting case at no extra cost. `target_document_id` is NULL while the
instrument being declared against is not in the registry — routine in a corpus digitised in
whatever order it yields, where the amending instrument frequently arrives first (ADR-0028) —
and is filled in when that document is registered. One table, two states of the same fact,
rather than a pending table and a real one that must be kept in step.

Anchors are stored as read, not resolved to section paths. At detection the target may not be
published and may have no chunks at all; resolution happens at confirmation, when the chunks
certainly exist and the anchor machinery can do an equality join against them (ADR-0036).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_declarations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "src_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        #: As the sentence wrote it, for display.
        sa.Column("target_legal_number", sa.Text(), nullable=False),
        #: `normalize_legal_number` of the above — what the arrival lookup matches on, so
        #: "41/2016/TT-NHNN" and "41 / 2016 / TT-NHNN." find each other. Computed by the
        #: Python normalizer and nowhere else; a key computed in SQL silently disagreed about
        #: `NĐ-CP` vs `ND-CP`, which is the lesson `relink_references.py` records.
        sa.Column("target_key", sa.Text(), nullable=False),
        #: NULL while the target is not in the registry.
        sa.Column(
            "target_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("target_anchors", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("replacement_anchors", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        #: The sentence. Not nullable: a declaration without the words it was read from cannot
        #: be confirmed at a glance, which is the entire economics of this path (ADR-0039).
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("block_id", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("detected_by", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('abrogates','replaces','amends')", name="ck_decl_kind"),
        # `waiting` is the parked state; `open` is ready for a steward. `applied` means the two
        # records exist and this row is history — kept, because "who confirmed that Điều 12 was
        # repealed, and on what sentence" must be answerable from one table.
        sa.CheckConstraint(
            "state IN ('waiting','open','applied','rejected')", name="ck_decl_state"
        ),
        sa.CheckConstraint(
            "src_document_id <> target_document_id", name="ck_decl_not_self"
        ),
    )

    # Idempotence on re-ingest: the same sentence read twice is one declaration. Anchors are
    # part of the key because one closing article legitimately declares several changes against
    # the same instrument — "bãi bỏ Điều 5" and "bãi bỏ khoản 2 Điều 12" of the same circular.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_declaration
            ON document_declarations (src_document_id, target_key, kind,
                                      COALESCE(target_anchors, '{}'::text[]))
        """
    )
    # The arrival lookup: one document is registered, which declarations were waiting for it?
    op.create_index(
        "ix_declarations_waiting",
        "document_declarations",
        ["target_key"],
        postgresql_where=sa.text("target_document_id IS NULL"),
    )
    # The steward queue, worked one declaring document at a time (ADR-0039's batch screen).
    op.create_index(
        "ix_declarations_queue", "document_declarations", ["state", "src_document_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_declarations_queue", table_name="document_declarations")
    op.drop_index("ix_declarations_waiting", table_name="document_declarations")
    op.execute("DROP INDEX IF EXISTS uq_declaration")
    op.drop_table("document_declarations")
