import uuid
from datetime import datetime
from typing import Any, ClassVar, Optional

from sqlalchemy import DateTime, ForeignKey, JSON, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Column, Field, SQLModel


class ScheduledJobRun(SQLModel, table=True):
    __tablename__ = "scheduled_job_runs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    job_id: uuid.UUID = Field(
        foreign_key="scheduled_jobs.id",
        sa_column_kwargs={"index": True},
    )
    started_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    finished_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    status: str  # "success" | "failed" | "skipped" | "running"
    error: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    detail: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    retry_attempt: int = Field(default=0)

    # FK to tasks.id — set when the run creates a task (migration 0103)
    task_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(
            "task_id",
            PGUUID(as_uuid=True),
            ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Populated via join in API responses — not stored in DB
    task_title: ClassVar[Optional[str]] = None
    task_status: ClassVar[Optional[str]] = None
