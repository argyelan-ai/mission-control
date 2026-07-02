"""agent_task_comment_cursor — per-agent, per-task cursor for comment delivery

Revision ID: 0073
Revises: 0072
Create Date: 2026-04-17

Allows /api/v1/agent/me/poll to deliver user-posted comments to non-gateway
agents (Docker cli-bridge, host Boss) without re-delivering seen comments.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_task_comment_cursor",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("last_seen_comment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("agent_task_comment_cursor")
