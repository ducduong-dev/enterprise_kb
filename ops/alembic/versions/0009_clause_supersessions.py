"""One clause replaced by another.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-16

The expiry ledger records that a rule *ended*. This records what took its place — which is the
difference between "khoản 2 Điều 12 hết hiệu lực từ 01/01/2026" and "…; quy định hiện hành là
Điều 7 Thông tư 15/2025". The first is a dead end for the reader; the second is an answer.

Same shape and same discipline as `document_expiry` (ADR-0030), deliberately: append-only,
`created_at`/`closed_at` for when the platform believed it, one open row per scope, and only
`confirmed` is ever served. A steward reading the two tables side by side should not have to
learn two mental models for what is the same kind of record — a decision about a rule, made by
somebody, on evidence, at a time.

**Anchored on the section path at both ends, never on a chunk id.** `_insert_chunks` deletes and
re-inserts every chunk of a version on each publish and each rechunk, so a chunk id here is a
dangling pointer one rechunk later. The section path is what the corpus itself uses to address a
clause, and it is the one identifier present on *every* chunk — the dotted `anchor` is NULL for
front matter, an appendix, a rate-schedule item or a procedure's "Bước 3", all of which can be
superseded like anything else.

`new_section_path` is nullable: a pure abrogation ends a clause and puts nothing in its place,
and recording that as a supersession with an invented replacement would be a lie the answer
would then repeat.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clause_supersessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # The clause that was replaced.
        sa.Column(
            "old_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("old_section_path", sa.Text(), nullable=False),
        # …and the one that replaced it. Both NULL together for a pure abrogation.
        sa.Column(
            "new_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("new_section_path", sa.Text(), nullable=True),
        sa.Column("basis", sa.Text(), nullable=False),
        #: When the replacement took effect. A confirmed supersession is not true "from now" —
        #: it is true from the newer clause's own effective date, so an `as_of` query before it
        #: still shows the older clause as current and unflagged (ADR-0033).
        sa.Column("supersedes_from", sa.Date(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        #: Which of ADR-0033's four buckets the adjudication reached. NULL on the declared path,
        #: where the corpus stated the answer and nothing was adjudicated.
        sa.Column("verdict", sa.Text(), nullable=True),
        #: "8%/năm → 10%/năm". What makes a steward's queue workable; a similarity score is not.
        sa.Column("quantity_delta", postgresql.JSONB(), nullable=True),
        sa.Column("scope_facets", postgresql.JSONB(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        #: The sentence it was read from, on the declared path. The whole reason confirming one
        #: is a glance rather than a document read (ADR-0029's discipline, again).
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("detected_by", sa.Text(), nullable=False),
        sa.Column("confirmed_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "basis IN ('declared','edge_article','detected','steward')",
            name="ck_clause_sup_basis",
        ),
        sa.CheckConstraint(
            "state IN ('proposed','confirmed','revoked')", name="ck_clause_sup_state"
        ),
        # A replacement is a document *and* a location, or neither. Half of one would render as
        # "replaced by …" with nothing after it.
        sa.CheckConstraint(
            "(new_document_id IS NULL) = (new_section_path IS NULL)",
            name="ck_clause_sup_replacement_is_whole",
        ),
        # A clause cannot replace itself.
        sa.CheckConstraint(
            "new_document_id IS NULL OR new_document_id <> old_document_id "
            "OR new_section_path <> old_section_path",
            name="ck_clause_sup_not_self",
        ),
    )

    # "The open row is the current belief", per replaced clause. A clause has one answer to
    # "what replaced this" at a time; a second decision closes the first rather than sitting
    # beside it.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_clause_sup_open
            ON clause_supersessions (old_document_id, old_section_path)
            WHERE closed_at IS NULL
        """
    )
    # The retrieval-side lookup: "are any of these documents' clauses superseded?"
    op.create_index(
        "ix_clause_sup_serving",
        "clause_supersessions",
        ["old_document_id", "state"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    # And the steward queue, which is worked newest-first per state.
    op.create_index("ix_clause_sup_queue", "clause_supersessions", ["state", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_clause_sup_queue", table_name="clause_supersessions")
    op.drop_index("ix_clause_sup_serving", table_name="clause_supersessions")
    op.execute("DROP INDEX IF EXISTS uq_clause_sup_open")
    op.drop_table("clause_supersessions")
