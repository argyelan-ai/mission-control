"""Session-to-session conversations (ADR-100 stage 2b).

- messages.sender_session_id: the sender when it is a terminal session
  principal (never a row in agents, so it cannot be sender_id).
- comms_conversations: one row per "session" thread — the two participant
  refs and the loop brake (replies without the operator, paused, the brake
  approval).
- comms_cursors: read position for principals without agent_thread_cursor.

Thread kind "session" needs no schema change (threads.kind is free text,
validated in app.comm_constants). Additive only; downgrade drops all three.

Revision ID: 0225_session_conversations
Revises: 0224_bench_w1_schema
"""
import sqlalchemy as sa
from alembic import op

revision = "0225_session_conversations"
down_revision = "0224_bench_w1_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("sender_session_id", sa.Uuid(), nullable=True))
    op.create_table(
        "comms_conversations",
        sa.Column("thread_id", sa.Uuid(), sa.ForeignKey("threads.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("a_ref", sa.String(80), nullable=False),
        sa.Column("b_ref", sa.String(80), nullable=False),
        sa.Column("replies_without_operator", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("brake_approval_id", sa.Uuid(), sa.ForeignKey("approvals.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("a_ref", "b_ref", name="uq_comms_conversations_pair"),
    )
    op.create_index("ix_comms_conversations_a_ref", "comms_conversations", ["a_ref"])
    op.create_index("ix_comms_conversations_b_ref", "comms_conversations", ["b_ref"])
    op.create_table(
        "comms_cursors",
        sa.Column("principal_ref", sa.String(80), primary_key=True),
        sa.Column("thread_id", sa.Uuid(), sa.ForeignKey("threads.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("last_acked_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("comms_cursors")
    op.drop_index("ix_comms_conversations_b_ref", table_name="comms_conversations")
    op.drop_index("ix_comms_conversations_a_ref", table_name="comms_conversations")
    op.drop_table("comms_conversations")
    op.drop_column("messages", "sender_session_id")
