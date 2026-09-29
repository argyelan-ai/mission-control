"""Skill-lab tables (frozen, see docs/produkt/landkarte.yaml F-skill-lab).

The playbook/automation/workflow layer that once wrote these rows was removed
in E5 (migration 0207); the tables stay.
"""
import uuid
from datetime import datetime
from app.utils import utcnow
from typing import Any

from sqlalchemy import JSON, DateTime, Text, text
from sqlmodel import Column, Field, SQLModel


class SkillPack(SQLModel, table=True):
    __tablename__ = "skill_packs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    key: str = Field(index=True, unique=True)
    name: str
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    category: str = "general"
    status: str = "active"
    icon: str | None = None
    color: str | None = None
    skill_keys: Any = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    guidance: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_by: str = "system"
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=utcnow),
    )


class SkillCandidate(SQLModel, table=True):
    __tablename__ = "skill_candidates"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID | None = Field(default=None, foreign_key="boards.id", nullable=True)
    project_id: uuid.UUID | None = Field(default=None, foreign_key="projects.id", nullable=True)
    # Plain columns since the playbooks/automations tables were dropped (0207).
    playbook_id: uuid.UUID | None = Field(default=None, nullable=True)
    automation_id: uuid.UUID | None = Field(default=None, nullable=True)
    candidate_type: str = "new_skill"  # new_skill | patch | playbook_improvement
    title: str
    summary: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    target_skill_key: str | None = None
    status: str = "open"  # open | approved | rejected | applied
    evidence: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    source_run_ids: Any = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    draft_skill_content: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    proposed_by: str
    reviewed_by: str | None = None
    reviewed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=utcnow),
    )
