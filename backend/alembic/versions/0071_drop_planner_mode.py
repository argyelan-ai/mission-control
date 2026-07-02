"""drop planner_mode column from tasks

Revision ID: 0071
Revises: 0070
Create Date: 2026-04-11

Kontext: Phase 6 (Boss-Autonomy-Overhaul) hat den Planner-Pfad komplett
entfernt (Router, Delegation-Guards, Dispatch-Logik, Template). Das
planner_mode Schema-Feld wurde aus Backward-Compat-Gruenden in Phase 6
stehen gelassen, wird aber nirgendwo mehr gelesen.

Phase D Cleanup: Feld + Constraint droppen.

Downgrade: re-create als nullable (KEIN NOT NULL mit default 'auto',
weil alte Tasks beim Up-Down-Up sonst unsaubere Werte haetten). Wer
downgraded kann das planner_mode manuell neu seeden.
"""
from alembic import op
import sqlalchemy as sa


revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Constraint zuerst entfernen, dann Spalte droppen
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_planner_mode")
    op.drop_column("tasks", "planner_mode")


def downgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "planner_mode",
            sa.String(),
            nullable=True,  # downgrade-state: nullable damit alte Rows OK sind
        ),
    )
    # kein CHECK-Constraint im downgrade — die Validation war ohnehin obsolet
