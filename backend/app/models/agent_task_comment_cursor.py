"""Per-agent, per-task cursor fuer User-Comment-Delivery via /me/poll.

Der Poll-Endpoint liefert neue User-Kommentare an den Agent aus. Damit derselbe
Kommentar nicht mehrfach zugestellt wird, merken wir uns den letzten Comment-ID,
den der Agent bereits gesehen hat — pro (agent_id, task_id).

Siehe Plan: docs/superpowers/plans/2026-04-17-comment-delivery-and-status-sync.md
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, text
from sqlmodel import Column, Field, SQLModel


class AgentTaskCommentCursor(SQLModel, table=True):
    __tablename__ = "agent_task_comment_cursor"

    agent_id: uuid.UUID = Field(foreign_key="agents.id", primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="tasks.id", primary_key=True)
    last_seen_comment_id: uuid.UUID | None = None
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("CURRENT_TIMESTAMP"),
            onupdate=datetime.utcnow,
        ),
    )
