"""MC's own chat record (ADR-103): chats, chat_sessions, chat_events.

First slice of "chats instead of fixed agents" (ADR-102). Additive only —
no existing table is touched. Everything that writes into these tables is
behind ``CHAT_RECORD_ENABLED`` (default off).

Search: a GIN index over ``to_tsvector('simple', search_text)`` — the
'simple' configuration on purpose (no stemming), because a chat mixes
German and English and a wrong-language stemmer finds less, not more.

Way back: flag off, then ``downgrade`` (drops the three tables) or
``TRUNCATE chats CASCADE``.

Revision ID: 0227_chat_record
Revises: 0226_comms_guest_cards
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0227_chat_record"
down_revision = "0226_comms_guest_cards"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("workspace_path", sa.String(), nullable=True),
        sa.Column("harness", sa.String(40), nullable=True),
        sa.Column("runtime_id", sa.Uuid(), sa.ForeignKey("runtimes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("permission_level", sa.String(40), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("legacy_agent_id", sa.Uuid(), sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("next_seq", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index(
        "uq_chats_legacy_agent",
        "chats",
        ["legacy_agent_id"],
        unique=True,
        postgresql_where=sa.text("legacy_agent_id IS NOT NULL"),
    )

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("chat_id", sa.Uuid(), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("harness", sa.String(40), nullable=True),
        sa.Column("runtime_id", sa.Uuid(), sa.ForeignKey("runtimes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("place", sa.String(20), nullable=True),
        sa.Column("native_session_id", sa.String(200), nullable=False),
        sa.Column("transcript_path", sa.Text(), nullable=True),
        sa.Column("cwd", sa.Text(), nullable=True),
        sa.Column("imported_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column("failed_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("chat_id", "native_session_id", name="uq_chat_sessions_native"),
    )
    op.create_index("ix_chat_sessions_chat_id", "chat_sessions", ["chat_id"])

    op.create_table(
        "chat_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("chat_id", sa.Uuid(), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("source_key", sa.String(300), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("role", sa.String(20), nullable=True),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column("search_text", sa.Text(), nullable=True),
        sa.Column("payload_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("chat_id", "seq", name="uq_chat_events_seq"),
        sa.UniqueConstraint("chat_id", "source_key", name="uq_chat_events_source"),
    )
    op.create_index("ix_chat_events_session_id", "chat_events", ["session_id"])
    op.create_index("ix_chat_events_chat_order", "chat_events", ["chat_id", "ts", "seq"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_chat_events_search ON chat_events "
            "USING GIN (to_tsvector('simple', coalesce(search_text, '')))"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_chat_events_search")
    op.drop_index("ix_chat_events_chat_order", table_name="chat_events")
    op.drop_index("ix_chat_events_session_id", table_name="chat_events")
    op.drop_table("chat_events")
    op.drop_index("ix_chat_sessions_chat_id", table_name="chat_sessions")
    op.drop_table("chat_sessions")
    op.drop_index("uq_chats_legacy_agent", table_name="chats")
    op.drop_table("chats")
