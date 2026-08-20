"""approvals.board_id nullable — group_gate-Approvals sind board-frei (ADR-075).

Gruppen (Multi-Agent-Gruppenchat) hängen bewusst an keinem Board; ihre
Gates (`action_type="group_gate"`) brauchen deshalb ein Approval ohne
board_id. Die globale Pending-Liste (GET /approvals) zeigt sie weiterhin;
board-gescopte Listen (GET /boards/{id}/approvals) filtern sie schlicht
nicht ein — genau die gewünschte Semantik.

Revision ID: 0182
Revises: 0181_agent_groups
"""
import sqlalchemy as sa
from alembic import op

revision = "0182_approval_board_nullable"
down_revision = "0181_agent_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "approvals", "board_id", existing_type=sa.Uuid(), nullable=True
    )


def downgrade() -> None:
    # Rückweg nur möglich, wenn keine board-freien Approvals existieren —
    # die müssten vorher gelöscht oder einem Board zugeordnet werden.
    op.alter_column(
        "approvals", "board_id", existing_type=sa.Uuid(), nullable=False
    )
