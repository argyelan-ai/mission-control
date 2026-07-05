"""Loops — ergebnisgesteuerte Task-Schleifen (ADR-051, L1).

Ein Loop ist ein Meta-Controller über normale Tasks: pro Runde erzeugt der
Loop-Runner einen ganz normalen Parent-Task und lässt die bestehende
Maschinerie arbeiten (Board-Lead-Orchestrierung, ACK, Watchdog, Review,
Approvals). Der Runner beobachtet nur den Ausgang und entscheidet:
weiter / pausieren / eskalieren / fertig.

Bewusst KEIN eigener Ausführungspfad (der Workflow-Fehler wird nicht
wiederholt — ADR-051 §Leitentscheidung).
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, text
from sqlmodel import Column, Field, SQLModel

# Status-Maschine:
# draft → running → waiting_gate → running → … → done | failed
#              ↘ paused ↗ (Operator oder Circuit-Breaker)
LOOP_STATUSES = ("draft", "running", "waiting_gate", "paused", "done", "failed")
TERMINAL_LOOP_STATUSES = ("done", "failed")
ACTIVE_LOOP_STATUSES = ("running", "waiting_gate")

BACKLOG_SOURCES = ("markdown", "project", "tag", "open_ended")


class Loop(SQLModel, table=True):
    __tablename__ = "loops"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID = Field(foreign_key="boards.id", index=True)
    project_id: uuid.UUID | None = Field(
        default=None, foreign_key="projects.id", nullable=True
    )
    name: str
    goal: str  # Markdown-Brief — wandert in jeden Runden-Task
    backlog_source: str = "markdown"  # markdown|project|tag|open_ended
    backlog_md: str | None = None  # bei source=markdown: die Item-Liste
    backlog_tag: str | None = None  # bei source=tag (L2)
    round_brief: str | None = None  # optionales Zusatz-Template pro Runde

    # ── Gates (Marks Entscheid: Default = nur bei Problemen/Merges) ────
    human_every_n_rounds: int = 0  # 0 = nie, 1 = jede Runde
    pause_on_failed_rounds: int = 2  # Circuit-Breaker
    escalate_on: list | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )  # z.B. ["merge_decision", "scope_change", "destructive"]

    # ── Reporting (L2) ──────────────────────────────────────────────────
    telegram_reports: bool = True  # Opt-out: kompakter Report nach jeder Runde

    # ── Stop-Bedingungen (L1: Runden + Zeit; Token/USD = L3) ───────────
    max_rounds: int = 10
    max_duration_minutes: int | None = None
    stop_on_backlog_empty: bool = True

    # ── Laufzeit-Zustand ────────────────────────────────────────────────
    status: str = Field(default="draft", index=True)
    rounds_completed: int = 0
    consecutive_failed_rounds: int = 0
    current_round_no: int = 0
    current_task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", nullable=True
    )
    last_error: str | None = None
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )


class LoopRound(SQLModel, table=True):
    __tablename__ = "loop_rounds"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    loop_id: uuid.UUID = Field(foreign_key="loops.id", index=True)
    round_no: int
    task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", nullable=True
    )
    outcome: str | None = None  # done | failed | aborted | stopped
    report: str | None = None  # kompakter Runden-Report (Pflicht-Disziplin)
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
