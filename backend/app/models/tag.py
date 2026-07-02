import uuid
from datetime import datetime

from sqlalchemy import DateTime, text
from sqlmodel import Column, Field, SQLModel


class Tag(SQLModel, table=True):
    __tablename__ = "tags"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(unique=True)
    slug: str = Field(unique=True, index=True)
    color: str | None = None
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )


class TagAssignment(SQLModel, table=True):
    __tablename__ = "tag_assignments"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    tag_id: uuid.UUID = Field(foreign_key="tags.id", index=True)
    task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", nullable=True
    )
    project_id: uuid.UUID | None = Field(
        default=None, foreign_key="projects.id", nullable=True
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
