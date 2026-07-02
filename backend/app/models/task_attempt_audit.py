"""Audit trail for dispatch_attempt_id changes.

Single Source of Truth for every set/clear of tasks.dispatch_attempt_id.
Written exclusively via app.services.dispatch_attempt_audit.{set_,clear_}
dispatch_attempt_id — never directly. Forensics use:

    SELECT created_at, caller, reason, old_attempt, new_attempt
    FROM task_attempt_audit
    WHERE task_id = '<uuid>'
    ORDER BY created_at;

Row is kept for the lifetime of the parent task (CASCADE on delete).
"""
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, text
from sqlmodel import Field, SQLModel


class TaskAttemptAudit(SQLModel, table=True):
    __tablename__ = "task_attempt_audit"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    task_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )

    # Both nullable: clear() writes old_attempt=<prev_uuid>, new_attempt=None.
    # Initial set writes old_attempt=None, new_attempt=<new_uuid>.
    old_attempt: uuid.UUID | None = Field(default=None, nullable=True)
    new_attempt: uuid.UUID | None = Field(default=None, nullable=True)

    # Caller identifies the code path that performed the write — used for
    # forensic grouping. Suggested values match the helper kwargs:
    #   "auto_dispatch", "agent_poll", "agent_subtask_create", "d1_silent_retry",
    #   "user_stop", "user_resume", "approval", "task_lifecycle",
    #   "watchdog_phase_complete", "watchdog_undispatched_recovery",
    #   "agent_comment"
    caller: str = Field(max_length=64, nullable=False)

    # Reason is free-form context (race outcome, timeout duration, etc.).
    reason: str | None = Field(default=None, max_length=256)

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("NOW()"),
            nullable=False,
        ),
    )

    __table_args__ = (
        Index("ix_task_attempt_audit_task_id_created_at", "task_id", "created_at"),
    )
