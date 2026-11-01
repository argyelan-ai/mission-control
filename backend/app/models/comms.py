"""Talking across harnesses — conversation and cursor rows (ADR-100).

A session-to-session conversation is an ordinary `threads` row of kind
"session" (messages live in `messages`, written by `messaging.post_message`,
§6 rule 3). `comms_conversations` adds what only such a thread needs: its two
participants and the loop brake. Participants are principal refs
("agent:<uuid>" or "session:<uuid>") — generic on purpose, because a
terminal session is its own principal, never a row in `agents` (§6 rule 1).

`comms_cursors` is the read position for principals that have no
`agent_thread_cursor` (terminal sessions); agents keep using their own.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, Uuid, text
from sqlmodel import Column, Field, SQLModel

from app.utils import utcnow


class CommsConversation(SQLModel, table=True):
    __tablename__ = "comms_conversations"
    __table_args__ = (
        # One conversation per pair; a_ref < b_ref (sorted), so the pair has
        # exactly one spelling. Must match migration 0225_session_conversations.
        UniqueConstraint("a_ref", "b_ref", name="uq_comms_conversations_pair"),
    )

    thread_id: uuid.UUID = Field(
        sa_column=Column(Uuid, ForeignKey("threads.id", ondelete="CASCADE"), primary_key=True)
    )
    a_ref: str = Field(sa_column=Column(String(80), nullable=False, index=True))
    b_ref: str = Field(sa_column=Column(String(80), nullable=False, index=True))
    # Loop brake (§6 rule 4): messages since the operator last let it run.
    replies_without_operator: int = Field(default=0)
    paused: bool = Field(
        default=False, sa_column=Column(Boolean, server_default=text("false"), nullable=False)
    )
    brake_approval_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(Uuid, ForeignKey("approvals.id", ondelete="SET NULL"), nullable=True),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    last_message_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class CommsCursor(SQLModel, table=True):
    __tablename__ = "comms_cursors"

    principal_ref: str = Field(sa_column=Column(String(80), primary_key=True))
    thread_id: uuid.UUID = Field(
        sa_column=Column(Uuid, ForeignKey("threads.id", ondelete="CASCADE"), primary_key=True)
    )
    last_acked_seq: int = Field(default=0)
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")),
    )


class CommsGuestCard(SQLModel, table=True):
    """A guest card the operator issues for terminal sessions (ADR-100 §6
    rule 1). Its token only enrolls a session (`POST /agent/comms/hello`);
    stored as a SHA-256 hash, shown once. Revoking it ends its sessions."""

    __tablename__ = "comms_guest_cards"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    label: str = Field(sa_column=Column(String(120), nullable=False))
    token_hash: str = Field(sa_column=Column(String(64), nullable=False, unique=True))
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    revoked_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    created_by_user_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )


class CommsSession(SQLModel, table=True):
    """A terminal session as its own principal: address
    `harness@machine:folder`, a session token (SHA-256 hash) with only
    comms:talk, an expiry no later than its card's. Never a row in agents."""

    __tablename__ = "comms_sessions"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    card_id: uuid.UUID = Field(
        sa_column=Column(Uuid, ForeignKey("comms_guest_cards.id", ondelete="CASCADE"), nullable=False, index=True)
    )
    address: str = Field(sa_column=Column(String(130), nullable=False, index=True))
    harness: str = Field(sa_column=Column(String(40), nullable=False))
    machine: str = Field(sa_column=Column(String(40), nullable=False))
    folder: str = Field(sa_column=Column(String(40), nullable=False))
    token_hash: str = Field(sa_column=Column(String(64), nullable=False, unique=True))
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    revoked_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
