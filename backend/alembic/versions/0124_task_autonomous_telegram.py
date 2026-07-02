"""Add tasks.autonomous_telegram for subtask-send routing rule

Revision ID: 0124
Revises: 0123
Create Date: 2026-05-17

Routing-Regel "wer dispatcht, der sendet" — Subtasks (parent_task_id NOT NULL)
duerfen normalerweise KEIN `mc telegram` direkt an den Operator senden. Der
Orchestrator (Boss) konsolidiert + sendet die finale Nachricht. Vor diesem
Patch sendete sowohl Researcher (als Subtask-Worker) als auch Boss (als
Orchestrator) — doppelter Telegram-Hit beim User.

Diese Migration fuegt das Override-Flag fuer den Edge-Case
"long-running Watch-Task" hinzu: Boss kann im Subtask-Brief
autonomous_telegram=True setzen, dann darf der Worker selbst senden
(z.B. "beobachte Argyelan-Channel und melde Kommentare").

Standalone-Tasks (parent_task_id IS NULL, z.B. Morning Briefing,
Scheduled) bleiben unberuehrt — der Worker IST der Dispatcher dort.
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
