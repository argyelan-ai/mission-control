"""Direct messages between agents (``agent_messages``).

Split out of the removed meetings module (E5); the table stays, the meeting
tables were dropped in migration 0208.
"""
import uuid
from datetime import datetime
from app.utils import utcnow

from sqlalchemy import DateTime, Text, text
from sqlmodel import Column, Field, SQLModel


class AgentMessage(SQLModel, table=True):
    """Direct messages between agents (independent of meetings)."""
    __tablename__ = "agent_messages"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    thread_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True)
    from_agent_id: uuid.UUID = Field(foreign_key="agents.id", index=True)
    to_agent_id: uuid.UUID = Field(foreign_key="agents.id", index=True)
    content: str = Field(sa_column=Column(Text))

    status: str = "pending"  # pending | delivered | replied | failed
    reply_to_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_messages.id", nullable=True
    )

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
