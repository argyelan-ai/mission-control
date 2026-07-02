import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
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
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )


class Playbook(SQLModel, table=True):
    __tablename__ = "playbooks"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workflow_id: uuid.UUID | None = Field(default=None, foreign_key="workflow_templates.id", nullable=True)
    board_id: uuid.UUID | None = Field(default=None, foreign_key="boards.id", nullable=True)
    project_id: uuid.UUID | None = Field(default=None, foreign_key="projects.id", nullable=True)
    skill_pack_id: uuid.UUID | None = Field(default=None, foreign_key="skill_packs.id", nullable=True)
    default_agent_id: uuid.UUID | None = Field(default=None, foreign_key="agents.id", nullable=True)
    kind: str = Field(index=True)
    name: str
    summary: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    goal: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    scope: str = "global"
    status: str = "draft"  # draft | review | active | archived
    current_version: int = 1
    input_contract: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    output_contract: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    current_config: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    preview_markdown: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    extra_metadata: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    review_notes: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_by: str
    approved_by: str | None = None
    approved_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )


class PlaybookVersion(SQLModel, table=True):
    __tablename__ = "playbook_versions"
    __table_args__ = (
        sa.UniqueConstraint("playbook_id", "version", name="uq_playbook_version"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    playbook_id: uuid.UUID = Field(foreign_key="playbooks.id", sa_column_kwargs={"index": True})
    version: int
    snapshot: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    change_reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_by: str
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )


class Automation(SQLModel, table=True):
    __tablename__ = "automations"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    playbook_id: uuid.UUID = Field(foreign_key="playbooks.id", sa_column_kwargs={"index": True})
    workflow_id: uuid.UUID | None = Field(default=None, foreign_key="workflow_templates.id", nullable=True)
    board_id: uuid.UUID | None = Field(default=None, foreign_key="boards.id", nullable=True)
    project_id: uuid.UUID | None = Field(default=None, foreign_key="projects.id", nullable=True)
    name: str
    summary: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    status: str = "draft"  # draft | active | paused | archived
    trigger_type: str = "manual"
    trigger_config: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    delivery_config: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    runtime_overrides: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    last_run_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    next_run_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_by: str
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )


class SkillCandidate(SQLModel, table=True):
    __tablename__ = "skill_candidates"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID | None = Field(default=None, foreign_key="boards.id", nullable=True)
    project_id: uuid.UUID | None = Field(default=None, foreign_key="projects.id", nullable=True)
    playbook_id: uuid.UUID | None = Field(default=None, foreign_key="playbooks.id", nullable=True)
    automation_id: uuid.UUID | None = Field(default=None, foreign_key="automations.id", nullable=True)
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
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )
