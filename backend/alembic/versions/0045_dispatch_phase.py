"""Add dispatch_phase for Pre-Dispatch Gating (Phase 1 Systemic Orchestration).

Revision ID: 0045
Revises: 0044
"""
from alembic import op
import sqlalchemy as sa

revision = "0045"
down_revision = "0044"


def upgrade() -> None:
    op.add_column("tasks", sa.Column("dispatch_phase", sa.String(), nullable=True))
    op.create_check_constraint(
        "ck_tasks_dispatch_phase",
        "tasks",
        "dispatch_phase IS NULL OR dispatch_phase IN ('planning', 'ready')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_tasks_dispatch_phase", "tasks")
    op.drop_column("tasks", "dispatch_phase")
