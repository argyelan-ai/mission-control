"""Add performance indexes for common query patterns.

Revision ID: 0029
Revises: 0028
Create Date: 2026-03-03

Indexes:
- tasks.status — Watchdog, TaskRunner, Dispatch queries filter by status
- task_comments (task_id + comment_type) — Recovery context, review messages
- board_memory (memory_type + board_id) — Dispatch context, knowledge queries
- agents (board_id + is_board_lead) — find_dispatch_target, _find_reviewer
"""

from alembic import op


revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index(
        "ix_task_comments_task_type",
        "task_comments",
        ["task_id", "comment_type"],
    )
    op.create_index(
        "ix_board_memory_type_board",
        "board_memory",
        ["memory_type", "board_id"],
    )
    op.create_index(
        "ix_agents_board_lead",
        "agents",
        ["board_id", "is_board_lead"],
    )


def downgrade() -> None:
    op.drop_index("ix_agents_board_lead", table_name="agents")
    op.drop_index("ix_board_memory_type_board", table_name="board_memory")
    op.drop_index("ix_task_comments_task_type", table_name="task_comments")
    op.drop_index("ix_tasks_status", table_name="tasks")
