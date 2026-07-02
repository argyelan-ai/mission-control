"""ProjectPhase — Phase innerhalb eines Projekts.

Eine Phase gruppiert Tasks und hat eigene Dependencies, Git-Branch,
und optionale Approval-Gates.
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, JSON, text
from sqlmodel import Column, Field, SQLModel


class ProjectPhase(SQLModel, table=True):
    __tablename__ = "project_phases"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True)

    title: str
    order: int = Field(default=0)
    status: str = Field(default="pending")
    # pending | active | completed | blocked | awaiting_approval

    depends_on_phases: list[Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    gate_required: bool = Field(default=False)

    failure_policy: str = Field(default="retry")
    # retry | halt | skip

    default_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )

    git_branch: str | None = None

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
