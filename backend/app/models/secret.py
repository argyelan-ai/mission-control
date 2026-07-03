import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, text
from sqlmodel import Column, Field, SQLModel


class Secret(SQLModel, table=True):
    """Encrypted secrets for API keys, tokens, etc.

    Values are stored Fernet-encrypted in the DB.
    The frontend only displays them masked.
    """

    __tablename__ = "secrets"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Unique key (e.g. "openai_api_key", "anthropic_api_key")
    key: str = Field(index=True, unique=True)

    # Encrypted value (Fernet ciphertext)
    encrypted_value: str = Field(sa_column=Column(Text, nullable=False))

    # Metadata for UI
    provider: str | None = None  # "openai", "anthropic", "ollama", "discord", etc.
    label: str | None = None  # Display name in UI
    description: str | None = None  # Help text

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("NOW()"),
            onupdate=datetime.utcnow,
        ),
    )
