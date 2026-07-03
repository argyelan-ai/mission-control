import uuid
from datetime import datetime
from typing import Any

from pydantic import field_validator
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Text, event, text, Uuid
from sqlmodel import Column, Field, SQLModel


class Agent(SQLModel, table=True):
    __tablename__ = "agents"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID | None = Field(default=None, foreign_key="boards.id", index=True, nullable=True)
    template_id: uuid.UUID | None = Field(default=None, foreign_key="agent_templates.id", nullable=True)

    # Provisioning (Agent Council integration)
    workspace_path: str | None = None          # Agent home path on host (~/.mc/workspaces/{slug} for cli-bridge; ~/.mc/agents/{slug} for legacy host). Kept per Phase 14 ADR-022 repurpose — 10/14 agents have values, 4 active consumers (agent_git.py, agent_scoped.py, dispatch.py, tasks.py).
    provision_status: str = Field(default="local")  # local | provisioning | provisioned | error
    provisioned_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    # Discord integration
    discord_channel_id: str | None = None
    discord_channel_name: str | None = None

    name: str
    role: str | None = None

    @field_validator("role", mode="before")
    @classmethod
    def validate_role(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from app.scopes import AgentRole
        try:
            AgentRole(v)
        except ValueError:
            valid = ", ".join(r.value for r in AgentRole)
            raise ValueError(f"Ungueltige Rolle: '{v}'. Gueltig: {valid}")
        return v

    # Stable filesystem slug — the partition key for ~/.mc/workspaces/<slug>
    # and ~/.mc/deliverables/<slug>. Persisted so it survives an agent rename
    # (the old name-derived slug broke every existing deliverable path on
    # rename). Populated by the before_insert listener below; fs_service.agent_slug()
    # falls back to name.lower().replace(" ","-") when NULL (legacy rows).
    # Migration 0129 backfills + adds the index.
    slug: str | None = Field(default=None, index=True)

    emoji: str | None = None
    status: str = Field(default="offline")
    model: str | None = None
    is_board_lead: bool = False

    # Auth
    agent_token_hash: str | None = None

    # Per-Agent API Key selection (optional). When set, docker_agent_sync
    # writes the decrypted value as OPENAI_API_KEY into the .env file in
    # the claude-config bind mount. start-claude.sh sources the .env
    # before the openclaude start. NULL = fallback to docker-compose env.
    # ON DELETE SET NULL (see migration 0070) — deleting a secret
    # does not crash any agent.
    secret_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="secrets.id",
        nullable=True,
        index=True,
    )

    # Per-Agent Runtime selection (cli-bridge agents only). NULL → falls back
    # to global default env vars. Set → docker_agent_sync renders the
    # Runtime's endpoint + model into the agent's .env file. ON DELETE SET NULL
    # so removing a runtime doesn't crash its agents (they revert to fallback).
    runtime_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid(as_uuid=True),
            ForeignKey("runtimes.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

    # Config (managed via UI)
    heartbeat_config: dict[str, Any] = Field(
        default_factory=lambda: {"interval": "5m", "target": "last"},
        sa_column=Column(JSON),
    )
    dispatch_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON),
    )
    skills: list[Any] = Field(default_factory=list, sa_column=Column(JSON))
    skill_filter: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    cli_plugins: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    cli_skills: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    mcp_servers: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    scopes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    identity_md: str | None = None
    soul_md: str | None = None
    tools_md: str | None = None
    # heartbeat_md removed in migration 0125 — was never read by agents.
    # SOUL.md is the only file injected into Claude's --append-system-prompt.
    rules_md: str | None = None
    memory_md: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    # Workstream D — per-agent persona section injected at the top of
    # SOUL.md.j2. ~80-120 tokens of English character voice. NULL means
    # the template renders a generic fallback (for legacy agents); the
    # 9 personas in docs/superpowers/specs/2026-04-20-agent-personas-draft.md
    # are seeded by Migration 0085.
    soul_persona_md: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # Phase 3 — Claude-Process Recycler (MEM-01) per-agent override.
    # NULL → follow global settings.agent_recycler_enabled (default True).
    # True/False → explicit per-agent enable/disable. Mirror of MEM-05 ACK-
    # timeout pattern (dispatch_config["ack_timeout_minutes"]) but as a
    # typed column rather than a JSON field. See ADR-024 + Migration 0090.
    recycler_enabled: bool | None = Field(
        default=None,
        sa_column=Column(Boolean(), nullable=True),
    )

    # Phase 8 — Deployer Resolution Auto-Promote Fix (BUG-01) per-agent override.
    # True (default) → existing single-step worker behaviour: posting a
    #   comment_type="resolution" auto-promotes in_progress→review (Path A in
    #   agent_comments.py:287; Path B in task_runner.py:771).
    # False → suppress BOTH auto-promote paths so the agent's multi-step
    #   lifecycle (deploy → verify → finalize) cannot be cut short by an
    #   intermediate "resolution"-shaped summary. The agent must explicitly
    #   PATCH status:review when truly done.
    # Set False on deployer rows by Migration 0092's data step. See
    # 08-CONTEXT.md decisions section + .planning/debug/deployer-task-early-review.md.
    auto_promote_on_resolution: bool = Field(
        default=True,
        sa_column=Column(
            Boolean(),
            nullable=False,
            server_default=text("true"),
        ),
    )

    # Status tracking
    last_seen_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    last_task_activity_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # use_alter=True: breaks the FK cycle agents↔tasks for SQLAlchemy INSERT ordering
    current_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid(as_uuid=True),
            ForeignKey("tasks.id", use_alter=True, name="fk_agents_current_task_id"),
            nullable=True,
        ),
    )

    # Runtime observability
    last_trigger_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    last_dispatch_error: str | None = None
    run_state: str = Field(default="idle")  # idle | running | recovering | aborted | blocked
    operational_mode: str = Field(default="active")  # active | paused
    agent_runtime: str = Field(default="cli-bridge")  # cli-bridge | claude-code | manual | host (Phase 24 ADR-029)
    requires_git_workflow: bool = Field(default=True)
    # Response language towards the operator (short code, e.g. "en", "de").
    # Templates are English; this only steers how the agent replies.
    language: str = Field(default="en", max_length=16)

    # Analytics snapshots
    context_tokens: int = 0
    context_max: int = 150_000
    session_message_count: int = 0
    total_tasks_completed: int = 0
    total_compactions: int = 0

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )


@event.listens_for(Agent, "before_insert")
def _agent_fill_slug(mapper, connection, target: "Agent") -> None:
    """Populate the stable filesystem slug on insert (never on rename)."""
    if not target.slug and target.name:
        target.slug = target.name.lower().replace(" ", "-")


class AgentMetrics(SQLModel, table=True):
    __tablename__ = "agent_metrics"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agents.id", index=True)

    # Time window
    period_start: datetime = Field(sa_column=Column(DateTime(timezone=True)))
    period_end: datetime = Field(sa_column=Column(DateTime(timezone=True)))

    # Productivity
    tasks_started: int = 0
    tasks_completed: int = 0
    comments_posted: int = 0

    # Stability
    context_tokens_avg: int = 0
    context_tokens_max: int = 0
    heartbeats_total: int = 0
    heartbeats_failed: int = 0
    errors_total: int = 0
    compactions: int = 0
    resets: int = 0

    # Timing
    avg_task_duration_minutes: int | None = None
    idle_minutes: int = 0

    # Model Usage Tracking (Theme 4: Wave 2)
    model_usage: dict | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
