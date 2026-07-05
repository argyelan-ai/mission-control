"""0148 — tasks.human_review_required flag (human review gate).

Operator-requested human review: review handoff skips the agent reviewer,
the task waits in `review` for Mark instead of being dispatched to Rex.

Revision ID: 0148
Revises: 0147
"""
import sqlalchemy as sa
from alembic import op

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("human_review_required", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "human_review_required")
