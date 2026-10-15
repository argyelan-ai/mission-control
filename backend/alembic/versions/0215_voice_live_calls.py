"""Live voice calls and their cost (voice PR 6, ADR-091 draft).

One row per live voice call: who (agent, name snapshot), when (start, last
usage report, end), how long, the token usage OpenAI reported and the cost MC
computed from it with the prices in settings. Feeds the daily budget and the
Insights "Voice" row.

``ref_hash`` = SHA-256 of the call's browser handle (the handle itself is not
stored). ``agent_id`` is nullable: an agent delete sets it to NULL (schema sweep in
routers/agents.py), so cost history outlives the agent.

Additive only: nothing else reads or writes this table.

Revision ID: 0215_voice_live_calls
Revises: 0214_checklist_item_source
"""
import sqlalchemy as sa
from alembic import op

revision = "0215_voice_live_calls"
down_revision = "0214_checklist_item_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_live_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ref_hash", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("agent_name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("openai_call_id", sa.String(length=160), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=32), nullable=True),
        sa.Column("duration_s", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_text_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_audio_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_text_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_audio_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_text_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_audio_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_voice_live_calls_ref_hash", "voice_live_calls", ["ref_hash"], unique=True)
    op.create_index("ix_voice_live_calls_agent_id", "voice_live_calls", ["agent_id"])
    op.create_index("ix_voice_live_calls_started_at", "voice_live_calls", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_voice_live_calls_started_at", table_name="voice_live_calls")
    op.drop_index("ix_voice_live_calls_agent_id", table_name="voice_live_calls")
    op.drop_index("ix_voice_live_calls_ref_hash", table_name="voice_live_calls")
    op.drop_table("voice_live_calls")
