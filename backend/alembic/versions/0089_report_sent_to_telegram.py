"""Task.report_sent_to_telegram Flag fuer Report-Back Hard-Gate

Revision ID: 0089
Revises: 0088
Create Date: 2026-04-22

Neues explizites Flag auf `tasks`: wird von `mc telegram` gesetzt sobald
der Agent einen Report an den Reports-Chat gesendet hat. Ersetzt das
alte `report_back_status`-Lifecycle-Pattern + den 10-Min-Fallback-Timer.

- `report_sent_to_telegram` (Boolean, default False, NOT NULL)
- Gate-Logik: bei agent-scoped PATCH status=done mit report_back_required=true
  und report_sent_to_telegram=false → 422
- Auto-Draft bei status=failed analog

Siehe `backend/app/routers/agent_scoped.py` fuer die Gate-Implementierung.

Idempotent. Downgrade entfernt die Spalte.
"""
import sqlalchemy as sa
from alembic import op


revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default=sa.false() → bestehende Rows kriegen False ohne Backfill-Query.
    # NOT NULL weil keine dreiwertige Logik (sent/not-sent ist binär).
    op.add_column(
        "tasks",
        sa.Column(
            "report_sent_to_telegram",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # server_default entfernen — neue Rows setzen das explizit via Model-Default
    op.alter_column("tasks", "report_sent_to_telegram", server_default=None)


def downgrade() -> None:
    op.drop_column("tasks", "report_sent_to_telegram")
