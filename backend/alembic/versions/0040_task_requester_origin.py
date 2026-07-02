"""Requester/Origin Tracking fuer Tasks.

Speichert WER und VON WO einen Task ausgeloest hat,
damit die Rueckmeldung an den richtigen Kanal geht.

- requester_channel: "telegram" | "discord" | "web" | "agent" | None
- requester_id: Telegram chat_id, Discord user_id, oder User-UUID

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
