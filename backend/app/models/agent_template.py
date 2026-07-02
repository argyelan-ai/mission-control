import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, text
from sqlmodel import Column, Field, SQLModel


class AgentTemplate(SQLModel, table=True):
    __tablename__ = "agent_templates"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    emoji: str = "🤖"
    role: str | None = None
    default_model: str | None = None
    soul_md: str | None = None
    skills: list[Any] = Field(default_factory=list, sa_column=Column(JSON))
    skill_filter: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    cli_plugins: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    cli_skills: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    mcp_servers: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    scopes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    dispatch_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON),
    )
    is_builtin: bool = False  # True = via Seeding erstellt, kann nicht gelöscht werden

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )
