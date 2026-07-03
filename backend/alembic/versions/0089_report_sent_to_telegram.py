"""Task.report_sent_to_telegram flag for report-back hard gate

Revision ID: 0089
Revises: 0088
Create Date: 2026-04-22

New explicit flag on `tasks`: set by `mc telegram` once the agent has
sent a report to the reports chat. Replaces the old
`report_back_status` lifecycle pattern + the 10-minute fallback timer.

- `report_sent_to_telegram` (Boolean, default False, NOT NULL)
- Gate logic: on agent-scoped PATCH status=done with report_back_required=true
  and report_sent_to_telegram=false → 422
- Auto-draft on status=failed analogously

See `backend/app/routers/agent_scoped.py` for the gate implementation.

Idempotent. Downgrade removes the column.
"""
import sqlalchemy as sa
from alembic import op


revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default=sa.false() → existing rows get False without a backfill query.
    # NOT NULL because there's no three-valued logic (sent/not-sent is binary).
    op.add_column(
        "tasks",
        sa.Column(
            "report_sent_to_telegram",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # remove server_default — new rows set this explicitly via the model default
    op.alter_column("tasks", "report_sent_to_telegram", server_default=None)


def downgrade() -> None:
    op.drop_column("tasks", "report_sent_to_telegram")
