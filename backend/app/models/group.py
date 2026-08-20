"""Gruppen — Multi-Agent-Gruppenchat (V1).

Eine Gruppe = ein Thread(kind="group") + diese Config-Zeile + Mitglieder.
Kein eigenes Nachrichtensystem: der Verlauf liegt als ganz normale Messages
auf dem Thread, Zustellung läuft über die bestehenden AgentThreadCursor
(Nudge+Pull). Die Runden-Engine (group_runner, PR B) ruft selbst kein LLM —
Moderations-Logik ist Code, das Urteil liefert der Lead-Agent als normaler
Teilnehmer-Turn.

Feldnamen der Loop-Konzepte (Budget, Gates, Circuit-Breaker) sind bewusst
identisch mit models/loop.py — Gruppen absorbieren Loops langfristig
(Plan-Entscheid 2026-08-20), gleiche Namen halten die Absorption billig.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlmodel import Column, Field, SQLModel

# Status-Maschine:
# draft → idle ⇄ running → waiting_gate → running → … → done | failed
#                    ↘ paused ↗ (Operator oder Circuit-Breaker)
# idle = keine Runden aktiv (Live-Verhalten: Erwähnte antworten direkt).
# Es gibt bewusst KEIN mode-Feld — das Verhalten folgt aus dem Status
# (Mark-Entscheid 2026-08-20: „ohne Schalter").
GROUP_STATUSES = ("draft", "idle", "running", "waiting_gate", "paused", "done", "failed")
TERMINAL_GROUP_STATUSES = ("done", "failed")
ACTIVE_GROUP_STATUSES = ("running", "waiting_gate")

GROUP_LIFECYCLES = ("one_shot", "standing")
GROUP_MEMBER_ROLES = ("lead", "critic", "member")
GROUP_ROUND_KINDS = ("autonomous", "live_impulse")

# Cap für den Dokument-Snapshot je Runde (Plan §4.4) — schützt die DB vor
# einem Lead, der Gigabytes ins Ergebnis-Dokument schreibt.
DOC_SNAPSHOT_MAX_BYTES = 64 * 1024


class AgentGroup(SQLModel, table=True):
    __tablename__ = "agent_groups"
    __table_args__ = (
        # Eine Gruppe ↔ genau ein Thread. Muss identisch in Migration 0181
        # stehen: Tests bauen die Tabellen aus dem Modell, Produktion aus der
        # Migration — nur wenn beide denselben Constraint tragen, prüft der
        # Test das Produktionsverhalten (Konvention aus models/thread.py).
        UniqueConstraint("thread_id", name="uq_agent_groups_thread_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # ondelete=CASCADE: eine Gruppe ohne ihren Thread hat keine Bedeutung —
    # der Verlauf IST der Thread.
    thread_id: uuid.UUID = Field(
        sa_column=Column(
            Uuid, ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    name: str
    goal: str  # Pflicht — eine Gruppe ohne Ziel gibt es nicht (422 im Router)
    lifecycle: str = "one_shot"  # one_shot | standing
    # Der Lead urteilt am Rundenende und pflegt das Ergebnis-Dokument.
    # SET NULL statt CASCADE: verliert die Gruppe ihren Lead (Agent gelöscht),
    # bleibt sie bestehen — die Engine pausiert dann mit Gate „neuen Lead wählen".
    lead_agent_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
        ),
    )

    # ── Deckel & Gates (Feldnamen identisch mit models/loop.py) ─────────
    max_rounds: int = 3  # harter Deckel, Pflicht (Kritiker-Entscheid: Default 3)
    max_duration_minutes: int | None = None
    budget_usd: float | None = None  # weiche Bremse an Rundengrenzen (Harvester-Lag!)
    budget_tokens: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    human_every_n_rounds: int = 0  # 0 = nie
    pause_on_failed_rounds: int = 2  # Circuit-Breaker
    operator_reports: bool = True

    # ── Gruppen-spezifisch ──────────────────────────────────────────────
    speaker_timeout_seconds: int = 600  # Säumige werden ehrlich übersprungen
    live_max_turns_per_impulse: int = 2  # Kappung der Live-Antwortkette (Hermes-Muster)
    # Relativ zu ~/.mc/references/ — z.B. "groups/spark-vergleich/result.md".
    result_doc_rel_path: str | None = None

    # ── Laufzeit-Zustand ────────────────────────────────────────────────
    status: str = Field(default="draft", index=True)
    rounds_completed: int = 0
    consecutive_failed_rounds: int = 0
    current_round_no: int = 0
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
        sa_column=Column(
            DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow
        ),
    )


class GroupMember(SQLModel, table=True):
    __tablename__ = "group_members"

    # Composite-PK (group_id, agent_id) — Muster AgentThreadCursor. Beide
    # CASCADE: eine Mitgliedschaft ohne Gruppe oder ohne Agent ist bedeutungslos.
    group_id: uuid.UUID = Field(
        sa_column=Column(
            Uuid,
            ForeignKey("agent_groups.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    agent_id: uuid.UUID = Field(
        sa_column=Column(
            Uuid, ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
        )
    )
    role: str = "member"  # lead | critic | member — nur Prompt-Baustein, kein Rechte-Modell
    added_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )


class GroupRound(SQLModel, table=True):
    __tablename__ = "group_rounds"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    group_id: uuid.UUID = Field(
        sa_column=Column(
            Uuid,
            ForeignKey("agent_groups.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    round_no: int
    kind: str = "autonomous"  # autonomous | live_impulse
    # seq der Brief-System-Message. NULL bis der Brief gepostet ist —
    # Recovery-Anker: Commit Rundenzustand → post_message → Commit brief_seq
    # (Crash dazwischen: der Tick adoptiert oder re-postet, at-least-once).
    brief_seq: int | None = None
    # Bei Rundenstart eingefrorene Sprecher-Slugs (alle Mitglieder ausser Lead).
    # Der Tick streicht, wer geantwortet hat (seq > brief_seq).
    pending_speakers: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default=text("'[]'")),
    )
    lead_prompt_seq: int | None = None
    # Marks Nachricht, die einen Live-Impuls auslöste. SET NULL: die Runde
    # überlebt eine gelöschte Nachricht (Muster Thread.task_id).
    trigger_message_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
        ),
    )
    outcome: str | None = None  # goal_reached | continue | ask_mark | failed | stopped
    report: str | None = None  # kompakter Runden-Report (Brief-Historie, Muster Loop)
    # Snapshot des Ergebnis-Dokuments nach dem Lead-Turn (Cap 64 KB) —
    # Versions-Verlauf fürs UI ohne Git-Maschinerie.
    doc_snapshot: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    tokens_used: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    cost_usd: float | None = None
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
