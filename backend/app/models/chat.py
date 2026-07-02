import uuid
from datetime import datetime

from sqlalchemy import DateTime, text
from sqlmodel import Column, Field, SQLModel


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chat_messages"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Polymorphic: board channel or agent DM
    channel_type: str  # 'board' | 'agent_dm'
    board_id: uuid.UUID | None = Field(
        default=None, foreign_key="boards.id", nullable=True, index=True
    )
    agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True, index=True
    )

    # Sender
    sender_type: str  # 'user' | 'agent'
    sender_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )

    content: str
    is_system_message: bool = False

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
