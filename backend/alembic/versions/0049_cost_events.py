"""Cost events — token/cost tracking per task and agent.

Revision ID: 0049
Revises: 0048
"""
from alembic import op
import sqlalchemy as sa

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cost_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("session_key", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False, server_default="session_snapshot"),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cost_events_agent_id", "cost_events", ["agent_id"])
    op.create_index("ix_cost_events_task_id", "cost_events", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_cost_events_task_id")
    op.drop_index("ix_cost_events_agent_id")
    op.drop_table("cost_events")
