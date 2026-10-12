import uuid
from datetime import datetime
from app.utils import utcnow

from sqlalchemy import DateTime, UniqueConstraint, text
from sqlmodel import Column, Field, SQLModel

#: Written by a person or an agent's explicit `mc checklist` call.
SOURCE_MANUAL = "manual"
#: Mirrored from the model's own todo list (services/checklist_todo_sync.py).
SOURCE_AGENT_TODO = "agent_todo"


class TaskChecklistItem(SQLModel, table=True):
    __tablename__ = "task_checklist_items"
    # One synced row per todo item and task. Manual rows have source_key NULL
    # (NULLs never collide), so this only guards the sync's own idempotency.
    __table_args__ = (UniqueConstraint("task_id", "source_key", name="uq_checklist_task_source_key"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="tasks.id", index=True)
    agent_id: uuid.UUID | None = Field(default=None, foreign_key="agents.id", nullable=True)

    title: str
    status: str = Field(default="pending")  # pending | in_progress | done | blocked | skipped
    sort_order: int = Field(default=0)
    # Where the row came from (SOURCE_MANUAL / SOURCE_AGENT_TODO). The todo
    # sync only ever touches its own rows; manual rows are never changed.
    source: str = Field(default=SOURCE_MANUAL, sa_column_kwargs={"server_default": SOURCE_MANUAL})
    # Stable identity of a synced row inside its task: the normalised item
    # text (+ "#n" for a repeated text). NULL for manual rows.
    source_key: str | None = Field(default=None, max_length=320, nullable=True)

    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
