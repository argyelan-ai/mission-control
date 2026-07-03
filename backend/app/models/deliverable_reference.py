"""DeliverableReference — Cross-Project Deliverable Reuse.

Allows referencing a deliverable from project A in project B.
Loose linking — no cascade delete.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, text
from sqlmodel import Column, Field, SQLModel


class DeliverableReference(SQLModel, table=True):
    __tablename__ = "deliverable_references"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source_deliverable_id: uuid.UUID | None = Field(
        default=None, foreign_key="task_deliverables.id", index=True, nullable=True
    )
    target_project_id: uuid.UUID | None = Field(
        default=None, foreign_key="projects.id", index=True, nullable=True
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
