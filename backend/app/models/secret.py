import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, text
from sqlmodel import Column, Field, SQLModel


class Secret(SQLModel, table=True):
    """Verschlüsselte Secrets für API-Keys, Tokens, etc.

    Werte werden mit Fernet verschlüsselt in der DB gespeichert.
    Im Frontend werden sie nur maskiert angezeigt.
    """

    __tablename__ = "secrets"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Eindeutiger Schlüssel (z.B. "openai_api_key", "anthropic_api_key")
    key: str = Field(index=True, unique=True)

    # Verschlüsselter Wert (Fernet-Ciphertext)
    encrypted_value: str = Field(sa_column=Column(Text, nullable=False))

    # Metadaten für UI
    provider: str | None = None  # "openai", "anthropic", "ollama", "discord", etc.
    label: str | None = None  # Anzeigename im UI
    description: str | None = None  # Hilfetext

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
