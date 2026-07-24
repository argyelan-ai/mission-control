"""Interaction Model 2.0 — Thread, Message, AgentThreadCursor.

Thread = a conversation container (per-task, side thread, or DM).
Message = a single entry in a thread's append-only log (seq unique per thread).
AgentThreadCursor = per-agent read/ack position within a thread, used by
the /me/poll-style delivery flow (mirrors AgentTaskCommentCursor's
composite-PK pattern).

See app.comm_constants for the canonical MESSAGE_TYPES/THREAD_KINDS/etc.
vocab — validated at the service layer (Task 3), not enforced here.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, UniqueConstraint, Uuid, text
from sqlmodel import Column, Field, SQLModel


class Thread(SQLModel, table=True):
    __tablename__ = "threads"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    kind: str  # "task" | "side" | "dm" (see comm_constants.THREAD_KINDS)
    # ondelete=SET NULL (mc-task-delete-guard): a thread survives its task's
    # deletion — same rationale as bench_entries.task_id — so a deleted task
    # never RESTRICTs against a thread that outlives it.
    task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(Uuid, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True),
    )
    project_id: uuid.UUID | None = Field(default=None, foreign_key="projects.id", nullable=True, index=True)
    title: str | None = None
    summary: str | None = None
    summary_through_seq: int | None = None
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    closed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_messages_thread_seq"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    thread_id: uuid.UUID = Field(foreign_key="threads.id", index=True)
    seq: int
    sender_type: str  # "user" | "agent" | "system" (see comm_constants)
    sender_id: uuid.UUID | None = Field(default=None, foreign_key="agents.id", nullable=True)
    message_type: str  # "message" | "question" | "status" | "decision" | "system"
    body: str
    reply_to: uuid.UUID | None = Field(default=None, foreign_key="messages.id", nullable=True)
    mentions: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default=text("'[]'")),
    )
    # {"awaiting": bool, "to": str, "priority": str, "options": list[str]|None,
    #  "default": str|None, "deadline": iso|None}
    question_meta: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )


class AgentThreadCursor(SQLModel, table=True):
    __tablename__ = "agent_thread_cursor"

    agent_id: uuid.UUID = Field(foreign_key="agents.id", primary_key=True)
    thread_id: uuid.UUID = Field(foreign_key="threads.id", primary_key=True)
    last_delivered_seq: int = Field(default=0)
    last_acked_seq: int = Field(default=0)
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("CURRENT_TIMESTAMP"),
            onupdate=datetime.utcnow,
        ),
    )
