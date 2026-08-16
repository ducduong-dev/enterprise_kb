"""Model responses, keyed by everything that can change them.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-16

Nothing in the platform caches a model response — not the ports, not the proxy config. Every
call is billed and waited for, every time, including the ones already made. Four workloads ask
the same question repeatedly: the clause-supersession backfill over 3,000 documents, rechunking
(which changes every chunk id and almost no text), CI evals against prompts that changed in
maybe one run of twenty, and regenerated merge drafts (ADR-0035).

The backfill is the one that matters. Re-running it today means paying for it again, which in
practice means people avoid re-running it and work from stale results — so the cache is what
makes the detection funnel a thing that can be improved rather than a thing that was run once.

**The key is a hash of everything that can change the answer**, and the rendered prompt is in
it. That is what makes the cache safe against the failure it could otherwise cause: a document
whose text changed produces a different rendered prompt and therefore a different key, so a
cached judgement can never be served for text it was not made about. It is also why the key is
not a document id or a chunk id — those survive an edit that changes the answer.

In Postgres rather than a sidecar, because the cached content is bank document text and a
model's findings about it. That belongs under the same backup, retention and audit rules as
everything else, not in a key-value store nobody inventories.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_cache",
        #: sha256 of model ‖ prompt_version ‖ rendered prompt ‖ temperature ‖ max_tokens.
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("model_id", sa.Text(), nullable=False),
        #: Recorded as well as hashed, so "which prompt produced this verdict" is a query
        #: rather than an archaeology exercise.
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("response", sa.Text(), nullable=False),
        sa.Column("finish_reason", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        #: Eviction is size-based and least-recently-used. Dropping the whole table changes
        #: nothing but the bill, which is the property that keeps this an optimisation with no
        #: semantics of its own.
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_model_cache_lru", "model_cache", ["last_used_at"])


def downgrade() -> None:
    op.drop_index("ix_model_cache_lru", table_name="model_cache")
    op.drop_table("model_cache")
