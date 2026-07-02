import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, text
from sqlmodel import Column, Field, SQLModel


class ActivityEvent(SQLModel, table=True):
    __tablename__ = "activity_events"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    event_type: str  # task.created, agent.online, approval.requested, etc.

    # Context (all optional)
    board_id: uuid.UUID | None = Field(
        default=None, foreign_key="boards.id", nullable=True, index=True
    )
    task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", nullable=True
    )
    agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )
    project_id: uuid.UUID | None = Field(
        default=None, foreign_key="projects.id", nullable=True
    )

    title: str
    detail: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    severity: str = "info"  # info | warning | error | critical

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    activity_event_id: uuid.UUID | None = Field(
        default=None, foreign_key="activity_events.id", nullable=True
    )
    channel: str  # 'discord' | 'telegram' | 'in_app'
    status: str = "pending"  # pending | sent | failed

    sent_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    error_message: str | None = None

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
