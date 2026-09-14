"""Per-agent, per-task cursor for user-comment delivery via /me/poll.

The poll endpoint delivers new user comments to the agent. To avoid delivering
the same comment more than once, we track the last comment ID the agent has
already seen — per (agent_id, task_id).

See plan: docs/superpowers/plans/2026-04-17-comment-delivery-and-status-sync.md

``last_signalled_comment_id`` (B1, PR #519 Rex review) is a SEPARATE watermark
for the heartbeat's soft-interrupt channel (``_collect_heartbeat_control`` in
routers/agents.py) — deliberately not the same field as
``last_seen_comment_id``. That column means "delivered via /me/poll"; the
heartbeat runs on every beat, independent of polling, and advancing the same
field there marked comments as seen before poll ever handed them to the
agent, silently swallowing delivery. Two readers, two meanings, two columns.
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
    last_signalled_comment_id: uuid.UUID | None = None
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("CURRENT_TIMESTAMP"),
            onupdate=datetime.utcnow,
        ),
    )
