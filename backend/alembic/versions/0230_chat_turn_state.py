"""Chat record: turn state per chat (``busy`` / ``idle`` with a timestamp).

The live recorder writes it from the turn start (the operator's message) and
the harness's own turn-end judgement (Claude Code ``stop_reason``, omp
``stopReason``), so a chat list can show "working" / "ready" for every chat,
not only for the one that is open.

- ``chats.turn_state`` — ``busy``, ``idle`` or NULL (no recorder saw a turn
  of this chat yet: unknown, never guessed as "ready").
- ``chats.turn_state_at`` — when the turn started (``busy``) or ended
  (``idle``), from the transcript's own timestamp.

Additive, two nullable columns, no backfill. Written only while
``CHAT_RECORD_ENABLED`` is on. Way back: ``downgrade`` drops both columns;
nothing else reads them.

Revision ID: 0230_chat_turn_state
Revises: 0229_chat_record_many_chats
"""
import sqlalchemy as sa
from alembic import op

revision = "0230_chat_turn_state"
down_revision = "0229_chat_record_many_chats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chats", sa.Column("turn_state", sa.String(10), nullable=True))
    op.add_column("chats", sa.Column("turn_state_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("chats", "turn_state_at")
    op.drop_column("chats", "turn_state")
