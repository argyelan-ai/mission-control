"""A thread message knows whether an agent's chat session wrote it.

Round-3 re-review M1: for comm_v2 agents the stale check
(``task_runner._check_stale_in_progress`` via ``messaging.last_task_activity``)
counts every thread message on the task as the run's sign of life. The chat's
`mc msg` ("how far are you?" → "still on step 2") landed on the same thread and
pushed the check back on a dead run. ``from_chat`` is set by the server (the
session-role header, ``chat_session_guard.is_chat_session``) — never by the
agent — and the run clock leaves those rows out.

Existing rows stay ``false``: nothing before this revision marked chat
messages, and a pre-deploy chat message is older than any stale window anyway.
Additive only; downgrade drops the column and the clock counts every message
again (the behaviour before this revision).

Revision ID: 0222_message_from_chat
Revises: 0221_bench_entry_position
"""
import sqlalchemy as sa
from alembic import op

revision = "0222_message_from_chat"
down_revision = "0221_bench_entry_position"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("from_chat", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("messages", "from_chat")
