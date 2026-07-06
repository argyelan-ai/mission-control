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

    # Ownership — who is responsible (immutable after creation)
    # Difference from assigned_agent_id: assigned changes (Developer → Reviewer → back),
    # owner stays the agent who created/delegated the task.
    owner_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )

    # Completion callback routing — who gets the done notification?
    # Separate from owner_agent_id (creator semantics, immutable).
    # null = fallback to board lead.
    # Automatically set to board lead when created by a non-board-lead.
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

    # Spawn session tracking (chat_send_isolated session keys + runId)
    spawn_run_id: str | None = None
    spawn_session_key: str | None = None

    # Workspace isolation (Bundle 4) — isolated working path per task
    workspace_path: str | None = None
    workspace_port: int | None = None

    # Checklist aggregate (T-1 — denormalized for fast queries)
    checklist_total: int = Field(default=0)
    checklist_done: int = Field(default=0)

    # planner_mode field removed in migration 0071 (2026-04-11, Phase D).
    # Boss plans itself via openclaude subagents, no planner intermediate.

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
    dispatch_attempt_id: str | None = None  # UUID per dispatch attempt, validates agent updates

    # Pre-Dispatch Gate (Phase 1 Systemic Orchestration)
    # None = legacy (immediate dispatch, no gating)
    # "planning" = dispatch blocked
    # "ready" = dispatch approved, becomes None after dispatch
    dispatch_phase: str | None = None

    # Review decision (explicit review decision instead of implicit status change)
    review_decision: str | None = None  # null | approved | changes_requested | hold
    review_decided_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    # Completion contract — what the operator expects
    report_back_required: bool = False
    report_back_channel: str | None = None      # "telegram" | "discord" | None
    report_back_chat_id: str | None = None       # Telegram chat_id or Discord channel_id
    report_back_requirements: str | None = None  # "summary,screenshot,before_after" (comma-separated)
    report_back_status: str | None = "none"      # Deprecated (old Henry fallback flow) — no longer actively used
    # Gate flag: set by `mc telegram` with current_task_id, checked by the status=done guard
    report_sent_to_telegram: bool = False
    # Routing rule "whoever dispatches, sends": subtasks (parent_task_id NOT NULL)
    # normally must NOT send mc telegram to the operator — the orchestrator
    # (Boss) consolidates + sends the final message. Exception for long-running watch
    # tasks (e.g. "watch channel and report events"): Boss sets autonomous_telegram=True
    # in the subtask brief, then the worker is allowed to send itself.
    autonomous_telegram: bool = Field(default=False)
    skip_review: bool = Field(default=False)      # Scheduler tasks: skip the review gate

    # Requester / Origin Tracking
    requester_channel: str | None = None   # "telegram" | "discord" | "web" | "agent"
    requester_id: str | None = None        # Chat ID, user ID, or agent UUID

    # Encrypted credentials (Fernet ciphertext, decrypted only for authorized agents)
    credentials_encrypted: str | None = None
    credential_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(Uuid, ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True),
    )

    # Delegation contract — structured required fields per task type
    delegation_type: str | None = None     # code_change | visual_proof | credential_bound | review
    branch_name: str | None = None         # e.g. "feature/format-duration"
    # use_alter=True: breaks the tasks↔task_deliverables FK cycle for SQLAlchemy INSERT ordering
    triggered_by_deliverable_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid(as_uuid=True),
            ForeignKey("task_deliverables.id", use_alter=True, name="fk_tasks_triggered_by_deliverable_id"),
            nullable=True,
        ),
    )
    # Which deliverable triggered this task (provenance)
    use_separate_repo: bool = Field(default=False)
    # Deprecated seit ADR-052 (Repo-Registry-Auswahl) — bleibt für API-Kompat;
    # der so erzeugte Task-Repo wird jetzt in der Registry mitregistriert.
    repo_id: uuid.UUID | None = Field(
        default=None, foreign_key="repos.id", nullable=True, index=True
    )
    # Ad-hoc-Tasks (ohne Projekt): explizit gewähltes Registry-Repo (ADR-052).
    # Bei Projekt-Tasks kommt das Repo weiterhin vom Projekt.
    target_url: str | None = None          # e.g. "http://localhost/tasks"
    acceptance_criteria: str | None = None  # V1: text, JSON migration possible later
    requires_auth: bool = False            # Does the task need login/auth?
    source_task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", nullable=True
    )  # For review: structural reference to the reviewed task
    expected_content: str | None = None  # For visual_proof: what should be visible on the page

    # Help request system — agent-to-agent collaboration
    help_request_from: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )  # Agent ID of the sender. Set = "I am a help-request subtask"
    blocked_by_task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", nullable=True
    )  # Reference to the help-request subtask blocking this task

    # Operator intake (Phase 2 — primarily for root/intake tasks)
    intake_mode: str | None = None           # "quick" | "structured" | null (legacy)
    request_kind: str | None = None          # Operator intent (separate from delegation_type)
    desired_output: str | None = None        # What should come out of this?
    scope_out: str | None = None             # What is explicitly NOT in scope
    risk_notes: str | None = None            # What must not break?
    reference_urls: list | None = Field(     # Links as JSON list
        default=None, sa_column=Column(JSON, nullable=True)
    )
    reference_notes: str | None = None       # Free-text references
    approval_policy: str | None = None       # Approval rule (captured, not enforced in Phase 2)
    autonomy_level: str | None = None        # Autonomy level (captured, not enforced in Phase 2)
    publish_allowed: bool | None = None      # Is publishing allowed?
    needs_browser: bool | None = None        # Is browser interaction needed? (separate from requires_auth)
    credential_consent: bool | None = None   # Operator has approved credential usage for this task
    # Operator explicitly requested human-simulating E2E testing: after review
    # approval the task goes through the user_test gate (tester agent drives
    # real flows via Playwright MCP) even without subtasks/needs_browser.
    e2e_test_required: bool | None = None
    # If True, the review handoff skips the agent reviewer — the task waits
    # in `review` for a human (appears in Inbox) and pings Mark via Telegram.
    human_review_required: bool | None = None
    # If True, a blocker on this task skips Board-Lead (Boss) triage and goes
    # straight to the operator (Mark), regardless of blocker_type. Opt-in per
    # task at creation; None/False keeps the default lead-first triage.
    blocker_to_operator: bool | None = None

    sort_order: int = 0
    is_auto_created: bool = False
    auto_reason: str | None = None

    # Creator (user who created the task via the UI)
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
    """Event sourcing for task status changes.

    Every status change is logged as an immutable event.
    Enables: silent failure detection, audit trail, duration analytics.
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
    reason: str | None = None  # Optional context (e.g. "aborted_recovery", "review_handoff")
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
