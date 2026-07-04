import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, text, Uuid
from sqlmodel import Column, Field, SQLModel


class BoardGroup(SQLModel, table=True):
    __tablename__ = "board_groups"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    slug: str = Field(unique=True, index=True)
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    sort_order: int = 0
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )


class Board(SQLModel, table=True):
    __tablename__ = "boards"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_group_id: uuid.UUID | None = Field(
        default=None, foreign_key="board_groups.id", nullable=True
    )
    name: str
    slug: str = Field(unique=True, index=True)
    description: str | None = None
    icon: str | None = None
    color: str | None = None

    # Default project for automatic assignment on task creation
    # use_alter=True: breaks the FK cycle boards↔projects for SQLAlchemy INSERT ordering
    default_project_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid(as_uuid=True),
            ForeignKey("projects.id", use_alter=True, name="fk_boards_default_project_id"),
            nullable=True,
        ),
    )

    # Workflow rules
    require_approval_for_done: bool = False
    require_review_before_done: bool = False
    only_lead_can_change_status: bool = False
    auto_dispatch_enabled: bool = False
    # Lead-first Blocker-Triage: Minuten, die der Board-Lead Zeit hat, einen
    # Blocker selbst zu loesen, bevor der Operator ein Approval bekommt.
    # 0 = Triage aus (jeder Blocker geht direkt an den Operator).
    blocker_triage_minutes: int = Field(default=15, nullable=False)

    # Goal tracking
    objective: str | None = None
    success_metrics: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    target_date: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    # Stats cache
    stats_cache: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    stats_cache_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    sort_order: int = 0
    is_archived: bool = False
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )


class Project(SQLModel, table=True):
    __tablename__ = "projects"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID = Field(foreign_key="boards.id", index=True)
    name: str
    description: str | None = None
    project_type: str = "feature"  # feature|website|content|research|automation|design|free
    status: str = "draft"  # draft|planning|active|paused|done|archived
    priority: str = "medium"
    plan_summary: str | None = None
    progress_pct: int = Field(default=0, ge=0, le=100)
    github_repo_url: str | None = None
    github_repo_name: str | None = None  # e.g. "<owner>/agar-io-clone"
    repo_id: uuid.UUID | None = Field(
        default=None, foreign_key="repos.id", nullable=True, index=True
    )
    # FK into the repos registry (ADR-050). Legacy github_repo_url/name stay
    # synced on link so existing clone/PR flows keep working.
    workspace_path: str | None = None  # Local path to the project (e.g. /private/tmp/my-portfolio)
    project_config: dict | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    created_by: str = "user"  # user|agent|planner
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
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )

    # Project System Extensions
    briefing_doc: str | None = None
    # Markdown, auto-updated on deliverable registration and phase completion

    parent_project_id: uuid.UUID | None = Field(
        default=None, foreign_key="projects.id", nullable=True
    )
    # Sub-projects: this project is part of parent_project_id

    last_active_phase_id: uuid.UUID | None = Field(
        default=None, nullable=True
    )
    # No FK constraint here — the phase table exists in a separate file.
    # Set as FK via migration.

    resume_briefing: str | None = None
    # Auto-generated from git log when resuming a paused project


class PlannerMessage(SQLModel, table=True):
    """Chat messages for planner and research sessions."""
    __tablename__ = "planner_messages"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True)
    role: str  # user|assistant|system
    content: str
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
