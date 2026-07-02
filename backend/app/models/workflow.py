import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import JSON, DateTime, Text, text
from sqlmodel import Column, Field, SQLModel


class WorkflowTemplate(SQLModel, table=True):
    __tablename__ = "workflow_templates"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID | None = Field(default=None, foreign_key="boards.id", nullable=True)
    project_id: uuid.UUID | None = Field(default=None, foreign_key="projects.id", nullable=True)
    name: str
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    trigger_type: str = "manual"  # manual | scheduled | event
    trigger_config: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    status: str = "draft"  # draft | validated | active | archived
    current_version: int = 1
    current_definition: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    max_runtime_minutes: int = 60
    policy_profile: str = "safe"
    execution_policy: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    delivery_config: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    reflect_on: str = "manual"
    next_run_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    last_validated_at: datetime | None = Field(
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


class WorkflowTemplateVersion(SQLModel, table=True):
    __tablename__ = "workflow_template_versions"
    __table_args__ = (
        sa.UniqueConstraint("workflow_id", "version", name="uq_workflow_template_version"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workflow_id: uuid.UUID = Field(
        foreign_key="workflow_templates.id",
        sa_column_kwargs={"index": True},
    )
    version: int
    definition_snapshot: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    change_reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_by: str
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )


class WorkflowRun(SQLModel, table=True):
    __tablename__ = "workflow_runs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workflow_id: uuid.UUID = Field(
        foreign_key="workflow_templates.id",
        sa_column_kwargs={"index": True},
    )
    workflow_version: int
    definition_snapshot: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    triggered_by: str  # user | scheduler | event | resume
    trigger_payload: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    started_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    status: str = "running"  # running | paused | completed | partial | failed | stopped | force_stopped
    current_step_key: str | None = None
    context: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    total_cost_tokens: int = 0
    delivery_status: str | None = None  # pending | sent | skipped | warning | failed
    delivery_error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    delivered_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )


class WorkflowStepRun(SQLModel, table=True):
    __tablename__ = "workflow_step_runs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    run_id: uuid.UUID = Field(
        foreign_key="workflow_runs.id",
        sa_column_kwargs={"index": True},
    )
    step_key: str
    step_index: int
    step_name: str
    step_type: str  # llm | deterministic | local
    execution_mode: str = "single"
    executor_type: str | None = None  # internal_api | webhook | script_ref | local_model
    attempt: int = 1
    status: str = "pending"  # pending | running | done | skipped | failed | interrupted
    rendered_input: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    session_key: str | None = None
    output_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    output_json: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    stdout: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    stderr: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    exit_code: int | None = None
    http_status: int | None = None
    artifacts: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    evaluation_result: Any | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    error_code: str | None = None
    error_message: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    tokens_used: int = 0
