import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, text, JSON, Uuid
from sqlmodel import Column, Field, SQLModel


class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID = Field(foreign_key="boards.id", index=True)
    project_id: uuid.UUID | None = Field(
        default=None, foreign_key="projects.id", nullable=True, index=True
    )
    phase_id: uuid.UUID | None = Field(
        default=None, foreign_key="project_phases.id", nullable=True, index=True
    )
    parent_task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", nullable=True
    )
    title: str
    description: str | None = None
    status: str = Field(default="inbox")
    priority: str = Field(default="medium")
    task_type: str = Field(default="story")  # story | bug | revision | chore

    # Assignment
    assigned_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )

    # Ownership — wer ist verantwortlich (immutable nach Erstellung)
    # Unterschied zu assigned_agent_id: assigned wechselt (Developer → Reviewer → zurueck),
    # owner bleibt der Agent der den Task erstellt/delegiert hat.
    owner_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )

    # Completion-Callback-Routing — wer bekommt die Done-Notification?
    # Getrennt von owner_agent_id (Creator-Semantik, immutable).
    # null = Fallback auf Board Lead.
    # Wird bei Erstellung durch Nicht-Board-Lead automatisch auf Board Lead gesetzt.
    callback_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True, index=True
    )

    # Timestamps
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    # Dispatch ACK Tracking
    dispatched_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    ack_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    due_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    # Spawn Session Tracking (chat_send_isolated Session-Keys + runId)
    spawn_run_id: str | None = None
    spawn_session_key: str | None = None

    # Workspace Isolation (Bundle 4) — isolierter Arbeitspfad pro Task
    workspace_path: str | None = None
    workspace_port: int | None = None

    # Checklist-Aggregat (T-1 — denormalisiert für schnelle Queries)
    checklist_total: int = Field(default=0)
    checklist_done: int = Field(default=0)

    # planner_mode Feld entfernt in Migration 0071 (2026-04-11, Phase D).
    # Boss plant selbst via openclaude-Subagents, kein Planner-Intermediate.

    # Content Pipeline Link
    pipeline_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="content_pipelines.id",
        nullable=True,
        sa_column_kwargs={"index": True},
    )
    pipeline_stage: str | None = None  # "research" | "writing" | "review"

    # Operational Controls
    run_control: str | None = None  # null | manual_hold | stopped
    dispatch_intent: str = Field(default="root")  # root | subtask | review_handoff | review_rework | manual_redispatch
    dispatch_attempt_id: str | None = None  # UUID pro Dispatch-Versuch, validiert Agent-Updates

    # Pre-Dispatch Gate (Phase 1 Systemic Orchestration)
    # None = Legacy (sofort dispatch, kein Gating)
    # "planning" = Dispatch blockiert
    # "ready" = Dispatch freigegeben, nach Dispatch → None
    dispatch_phase: str | None = None

    # Review Decision (explizite Review-Entscheidung statt impliziter Status-Änderung)
    review_decision: str | None = None  # null | approved | changes_requested | hold
    review_decided_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    # Completion Contract — was der Operator erwartet
    report_back_required: bool = False
    report_back_channel: str | None = None      # "telegram" | "discord" | None
    report_back_chat_id: str | None = None       # Telegram chat_id oder Discord channel_id
    report_back_requirements: str | None = None  # "summary,screenshot,before_after" (kommasepariert)
    report_back_status: str | None = "none"      # Deprecated (alter Henry-Fallback-Flow) — nicht mehr aktiv genutzt
    # Gate-Flag: wird durch `mc telegram` mit current_task_id gesetzt, vom status=done Guard geprueft
    report_sent_to_telegram: bool = False
    # Routing-Regel "wer dispatcht, der sendet": Subtasks (parent_task_id NOT NULL)
    # duerfen normalerweise KEIN mc telegram an den Operator senden — der Orchestrator
    # (Boss) konsolidiert + sendet final. Ausnahme fuer long-running Watch-Tasks
    # (z.B. "beobachte Channel und melde Events"): Boss setzt autonomous_telegram=True
    # im Subtask-Brief, dann darf der Worker selbst senden.
    autonomous_telegram: bool = Field(default=False)
    skip_review: bool = Field(default=False)      # Scheduler-Tasks: Review-Gate überspringen

    # Requester / Origin Tracking
    requester_channel: str | None = None   # "telegram" | "discord" | "web" | "agent"
    requester_id: str | None = None        # Chat-ID, User-ID, oder Agent-UUID

    # Verschluesselte Credentials (Fernet-Ciphertext, nur fuer berechtigte Agents entschluesselt)
    credentials_encrypted: str | None = None
    credential_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(Uuid, ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True),
    )

    # Delegation Contract — strukturierte Pflichtfelder pro Task-Typ
    delegation_type: str | None = None     # code_change | visual_proof | credential_bound | review
    branch_name: str | None = None         # z.B. "feature/format-duration"
    # use_alter=True: bricht den FK-Zyklus tasks↔task_deliverables fuer SQLAlchemy INSERT-Sortierung
    triggered_by_deliverable_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid(as_uuid=True),
            ForeignKey("task_deliverables.id", use_alter=True, name="fk_tasks_triggered_by_deliverable_id"),
            nullable=True,
        ),
    )
    # Welches Deliverable hat diesen Task ausgelöst (Provenance)
    use_separate_repo: bool = Field(default=False)
    target_url: str | None = None          # z.B. "http://localhost/tasks"
    acceptance_criteria: str | None = None  # V1: text, spaeter JSON-Migration moeglich
    requires_auth: bool = False            # Braucht der Task Login/Auth?
    source_task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", nullable=True
    )  # Fuer review: strukturelle Referenz zum reviewten Task
    expected_content: str | None = None  # Fuer visual_proof: was soll auf der Seite sichtbar sein

    # Help Request System — Agent-zu-Agent Kollaboration
    help_request_from: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )  # Agent-ID des Absenders. Gesetzt = "ich bin ein Help-Request-Subtask"
    blocked_by_task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", nullable=True
    )  # Referenz auf den Help-Request-Subtask der diesen Task blockiert

    # Operator-Intake (Phase 2 — primaer fuer Root-/Intake-Tasks)
    intake_mode: str | None = None           # "quick" | "structured" | null (Legacy)
    request_kind: str | None = None          # Operator-Intent (getrennt von delegation_type)
    desired_output: str | None = None        # Was soll rauskommen?
    scope_out: str | None = None             # Was ist explizit NICHT im Scope
    risk_notes: str | None = None            # Was darf nicht kaputtgehen?
    reference_urls: list | None = Field(     # Links als JSON-Liste
        default=None, sa_column=Column(JSON, nullable=True)
    )
    reference_notes: str | None = None       # Freitext-Referenzen
    approval_policy: str | None = None       # Freigabe-Regel (erfasst, nicht enforced in Phase 2)
    autonomy_level: str | None = None        # Autonomie-Level (erfasst, nicht enforced in Phase 2)
    publish_allowed: bool | None = None      # Darf veroeffentlicht werden?
    needs_browser: bool | None = None        # Browser-Interaktion noetig? (getrennt von requires_auth)
    credential_consent: bool | None = None   # Operator hat Credential-Nutzung fuer diesen Auftrag freigegeben

    sort_order: int = 0
    is_auto_created: bool = False
    auto_reason: str | None = None

    # Ersteller (User, der den Task über die UI angelegt hat)
    created_by_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", nullable=True
    )

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )


class TaskEvent(SQLModel, table=True):
    """Event Sourcing fuer Task-Status-Aenderungen.

    Jede Statusaenderung wird als immutables Event geloggt.
    Ermoeglicht: Silent Failure Detection, Audit Trail, Duration Analytics.
    """
    __tablename__ = "task_events"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="tasks.id", index=True)
    from_status: str
    to_status: str
    changed_by: str  # "user" | "agent" | "watchdog" | "system"
    agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )
    reason: str | None = None  # Optionaler Kontext (z.B. "aborted_recovery", "review_handoff")
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )


class TaskDependency(SQLModel, table=True):
    __tablename__ = "task_dependencies"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="tasks.id", index=True)
    depends_on_task_id: uuid.UUID = Field(foreign_key="tasks.id")
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )


class TaskComment(SQLModel, table=True):
    __tablename__ = "task_comments"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="tasks.id", index=True)
    author_type: str  # 'user' | 'agent'
    author_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )
    comment_type: str = Field(default="message")
    # message | handoff | blocker | progress | resolution | feedback
    content: str
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
