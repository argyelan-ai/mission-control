"""task.callback_agent_id — Completion-Callback-Target getrennt von owner

Revision ID: 0068
Revises: 0067
Create Date: 2026-04-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("callback_agent_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_tasks_callback_agent_id",
        "tasks",
        "agents",
        ["callback_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_tasks_callback_agent_id"), "tasks", ["callback_agent_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_tasks_callback_agent_id"), table_name="tasks")
    op.drop_constraint("fk_tasks_callback_agent_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "callback_agent_id")
