"""tasks.origin_thread_id — the conversation the order came from.

Thread-anchor fix, part 2 (2026-08-05): a task remembers the chat thread it
was ordered in, delegated subtasks inherit it, and the report endpoint mirrors
the final report into that thread. ondelete=SET NULL — a vanished conversation
must never block or delete a task (same rationale as fk_tasks_thread_id).

Revision ID: 0174_task_origin_thread
Revises: 0173_task_thread_unique
"""
import sqlalchemy as sa

from alembic import op

revision = "0174_task_origin_thread"
down_revision = "0173_task_thread_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("origin_thread_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_tasks_origin_thread_id",
        "tasks",
        "threads",
        ["origin_thread_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_tasks_origin_thread_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "origin_thread_id")
