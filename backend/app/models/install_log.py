import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, text
from sqlmodel import Column, Field, SQLModel


class InstallLog(SQLModel, table=True):
    __tablename__ = "install_log"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    approval_id: uuid.UUID | None = Field(
        default=None, foreign_key="approvals.id", nullable=True
    )
    requester_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )
    target_agent_id: uuid.UUID = Field(foreign_key="agents.id", index=True)
    action_type: str = Field(index=True)
    resource_name: str
    source: str | None = None
    result: str  # "success" | "failed" | "rolled_back"
    error: str | None = None
    installed_version: str | None = None
    previous_state: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True), server_default=text("NOW()"), index=True
        ),
    )
