"""Chat record: many chats per agent (slice S2 of "chats instead of fixed agents").

ADR-103 allowed one chat per agent (``uq_chats_legacy_agent``). S2 lets an
agent carry several chats, each pinned to its own native harness session:

- ``chats.agent_default`` — the agent's own chat (every existing row with an
  agent is one). It still exists once per agent (``uq_chats_agent_default``)
  and takes every native session no other chat pinned.
- ``chats.native_session_id`` — the native session a further chat drives;
  unique per agent (``uq_chats_agent_native``).

Additive: two columns, the old unique index is replaced by a narrower one.
Everything that writes here is behind ``CHAT_RECORD_ENABLED`` (default off).

Way back: flag off, then ``downgrade`` — further chats are DETACHED from
their agent (``legacy_agent_id`` set to NULL, their rows and events stay),
so the old one-chat-per-agent index can be built again; nothing is deleted.
They are also ARCHIVED: with the pin gone, the next sweep imports their
native sessions into the agent's chat again (full copy). The detached
chats keep the old copy, hidden from the list and from search.

Revision ID: 0229_chat_record_many_chats
Revises: 0228_knowledge_root
"""
import sqlalchemy as sa
from alembic import op

revision = "0229_chat_record_many_chats"
down_revision = "0228_knowledge_root"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chats",
        sa.Column("agent_default", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("chats", sa.Column("native_session_id", sa.String(200), nullable=True))
    # Every chat that exists today is the one chat of its agent.
    op.execute("UPDATE chats SET agent_default = TRUE WHERE legacy_agent_id IS NOT NULL")
    op.drop_index("uq_chats_legacy_agent", table_name="chats")
    op.create_index(
        "uq_chats_agent_default",
        "chats",
        ["legacy_agent_id"],
        unique=True,
        postgresql_where=sa.text("agent_default"),
        sqlite_where=sa.text("agent_default"),
    )
    op.create_index(
        "uq_chats_agent_native",
        "chats",
        ["legacy_agent_id", "native_session_id"],
        unique=True,
        postgresql_where=sa.text("native_session_id IS NOT NULL"),
        sqlite_where=sa.text("native_session_id IS NOT NULL"),
    )
    op.create_index("ix_chats_legacy_agent_id", "chats", ["legacy_agent_id"])


def downgrade() -> None:
    op.drop_index("ix_chats_legacy_agent_id", table_name="chats")
    op.drop_index("uq_chats_agent_native", table_name="chats")
    op.drop_index("uq_chats_agent_default", table_name="chats")
    # Keep the further chats and their record, only detach them — and
    # archive them: without the pin the importer reads their native sessions
    # into the agent's chat again, so a visible detached copy would show
    # that history twice (archived chats leave the list and search).
    op.execute(
        "UPDATE chats SET archived_at = COALESCE(archived_at, CURRENT_TIMESTAMP), legacy_agent_id = NULL "
        "WHERE agent_default = FALSE AND legacy_agent_id IS NOT NULL"
    )
    op.create_index(
        "uq_chats_legacy_agent",
        "chats",
        ["legacy_agent_id"],
        unique=True,
        postgresql_where=sa.text("legacy_agent_id IS NOT NULL"),
    )
    op.drop_column("chats", "native_session_id")
    op.drop_column("chats", "agent_default")
