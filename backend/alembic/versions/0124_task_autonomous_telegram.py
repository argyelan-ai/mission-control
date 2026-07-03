"""Add tasks.autonomous_telegram for subtask-send routing rule

Revision ID: 0124
Revises: 0123
Create Date: 2026-05-17

Routing rule "whoever dispatches, sends" — subtasks (parent_task_id NOT NULL)
normally must NOT send `mc telegram` directly to the operator. The
orchestrator (Boss) consolidates + sends the final message. Before this
patch, both the researcher (as subtask worker) and Boss (as orchestrator)
sent — a duplicate Telegram hit for the user.

This migration adds the override flag for the edge case
"long-running watch task": Boss can set autonomous_telegram=True
in the subtask brief, then the worker is allowed to send itself
(e.g. "watch the Argyelan channel and report comments").

Standalone tasks (parent_task_id IS NULL, e.g. Morning Briefing,
Scheduled) remain unaffected — the worker IS the dispatcher there.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "autonomous_telegram",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "autonomous_telegram")
