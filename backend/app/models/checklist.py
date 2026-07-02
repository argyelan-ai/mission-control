import uuid
from datetime import datetime

from sqlalchemy import DateTime, text
from sqlmodel import Column, Field, SQLModel


class TaskChecklistItem(SQLModel, table=True):
    __tablename__ = "task_checklist_items"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="tasks.id", index=True)
    agent_id: uuid.UUID | None = Field(default=None, foreign_key="agents.id", nullable=True)

    title: str
    status: str = Field(default="pending")  # pending | in_progress | done | blocked | skipped
    sort_order: int = Field(default=0)

    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
