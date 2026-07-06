"""0149 — tasks.blocker_to_operator flag (per-task blocker routing).

Opt-in per task: a blocker on this task skips Board-Lead (Boss) triage and goes
straight to the operator (Mark). Nullable — existing tasks keep lead-first triage.

Revision ID: 0149
Revises: 0148
"""
import sqlalchemy as sa
from alembic import op

revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("blocker_to_operator", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "blocker_to_operator")
