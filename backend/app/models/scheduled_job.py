import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Column, Field, SQLModel

from sqlalchemy import JSON


# Valid schedule_type values: "daily" | "weekdays" | "interval" | "cron" | "weekly_custom"


class ScheduledJob(SQLModel, table=True):
    __tablename__ = "scheduled_jobs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    description: Optional[str] = None
    enabled: bool = Field(default=True)

    # Schedule
    schedule_type: str  # "daily" | "weekdays" | "interval" | "cron" | "weekly_custom"
    schedule_time: Optional[str] = None          # "07:30" for daily/weekdays
    schedule_interval_hours: Optional[int] = None  # for interval

    # New schedule fields (migration 0103)
    schedule_cron: Optional[str] = Field(default=None)
    schedule_weekdays: Optional[List[int]] = Field(default=None, sa_column=Column(JSON))
    start_date: Optional[date] = Field(default=None)
    end_date: Optional[date] = Field(default=None)

    # Action
    action_type: str  # "chat_send" | "api_call" | "create_task"
    agent_id: Optional[uuid.UUID] = Field(default=None, foreign_key="agents.id", nullable=True)
    agent_name: Optional[str] = None   # Fallback for lookup when agent_id is null
    message: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    api_endpoint: Optional[str] = None

    # Task payload + tags (migration 0103)
    task_payload: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    tags: List[str] = Field(default_factory=list, sa_column=Column(JSON))

    # Retry
    retry_max: int = Field(default=0)
    retry_delay_minutes: int = Field(default=5)

    # Dependencies (self-referential FK)
    depends_on_job_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(
            "depends_on_job_id",
            PGUUID(as_uuid=True),
            ForeignKey("scheduled_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Notifications
    notify_on_failure: bool = Field(default=False)

    # Discord delivery
    discord_channel_id: Optional[str] = None
    discord_channel_name: Optional[str] = None

    # create_task action
    task_board_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="boards.id", nullable=True
    )
    task_title: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    task_priority: Optional[str] = None
    task_skip_review: bool = Field(default=False)

    # Snooze + failure tracking (migration 0103)
    snoozed_until: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    consecutive_failures: int = Field(default=0)

    # Run history (last run — backward compat)
    last_run_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    last_run_status: Optional[str] = None   # "success" | "failed"
    last_run_error: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    next_run_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
