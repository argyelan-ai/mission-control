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

    # Default-Projekt fuer automatische Zuweisung bei Task-Erstellung
    # use_alter=True: bricht den FK-Zyklus boards↔projects fuer SQLAlchemy INSERT-Sortierung
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
    github_repo_name: str | None = None  # z.B. "<owner>/agar-io-clone"
    workspace_path: str | None = None  # Lokaler Pfad zum Projekt (z.B. /private/tmp/my-portfolio)
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
    # Markdown, auto-updated bei Deliverable-Registrierung und Phase-Abschluss

    parent_project_id: uuid.UUID | None = Field(
        default=None, foreign_key="projects.id", nullable=True
    )
    # Sub-Projekte: dieses Projekt ist Teil von parent_project_id

    last_active_phase_id: uuid.UUID | None = Field(
        default=None, nullable=True
    )
    # Kein FK-Constraint hier — Phase-Tabelle existiert in separatem File.
    # Wird per Migration als FK gesetzt.

    resume_briefing: str | None = None
    # Auto-generiert aus git log beim Wiederaufnehmen eines pausierten Projekts


class PlannerMessage(SQLModel, table=True):
    """Chat-Nachrichten fuer Planner und Research Sessions."""
    __tablename__ = "planner_messages"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True)
    role: str  # user|assistant|system
    content: str
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
