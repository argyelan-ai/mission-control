"""Credentials Vault — verschluesselte Zugangsdaten fuer Agent-Tasks."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, text
from sqlmodel import Column, Field, SQLModel


class Credential(SQLModel, table=True):
    """Verschluesselte Credential-Eintraege (Logins, Tokens, Freitext).

    encrypted_data enthaelt ein Fernet-verschluesseltes JSON-Objekt:
    - login:  {"username": "...", "password": "..."}
    - token:  {"token": "..."}
    - custom: {"content": "..."}
    """

    __tablename__ = "credentials"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(index=True)
    credential_type: str = Field(default="login")  # login | token | custom
    encrypted_data: str = Field(sa_column=Column(Text, nullable=False))
    url: str | None = None
    notes: str | None = None

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
