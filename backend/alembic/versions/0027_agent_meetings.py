"""Agent Meetings — 3 neue Tabellen fuer strukturierte Agent-Diskussionen.

Revision ID: 0027
Revises: 0026
"""

from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── agent_meetings ──────────────────────────────────────────────────
    op.create_table(
        "agent_meetings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("board_id", sa.Uuid(), sa.ForeignKey("boards.id"), nullable=False, index=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("meeting_type", sa.String(), nullable=False, server_default="ad_hoc"),
        sa.Column("status", sa.String(), nullable=False, server_default="scheduled"),
        sa.Column("agenda", sa.JSON(), nullable=True),
        sa.Column("participant_ids", sa.JSON(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("decisions", sa.JSON(), nullable=True),
        sa.Column("action_items", sa.JSON(), nullable=True),
        sa.Column("memory_id", sa.Uuid(), sa.ForeignKey("board_memory.id"), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )

    # ── agent_meeting_messages ──────────────────────────────────────────
    op.create_table(
        "agent_meeting_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("meeting_id", sa.Uuid(), sa.ForeignKey("agent_meetings.id"), nullable=False, index=True),
        sa.Column("agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("agent_name", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("topic_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )

    # ── agent_messages (Direktnachrichten) ──────────────────────────────
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("thread_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("from_agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=False, index=True),
        sa.Column("to_agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=False, index=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("reply_to_id", sa.Uuid(), sa.ForeignKey("agent_messages.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("agent_messages")
    op.drop_table("agent_meeting_messages")
    op.drop_table("agent_meetings")
