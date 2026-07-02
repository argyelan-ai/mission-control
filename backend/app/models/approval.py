import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, text
from sqlmodel import Column, Field, SQLModel


class Approval(SQLModel, table=True):
    __tablename__ = "approvals"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID = Field(foreign_key="boards.id", index=True)
    task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", nullable=True
    )
    # Nullable: Watchdog-Approvals (z.B. review_stuck) betreffen auch Tasks
    # ohne zugewiesenen Agent — NOT NULL liess jeden Watchdog-Tick crashen.
    agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )
    action_type: str  # 'mark_done', 'deploy', 'config_change', 'question'
    description: str
    payload: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    confidence: float | None = None
    status: str = "pending"  # pending | approved | rejected | expired

    resolved_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    resolver_note: str | None = None
    failure_reason: str | None = None  # set by InstallExecutor on install failure
    expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    # Autonomy Level (Theme 3: Wave 2)
    autonomy_level: str | None = None  # "L1" | "L2" | "L3" | null

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
