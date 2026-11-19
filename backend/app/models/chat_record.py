"""MC's own chat record (ADR-103, first slice of "chats instead of fixed agents").

Three additive tables:

- ``chats`` — the unit of work: title, pair (harness × runtime × model),
  owner, and — for chats imported from an existing agent — ``legacy_agent_id``.
- ``chat_sessions`` — one row per NATIVE session of one harness (one Claude
  Code / omp transcript file). The record of a chat runs through all of them.
- ``chat_events`` — the record itself: append-only, one row per event in the
  harness-neutral event format of ``services/transcript_chat.py`` (the dict
  goes into ``payload`` unchanged, apart from a size cap on tool results).

Nothing here drives an agent. Everything is behind ``settings.chat_record_enabled``
(default off); the way back is "flag off + truncate these three tables".
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, Column, DateTime, Index, Integer, Text, UniqueConstraint, false, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.utils import utcnow

#: JSON on SQLite (test lane), JSONB on Postgres — same as the migration.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Chat(SQLModel, table=True):
    __tablename__ = "chats"
    __table_args__ = (
        # One DEFAULT chat per agent (migration 0229; before: one chat per
        # agent) — the importer and the live sink route every session that no
        # other chat pinned into it, so it must never fork.
        Index(
            "uq_chats_agent_default",
            "legacy_agent_id",
            unique=True,
            postgresql_where=text("agent_default"),
            sqlite_where=text("agent_default"),
        ),
        # A native session belongs to at most one chat of an agent (S2).
        Index(
            "uq_chats_agent_native",
            "legacy_agent_id",
            "native_session_id",
            unique=True,
            postgresql_where=text("native_session_id IS NOT NULL"),
            sqlite_where=text("native_session_id IS NOT NULL"),
        ),
        Index("ix_chats_legacy_agent_id", "legacy_agent_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    title: str = Field(max_length=200)
    owner_user_id: uuid.UUID | None = Field(default=None, foreign_key="users.id", nullable=True)
    project_id: uuid.UUID | None = Field(default=None, foreign_key="projects.id", nullable=True)
    workspace_path: str | None = None
    harness: str | None = Field(default=None, max_length=40)
    runtime_id: uuid.UUID | None = Field(default=None, foreign_key="runtimes.id", nullable=True)
    model: str | None = Field(default=None, max_length=200)
    permission_level: str | None = Field(default=None, max_length=40)
    pinned: bool = False
    legacy_agent_id: uuid.UUID | None = Field(default=None, foreign_key="agents.id", nullable=True)
    #: The agent's own chat (the importer's "one chat per agent"): it takes
    #: every native session no other chat of the agent pinned. Further chats
    #: on the same agent (S2, "many chats per harness") have ``False``.
    agent_default: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=false())
    )
    #: The native session this chat drives (S2). Claude Code: chosen by MC
    #: at creation (``claude --session-id``). omp: set once ACP
    #: ``session/new`` returned it. ``None`` on default chats and on chats
    #: whose first session has not started yet.
    native_session_id: str | None = Field(default=None, max_length=200)
    #: Next free ``chat_events.seq`` — bumped atomically by the writer
    #: (``UPDATE … SET next_seq = next_seq + n RETURNING``) so the live sink
    #: and the importer never hand out the same number.
    next_seq: int = Field(default=1, sa_column=Column(BigInteger, nullable=False, server_default="1"))
    last_activity_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    archived_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")),
    )


class ChatSession(SQLModel, table=True):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        UniqueConstraint("chat_id", "native_session_id", name="uq_chat_sessions_native"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    chat_id: uuid.UUID = Field(foreign_key="chats.id", index=True, ondelete="CASCADE")
    harness: str | None = Field(default=None, max_length=40)
    runtime_id: uuid.UUID | None = Field(default=None, foreign_key="runtimes.id", nullable=True)
    model: str | None = Field(default=None, max_length=200)
    #: ``container`` / ``host``.
    place: str | None = Field(default=None, max_length=20)
    native_session_id: str = Field(max_length=200)
    transcript_path: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    cwd: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    #: Bytes of the native file already read into the record (importer) — a
    #: file that has not grown since is skipped on the next run.
    imported_bytes: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, server_default="0"))
    #: The last import of this file failed: the error class (no message — it
    #: can quote file content), the file size it failed at, and when. A file
    #: that has not changed since is not read again; the read API shows the
    #: mark ("record incomplete") instead of a silent gap.
    last_error: str | None = Field(default=None, max_length=200)
    failed_bytes: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, server_default="0"))
    last_error_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    ended_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    end_reason: str | None = Field(default=None, max_length=40)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")),
    )


class ChatEvent(SQLModel, table=True):
    __tablename__ = "chat_events"
    __table_args__ = (
        UniqueConstraint("chat_id", "seq", name="uq_chat_events_seq"),
        # The dedupe key: the importer and the live tailer derive the same
        # key for the same event, so a re-run or an overlap never doubles it.
        UniqueConstraint("chat_id", "source_key", name="uq_chat_events_source"),
        Index("ix_chat_events_chat_order", "chat_id", "ts", "seq"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    chat_id: uuid.UUID = Field(foreign_key="chats.id", ondelete="CASCADE")
    session_id: uuid.UUID = Field(foreign_key="chat_sessions.id", index=True, ondelete="CASCADE")
    seq: int = Field(sa_column=Column(BigInteger, nullable=False))
    source_key: str = Field(max_length=300)
    ts: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    kind: str = Field(max_length=40)
    role: str | None = Field(default=None, max_length=20)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONType, nullable=False))
    #: Plain text for search (message / thinking text, tool title, command).
    search_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    payload_bytes: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    #: sha256 of the stored JSON — a re-read event with the same hash is not
    #: written again (no rewrite, no new seq).
    payload_hash: str | None = Field(default=None, max_length=64)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")),
    )
