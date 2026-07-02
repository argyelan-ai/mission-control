"""task created_by_user_id

Revision ID: 0055
Revises: 0054
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "created_by_user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_tasks_created_by_user", "tasks", ["created_by_user_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_created_by_user", table_name="tasks")
    op.drop_column("tasks", "created_by_user_id")
