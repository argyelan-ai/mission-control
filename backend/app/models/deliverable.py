"""Task Deliverables — Agent-registrierte Ergebnisse pro Task.

V2: scope, content, tags, is_pinned, is_reusable, git_commit_hash hinzugefügt.
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, JSON, text
from sqlmodel import Column, Field, SQLModel


class TaskDeliverable(SQLModel, table=True):
    __tablename__ = "task_deliverables"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="tasks.id", index=True)
    # Phase 26 Plan 04 (HERM-11/F4): nullable because admin-scoped POST
    # (tasks.py /boards/{bid}/tasks/{tid}/deliverables) inserts without an
    # agent token — the operator via UI or Hermes via MCP using admin JWT.
    agent_id: uuid.UUID | None = Field(default=None, foreign_key="agents.id")

    deliverable_type: str  # screenshot | file | url | artifact | document | data

    title: str
    path: str | None = None
    description: str | None = None

    # V2: Inhalt direkt gespeichert (für Markdown-Deliverables)
    content: str | None = None

    # V2: Sichtbarkeits-Ebene
    scope: str = Field(default="task")  # task | phase | project

    # V2: Tags für Suche (JSON Array)
    tags: list[Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    # V2: Kontext-Injection Flags
    is_pinned: bool = Field(default=False)
    is_reusable: bool = Field(default=False)

    # V2: Git-Traceability
    git_commit_hash: str | None = None

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"))
    )
