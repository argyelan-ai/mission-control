"""Interaction Model 2.0 — Thread, Message, AgentThreadCursor, UserThreadCursor.

Thread = a conversation container (per-task, side thread, or DM).
Message = a single entry in a thread's append-only log (seq unique per thread).
AgentThreadCursor = per-agent read/ack position within a thread, used by
the /me/poll-style delivery flow (mirrors AgentTaskCommentCursor's
composite-PK pattern).
UserThreadCursor = per-user read position within a thread, backing
`my_read_seq` in the user-side thread READ API (same composite-PK pattern).

See app.comm_constants for the canonical MESSAGE_TYPES/THREAD_KINDS/etc.
vocab — validated at the service layer (Task 3), not enforced here.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlmodel import Column, Field, SQLModel


class Thread(SQLModel, table=True):
    __tablename__ = "threads"
    __table_args__ = (
        # Ein Agent hat hoechstens EINEN DM-Thread mit dem Operator. Partiell,
        # damit Task-/Side-Threads (agent_id IS NULL) unberuehrt bleiben.
        # Muss identisch in Migration 0165 stehen: Tests bauen die Tabellen aus
        # diesem Modell, Produktion aus der Migration — nur wenn beide denselben
        # Index tragen, prueft der Test tatsaechlich das Produktionsverhalten.
        Index(
            "uq_threads_dm_per_agent",
            "agent_id",
            unique=True,
            sqlite_where=text("kind = 'dm'"),
            postgresql_where=text("kind = 'dm'"),
        ),
        # 1:1 Telegram-Thema <-> Thread. Muss identisch in Migration 0166 stehen:
        # Tests bauen die Tabellen aus dem Modell, Produktion aus der Migration —
        # nur wenn beide denselben Constraint tragen, prueft der Test das
        # Produktionsverhalten (der Fehler aus PR #171). NULL ist im Sinne von
        # UNIQUE kein Wert: beliebig viele Threads bleiben ohne Thema.
        UniqueConstraint("telegram_topic_id", name="uq_threads_telegram_topic_id"),
        # 1:1 Slack-Thread <-> MC-Thread, gleiche Begruendung wie oben (Modell
        # und Migration 0170 muessen denselben Constraint tragen). Slack macht
        # NULL nicht selbst eindeutig — mehrere ungemappte Threads bleiben also
        # erlaubt, genau wie bei Telegram.
        UniqueConstraint("slack_thread_ts", name="uq_threads_slack_thread_ts"),
        # Ein Task hat hoechstens EINEN Task-Thread. Partiell (kind='task'),
        # damit side-Threads desselben Tasks unberuehrt bleiben. Muss identisch
        # in Migration 0173 stehen (gleiche Begruendung wie oben): ein stale
        # Task-Objekt liess den Dispatcher am 2026-08-04 einen zweiten Thread
        # anlegen — die Operator-Nachricht im ersten wurde dadurch unsichtbar.
        Index(
            "uq_threads_task_per_task",
            "task_id",
            unique=True,
            sqlite_where=text("kind = 'task'"),
            postgresql_where=text("kind = 'task'"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    kind: str  # "task" | "side" | "dm" (see comm_constants.THREAD_KINDS)
    # ondelete=SET NULL (mc-task-delete-guard): a thread survives its task's
    # deletion — same rationale as bench_entries.task_id — so a deleted task
    # never RESTRICTs against a thread that outlives it.
    task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(Uuid, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True),
    )
    project_id: uuid.UUID | None = Field(default=None, foreign_key="projects.id", nullable=True, index=True)
    # Gespraechspartner bei kind="dm" (Mark <-> dieser Agent). Bei task/side
    # None — dort ergibt sich die Teilnahme aus Aufgabe bzw. Erwaehnung.
    # ondelete=CASCADE: ein DM-Thread ohne seinen Agenten hat keine Bedeutung.
    agent_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid, ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
        ),
    )
    # Telegram-Forum-Thema (message_thread_id) dieses Threads. NULL = noch keins
    # (lazy angelegt bei der ersten Nachricht). Das Allgemein-Thema hat keine
    # eigene ID — es bleibt NULL, seine Nachrichten gehen ohne message_thread_id.
    telegram_topic_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    # Slack-Thread (`thread_ts`, z.B. "1753699200.001900") dieses Threads im
    # Standardkanal. NULL = noch keiner (lazy bei der ersten Nachricht angelegt).
    # Der Allgemein-Chat (kind="dm") bekommt bewusst KEINEN eigenen Slack-Thread
    # — er schreibt in den Kanal selbst, analog zum Telegram-Allgemein-Thema.
    # Slack liefert die ts als String; sie sieht aus wie eine Zahl, ist aber
    # keine (fuehrende/nachlaufende Nullen sind bedeutungstragend).
    slack_thread_ts: str | None = Field(default=None, nullable=True)
    title: str | None = None
    summary: str | None = None
    summary_through_seq: int | None = None
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    closed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_messages_thread_seq"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    thread_id: uuid.UUID = Field(foreign_key="threads.id", index=True)
    seq: int
    sender_type: str  # "user" | "agent" | "system" (see comm_constants)
    sender_id: uuid.UUID | None = Field(default=None, foreign_key="agents.id", nullable=True)
    message_type: str  # "message" | "question" | "status" | "decision" | "system"
    body: str
    reply_to: uuid.UUID | None = Field(default=None, foreign_key="messages.id", nullable=True)
    mentions: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default=text("'[]'")),
    )
    # {"awaiting": bool, "to": str, "priority": str, "options": list[str]|None,
    #  "default": str|None, "deadline": iso|None}
    question_meta: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )


class AgentThreadCursor(SQLModel, table=True):
    __tablename__ = "agent_thread_cursor"

    agent_id: uuid.UUID = Field(foreign_key="agents.id", primary_key=True)
    thread_id: uuid.UUID = Field(foreign_key="threads.id", primary_key=True)
    last_delivered_seq: int = Field(default=0)
    last_acked_seq: int = Field(default=0)
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("CURRENT_TIMESTAMP"),
            onupdate=datetime.utcnow,
        ),
    )


class UserThreadCursor(SQLModel, table=True):
    __tablename__ = "user_thread_cursor"

    user_id: uuid.UUID = Field(foreign_key="users.id", primary_key=True)
    thread_id: uuid.UUID = Field(foreign_key="threads.id", primary_key=True)
    last_read_seq: int = Field(default=0)
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("CURRENT_TIMESTAMP"),
            onupdate=datetime.utcnow,
        ),
    )
