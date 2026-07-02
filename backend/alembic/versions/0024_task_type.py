"""add task_type to tasks

Revision ID: 0024
"""
from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("task_type", sa.String(), server_default="story", nullable=False))
    op.create_index("ix_tasks_project_id_task_type", "tasks", ["project_id", "task_type"])


def downgrade() -> None:
    op.drop_index("ix_tasks_project_id_task_type", table_name="tasks")
    op.drop_column("tasks", "task_type")
