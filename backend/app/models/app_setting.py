"""Global, DB-backed runtime settings (channels settings page).

Distinct from ``user_settings`` (per-user UI state): these are OPERATOR
decisions about how the system behaves — which chat channel serves which
function. Values override the env-loaded pydantic defaults at runtime via
``services/channel_config.apply_channel_overrides``; env remains the
default so a plain .env install keeps working (OSS-installable rule).

Only keys in ``channel_config.CHANNEL_SETTING_FIELDS`` may live here —
the service enforces the allowlist, this table stays dumb on purpose.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, text
from sqlmodel import Field, SQLModel


class AppSetting(SQLModel, table=True):
    __tablename__ = "app_settings"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    key: str = Field(unique=True, index=True)
    # Stored as string; the allowlist in channel_config carries the type and
    # coerces on read ("true"/"false" for bools). Keeps the table trivial.
    value: str
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True), server_default=text("NOW()"), nullable=False
        ),
    )
