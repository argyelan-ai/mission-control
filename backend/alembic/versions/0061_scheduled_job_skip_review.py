"""scheduled_job task_skip_review + task skip_review

Revision ID: 0061
Revises: 0060
Create Date: 2026-04-01
"""
from alembic import op
import sqlalchemy as sa

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduled_jobs",
        sa.Column("task_skip_review", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "tasks",
        sa.Column("skip_review", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("scheduled_jobs", "task_skip_review")
    op.drop_column("tasks", "skip_review")
