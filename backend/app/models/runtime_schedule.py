import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Text
from sqlmodel import Column, Field, SQLModel


class RuntimeSchedule(SQLModel, table=True):
    __tablename__ = "runtime_schedules"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    runtime_id: str  # Key aus runtimes.json, z.B. "nemotron-super"
    name: str
    action: str  # "start" | "stop"
    time_of_day: str  # "HH:MM" im 24h-Format, z.B. "22:00"
    days: str  # "daily" | "weekdays" | "weekends"
    unload_first: bool = Field(default=False)  # lms unload --all vorher (nur lmstudio)
    enabled: bool = Field(default=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class RuntimeScheduleRun(SQLModel, table=True):
    __tablename__ = "runtime_schedule_runs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    schedule_id: uuid.UUID = Field(foreign_key="runtime_schedules.id")
    executed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    success: bool
    message: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
