"""Deploy-History — Tracking aller Deployments."""
import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class DeployHistory(SQLModel, table=True):
    __tablename__ = "deploy_history"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    service: str  # "backend", "frontend", "caddy"
    action: str  # "rebuild", "restart", "rollback", "backup"
    triggered_by: str  # Agent-Name oder "user"
    agent_id: uuid.UUID | None = Field(default=None, foreign_key="agents.id")
    task_id: uuid.UUID | None = Field(default=None)
    success: bool = True
    rolled_back: bool = False
    health_status: str | None = None  # "healthy", "unhealthy", "unknown"
    duration_seconds: float | None = None
    error: str | None = None
    logs_tail: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
