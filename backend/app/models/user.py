import uuid
from datetime import datetime
from typing import Any

from sqlmodel import Column, DateTime, Field, SQLModel
from sqlalchemy import JSON, text


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        sa_column_kwargs={"server_default": text("gen_random_uuid()")},
    )
    email: str = Field(unique=True, index=True)
    name: str
    preferred_name: str | None = None
    avatar_url: str | None = None
    timezone: str = "Europe/Berlin"
    settings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Auth
    password_hash: str | None = None
    role: str = Field(default="viewer")  # "admin" | "operator" | "viewer"
    is_active: bool = Field(default=True)
    token_version: int = Field(default=0)  # Increment on logout → invalidates all JWTs
    # Timestamps
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )


class UserSettings(SQLModel, table=True):
    __tablename__ = "user_settings"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    key: str
    value: dict[str, Any] = Field(sa_column=Column(JSON))
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )

    class Config:
        table_name = "user_settings"
