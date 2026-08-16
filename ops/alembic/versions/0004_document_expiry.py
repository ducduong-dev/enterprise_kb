"""The expiry ledger.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-16

`document_versions.effective_to` and its denormalized copy on `chunks` have existed since the
initial schema, and the ACL predicate has always read them — set the column and the chunk stops
being retrievable that day, inside the query. Nothing ever wrote it. The serving half of expiry
was built and the deciding half did not exist, so an instrument that ceased to apply was served
as current, forever.

It cannot simply start being written on the version. `effective_from` can live there because the
document states it *about itself*, before publication. Expiry is the opposite shape: it is
almost always learned later, from a different instrument, and a version is immutable (INV-9)
precisely because the chunks carry a copy of its dates — move them under a published version and
a past `as_of` query stops being reproducible.

So expiry is written down as what it is: a decision, in its own append-only table (ADR-0030).

Three things this schema is built around:

* **Nothing is ever updated.** A withdrawn expiry is a new `revoked` row, not a deleted one.
  "Why did this instrument disappear from search on 3 May, and who decided that?" must be
  answerable from one table without reading a log.
* **Two clocks.** `effective_to` is the world's; `created_at`/`closed_at` are this platform's
  belief. A reviewer reconstructing a past answer needs the second (INV-11), and they differ
  whenever an expiry is discovered late or a date is corrected.
* **One open row per scope.** The unique index below is the schema half of "the open row is the
  current belief": a new row for the same scope must close its predecessor in the same
  transaction. Scope is the anchor set, because a partial expiry over Điều 12 and another over
  Điều 40 are both legitimately open at once — which is the ordinary shape in this corpus, where
  instruments are abrogated in pieces.

No projection onto `chunks` here and no backfill: this migration creates the place a decision is
recorded. Confirming one is the registry's job, and it projects the dates inside that
transaction so serving never waits on a scheduled job (ADR-0031).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_expiry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        #: The last day it applied. Inclusive, so it compares directly against a query date
        #: under the existing predicate `effective_to IS NULL OR effective_to >= :effective_on`.
        sa.Column("effective_to", sa.Date(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column(
            "source_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        #: NULL/empty = the whole document. Anchors are clause-precise ("12", "12.2", "12.2a")
        #: and articles are derived from them, kept because the impact traversal asks a
        #: genuinely article-shaped question (ADR-0036).
        sa.Column("articles", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column("anchors", postgresql.ARRAY(sa.Text()), nullable=True),
        #: The sentence the date was read from. Confirming is then one glance (ADR-0029).
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("detected_by", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        #: Written by the row that replaces this one. NULL means this is the current belief.
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "basis IN ('self_stated','abrogated_by','declared_by','steward')",
            name="ck_expiry_basis",
        ),
        sa.CheckConstraint(
            "state IN ('proposed','confirmed','revoked')",
            name="ck_expiry_state",
        ),
        # Only a basis that attributes the decision to another instrument may name one. The
        # converse — abrogated_by must *have* a source — is deliberately not enforced here:
        # an authorized purge of the abrogating instrument sets this to NULL (INV-9), and the
        # surviving row is degraded but not misleading, whereas a `self_stated` row pointing
        # at another document attributes the decision to the wrong place and is.
        sa.CheckConstraint(
            "source_document_id IS NULL OR basis IN ('abrogated_by','declared_by')",
            name="ck_expiry_source_matches_basis",
        ),
        sa.CheckConstraint("source_document_id <> document_id", name="ck_expiry_not_self"),
    )

    # "The open row is the current belief", enforced rather than remembered. Scoped by the
    # anchor set so partial expiries over different clauses coexist; NULL and '{}' are the same
    # scope (the whole document) and must not slip past each other.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_expiry_open_per_scope
            ON document_expiry (document_id, (COALESCE(anchors, '{}'::text[])))
            WHERE closed_at IS NULL
        """
    )
    # The sweep's query: confirmed expiries whose date has passed (ADR-0031). Partial rows are
    # excluded there rather than here — the sweep skips them, it does not fail to find them.
    op.create_index(
        "ix_expiry_due",
        "document_expiry",
        ["state", "effective_to"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    # "What did we believe on date D" — the second clock, read by the inspection screen beside
    # `as_of`. Cheap to add now, awkward to add once the table is large.
    op.create_index("ix_expiry_belief_window", "document_expiry", ["document_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_expiry_belief_window", table_name="document_expiry")
    op.drop_index("ix_expiry_due", table_name="document_expiry")
    op.execute("DROP INDEX IF EXISTS uq_expiry_open_per_scope")
    op.drop_table("document_expiry")
