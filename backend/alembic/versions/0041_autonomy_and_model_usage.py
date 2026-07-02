"""Autonomy Levels + Model Usage Tracking.

Revision ID: 0041
Revises: 0040
"""
from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"


def upgrade() -> None:
    # Theme 3: Autonomy Level auf Approvals
    op.add_column(
        "approvals",
        sa.Column("autonomy_level", sa.String(), nullable=True),
    )
    # Theme 4: Model Usage als JSON auf AgentMetrics
    op.add_column(
        "agent_metrics",
        sa.Column("model_usage", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_metrics", "model_usage")
    op.drop_column("approvals", "autonomy_level")
