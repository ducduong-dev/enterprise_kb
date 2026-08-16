"""Task types for the expiry sweep.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-16

`review_tasks.task_type` is a CHECK over a fixed list rather than an enum type, so adding a
kind of work a steward can be given is a constraint swap. Two arrive with the sweep (ADR-0031):

* `expiry_review` — a confirmed expiry lands in 30 days. The document has not left service and
  will not until its date; this is the warning that lets somebody object while objecting is
  still cheap.
* `periodic_review` — `documents.review_by` has come round. The column has been settable and
  indexed since the initial schema and read by nothing, which is the reason it exists.

Deliberately two types and not one with a flag. "Is this instrument really finished?" and "is
this procedure still accurate?" are different questions, land on different desks, and a single
queue mixing them is a queue that gets triaged rather than worked.
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_OLD = (
    "task_type IN ('idp_review','identity_review','merge_review','impact_review','pii_override')"
)
_NEW = (
    "task_type IN ('idp_review','identity_review','merge_review','impact_review',"
    "'pii_override','expiry_review','periodic_review')"
)


def upgrade() -> None:
    op.drop_constraint("ck_tasks_type", "review_tasks", type_="check")
    op.create_check_constraint("ck_tasks_type", "review_tasks", _NEW)


def downgrade() -> None:
    # Any task of the new types has to go before the old constraint can hold again. They are
    # advisory — a warning and a re-attestation prompt — so dropping them loses no decision.
    op.execute("DELETE FROM review_tasks WHERE task_type IN ('expiry_review','periodic_review')")
    op.drop_constraint("ck_tasks_type", "review_tasks", type_="check")
    op.create_check_constraint("ck_tasks_type", "review_tasks", _OLD)
