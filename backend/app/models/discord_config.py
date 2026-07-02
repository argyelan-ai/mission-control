"""Discord global configuration (single-row).

Phase 30 spinoff from `gateways.discord_*`. The row is created by migration
0123 (seeded from the old gateways row or NULL fallback). Single-row
invariant is enforced in application logic — the `routers/discord.py`
admin endpoint reads `SELECT ... LIMIT 1`, never `WHERE id = ?`, and
never INSERTs a second row.

Webhook URL for ops alerts stays in env var DISCORD_WEBHOOK_OPS (it is a
secret, not a config knob). discord_config holds only the public-ish
guild_id + category_id + bot_configured triplet.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, text
from sqlmodel import Column, Field, SQLModel


class DiscordConfig(SQLModel, table=True):
    __tablename__ = "discord_config"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    guild_id: str | None = None
    category_id: str | None = None
    bot_configured: bool = False
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("NOW()"),
            onupdate=datetime.utcnow,
        ),
    )
