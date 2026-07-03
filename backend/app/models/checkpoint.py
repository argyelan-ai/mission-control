"""Task Checkpoints — agent-saved intermediate states for crash recovery.

Minimal V1: agent writes checkpoint, recovery reads it.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, JSON, text
from sqlmodel import Column, Field, SQLModel


class TaskCheckpoint(SQLModel, table=True):
    __tablename__ = "task_checkpoints"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="tasks.id", index=True)
    agent_id: uuid.UUID = Field(foreign_key="agents.id")

    # auto | manual (agent decides)
    checkpoint_type: str = Field(default="manual")

    # Brief work status (free text, ~200 characters)
    state_summary: str

    # Structured data: completed steps, next steps, artifacts
    context_data: dict | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"))
    )
