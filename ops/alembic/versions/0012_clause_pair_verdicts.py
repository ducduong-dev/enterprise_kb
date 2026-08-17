"""What the funnel concluded about a pair of clauses, including "nothing is wrong here".

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-17

`clause_supersessions` answers "what replaced this clause". It cannot answer "what did we
decide about these two clauses", and it was a mistake to assume it could: `uq_clause_sup_open`
allows one open row per *replaced clause*, so a clause that is `different_scope` against one
candidate and `superseded` by another has two answers and room for one. Worse, a
`different_scope` verdict stored there with a null replacement is byte-for-byte a pure
abrogation — the opposite of what it means (ADR-0033, *Corrections from building gate 5*).

So the three non-supersession verdicts live here, and so does the supersession verdict — this
table records the *adjudication*, `clause_supersessions` records the *consequence*. Only one of
the four buckets has a consequence.

Recording them is a requirement rather than an optimisation. M9d's acceptance criteria say a
same-subject pair differing only in customer segment is **recorded** `different_scope` by gate
3, and "recorded" is this table. That a corpus-wide re-run can then skip the pair is a
convenience on top, and a smaller one than it first appears: ADR-0035's cache already makes a
repeated *model call* free, so what is saved here is the pair assembly and the gates, not the
expensive part.

**A row means we reached a conclusion.** No row means either nobody looked or the adjudication
did not conclude — a model that was unreachable, a reply that could not be read — and both must
be retried, so neither is written. That is the whole retry rule and it needs no state column.

**The pair is normalized, not directional.** `(left, right)` is stored with the lexically
smaller `(document_id, section_path)` first, so adjudicating A against B and later B against A
finds the same row. Direction lives in `clause_supersessions` where it means something; here it
would only give one pair two identities and let a backfill do the work twice.

**Keyed on section path, never on chunk id**, for the reason every other M9 table is:
`_insert_chunks` deletes and re-inserts every chunk of a version, so a rechunk would orphan
every row here.

**`text_digest` is what makes a stored verdict safe.** It hashes both clause texts, so a clause
whose wording changed no longer matches its stored verdict and is adjudicated again. Without it
this table would serve a judgement about text that no longer exists — the same failure
ADR-0035's cache key is shaped to prevent, and the reason neither keys on an id.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clause_pair_verdicts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Normalized: the lexically smaller (document_id, section_path) is always "left".
        sa.Column(
            "left_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("left_section_path", sa.Text(), nullable=False),
        sa.Column(
            "right_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("right_section_path", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        # Which step reached it. The eval reports the split between pairs the gates resolved and
        # pairs the model did, and this column is where that number comes from.
        sa.Column("settled_by", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("quantity_delta", postgresql.JSONB(), nullable=True),
        sa.Column("scope_facets", postgresql.JSONB(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        # Hash of both clause texts. A verdict survives only as long as the text it was about.
        sa.Column("text_digest", sa.Text(), nullable=False),
        sa.Column("detected_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "verdict IN ('same_rule_restated','superseded','different_scope',"
            "'conflicting_unresolved')",
            name="ck_pair_verdict",
        ),
        # A pair is two *different* clauses. Same document and same path is one clause.
        sa.CheckConstraint(
            "left_document_id <> right_document_id OR left_section_path <> right_section_path",
            name="ck_pair_not_self",
        ),
        # The normalization, enforced rather than trusted: a writer that inserted a pair the
        # wrong way round would create the duplicate this table exists to prevent, and would do
        # it silently.
        sa.CheckConstraint(
            "(left_document_id, left_section_path) < (right_document_id, right_section_path)",
            name="ck_pair_normalized",
        ),
    )

    # One conclusion per pair. A re-adjudication replaces the row rather than joining it —
    # unlike the ledgers, this is not a belief anyone reasons about historically; it is a cache
    # of work, and `text_digest` already says whether it is still about the right text.
    op.create_index(
        "uq_clause_pair",
        "clause_pair_verdicts",
        ["left_document_id", "left_section_path", "right_document_id", "right_section_path"],
        unique=True,
    )
    # "What have we concluded about this document's clauses" — the inspection screen's question,
    # and the funnel's own "have I already done this pair" lookup.
    op.create_index(
        "ix_clause_pair_left", "clause_pair_verdicts", ["left_document_id", "left_section_path"]
    )
    op.create_index(
        "ix_clause_pair_right", "clause_pair_verdicts", ["right_document_id", "right_section_path"]
    )
    # The eval's split, and the queue of what a model actually judged.
    op.create_index("ix_clause_pair_verdict", "clause_pair_verdicts", ["verdict", "settled_by"])


def downgrade() -> None:
    op.drop_index("ix_clause_pair_verdict", table_name="clause_pair_verdicts")
    op.drop_index("ix_clause_pair_right", table_name="clause_pair_verdicts")
    op.drop_index("ix_clause_pair_left", table_name="clause_pair_verdicts")
    op.drop_index("uq_clause_pair", table_name="clause_pair_verdicts")
    op.drop_table("clause_pair_verdicts")
