"""tasks.hold_reason — free-text reason for run_control=manual_hold

Revision ID: 0197_task_hold_reason
Revises: 0196_runtime_supports_vision
Create Date: 2026-09-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0197_task_hold_reason"
down_revision = "0196_runtime_supports_vision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("hold_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "hold_reason")
