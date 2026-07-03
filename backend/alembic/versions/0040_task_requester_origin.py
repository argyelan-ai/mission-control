"""Requester/origin tracking for tasks.

Stores WHO triggered a task and FROM WHERE,
so the response goes back to the right channel.

- requester_channel: "telegram" | "discord" | "web" | "agent" | None
- requester_id: Telegram chat_id, Discord user_id, or User UUID

Revision ID: 0040
Revises: 0039
"""
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"


def upgrade() -> None:
    op.add_column("tasks", sa.Column("requester_channel", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("requester_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "requester_id")
    op.drop_column("tasks", "requester_channel")
