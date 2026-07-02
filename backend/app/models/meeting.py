"""Agent Meetings — Tabellen fuer strukturierte Agent-Diskussionen.

Drei Tabellen:
- AgentMeeting: Meeting-Session (weekly/ad_hoc/retrospective)
- AgentMeetingMessage: Einzelne Nachrichten im Meeting-Verlauf
- AgentMessage: Direktnachrichten zwischen Agents (unabhaengig von Meetings)
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Text, text
from sqlmodel import Column, Field, SQLModel


class AgentMeeting(SQLModel, table=True):
    __tablename__ = "agent_meetings"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID = Field(foreign_key="boards.id", index=True)
    title: str
    meeting_type: str = "ad_hoc"  # weekly | ad_hoc | retrospective
    status: str = "scheduled"  # scheduled | running | completed | failed | cancelled

    agenda: list[str] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    participant_ids: list[str] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    # Ergebnisse
    summary: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    decisions: list[dict[str, Any]] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    action_items: list[dict[str, Any]] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    # Referenz auf gespeicherte Board-Memory
    memory_id: uuid.UUID | None = Field(
        default=None, foreign_key="board_memory.id", nullable=True
    )

    scheduled_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )


class AgentMeetingMessage(SQLModel, table=True):
    __tablename__ = "agent_meeting_messages"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    meeting_id: uuid.UUID = Field(foreign_key="agent_meetings.id", index=True)
    agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )
    agent_name: str | None = None

    role: str  # facilitator_question | agent_response | system_note | summary
    content: str = Field(sa_column=Column(Text))
    round: int = 1
    topic_index: int = 0

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )


class AgentMessage(SQLModel, table=True):
    """Direktnachrichten zwischen Agents (unabhaengig von Meetings)."""
    __tablename__ = "agent_messages"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    thread_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True)
    from_agent_id: uuid.UUID = Field(foreign_key="agents.id", index=True)
    to_agent_id: uuid.UUID = Field(foreign_key="agents.id", index=True)
    content: str = Field(sa_column=Column(Text))

    status: str = "pending"  # pending | delivered | replied | failed
    reply_to_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_messages.id", nullable=True
    )

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
