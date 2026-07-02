"""add help_request_from and blocked_by_task_id to tasks

Revision ID: 0066
Revises: 0065
Create Date: 2026-04-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0065"

def upgrade() -> None:
    op.add_column("tasks", sa.Column("help_request_from", sa.UUID(), nullable=True))
    op.add_column("tasks", sa.Column("blocked_by_task_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_tasks_help_request_from", "tasks", "agents", ["help_request_from"], ["id"]
    )
    op.create_foreign_key(
        "fk_tasks_blocked_by_task_id", "tasks", "tasks", ["blocked_by_task_id"], ["id"]
    )

def downgrade() -> None:
    op.drop_constraint("fk_tasks_blocked_by_task_id", "tasks", type_="foreignkey")
    op.drop_constraint("fk_tasks_help_request_from", "tasks", type_="foreignkey")
    op.drop_column("tasks", "blocked_by_task_id")
    op.drop_column("tasks", "help_request_from")
