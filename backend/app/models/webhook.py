import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, text
from sqlmodel import Column, Field, SQLModel


class Webhook(SQLModel, table=True):
    __tablename__ = "webhooks"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID = Field(foreign_key="boards.id", index=True)
    name: str
    secret: str | None = None
    is_enabled: bool = True
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )


class WebhookPayload(SQLModel, table=True):
    __tablename__ = "webhook_payloads"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    webhook_id: uuid.UUID = Field(foreign_key="webhooks.id", index=True)
    payload: dict[str, Any] = Field(sa_column=Column(JSON))
    headers: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    source_ip: str | None = None
    processed: bool = False
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
