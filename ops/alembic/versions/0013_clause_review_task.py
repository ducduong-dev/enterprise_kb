"""The eighth review task type: a detected clause supersession waiting for a person.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-17

ADR-0033's `proposed` state "changes nothing a user can see. It is a row in a steward queue."
Until now there was no queue to be a row in: `clause_supersessions` had a state but nothing put
it in front of anybody, so the funnel would have produced proposals that were, in practice,
invisible.

Deliberately its own type rather than a flag on `expiry_review`. The two ask different
questions of different people — "is this instrument really finished?" is the whole document and
usually Legal's, while "did this clause replace that one?" is two paragraphs side by side and
belongs with whoever owns the subject. Same reasoning as migration 0005, which split
`expiry_review` from `periodic_review` for the same reason.

The task hangs off the *older* clause's document version, because that is the document whose
serving changes when the proposal is confirmed, and it is the one a steward will want open.
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_OLD = (
    "task_type IN ('idp_review','identity_review','merge_review','impact_review',"
    "'pii_override','expiry_review','periodic_review')"
)
_NEW = (
    "task_type IN ('idp_review','identity_review','merge_review','impact_review',"
    "'pii_override','expiry_review','periodic_review','clause_review')"
)


def upgrade() -> None:
    op.drop_constraint("ck_tasks_type", "review_tasks", type_="check")
    op.create_check_constraint("ck_tasks_type", "review_tasks", _NEW)


def downgrade() -> None:
    op.execute("DELETE FROM review_tasks WHERE task_type = 'clause_review'")
    op.drop_constraint("ck_tasks_type", "review_tasks", type_="check")
    op.create_check_constraint("ck_tasks_type", "review_tasks", _OLD)
