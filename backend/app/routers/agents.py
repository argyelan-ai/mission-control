import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.auth import require_agent, require_control_plane, require_user, require_user_or_control_plane
from app.database import get_session
from app.models.agent import Agent, AgentMetrics
from app.models.task import Task
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.services.sse import make_sse_response
from app.utils import utcnow
from app.services.template_renderer import render_all_agent_files, render_agent_file, build_agent_context

router = APIRouter(prefix="/api/v1", tags=["agents"])

CONFIG_FILE_TYPES = {"tools_md", "rules_md", "identity_md", "soul_md", "memory_md"}

# Provisioning constants and functions — delegated to services/provisioning.py
# Phase 29: the gateway path is removed; cli-bridge + host remain. Plan 29-07
# further refactors services/provisioning.py (symbol cleanup).
from app.services.provisioning import (
    extract_token_from_tools_md as _extract_token_from_tools_md,
    provision_agent_background as _provision_agent_background,
)
class AgentCreate(BaseModel):
    name: str
    emoji: str | None = "🤖"
    role: str | None = None
    model: str | None = None
    board_id: uuid.UUID | None = None
    is_board_lead: bool = False
    context_max: int = 200000
    # cli-bridge is the post-sunset mainstream (Phase 30). The old 'openclaw'
    # default hit the CHECK constraint that forbids retired runtimes — API
    # callers omitting the field got a 500 instead of an agent.
    agent_runtime: str = "cli-bridge"
    # Optional LLM-runtime binding at create time (UUID or slug, resolved
    # server-side like the PATCH path). Set BEFORE provisioning so the
    # one-click chain renders the right image/env from the start — the
    # detail-page switch service stays the path for changing it later.
    runtime_id: str | None = None
    # ── Onboarding-wizard fields (2026-07-10) ────────────────────────────────
    # The wizard funnels custom / template-prefill / duplicate all through
    # this ONE create call, so create must carry the full agent config.
    # ADR-056 harness axis. None = derive from the runtime's protocol.
    harness: str | None = None
    # Explicit scope list. Empty [] means ALL 16 scopes (backward-compat), so
    # the wizard always sends a concrete list — a new agent is never silently
    # all-powerful.
    scopes: list[str] = []
    # SOUL/persona markdown (from a template or a duplicated agent). None =
    # the default SOUL.md.j2 render at provision time.
    soul_md: str | None = None
    # cli-bridge skill allowlist. None = all skills.
    skill_filter: list[str] | None = None
    # cli-bridge plugin allowlist. None = all installed plugins.
    cli_plugins: list[str] | None = None

    @field_validator("harness")
    @classmethod
    def _validate_harness(cls, v: str | None) -> str | None:
        # Any known harness is valid at CREATE — cli-bridge (claude/openclaude/omp)
        # AND host-only harnesses (hermes, grok — ADR-064/066). HARNESS_PROTOCOLS is
        # the canonical set. Unlike the cli-bridge switch path (AgentUpdate), a host
        # harness like grok can ONLY be set here: derive_harness() returns None for a
        # grok-cloud runtime, so the wizard must pass harness="grok" explicitly.
        from app.services.harness_compat import HARNESS_PROTOCOLS
        if v is not None and v not in HARNESS_PROTOCOLS:
            allowed = ", ".join(sorted(HARNESS_PROTOCOLS))
            raise ValueError(f"harness muss einer von: {allowed} sein")
        return v

    @model_validator(mode="after")
    def _host_requires_runtime_id(self):
        # Host agents can only get a runtime_id at create time — the PATCH
        # switch path is cli-bridge-only (see update_agent()), and /provision
        # 400s without one. Without this guard, creating a host agent without
        # runtime_id is an unrecoverable dead-end (2026-07-10 host E2E test).
        if self.agent_runtime == "host" and not self.runtime_id:
            raise ValueError(
                "Host-Agents benoetigen eine runtime_id beim Erstellen "
                "(nachtraeglicher Wechsel wird nicht unterstuetzt)"
            )
        return self


class AgentUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    emoji: str | None = None
    model: str | None = None
    board_id: uuid.UUID | None = None
    is_board_lead: bool | None = None
    heartbeat_config: dict[str, Any] | None = None
    skills: list[Any] | None = None
    context_max: int | None = None
    operational_mode: str | None = None  # active | paused
    # Per-Agent API-Key selection (optional FK on secrets.id).
    # None = fallback to docker-compose env. "null" as explicit unset via JSON.
    secret_id: uuid.UUID | None = None
    # Whether this agent must produce git commits on task completion.
    # True (default) for Developer/Deployer/Reviewer. False for Designer/
    # Research/Writer/Orchestrator, which deliver files as deliverables instead of code.
    requires_git_workflow: bool | None = None
    # Per-Agent runtime (cli-bridge only). Setting this makes sync-config /
    # bootstrap render OPENAI_BASE_URL + OPENAI_MODEL from the runtime row.
    # Validated in update_agent(): rejected for host/openclaw agents. When the
    # runtime_id field is part of the patch body, update_agent() delegates to
    # `agent_runtime_switch.switch_agent_runtime()` (Phase 15) for atomic
    # switching with rollback. The two flags below tune that flow.
    # Accepts either a UUID (DB-backed) or a slug (legacy /runtimes JSON registry).
    # Resolved to a Runtime row in update_agent() before delegating to the switch
    # service. Plain str so Pydantic doesn't reject slugs like "qwen-general".
    runtime_id: str | None = None
    # Allow switching while the agent has `current_task_id` set. Default False
    # — caller must explicitly opt in via UI confirm modal.
    force_when_in_progress: bool | None = None
    # ADR-056: the harness axis. When present in the PATCH body, it is passed to
    # the switch service as `new_harness`. A harness-only change (without
    # runtime_id) re-switches the agent onto its CURRENT runtime with the new
    # harness. Only the three known harnesses are accepted.
    harness: str | None = None
    # Context-economy Stage 2 (Migration 0151) — opt in/out of the L1
    # Operating Card. Plain field-merge, no restart-triggering side effect;
    # the CARD.md write/removal happens on the next sync-config call.
    use_operating_card: bool | None = None

    @field_validator("harness")
    @classmethod
    def _validate_harness(cls, v: str | None) -> str | None:
        if v is not None and v not in ("claude", "openclaude", "omp"):
            raise ValueError(
                "harness muss 'claude', 'openclaude' oder 'omp' sein"
            )
        return v


class TriggerPayload(BaseModel):
    message: str = "Please continue with your current task."


class AgentHeartbeatPayload(BaseModel):
    status: str = "idle"  # idle | working
    task_id: str | None = None
    # CTX-01 (Phase 6): Docker agents self-report context-window usage from
    # tmux statusline scrape. Range-validated 0..100 to prevent garbage writes
    # (T-06-02-01 in plan threat model). None means "not reported this cycle".
    context_pct: float | None = Field(default=None, ge=0, le=100)


class ConfigFileUpdate(BaseModel):
    content: str


@router.post("/agents", status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    from app.auth import generate_agent_token
    raw_token, token_hash = generate_agent_token()

    # Auto-generated TOOLS.md with the correct token (will never be available in plaintext again)
    board_id_str = str(payload.board_id) if payload.board_id else None
    tools_md = _generate_tools_md(payload.name, payload.emoji or "🤖", raw_token, board_id_str, is_board_lead=payload.is_board_lead, scopes=payload.scopes)

    # Resolve the optional LLM-runtime binding BEFORE creating the agent —
    # a bad slug should 404 without leaving a half-created agent behind.
    resolved_runtime_id: uuid.UUID | None = None
    if payload.runtime_id:
        resolved_runtime_id = await _resolve_runtime_id(session, payload.runtime_id)

    agent = Agent(
        name=payload.name,
        emoji=payload.emoji,
        role=payload.role,
        model=payload.model,
        board_id=payload.board_id,
        is_board_lead=payload.is_board_lead,
        context_max=payload.context_max,
        agent_token_hash=token_hash,
        tools_md=tools_md,
        agent_runtime=payload.agent_runtime,
        runtime_id=resolved_runtime_id,
        harness=payload.harness,
        scopes=payload.scopes,
        soul_md=payload.soul_md,
        skill_filter=payload.skill_filter,
        cli_plugins=payload.cli_plugins,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    # Vault write mc_token_{slug} — /internal/bootstrap delivers the token to
    # the container (Fresh-Install-Fix 2026-07-02: before this there was NO
    # write path, poll.sh crash-looped with 'MC_TOKEN is not set').
    from app.services.secrets_helper import upsert_agent_token_secret
    await upsert_agent_token_secret(session, agent, raw_token)
    if payload.agent_runtime == "cli-bridge":
        # One-click create (Day-2 basics fix): render config via the host
        # helper (reusing the token just returned so it stays valid), then
        # compose + container start. Bridge down → honest provision_failed
        # event with remediation; the agent stays 'local'.
        background_tasks.add_task(_auto_provision_cli_bridge, agent.id, raw_token)
    elif payload.agent_runtime not in ("free-code-bridge", "manual", "host"):
        # Host agents provision via the wizard's explicit POST /provision
        # call — scheduling this here would race it and, since the host
        # branch of provision_agent_background() is a no-op, falsely flip
        # provision_status to "provisioned" before any files are staged.
        background_tasks.add_task(_provision_agent_background, agent.id)
    result = agent.model_dump()
    result["token"] = raw_token  # returned once, never stored in plaintext
    return result


class SoulPreviewRequest(BaseModel):
    name: str
    emoji: str | None = "🤖"
    role: str | None = None
    soul_md: str | None = None
    board_id: uuid.UUID | None = None
    is_board_lead: bool = False
    scopes: list[str] = []


@router.post("/agents/preview-soul")
async def preview_soul(
    payload: SoulPreviewRequest,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Render SOUL.md.j2 for a transient (non-persisted) agent.

    Powers the wizard's live persona preview (Step 2). No DB write, no
    provisioning — a draft render only. Best-effort: template errors return
    a soft message instead of a 500 so the preview never blocks typing.
    """
    draft = Agent(
        name=payload.name,
        emoji=payload.emoji,
        role=payload.role,
        soul_md=payload.soul_md,
        board_id=payload.board_id,
        is_board_lead=payload.is_board_lead,
        scopes=payload.scopes,
    )
    try:
        context = build_agent_context(draft, board_id=str(payload.board_id) if payload.board_id else None)
        soul = render_agent_file("SOUL.md.j2", context)
    except Exception as exc:  # noqa: BLE001 — preview must never hard-fail
        logger.warning("preview_soul render failed for draft %s: %s", payload.name, exc)
        soul = f"# {payload.emoji or ''} {payload.name}\n\n_(Vorschau nicht verfügbar — Standard-SOUL wird beim Erstellen erzeugt.)_"
    return {"soul_md": soul}


async def _auto_provision_cli_bridge(agent_id: uuid.UUID, raw_token: str) -> None:
    """One-click provisioning chain for freshly created cli-bridge agents.

    1. Probe the host helper (scripts/cli-bridge.py, :18792) — if it is not
       running, emit an actionable provision_failed event and stop; the
       agent honestly stays 'local'.
    2. Bridge render (~/.mc/agents/<slug>/ + worker) via provision_cli_agent,
       reusing the create-time token — NO rotation, the token the operator
       just saw stays valid.
    3. Container half via provision_agent_background (compose render, file
       sync, container start).

    Best-effort: any infra error is logged, never raised — a failed
    background provision must not crash the create request/worker. The
    agent then honestly stays 'local' (ProvisionBadge + Provision button).
    """
    try:
        from app.database import engine
        from app.routers import cli_terminal
        from app.services import provisioning

        async with AsyncSession(engine, expire_on_commit=False) as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                logger.error("_auto_provision_cli_bridge: Agent %s not found", agent_id)
                return

            if cli_terminal._bridge_get("/health") is None:
                from app.config import settings as _settings
                await emit_event(
                    session,
                    "agent.provision_failed",
                    f"{agent.name}: cli-bridge host helper not reachable "
                    f"({_settings.free_code_bridge_url}). Start it with "
                    "`python3 scripts/cli-bridge.py`, then click Provision on "
                    "the agent page — see docs/setup/first-agent.md.",
                    severity="warning",
                    agent_id=agent.id,
                    board_id=agent.board_id,
                )
                return

            payload = cli_terminal.CliProvisionPayload(
                model=agent.model or "nvidia/nemotron-3-super",
                mc_token=raw_token,
            )
            await cli_terminal.provision_cli_agent(agent.id, payload, session, None)

        # Container half opens its own session (BackgroundTask convention).
        await provisioning.provision_agent_background(agent_id)
    except Exception:
        logger.exception(
            "_auto_provision_cli_bridge(%s): one-click provisioning failed — "
            "agent stays 'local', provision manually from the agent page",
            agent_id,
        )


@router.get("/agents")
async def list_agents(
    board_id: uuid.UUID | None = Query(None),
    include_unassigned: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    query = select(Agent)
    if board_id and not include_unassigned:
        query = query.where(Agent.board_id == board_id)
    elif board_id and include_unassigned:
        query = query.where(
            (Agent.board_id == board_id) | (Agent.board_id == None)  # noqa: E711
        )
    # without board_id: all
    query = query.order_by(Agent.name)
    result = await session.exec(query)
    return result.all()


@router.get("/agents/stream")
async def stream_agents(current_user = Depends(require_user)):
    return make_sse_response([RedisKeys.agents_events()])


@router.get("/agents/{agent_id}/terminal-events/stream")
async def stream_terminal_events(
    agent_id: uuid.UUID,
    current_user=Depends(require_user),
):
    """SSE channel that fires when the agent's underlying container is
    recreated (e.g. after a runtime switch with image change).

    The Sessions page subscribes per visible agent and re-mounts the
    TerminalPanel when an event lands so the user doesn't see a frozen
    or stale tmux buffer. Phase 15 T3.7.
    """
    from app.services.agent_runtime_switch import terminal_remount_channel
    return make_sse_response([terminal_remount_channel(agent_id)])


@router.get("/agents/metrics/comparison")
async def agents_metrics_comparison(
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    result = await session.exec(select(Agent).order_by(Agent.name))
    agents = result.all()
    return [
        {
            "agent_id": str(a.id),
            "name": a.name,
            "emoji": a.emoji,
            "status": a.status,
            "context_pct": round(a.context_tokens / a.context_max * 100, 1) if a.context_max else 0,
            "total_tasks_completed": a.total_tasks_completed,
            "total_compactions": a.total_compactions,
        }
        for a in agents
    ]


@router.get("/agents/runtime-status")
async def agents_runtime_status(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Compact runtime status of all agents for board observability."""
    result = await session.exec(select(Agent).where(Agent.board_id.isnot(None)).order_by(Agent.name))  # type: ignore[arg-type]
    agents = result.all()
    return [
        {
            "agent_id": str(a.id),
            "name": a.name,
            "emoji": a.emoji,
            "status": a.status,
            "run_state": a.run_state,
            "current_task_id": str(a.current_task_id) if a.current_task_id else None,
            "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
            "last_trigger_at": a.last_trigger_at.isoformat() if a.last_trigger_at else None,
            "last_dispatch_error": a.last_dispatch_error,
            "provision_status": a.provision_status,
        }
        for a in agents
    ]


# ── Specialized Agents Setup (MUST come before {agent_id} routes) ─────────────

SPECIALIZED_AGENTS_SPECS = [
    {
        "name": "Planner",
        "emoji": "📋",
        "role": "planner",
        "rules_md": """## When you are stuck (ERROR RECOVERY)
- 1st attempt: analyze the error, try an alternative approach
- 2nd attempt: if that doesn't work either → IMMEDIATELY:
  PATCH status: blocked
  POST comment: "Blocked: [exact error + what was tried]"
- NEVER give up silently or endlessly repeat the same thing""",
        "soul_md": """# Planner — Mission Control

You are the Planner of Mission Control. You plan projects and structure tasks.

## Your core responsibilities
- Break the user's ideas down into concrete, actionable tasks
- Create tasks with clear titles, descriptions, and priorities
- Identify and communicate dependencies between tasks
- Propose realistic time estimates and sequencing

## Workflow
1. User describes a project or feature idea
2. You ask targeted follow-up questions (scope, technology, priorities)
3. You create tasks via POST /api/v1/agent/boards/{board_id}/tasks
4. Status: created tasks start with status="inbox"

## Task format
Each task: clear title + short description of what needs to be done.
Max 8 tasks per project — fewer but precise is better.
""",
    },
    {
        "name": "Researcher",
        "emoji": "🔍",
        "role": "researcher",
        "rules_md": """## When you are stuck (ERROR RECOVERY)
- 1st attempt: analyze the error, try an alternative approach
- 2nd attempt: if that doesn't work either → IMMEDIATELY:
  PATCH status: blocked
  POST comment: "Blocked: [exact error + what was tried]"
- NEVER give up silently or endlessly repeat the same thing""",
        "soul_md": """# Researcher — Mission Control

You are the Researcher of Mission Control. You research topics thoroughly and document findings.

## Your core responsibilities
- Research topics comprehensively
- Structure results: summary, key points, sources
- Save findings in the Knowledge Base
- Work through content pipeline research stages

## Workflow
1. Receive an assignment (task or pipeline message)
2. Research the topic
3. For a content pipeline: POST /api/v1/agent/content/{pipeline_id}/submit
   Body: {"stage": "research", "content": "structured summary"}
4. For a research session: set task to done, create a KB entry

## Output format
Always Markdown. Structure: ## Summary, ## Key Points, ## Sources, ## Recommendations
""",
    },
    {
        "name": "Writer",
        "emoji": "✍️",
        "role": "writer",
        "rules_md": """## When you are stuck (ERROR RECOVERY)
- 1st attempt: analyze the error, try an alternative approach
- 2nd attempt: if that doesn't work either → IMMEDIATELY:
  PATCH status: blocked
  POST comment: "Blocked: [exact error + what was tried]"
- NEVER give up silently or endlessly repeat the same thing""",
        "soul_md": """# Writer — Mission Control

You are the Writer of Mission Control. You write high-quality content drafts.

## Your core responsibilities
- Create drafts based on research and brief
- Hit the right style for the target audience
- Master different content types: blog, social, newsletter, docs

## Workflow
1. Receive a writing task with research summary and brief
2. Write a complete draft
3. For a content pipeline: POST /api/v1/agent/content/{pipeline_id}/submit
   Body: {"stage": "writing", "content": "complete draft"}
4. Set task to done

## Style principles
- Write clearly and understandably
- Concrete examples instead of buzzwords
- Appropriate length for the content type
""",
    },
    {
        "name": "Reviewer",
        "emoji": "👀",
        "role": "reviewer",
        "rules_md": """Verify EVERYTHING yourself:
- Run the tests in the developer workspace
- Check whether tests actually exist and are meaningful
- No rubber-stamping — if tests are missing, reject
- Post the verification output as evidence

## When you are stuck (ERROR RECOVERY)
- 1st attempt: analyze the error, try an alternative approach
- 2nd attempt: if that doesn't work either → IMMEDIATELY:
  PATCH status: blocked
  POST comment: "Blocked: [exact error + what was tried]"
- NEVER give up silently or endlessly repeat the same thing""",
        "soul_md": """# Reviewer — Mission Control

You are the Reviewer of Mission Control. You review content drafts critically and constructively.

## Your core responsibilities
- Review drafts for quality, accuracy, and style
- Give concrete, actionable feedback
- Clearly name strengths and weaknesses

## Workflow
1. Receive a review task with a draft
2. Critically review the draft
3. For a content pipeline: POST /api/v1/agent/content/{pipeline_id}/submit
   Body: {"stage": "review", "content": "structured feedback"}
4. Set task to done

## Feedback format
## What works well
[Strengths of the draft]

## What should be improved
[Concrete weaknesses with suggested improvements]

## Assessment
[Recommendation: approved / needs revision]
""",
    },
]


class SpecializedAgentSetupRequest(BaseModel):
    board_id: uuid.UUID
    provision: bool = False  # provision on gateway (requires active RPC connection)


@router.post("/agents/setup-specialized", status_code=status.HTTP_201_CREATED)
async def setup_specialized_agents(
    body: SpecializedAgentSetupRequest,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """
    Creates 4 specialized agents for the content pipeline: Planner, Researcher, Writer, Reviewer.
    Uses templates from the DB (with model config). Falls back to SPECIALIZED_AGENTS_SPECS.
    Tokens are returned once and are not retrievable afterwards.
    """
    from app.auth import generate_agent_token
    from app.models.agent_template import AgentTemplate

    board_id_str = str(body.board_id)
    created = []
    TEMPLATE_NAMES = ["Planner", "Researcher", "Writer", "Reviewer"]

    # Load templates from the DB
    result = await session.exec(
        select(AgentTemplate).where(AgentTemplate.name.in_(TEMPLATE_NAMES))
    )
    templates = {t.name: t for t in result.all()}

    for spec in SPECIALIZED_AGENTS_SPECS:
        raw_token, token_hash = generate_agent_token()

        # Prefer template from the DB (has model config), otherwise fall back to SPEC
        tmpl = templates.get(spec["name"])

        # Scopes: template > default lookup > empty
        from app.scopes import get_default_scopes
        agent_scopes = list(tmpl.scopes or []) if tmpl and tmpl.scopes else get_default_scopes(spec["name"])

        tools_md = _generate_tools_md(spec["name"], spec["emoji"], raw_token, board_id_str, scopes=agent_scopes)

        agent = Agent(
            board_id=body.board_id,
            name=spec["name"],
            emoji=spec["emoji"],
            role=spec["role"],
            model=tmpl.default_model if tmpl else None,  # Fix: always set the model
            is_board_lead=False,
            soul_md=tmpl.soul_md if tmpl else spec["soul_md"],
            rules_md=spec.get("rules_md"),
            skills=list(tmpl.skills or []) if tmpl else [],
            scopes=agent_scopes,
            tools_md=tools_md,
            agent_token_hash=token_hash,
            template_id=tmpl.id if tmpl else None,
            provision_status="local",
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)

        # Vault write mc_token_{slug} for /internal/bootstrap (see create_agent).
        from app.services.secrets_helper import upsert_agent_token_secret
        await upsert_agent_token_secret(session, agent, raw_token)

        # Provision (cli-bridge path) — gateway path removed (Phase 29).
        # `_provision_agent_background` handles cli-bridge runtimes; the legacy
        # Gateway-RPC provision call has been removed.
        # body.provision is a no-op for the gateway path; cli-bridge agents
        # provision via the background task below (Plan 29-07).

        created.append({
            "id": str(agent.id),
            "name": agent.name,
            "emoji": agent.emoji,
            "model": agent.model,
            "token": raw_token,  # return once
        })

    await emit_event(
        session,
        "agents.specialized_setup",
        "4 spezialisierte Agents erstellt: Planner, Researcher, Writer, Reviewer",
        severity="info",
        board_id=body.board_id,
    )

    return {
        "created": created,
        "count": len(created),
        "note": "Tokens werden nur einmalig angezeigt. Sicher aufbewahren!",
    }


# ── Agent Coordination Setup (MUST come before {agent_id} routes) ─────────────


class SetupCoordinationPayload(BaseModel):
    board_slug: str = "mc-dev"
    sync_to_gateway: bool = True


@router.post("/agents/setup-coordination")
async def setup_agent_coordination(
    payload: SetupCoordinationPayload | None = None,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """
    Set up agent coordination: Henry (Lead), Cody (Dev), Rex (Review).
    Sets identities, roles, config files, and board assignments.
    """
    from app.models.board import Board

    board_slug = (payload and payload.board_slug) or "mc-dev"
    sync_to_gateway = (payload and payload.sync_to_gateway) if payload else True

    # Find the board
    result = await session.exec(select(Board).where(Board.slug == board_slug))
    board = result.first()
    if not board:
        raise HTTPException(status_code=404, detail=f"Board '{board_slug}' not found")

    # Load all agents
    result = await session.exec(select(Agent))
    all_agents = result.all()

    setup_results = []

    for config_key, config in AGENT_CONFIGS.items():
        # Find the agent by name match (case-insensitive)
        agent = None
        for a in all_agents:
            if a.name.lower() in config["match_names"]:
                agent = a
                break

        if not agent:
            setup_results.append({
                "agent": config["name"],
                "status": "not_found",
                "detail": f"Kein Agent mit Name {config['match_names']} gefunden",
            })
            continue

        # Update DB fields
        agent.name = config["name"]
        agent.emoji = config["emoji"]
        agent.role = config["role"]
        agent.is_board_lead = config["is_board_lead"]
        agent.board_id = board.id

        # Set skills + scopes + process rules
        agent.skills = config.get("skills", [])
        agent.scopes = config.get("scopes", [])
        if "rules_md" in config:
            agent.rules_md = config["rules_md"]

        # Set config files — soul_md via templates
        # heartbeat_md removed in migration 0125 — was never read by agents.
        agent.identity_md = config["identity_md"]

        board_agents_list = list(all_agents)
        try:
            rendered = render_all_agent_files(
                agent,
                board_id=str(board.id),
                agents_on_board=board_agents_list,
            )
            agent.soul_md = rendered.get("SOUL.md") or config["soul_md"]
            if not agent.memory_md and rendered.get("MEMORY.md"):
                agent.memory_md = rendered["MEMORY.md"]
        except Exception as e:
            logger.warning("Template-Rendering fehlgeschlagen fuer %s: %s — nutze Fallback", agent.name, e)
            agent.soul_md = config["soul_md"]

        # Regenerate tools_md for all agents with an existing token
        if agent.tools_md:
            existing_token = _extract_token_from_tools_md(agent.tools_md)
            if existing_token:
                board_id_str = str(agent.board_id) if agent.board_id else None
                agent.tools_md = _generate_tools_md(
                    agent.name, agent.emoji or "🎯", existing_token, board_id_str,
                    is_board_lead=config["is_board_lead"], scopes=config.get("scopes", []),
                    runtime=getattr(agent, "agent_runtime", "docker") or "docker",
                )

        agent.updated_at = utcnow()
        session.add(agent)

        # Gateway sync removed (Phase 29). Disk persistence is now the
        # sole source of truth; cli-bridge picks it up via
        # `sync_docker_agent_files` on the next restart.

        setup_results.append({
            "agent": config["name"],
            "agent_id": str(agent.id),
            "status": "configured",
            "is_board_lead": config["is_board_lead"],
            "board": board.name,
        })

    await session.commit()

    await emit_event(
        session,
        "agents.coordination_setup",
        "Agent Coordination Setup abgeschlossen",
        severity="info",
        board_id=board.id,
    )

    return {"board": board.name, "agents": setup_results}


# ── Single Agent CRUD ────────────────────────────────────────────────────────


class AssignBoardPayload(BaseModel):
    board_id: uuid.UUID | None = None


@router.patch("/agents/{agent_id}/assign-board")
async def assign_agent_board(
    agent_id: uuid.UUID,
    payload: AssignBoardPayload,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Change (or remove) an agent's board assignment."""
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.board_id = payload.board_id
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


@router.get("/agents/{agent_id}")
async def get_agent(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.patch("/agents/{agent_id}")
async def update_agent(
    agent_id: uuid.UUID,
    payload: AgentUpdate,
    session: AsyncSession = Depends(get_session),
    restart: bool = Query(False, description="docker restart the container after applying changes"),
    current_user = Depends(require_user),
):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    changes = payload.model_dump(exclude_none=True)

    # operational_mode validation + audit
    if "operational_mode" in changes:
        new_mode = changes["operational_mode"]
        if new_mode not in ("active", "paused"):
            raise HTTPException(status_code=422, detail="operational_mode muss 'active' oder 'paused' sein")
        old_mode = agent.operational_mode
        if old_mode != new_mode:
            from app.services.activity import emit_event
            await emit_event(
                session, "agent.mode_changed",
                f"Agent {agent.name}: {old_mode} → {new_mode}",
                agent_id=agent.id,
                board_id=agent.board_id,
                detail={"old_mode": old_mode, "new_mode": new_mode, "changed_by": str(current_user.id)},
            )

    # runtime_id is processed via the dedicated switch service (Phase 15).
    # Pull it OUT of the generic field-merge below so the atomic flow owns the
    # mutation (DB write, file render, container restart, rollback).
    #
    # IMPORTANT: use exclude_unset (not exclude_none) here so that an explicit
    # {"runtime_id": null} in the PATCH body is detected — exclude_none would
    # silently drop the null and make clearing the binding impossible.
    _unset_raw = payload.model_dump(exclude_unset=True)
    runtime_change_present = "runtime_id" in _unset_raw
    new_runtime_id = _unset_raw.get("runtime_id") if runtime_change_present else None
    # ADR-056: the harness axis rides the same switch service. A harness-only
    # change (without runtime_id) re-switches the agent onto its CURRENT runtime
    # with the new harness — the switch service owns the `agent.harness` write,
    # so pull harness OUT of the generic field-merge below.
    harness_change_present = "harness" in _unset_raw
    new_harness = _unset_raw.get("harness")
    changes.pop("harness", None)
    # Remove runtime_id from changes so the generic setattr loop doesn't touch it.
    changes.pop("runtime_id", None)
    force_when_in_progress = bool(changes.pop("force_when_in_progress", False))

    for k, v in changes.items():
        setattr(agent, k, v)
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()
    await session.refresh(agent)

    # Model changes are no longer pushed via the gateway (Phase 29).
    # DB state is the sole source of truth; cli-bridge honors it on the
    # next restart through `sync_docker_agent_files`.

    switch_summary: dict[str, Any] | None = None
    restart_result: dict[str, str] | None = None

    # A runtime change with an explicit target, OR a harness-only change on an
    # agent that already has a runtime binding, both delegate to the switch
    # service. The exception-mapping block is shared — only the resolved target
    # runtime differs (new runtime vs. the agent's current one).
    do_runtime_switch = runtime_change_present and new_runtime_id is not None
    do_harness_only_switch = (
        harness_change_present and new_harness and agent.runtime_id is not None
    )

    if runtime_change_present and new_runtime_id is None:
        # Explicit unset → simple DB-only path (clear runtime_id, no restart).
        agent.runtime_id = None
        agent.updated_at = utcnow()
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
    elif do_runtime_switch or do_harness_only_switch:
        from app.services.agent_runtime_switch import (
            switch_agent_runtime,
            AgentBusyError,
            AgentNotSwitchableError,
            RuntimeIncompatibleError,
            RuntimeNotFoundError,
            RuntimeSwitchLockTimeout,
            SwitchHealthCheckFailed,
        )
        if do_runtime_switch:
            target_id = await _resolve_runtime_id(session, new_runtime_id)
        else:
            # harness-only: re-switch onto the SAME runtime with the new harness.
            target_id = agent.runtime_id
        try:
            result_obj = await switch_agent_runtime(
                session,
                agent,
                target_id,
                new_harness=new_harness,
                force_when_in_progress=force_when_in_progress,
            )
            switch_summary = result_obj.to_dict()
        except RuntimeNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except AgentNotSwitchableError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except RuntimeIncompatibleError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except AgentBusyError as e:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "agent_busy",
                    "message": str(e),
                    "current_task_id": str(e.current_task_id) if e.current_task_id else None,
                },
            )
        except RuntimeSwitchLockTimeout as e:
            raise HTTPException(status_code=409, detail=str(e))
        except SwitchHealthCheckFailed as e:
            raise HTTPException(status_code=503, detail=str(e))
        await session.refresh(agent)
    elif harness_change_present and new_harness and agent.runtime_id is None:
        # Harness-only change, but the agent has no runtime binding to
        # re-switch onto — without this guard the request above would fall
        # through silently (200, nothing applied).
        raise HTTPException(
            status_code=422,
            detail="Agent hat keine Runtime-Bindung — Harness kann nicht gewechselt werden. Zuerst eine Runtime zuweisen.",
        )
    elif restart:
        # Plain restart path (no runtime change) — keep existing semantics for
        # callers that just want a container bounce after touching e.g. soul_md.
        from app.services.docker_agent_sync import (
            sync_agent_files,
            restart_docker_agent_container,
        )
        try:
            await sync_agent_files(session, agent)  # dispatcher: host vs docker
        except Exception as e:
            logger.warning("sync_agent_files after patch failed: %s", e)
        if agent.agent_runtime == "cli-bridge":
            restart_result = restart_docker_agent_container(agent)

    result = agent.model_dump()
    if switch_summary is not None:
        result["_switch"] = switch_summary
    if restart_result is not None:
        result["_restart"] = restart_result
    return result


# ── Runtime Switch Preview (Phase 15) ────────────────────────────────────


class RuntimeSwitchPreviewPayload(BaseModel):
    # UUID (DB id) or slug — resolved server-side. The /runtimes legacy JSON
    # response uses slug-as-id, so the dropdown often passes a slug.
    runtime_id: str
    force_when_in_progress: bool = False
    # ADR-056: optional target harness for the preview — passed straight through
    # as `new_harness` so the modal can show the image change a harness switch
    # would cause. None = keep the agent's current/derived harness.
    harness: str | None = None


async def _resolve_runtime_id(
    session: AsyncSession, value: str | uuid.UUID
) -> uuid.UUID:
    """Accept either a UUID string or a runtime slug; return the DB UUID."""
    if isinstance(value, uuid.UUID):
        return value
    s = str(value).strip()
    try:
        return uuid.UUID(s)
    except ValueError:
        pass
    from app.models.runtime import Runtime as _Runtime
    from sqlmodel import select as _select
    res = await session.exec(_select(_Runtime).where(_Runtime.slug == s))
    rt = res.first()
    if not rt:
        from fastapi import HTTPException as _HE
        raise _HE(status_code=404, detail=f"Runtime '{s}' nicht gefunden")
    return rt.id


@router.post("/agents/{agent_id}/preview-runtime-switch")
async def preview_runtime_switch(
    agent_id: uuid.UUID,
    payload: RuntimeSwitchPreviewPayload,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Dry-run preview of a runtime switch — no mutation.

    Returns the same `SwitchResult` shape as a real switch (image_switched,
    warnings, runtime summaries) so the UI can render its confirm modal.
    Hard incompatibilities (404 / 422 / 409) still surface — only the
    actual restart + rollback is skipped.
    """
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    from app.services.agent_runtime_switch import (
        switch_agent_runtime,
        AgentBusyError,
        AgentNotSwitchableError,
        RuntimeIncompatibleError,
        RuntimeNotFoundError,
    )
    resolved_id = await _resolve_runtime_id(session, payload.runtime_id)
    try:
        result_obj = await switch_agent_runtime(
            session,
            agent,
            resolved_id,
            new_harness=payload.harness,
            force_when_in_progress=payload.force_when_in_progress,
            dry_run=True,
        )
    except RuntimeNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AgentNotSwitchableError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeIncompatibleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except AgentBusyError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "agent_busy",
                "message": str(e),
                "current_task_id": str(e.current_task_id) if e.current_task_id else None,
            },
        )
    return result_obj.to_dict()


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Deletes an agent including all FK references.

    Currently 27 tables have FKs on agents.id (see the mc-task-delete-guard
    skill — same pattern, different root table). Most are nullable and get
    set to NULL. 7 are NOT NULL and must be deleted first, otherwise
    session.delete(agent) fails with an IntegrityError.

    NOT NULL FK tables (must be DELETEd):
      agent_messages.from_agent_id, .to_agent_id
      agent_metrics.agent_id
      approvals.agent_id
      cost_events.agent_id
      task_checkpoints.agent_id
      task_deliverables.agent_id

    Nullable FK tables (SET NULL):
      activity_events, agent_meeting_messages, board_memory,
      chat_messages.{sender_agent_id, agent_id},
      content_pipelines.{writing, review, research}_agent_id,
      deploy_history, playbooks.default_agent_id,
      project_phases.default_agent_id, scheduled_jobs, skill_runs,
      task_checklist_items, task_comments.author_agent_id, task_events,
      tasks.{callback, owner, help_request_from, assigned}_agent_id
    """
    from sqlalchemy import text

    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Gateway cleanup removed (Phase 29). DB delete + FK cleanup is now
    # the sole persistence; cli-bridge containers are managed centrally
    # via docker_agent_sync.

    # Some FK-cleanup tables (e.g. skill_runs) have no SQLModel class — they
    # exist only via Alembic migrations. Under the SQLite test harness (schema
    # built from SQLModel metadata) those tables are absent, so blindly issuing
    # the cleanup SQL raised "no such table". Reflect the live table set once
    # and skip anything absent: on Postgres prod every table exists → identical
    # behaviour; the guard just makes the cleanup resilient to schema drift.
    from sqlalchemy import inspect as _sa_inspect

    existing_tables = set(
        await session.run_sync(
            lambda sync_session: _sa_inspect(sync_session.get_bind()).get_table_names()
        )
    )

    # FK cleanup — delete NOT NULL rows
    not_null_deletes = [
        ("agent_messages", "from_agent_id = :aid OR to_agent_id = :aid"),
        ("agent_metrics", "agent_id = :aid"),
        ("approvals", "agent_id = :aid"),
        ("cost_events", "agent_id = :aid"),
        ("task_checkpoints", "agent_id = :aid"),
        ("task_deliverables", "agent_id = :aid"),
    ]
    for table, where in not_null_deletes:
        if table not in existing_tables:
            continue
        await session.execute(
            text(f"DELETE FROM {table} WHERE {where}"),
            {"aid": str(agent_id)},
        )

    # FK cleanup — set nullable columns to NULL (only if the column
    # exists — each statement is idempotent)
    nullable_updates = [
        ("activity_events", "agent_id"),
        ("agent_meeting_messages", "agent_id"),
        ("board_memory", "agent_id"),
        ("chat_messages", "sender_agent_id"),
        ("chat_messages", "agent_id"),
        ("content_pipelines", "writing_agent_id"),
        ("content_pipelines", "review_agent_id"),
        ("content_pipelines", "research_agent_id"),
        ("deploy_history", "agent_id"),
        ("playbooks", "default_agent_id"),
        ("project_phases", "default_agent_id"),
        ("scheduled_jobs", "agent_id"),
        ("skill_runs", "agent_id"),
        ("task_checklist_items", "agent_id"),
        ("task_comments", "author_agent_id"),
        ("task_events", "agent_id"),
        ("tasks", "callback_agent_id"),
        ("tasks", "owner_agent_id"),
        ("tasks", "help_request_from"),
        ("tasks", "assigned_agent_id"),
    ]
    for table, col in nullable_updates:
        if table not in existing_tables:
            continue
        await session.execute(
            text(f"UPDATE {table} SET {col} = NULL WHERE {col} = :aid"),
            {"aid": str(agent_id)},
        )

    # External-artifact cleanup (found 2026-07-11 — DELETE only touched the DB,
    # leaving the vault token, the compose service block and the staged host
    # files behind). Capture the fields we need BEFORE commit, because the ORM
    # object's attributes expire once the row is gone.
    from types import SimpleNamespace
    from app.services.secrets_helper import delete_agent_token_secret

    agent_name = agent.name
    agent_runtime = getattr(agent, "agent_runtime", None)
    # The stable, insert-time slug (Agent._agent_fill_slug) — NOT the current
    # name, which a plain PATCH rename can change without rotating the token or
    # re-rendering compose. Both the vault key and the compose block were keyed
    # off the ORIGINAL name, and the slug preserves it. Fall back to deriving
    # from name for any legacy row that somehow lacks a slug.
    stable_slug = agent.slug or (agent.name or "").lower().replace(" ", "-")
    host_snapshot = (
        SimpleNamespace(slug=agent.slug, name=agent.name)
        if agent_runtime == "host"
        else None
    )
    compose_slug = stable_slug if agent_runtime == "cli-bridge" else None

    # Vault token — deleted inside this transaction so it rolls back atomically
    # with the agent if the delete fails.
    await delete_agent_token_secret(session, stable_slug)

    await session.delete(agent)
    await session.commit()
    logger.info("Agent %s (%s) deleted with FK cleanup", agent_name, agent_id)

    # Filesystem / compose teardown runs AFTER the commit succeeds — best-effort,
    # so an rmtree or lock error never resurrects a half-deleted agent.
    if host_snapshot is not None:
        try:
            from app.services.host_provisioning import teardown_host_agent_files
            teardown_host_agent_files(host_snapshot)
        except Exception as e:  # noqa: BLE001
            logger.warning("host file teardown for %s failed: %s", agent_name, e)

    if compose_slug is not None:
        try:
            from app.services.compose_renderer import prune_compose_agent
            await prune_compose_agent(compose_slug)
        except Exception as e:  # noqa: BLE001
            logger.warning("compose prune for %s failed: %s", agent_name, e)


# ── Config ────────────────────────────────────────────────────────────────────

@router.get("/agents/{agent_id}/config")
async def get_agent_config(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Dynamically generate USER.md
    user_md_content = None
    try:
        from app.services.template_renderer import render_agent_file, build_agent_context
        from sqlmodel import select as sql_select
        board_agents = []
        if agent.board_id:
            result = await session.exec(sql_select(Agent).where(Agent.board_id == agent.board_id))
            board_agents = list(result.all())
        user_md_content = render_agent_file("USER.md.j2", build_agent_context(
            agent, board_id=str(agent.board_id) if agent.board_id else None,
            agents_on_board=board_agents,
        ))
    except Exception:
        pass

    return {
        "tools_md": agent.tools_md,
        "rules_md": agent.rules_md,
        "identity_md": agent.identity_md,
        "soul_md": agent.soul_md,
        "memory_md": agent.memory_md,
        "user_md": user_md_content,
    }


@router.get("/agents/{agent_id}/config/{file_type}")
async def get_agent_config_file(
    agent_id: uuid.UUID,
    file_type: str,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    if file_type not in CONFIG_FILE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown file type: {file_type}")
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"file_type": file_type, "content": getattr(agent, file_type)}


@router.put("/agents/{agent_id}/config/{file_type}")
async def update_agent_config_file(
    agent_id: uuid.UUID,
    file_type: str,
    payload: ConfigFileUpdate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    if file_type not in CONFIG_FILE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown file type: {file_type}")
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Validate TOOLS.md
    if file_type == "tools_md":
        warnings = _validate_tools_md(payload.content)
    else:
        warnings = []

    # Save to DB
    setattr(agent, file_type, payload.content)
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()

    # Auto-sync to gateway removed (Phase 29). The DB write + template render
    # is the sole source of truth; cli-bridge fetches the files via
    # `sync_docker_agent_files` on the next restart.

    result = {"saved": True, "gateway_sync": False, "warnings": warnings}
    return result


def _generate_tools_md(
    name: str,
    emoji: str,
    raw_token: str,
    board_id: str | None,
    is_board_lead: bool = False,
    scopes: list[str] | None = None,
    runtime: str = "docker",
) -> str:
    """Proxy — delegates to services/tools_md_builder.py.

    runtime: "host" (Boss) or "docker" (cli-bridge, default). Only
    determines the phrasing of the vault section (host path vs container mount).
    """
    from app.services.tools_md_builder import generate_tools_md
    return generate_tools_md(
        name, emoji, raw_token, board_id, is_board_lead, scopes, runtime=runtime
    )


def _validate_tools_md(content: str) -> list[str]:
    warnings = []
    if "Authorization: Bearer" not in content:
        warnings.append("Auth header should be 'Authorization: Bearer <token>'")
    if "$AUTH_TOKEN" in content or "$BOARD_ID" in content:
        warnings.append("Shell variables ($AUTH_TOKEN etc.) do not work in agent context")
    return warnings


# ── Actions ───────────────────────────────────────────────────────────────────

@router.post("/agents/{agent_id}/trigger")
async def trigger_agent(
    agent_id: uuid.UUID,
    payload: TriggerPayload,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user_or_control_plane),
):
    """Deprecated (Phase 29 — Gateway sunset).

    The synchronous trigger endpoint used the OpenClaw Gateway's
    chat-send + poll-reply RPC pair to push a message and wait for a reply.
    With the gateway removed, agents are addressed via TaskComment
    (cli-bridge poll.sh) — there is no synchronous request/response channel.
    Frontend rebuild in Phase 31 will remove the call site.
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Synchronous trigger endpoint removed in Phase 29 (Gateway sunset). "
            "Use TaskComment-based task delivery instead. Frontend rebuild in Phase 31."
        ),
    )


@router.post("/agents/{agent_id}/reset")
async def reset_agent(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user_or_control_plane),
):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if getattr(agent, "agent_runtime", "openclaw") == "cli-bridge":
        # CLI-Bridge agents: restart the worker instead of resetting the session
        from app.routers.cli_terminal import _bridge_post
        agent_slug = agent.name.lower().replace(" ", "-")
        result = _bridge_post(f"/worker/{agent_slug}/restart", {})
        if result is None or not result.get("ok"):
            raise HTTPException(status_code=503, detail=f"Worker restart fehlgeschlagen: {result}")
        return {"ok": True, "message": f"Worker für {agent.name} neu gestartet.", "bridge_result": result}
    if getattr(agent, "agent_runtime", "openclaw") == "host":
        # Phase 26 / HERM-13 follow-up: Host-runtime agents (Hermes) delegate to
        # the host-agent lifecycle endpoint (which routes via bridge HTTP for
        # Hermes specifically and via SSH+launchctl for Boss). Same code path
        # as POST /host-agents/{id}/restart.
        from app.routers.cli_terminal import _host_agent_lifecycle
        return await _host_agent_lifecycle(agent, "restart")
    # Gateway-based reset removed in Phase 29 (Gateway sunset).
    # Only cli-bridge and host runtimes are supported above; falling through here
    # means the agent has an obsolete agent_runtime value.
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Gateway-based session reset removed in Phase 29 (Gateway sunset). "
            "Only cli-bridge (worker restart) and host (lifecycle restart) "
            "runtimes are supported."
        ),
    )


async def _redispatch_after_reset(session: AsyncSession, agent: Agent) -> None:
    """Stub kept for backward-compatibility callers; gateway reset removed (Phase 29)."""
    return None


@router.post("/agents/{agent_id}/health-check")
async def agent_health_check(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Runtime-aware readiness for the onboarding wizard's final gate.

    'ready' means the agent is provisioned AND its session is alive — NOT
    that it answered a message (the synchronous trigger channel was retired
    in Phase 29). Reuses existing liveness signals: the cli-bridge helper
    probe + heartbeat status (cli-bridge), recent last_seen_at (host).
    """
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    runtime = getattr(agent, "agent_runtime", "cli-bridge") or "cli-bridge"
    alive_states = {"online", "busy", "idle", "working"}
    checks: list[dict[str, Any]] = []

    provisioned = agent.provision_status == "provisioned"
    checks.append({
        "label": "provisioned",
        "ok": provisioned,
        "detail": f"provision_status={agent.provision_status}",
    })

    if runtime == "cli-bridge":
        from app.routers import cli_terminal
        helper_ok = cli_terminal._bridge_get("/health") is not None
        checks.append({
            "label": "cli-bridge helper",
            "ok": helper_ok,
            "detail": "reachable" if helper_ok else "scripts/cli-bridge.py not reachable",
        })
        heartbeating = agent.status in alive_states
        checks.append({
            "label": "heartbeat",
            "ok": heartbeating,
            "detail": f"status={agent.status}",
        })
    elif runtime == "host":
        recent = False
        if agent.last_seen_at is not None:
            delta = utcnow() - agent.last_seen_at
            recent = delta.total_seconds() < 180
        checks.append({
            "label": "host heartbeat",
            "ok": recent,
            "detail": "seen <3min ago" if recent else "no recent heartbeat — launchd job may not be loaded",
        })
    else:
        checks.append({
            "label": "runtime",
            "ok": True,
            "detail": f"{runtime}: no automated liveness signal",
        })

    ready = all(c["ok"] for c in checks)
    return {
        "provision_status": agent.provision_status,
        "runtime": runtime,
        "ready": ready,
        "checks": checks,
    }


@router.post("/agents/{agent_id}/heartbeat")
async def trigger_heartbeat(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user_or_control_plane),
):
    """Deprecated (Phase 29 — Gateway sunset).

    The heartbeat trigger used the legacy chat-send RPC (or GatewayClient HTTP)
    to push a nudge message into an openclaw session. With the gateway gone,
    agents are addressed via TaskComment polled by `poll.sh`. Heartbeats are
    no longer task-scoped — Operators should create an explicit task with a
    Heartbeat-Brief instead. Frontend will remove the call site in Phase 31.
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Heartbeat trigger removed in Phase 29 (Gateway sunset). "
            "Use TaskComment-based delivery (create a task with a heartbeat brief) "
            "to address an agent. Frontend rebuild in Phase 31."
        ),
    )


async def _lead_heartbeat_dispatch(session: AsyncSession, agent: Agent) -> dict:
    """Deprecated (Phase 29 — Gateway sunset). Stub returns 410-style payload.

    The Board Lead heartbeat used the legacy chat-send RPC to push a board
    snapshot into the lead's openclaw session. With the gateway gone, a Board
    Lead snapshot should be delivered as a TaskComment on a dedicated heartbeat
    task. Kept as a stub for any straggler callers; the heartbeat endpoint
    itself returns 410.
    """
    return {
        "source": "deprecated",
        "dispatch": False,
        "reason": "Gateway sunset (Phase 29) — use TaskComment-based heartbeat brief.",
    }


# ── Analytics ─────────────────────────────────────────────────────────────────

@router.get("/agents/{agent_id}/metrics")
async def get_agent_metrics(
    agent_id: uuid.UUID,
    period: str = Query("7d"),
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    days = {"7d": 7, "30d": 30, "all": 365}.get(period, 7)
    since = utcnow() - timedelta(days=days)

    result = await session.exec(
        select(AgentMetrics)
        .where(AgentMetrics.agent_id == agent_id, AgentMetrics.period_start >= since)
        .order_by(AgentMetrics.period_start)
    )
    return result.all()


@router.get("/agents/{agent_id}/metrics/summary")
async def get_agent_metrics_summary(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    context_pct = round(agent.context_tokens / agent.context_max * 100, 1) if agent.context_max else 0

    return {
        "agent_id": str(agent_id),
        "name": agent.name,
        "status": agent.status,
        "context_tokens": agent.context_tokens,
        "context_max": agent.context_max,
        "context_pct": context_pct,
        "total_tasks_completed": agent.total_tasks_completed,
        "total_compactions": agent.total_compactions,
        "last_seen_at": agent.last_seen_at,
        "last_task_activity_at": agent.last_task_activity_at,
    }


@router.get("/agents/{agent_id}/runtime-switch-progress")
async def runtime_switch_progress(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Live progress of an in-flight runtime switch (polled by the modal)."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    redis = await get_redis()
    raw = await redis.get(RedisKeys.agent_switch_progress(str(agent_id)))
    if raw is None:
        return {"step": None}
    import json as _json
    return _json.loads(raw)


# ── Token Reset ───────────────────────────────────────────────────────────────


@router.post("/agents/{agent_id}/reset-token")
async def reset_agent_token(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """
    Generates a new agent token and updates TOOLS.md.
    Useful for agents that were set up via setup-coordination (no token).
    Returns: { token } — visible once, not retrievable afterwards!
    """
    from app.auth import generate_agent_token

    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent nicht gefunden")

    raw_token, token_hash = generate_agent_token()

    board_id_str = str(agent.board_id) if agent.board_id else None
    agent.agent_token_hash = token_hash
    agent.tools_md = _generate_tools_md(
        agent.name,
        agent.emoji or "🤖",
        raw_token,
        board_id_str,
        is_board_lead=agent.is_board_lead or False,
        scopes=agent.scopes or [],
        runtime=getattr(agent, "agent_runtime", "docker") or "docker",
    )
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()

    # Vault rotation: the new token must overwrite mc_token_{slug}, otherwise
    # /internal/bootstrap serves the old one on the next container start.
    from app.services.secrets_helper import upsert_agent_token_secret
    await upsert_agent_token_secret(session, agent, raw_token)

    # Gateway sync removed (Phase 29). New TOOLS.md sits on disk; the
    # cli-bridge `sync_docker_agent_files` path picks it up on the next restart.

    await emit_event(
        session,
        "agent.token_reset",
        f"{agent.emoji or '🤖'} {agent.name}: Token zurückgesetzt",
        severity="info",
        agent_id=agent.id,
        board_id=agent.board_id,
    )

    return {
        "agent_id": str(agent.id),
        "name": agent.name,
        "token": raw_token,  # one-time — not retrievable afterwards
    }


# ── Agent Council: Provisioning ──────────────────────────────────────────────


class ProvisionPayload(BaseModel):
    """Optional overrides for provisioning (CLI-Bridge agents).

    Phase 30: `gateway_id` removed — provisioning is runtime-aware and no
    longer gated by Gateway-row presence.
    """
    discord_channel: bool = False  # create and bind Discord channel
    # CLI-Bridge fields (only used for cli-bridge agents)
    model: str | None = None
    system_prompt: str | None = None
    role: str | None = None
    skills: list[str] | None = None
    extra_plugins: list[str] | None = None


class DiscordChannelCreate(BaseModel):
    name: str
    context: str  # system prompt / channel purpose


class DiscordChannelRename(BaseModel):
    new_name: str


class SyncConfigPayload(BaseModel):
    """Optionally restrict which config files to sync."""
    file_types: list[str] | None = None  # None = sync all


@router.post("/agents/{agent_id}/provision")
async def provision_agent_on_gateway(
    agent_id: uuid.UUID,
    payload: ProvisionPayload | None = None,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Provision agent — cli-bridge or host runtime only (Phase 29).

    The openclaw gateway code path was removed; if a caller hits this endpoint
    for an agent with `agent_runtime == "openclaw"`, the trailing 410 ensures a
    clear error message. cli-bridge + host runtimes early-return below.
    """
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # CLI-Bridge agents have their own provision endpoint in cli_terminal.py —
    # since agents.router is registered before cli_terminal.router, we intercept here.
    if getattr(agent, "agent_runtime", "openclaw") == "cli-bridge":
        from app.routers.cli_terminal import provision_cli_agent, CliProvisionPayload
        cli_payload = None
        if payload:
            kwargs: dict = {}
            if getattr(payload, "model", None) is not None:
                kwargs["model"] = payload.model
            if getattr(payload, "system_prompt", None) is not None:
                kwargs["system_prompt"] = payload.system_prompt
            if getattr(payload, "role", None) is not None:
                kwargs["role"] = payload.role
            if getattr(payload, "skills", None) is not None:
                kwargs["skills"] = payload.skills
            if getattr(payload, "extra_plugins", None) is not None:
                kwargs["extra_plugins"] = payload.extra_plugins
            cli_payload = CliProvisionPayload(**kwargs)
        return await provision_cli_agent(agent_id, cli_payload, session, current_user)

    # Host-side agents (Phase 24, HERM-01): Hermes Worker on macOS launchd.
    # Branches on runtime.runtime_type=='hermes' so future host workers can
    # add their own elif. Idempotent re-run is supported (overwrite env, no
    # 409 guard). Failure rolls provision_status back to 'local'.
    if getattr(agent, "agent_runtime", "openclaw") == "host":
        from app.models.runtime import Runtime

        runtime = (
            await session.get(Runtime, agent.runtime_id) if agent.runtime_id else None
        )
        if runtime is None:
            raise HTTPException(
                status_code=400,
                detail="Host agent has no runtime_id — cannot provision",
            )

        # Mark provisioning (idempotent — re-run on already-provisioned host agent OK)
        previous_status = agent.provision_status
        agent.provision_status = "provisioning"
        session.add(agent)
        await session.commit()

        try:
            # Adapter-registered host harnesses (ADR-064: hermes, grok, …)
            # take the adapter path; anything else falls through to the
            # generic wizard staging path below (ADR-063).
            from app.services.harness_compat import derive_harness, is_compatible, incompat_reason
            from app.services.host_harness_adapter import get_adapter

            harness = agent.harness or derive_harness(runtime)
            adapter = get_adapter(harness)
            if adapter is not None:
                if not is_compatible(harness, runtime):
                    raise HTTPException(status_code=422, detail=incompat_reason(harness, runtime))
                result = await adapter.bootstrap(session, agent, runtime)
                return {
                    "status": "provisioned",
                    "agent_id": str(agent.id),
                    "token": result["token"],  # one-time visible
                    "env_path": result["env_path"],
                    "plist_loaded": result["plist_loaded"],
                    "plist_already": result["plist_already"],
                    "tmux_session": result["tmux_session"],
                    "workspace_path": result["workspace_path"],
                }

            # Generic host agent (onboarding wizard, 2026-07-10): stage
            # plist + run.sh + agent.env into ~/.mc/agents/<slug>/. launchctl
            # load is gated behind settings.host_agent_autoload_enabled.
            from app.auth import generate_agent_token
            from app.services import host_provisioning
            from app.services.secrets_helper import upsert_agent_token_secret

            raw_token, token_hash = generate_agent_token()
            stage = await host_provisioning.stage_host_agent_files(
                agent, runtime, raw_token, session=session
            )
            load = host_provisioning.maybe_load_plist(stage)

            # Only mutate the persisted hash once staging (fallible I/O) has
            # succeeded — otherwise a raise here would leave the generic
            # except-handler below to commit a new hash for a token that was
            # never staged/returned, destroying the previously working one.
            agent.agent_token_hash = token_hash
            agent.workspace_path = stage.workspace_path
            agent.provision_status = "provisioned" if load["loaded"] else "provisioning"
            agent.provisioned_at = utcnow() if load["loaded"] else None
            agent.updated_at = utcnow()
            session.add(agent)
            await session.commit()
            await upsert_agent_token_secret(session, agent, raw_token)
            await emit_event(
                session,
                "agent.provisioned" if load["loaded"] else "agent.host_files_staged",
                (
                    f"{agent.name} (host) provisioniert + launchd geladen"
                    if load["loaded"]
                    else f"{agent.name} (host): Dateien nach {stage.workspace_path} gerendert. "
                    f"Zum Aktivieren auf dem Host ausführen: {stage.launchctl_command}"
                ),
                severity="info",
                agent_id=agent.id,
                board_id=agent.board_id,
            )
            return {
                "status": agent.provision_status,
                "agent_id": str(agent.id),
                "token": raw_token,  # one-time visible
                "workspace_path": stage.workspace_path,
                "plist_label": stage.plist_label,
                "plist_staged_path": stage.plist_staged_path,
                "launchctl_command": stage.launchctl_command,
                "plist_loaded": load["loaded"],
            }
        except HTTPException:
            agent.provision_status = previous_status
            session.add(agent)
            await session.commit()
            raise
        except Exception as e:
            logger.error(
                "host-agent provision failed for %s: %s", agent.name, e, exc_info=True
            )
            agent.provision_status = "local"
            agent.updated_at = utcnow()
            session.add(agent)
            await session.commit()
            await emit_event(
                session,
                "agent.provision_failed",
                f"{agent.name} (host) Provisioning fehlgeschlagen: {e}",
                severity="error",
                agent_id=agent.id,
                board_id=agent.board_id,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Host-agent provisioning failed: {e}",
            )

    # Gateway provisioning removed (Phase 29 — gateway sunset).
    # Only cli-bridge and host runtimes are supported by the provision
    # endpoint; all other `agent_runtime` values are obsolete and
    # return 410.
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "OpenClaw Gateway provisioning removed in Phase 29 (Gateway sunset). "
            "Only cli-bridge and host runtimes are supported. "
            "Set agent_runtime accordingly and re-provision."
        ),
    )


@router.post("/agents/{agent_id}/sync-config")
async def sync_agent_config_to_gateway(
    agent_id: uuid.UUID,
    payload: SyncConfigPayload | None = None,
    restart: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Push all (or selected) config files from MC into the claude-config
    Bind-Mount (cli-bridge / Docker-V2) or the host runtime workspace.

    Phase 29: the openclaw gateway path is removed — agent_runtime must
    be either "cli-bridge" or "host".

    Query Parameters:
        restart: When true:
                 - cli-bridge (Docker): the container is restarted
                 - host: only writes files, no restart (caller managed)

    Runtime switch:
    - cli-bridge (Docker) -> sync_docker_agent_files() into the host filesystem
                          + optional restart_docker_agent_container()
    - host -> sync_host_agent_files() (no restart)
    """

    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # cli-bridge (Docker-V2 / Host-Legacy) path: renders templates into the
    # claude-config bind mount; the Docker container reads SOUL.md on
    # openclaude startup via the start-claude.sh wrapper.
    if getattr(agent, "agent_runtime", "openclaw") == "cli-bridge":
        from app.services.docker_agent_sync import (
            sync_docker_agent_files,
            restart_docker_agent_container,
        )
        file_sync_results = await sync_docker_agent_files(session, agent)
        response: dict = {"synced": file_sync_results, "runtime": "cli-bridge"}
        if restart:
            restart_result = restart_docker_agent_container(agent)
            response["restart"] = restart_result
        await emit_event(
            session,
            "agent.config_synced",
            f"{agent.name} config synced to claude-config (cli-bridge)" + (" + restarted" if restart else ""),
            severity="info",
            agent_id=agent.id,
            board_id=agent.board_id,
            detail=response,
        )
        return response

    # Host-runtime Pfad (Boss native claude CLI, Hermes MCP bridge): write
    # rendered templates to ``agent.workspace_path/claude-config/`` so the
    # host process picks them up on next start. No restart hook here — the
    # caller is responsible for bouncing the host process if needed.
    if getattr(agent, "agent_runtime", "openclaw") == "host":
        from app.services.docker_agent_sync import sync_host_agent_files
        file_sync_results = await sync_host_agent_files(session, agent)
        response = {"synced": file_sync_results, "runtime": "host"}
        await emit_event(
            session,
            "agent.config_synced",
            f"{agent.name} config synced to claude-config (host)",
            severity="info",
            agent_id=agent.id,
            board_id=agent.board_id,
            detail=response,
        )
        return response

    # openclaw gateway path removed (Phase 29 — gateway sunset).
    # Anyone hitting this endpoint has an obsolete `agent_runtime` setting
    # (anything other than "cli-bridge" / "host"). Response: 410 Gone with a hint.
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Gateway-based sync-config removed in Phase 29 (Gateway sunset). "
            "Set agent_runtime to 'cli-bridge' or 'host' and re-provision."
        ),
    )


# ── Agent Council: Discord Channel Management — Phase 29 Redirect ────────────
#
# The Discord-channel endpoints moved to `routers/discord.py` (D-04, Plan 29-01).
# Frontend still calls the OLD paths (`/api/v1/agents/{id}/discord-channel`)
# until the Phase 31 rebuild. To avoid 404 spam, we redirect with HTTP 307
# (preserves the HTTP method + body, unlike 308 which is permanent).
#
# Phase 31 frontend rebuild removes the redirects.
from fastapi.responses import RedirectResponse


@router.post("/agents/{agent_id}/discord-channel", include_in_schema=False)
async def _redirect_create_discord_channel(agent_id: uuid.UUID):
    return RedirectResponse(
        url=f"/api/v1/discord/agents/{agent_id}/channel",
        status_code=307,
    )


@router.patch("/agents/{agent_id}/discord-channel", include_in_schema=False)
async def _redirect_rename_discord_channel(agent_id: uuid.UUID):
    return RedirectResponse(
        url=f"/api/v1/discord/agents/{agent_id}/channel",
        status_code=307,
    )


@router.delete("/agents/{agent_id}/discord-channel", include_in_schema=False)
async def _redirect_delete_discord_channel(agent_id: uuid.UUID):
    return RedirectResponse(
        url=f"/api/v1/discord/agents/{agent_id}/channel",
        status_code=307,
    )


# Agent configurations for the coordination setup (at the end of the file, so the router code above stays clear)
from app.scopes import ALL_SCOPES, DEFAULT_SCOPES, AgentRole

AGENT_CONFIGS = {
    "henry": {
        "match_names": ["henry", "main"],
        "name": "Henry",
        "emoji": "\U0001f3af",  # 🎯
        "role": "lead",
        "is_board_lead": True,
        "skills": [],
        "scopes": ALL_SCOPES,
        "rules_md": """## When you are stuck (ERROR RECOVERY)
- 1st attempt: analyze the error, try an alternative approach
- 2nd attempt: if that doesn't work either → IMMEDIATELY:
  PATCH status: blocked
  POST comment: "Blocked: [exact error + what was tried]"
- NEVER give up silently or endlessly repeat the same thing
- For delegation problems (no agent available, agent not responding) → blocked + comment""",
        "identity_md": """# Henry — Lead & Coordinator

## Who I am
I am Henry, the Lead Agent in the Mission Control system. I coordinate the team and make sure tasks get distributed and completed efficiently.

## My role
- **Board Lead** on the MC Dev board
- Review new tasks and assign them to the right agent
- Keep track of progress
- Ask the operator when things are unclear
- Ensure quality before tasks get marked done

## My team
- **Cody** (Fullstack Developer): writes code, builds features, fixes bugs
- **Rex** (Code Review & Security): reviews, security checks, quality assurance

## Decision principle
- Simple tasks: delegate directly to the right agent
- Big decisions: convene a council (all agents give input)
- Critical matters: ask the operator for approval
""",
        "soul_md": """# Henry — Personality

## Values
- Efficiency: no unnecessary bureaucracy, just get it done
- Quality: better right once than fast three times
- Teamwork: every agent has their strengths — we use them
- Transparency: the operator always knows what's going on

## Working style
- I think in a structured way and prioritize by impact
- I delegate based on specialization, not at random
- I keep it brief and direct
- I'd rather ask once too often than too rarely

## Communication style
- Use emojis to lighten up messages (🎯 for goals, ✅ for completed things, 📋 for lists, 🔍 for analysis, etc.)
- Use Markdown formatting: **bold** for important things, lists for multiple points
- Friendly and direct — no dry robotic tone
""",
    },
    "cody": {
        "match_names": ["cody"],
        "name": "Cody",
        "emoji": "\U0001f9d1\u200d\U0001f4bb",  # 🧑‍💻
        "role": "developer",
        "is_board_lead": False,
        "skills": [],
        "scopes": DEFAULT_SCOPES[AgentRole.DEVELOPER],
        "rules_md": """Apply TDD — test FIRST:
1. Write a test describing the desired behavior
2. Run the test — it MUST fail (RED)
3. Write the minimal code to make the test pass (GREEN)
4. Refactor — tests must stay green

No claim without proof:
- Run tests BEFORE reporting "done"
- Post the test output as evidence in the comment

## When you are stuck (ERROR RECOVERY)
- 1st attempt: analyze the error, try an alternative approach
- 2nd attempt: if that doesn't work either → IMMEDIATELY:
  PATCH status: blocked
  POST comment: "Blocked: [exact error + what was tried]"
- NEVER give up silently or endlessly repeat the same thing
- For build errors you can't resolve → blocked + error message as a comment""",
        "identity_md": """# Cody — Fullstack Developer

## Who I am
I am Cody, the Fullstack Developer on the Mission Control team. I write code, build features, and fix bugs.

## My specialization
- Frontend: Next.js, React, TypeScript, Tailwind CSS
- Backend: Python, FastAPI, SQLModel, PostgreSQL
- Infrastructure: Docker, Docker Compose
- General: feature development, bug fixing, refactoring

## My workflow
1. Read and understand the task
2. Analyze the relevant code
3. Plan the implementation
4. Write and test the code
5. Set task to review (Rex checks it)

## Collaboration
- Henry assigns me tasks — I work through them
- Rex reviews my code — I take the feedback seriously
- When something is unclear: leave a comment on the task
""",
        "soul_md": """# Cody — Personality

## Values
- Clean code: readable, maintainable, well-structured
- Pragmatism: the simplest solution that works
- Learning: trying out new patterns and tools
- Reliability: what I take on gets finished

## Working style
- I always read the existing code first before changing anything
- I stick to the existing architecture and patterns
- I test my changes before marking them done
- I only document what's necessary — code should be self-explanatory

## Communication style
- Use emojis: 🧑‍💻 for code work, ✅ for finished things, 🐛 for bugs, 🚀 for new features, ⚠️ for warnings
- Markdown formatting: **bold** for important things, code blocks for code, lists for steps
- Enthusiastic but matter-of-fact — show enjoyment in building things
""",
    },
    "rex": {
        "match_names": ["rex"],
        "name": "Rex",
        "emoji": "\U0001f6e1\ufe0f",  # 🛡️
        "role": "reviewer",
        "is_board_lead": False,
        "skills": [],
        "scopes": DEFAULT_SCOPES[AgentRole.REVIEWER],
        "rules_md": """Verify EVERYTHING yourself:
- Run the tests in the developer workspace
- Check whether tests actually exist and are meaningful
- No rubber-stamping — if tests are missing, reject
- Post the verification output as evidence

## When you are stuck (ERROR RECOVERY)
- 1st attempt: analyze the error, try an alternative approach
- 2nd attempt: if that doesn't work either → IMMEDIATELY:
  PATCH status: blocked
  POST comment: "Blocked: [exact error + what was tried]"
- NEVER give up silently or endlessly repeat the same thing""",
        "identity_md": """# Rex — Code Review & Security

## Who I am
I am Rex, responsible for code reviews and security on the Mission Control team. I make sure the code is high-quality and secure.

## My specialization
- Code review: architecture, patterns, best practices
- Security: OWASP Top 10, input validation, auth checks
- Quality: edge cases, error handling, performance
- Testing: checking test coverage, identifying critical paths

## My workflow
1. Check tasks in review status
2. Analyze code changes
3. Run security checks
4. Leave feedback as a comment
5. On problems: set task back to in progress
6. On approval: set task to done (as a board-lead recommendation to Henry)

## Collaboration
- Henry assigns me review tasks
- I review Cody's code — constructively and respectfully
- On security concerns: immediately request approval from the operator
""",
        "soul_md": """# Rex — Personality

## Values
- Security: no compromise on security matters
- Thoroughness: every change deserves a close look
- Constructiveness: criticism always comes with a suggested improvement
- Vigilance: proactively look for problems, don't wait

## Working style
- I review systematically: logic first, then security, then style
- I explain my findings clearly and understandably
- I only block on real problems, not on style questions
- I learn from past reviews and remember patterns

## Communication style
- Use emojis: 🛡️ for security, ✅ for approved, ❌ for rejected, 🔍 for findings, ⚠️ for warnings
- Markdown formatting: **bold** for critical points, lists for findings
- Matter-of-fact but not cold — constructive feedback with a clear tone
""",
    },
}



# Endpoint function is defined above (before {agent_id} routes)


# ── Agent Task-Sessions ───────────────────────────────────────────────────────


@router.get("/agents/{agent_id}/task-sessions")
async def get_agent_task_sessions(
    agent_id: uuid.UUID,
    limit: int = Query(20, le=50),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """All dispatched tasks of this agent with session keys."""
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    result = await session.exec(
        select(Task)
        .where(Task.assigned_agent_id == agent_id)
        .where(Task.dispatched_at.isnot(None))
        .order_by(Task.updated_at.desc())
        .limit(limit)
    )
    tasks = result.all()

    items = []
    for t in tasks:
        # Phase 30: gateway_agent_id session-key reconstruction dropped.
        # `spawn_session_key` is now the canonical source — populated at
        # dispatch time for subagent-runtime tasks, NULL otherwise.
        items.append({
            "task_id": str(t.id),
            "title": t.title,
            "status": t.status,
            "session_key": t.spawn_session_key,
            "has_active_session": t.spawn_session_key is not None,
            "dispatched_at": t.dispatched_at.isoformat() if t.dispatched_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        })
    return items


# Comment types that get delivered as actionable system events to the
# responsible agent (in addition to user comments). Single source of
# truth: app/comment_types.py (REL-01). The alias keeps the historical
# import name `_DELIVER_SYSTEM_COMMENT_TYPES` for existing tests.
#
# Live-bug history (see the comment_types.py module docstring):
#   - 2026-04-23: feedback comment silent-drop, Tester got stuck
#   - 2026-04-24 (PR #99/#110): install_completed/install_failed silent-drop
# To add a new type → edit app/comment_types.py.
from app.comment_types import DELIVERABLE_SYSTEM_TYPES as _DELIVER_SYSTEM_COMMENT_TYPES  # noqa: E402


def _is_deliverable_for(c, agent_id) -> bool:
    """True if the comment should be delivered to the agent.

    Deliver:
      (a) User comments (author_type='user')   -> the operator talks directly to the agent
      (b) Actionable events on the task, NOT from the agent itself
          (subtask_completed, resolution, blocker, system)

    Skip:
      - The agent's own comments (no echo loop)
      - Routine comments (checkpoint, progress, message from other agents, audit, etc.)
    """
    if c.author_type == "user":
        return True
    # The agent's own comments are NEVER delivered (author_agent_id == polling agent)
    if c.author_agent_id == agent_id:
        return False
    # For agent/system authored comments: only if comment_type is actionable
    return c.comment_type in _DELIVER_SYSTEM_COMMENT_TYPES


def _comment_source(c) -> str:
    """Category for client-side display."""
    if c.author_type == "user":
        return "user"
    if c.comment_type in _DELIVER_SYSTEM_COMMENT_TYPES:
        return "system"
    return "other"


async def _collect_and_ack_new_comments(agent: Agent, session: AsyncSession) -> list[dict]:
    """Collects new comments on the agent's active tasks.

    Delivers user comments (messages from the operator) AND actionable system
    events (subtask_completed, resolution, blocker) — the latter even if they
    were written by another agent (e.g. the worker on a subtask). This is how
    the parent-task agent learns, e.g., that its delegated subtask is done.

    Cursor advancement happens here — a comment isn't delivered again on the
    next poll. The cursor is per (agent, task). If missing, all relevant
    comments count as new (first poll after task claim).
    """
    from app.models.task import TaskComment
    from app.models.agent_task_comment_cursor import AgentTaskCommentCursor

    # 2026-05-18: added `done` + `user_test`. The operator had written a
    # comment on a done task ("MC Home Page fixen", 19:51 UTC) and expected
    # Boss to react — the comment landed in the void because the previous
    # filter excluded terminal lanes. `failed`/`aborted` are deliberately
    # left out: those should be explicitly re-opened, not handled via comment.
    active_res = await session.exec(
        select(Task).where(
            Task.assigned_agent_id == agent.id,
            Task.status.in_(["in_progress", "inbox", "review", "blocked", "done", "user_test"]),  # type: ignore[union-attr]
        )
    )
    active_tasks = list(active_res.all())
    if not active_tasks:
        return []

    new_comments: list[dict] = []
    for task in active_tasks:
        cursor_res = await session.exec(
            select(AgentTaskCommentCursor).where(
                AgentTaskCommentCursor.agent_id == agent.id,
                AgentTaskCommentCursor.task_id == task.id,
            )
        )
        cursor = cursor_res.first()
        last_seen = cursor.last_seen_comment_id if cursor else None

        # Load all comments chronologically; filtering happens in Python to
        # keep the DB query simple and to express the "skip own comments"
        # check cleanly.
        all_res = await session.exec(
            select(TaskComment)
            .where(TaskComment.task_id == task.id)
            .order_by(TaskComment.created_at.asc())  # type: ignore[union-attr]
        )
        all_comments = list(all_res.all())
        if not all_comments:
            continue

        # Deliverable subset
        deliverable = [c for c in all_comments if _is_deliverable_for(c, agent.id)]
        if not deliverable:
            continue

        # Slice "unseen" relative to the cursor. The cursor can point to a
        # non-deliverable comment (history) — we search for the ID in the
        # full log and take the deliverables after that position.
        if last_seen is None:
            unseen = deliverable
        else:
            last_idx = next(
                (i for i, c in enumerate(all_comments) if c.id == last_seen),
                -1,
            )
            if last_idx < 0:
                unseen = deliverable
            else:
                unseen = [c for c in deliverable if all_comments.index(c) > last_idx]

        for c in unseen:
            new_comments.append({
                "comment_id": str(c.id),
                "task_id": str(task.id),
                "task_title": task.title,
                "content": c.content,
                "comment_type": c.comment_type,
                "source": _comment_source(c),
                "created_at": c.created_at.isoformat(),
            })

        # Set the cursor to the last seen REAL comment in the full log (not
        # just the last deliverable one), so the cursor stays monotonic.
        # Atomic upsert — avoids UniqueConstraintViolation on parallel polls
        # (bug 2026-04-22: 3x DETAIL: Key (agent_id, task_id) already exists in the log).
        if unseen:
            last_id = all_comments[-1].id
            await _upsert_cursor(session, agent.id, task.id, last_id)

    if new_comments:
        await session.commit()

    return new_comments


async def _upsert_cursor(
    session: AsyncSession,
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    last_seen_comment_id: uuid.UUID,
) -> None:
    """Dialect-agnostic upsert for AgentTaskCommentCursor.

    Uses PostgreSQL/SQLite `INSERT ... ON CONFLICT DO UPDATE` — an atomic
    DB operation, no race condition for concurrent polls on the same
    (agent_id, task_id).
    """
    from app.models.agent_task_comment_cursor import AgentTaskCommentCursor as _Cursor

    dialect = session.bind.dialect.name if session.bind else "postgresql"
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:
        from sqlalchemy.dialects.postgresql import insert as _insert

    stmt = _insert(_Cursor.__table__).values(
        agent_id=agent_id,
        task_id=task_id,
        last_seen_comment_id=last_seen_comment_id,
    ).on_conflict_do_update(
        index_elements=["agent_id", "task_id"],
        set_={
            "last_seen_comment_id": last_seen_comment_id,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    await session.execute(stmt)


@router.get("/agent/me/poll")
async def agent_poll(
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Unified polling endpoint for Docker agents (replaces /me/next-task + /me/active-task-status).

    Returns one of four states:
    - cancelled: active task was set to failed → poll.sh sends ESC to claude
    - working: agent has in_progress or blocked task → poll.sh does nothing
    - new_task: claimed a new inbox task → poll.sh pastes prompt to tmux
    - idle: nothing to do

    Every response additionally includes `new_comments` (a list) — new user
    comments on active tasks that the agent hasn't seen yet. poll.sh pastes
    them as separate messages into the tmux session.
    """
    import datetime as dt
    from app.services.dispatch import build_agent_task_prompt

    new_comments = await _collect_and_ack_new_comments(agent, session)

    # 1. Failed tasks first — the agent must get ESC before anything else happens
    failed_result = await session.exec(
        select(Task)
        .where(Task.assigned_agent_id == agent.id)
        .where(Task.status == "failed")
        .order_by(Task.updated_at.desc())
        .limit(1)
    )
    failed = failed_result.first()
    if failed is not None:
        return {"state": "cancelled", "task_id": str(failed.id), "new_comments": new_comments}

    # 1b. Manually stopped tasks (run_control=stopped). Own state so that
    # poll.sh cleanly terminates the session (ESC + /clear + context reset)
    # without treating it as a failure. Resume later generates a fresh
    # dispatch_attempt_id + full prompt delivery via the inbox-claim path.
    stopped_result = await session.exec(
        select(Task)
        .where(Task.assigned_agent_id == agent.id)
        .where(Task.run_control == "stopped")
        .order_by(Task.updated_at.desc())
        .limit(1)
    )
    stopped = stopped_result.first()
    if stopped is not None:
        return {"state": "stopped", "task_id": str(stopped.id), "new_comments": new_comments}

    # 2. Phase-approval tasks take priority over the "working" bail-out, because the
    # parent deliberately stays in_progress until the Board Lead processes the approval task.
    # Without this check, the approval task would be unreachable.
    approval_result = await session.exec(
        select(Task)
        .where(Task.assigned_agent_id == agent.id)
        .where(Task.status == "inbox")
        .where(Task.delegation_type == "phase_approval")
        .order_by(Task.created_at.asc())
        .limit(1)
    )
    approval_task = approval_result.first()
    if approval_task is not None:
        task = approval_task
    else:
        # 3. Agent with an active task. Two cases:
        #    a) ack_at set → agent already got the prompt → `working`
        #    b) ack_at == NULL → task dispatched but prompt never delivered to the agent
        #       (happens with "direct" dispatch that skips inbox, or when
        #       recover-task resets the task to inbox and dispatch claims it
        #       again immediately before poll.sh sees it). In that case,
        #       fall through to prompt delivery below.
        # F1 fix (Plan 26-02 / HERM-10): after the split, the inbox path no
        # longer flips status — a task in status "inbox" with dispatched_at
        # set + ack_at NULL stays visible further below in the
        # inbox-candidates block (that's OK; the bridge cache dedupes on task_id).
        # Include `review` so review-handoffs to cli-bridge agents actually
        # reach them (ADR-022 review finding FB-1 — the old predicate only
        # covered inbox/in_progress/blocked, so Rex never saw tasks Rex
        # was supposed to review). Claim-semantics: an unacked review
        # task gets delivered once and keeps status=review (status only
        # flips when Rex explicitly PATCHes to in_progress or done).
        # Review fix B-2 (poll hardening): prefer the task the agent is
        # ACTUALLY working on (agent.current_task_id, set at claim/ACK time)
        # over the updated_at-desc heuristic. Without this, a freshly
        # unblocked old task (freshest updated_at, stale ack_at set) shadows
        # the task the real session is running — poll reports "working" on
        # the wrong task.
        active = None
        if agent.current_task_id is not None:
            _cur_result = await session.exec(
                select(Task)
                .where(Task.id == agent.current_task_id)
                .where(Task.assigned_agent_id == agent.id)
                .where(Task.status.in_(["in_progress", "blocked", "review"]))
                .limit(1)
            )
            active = _cur_result.first()
        if active is None:
            active_result = await session.exec(
                select(Task)
                .where(Task.assigned_agent_id == agent.id)
                .where(Task.status.in_(["in_progress", "blocked", "review"]))
                .order_by(Task.updated_at.desc())
                .limit(1)
            )
            active = active_result.first()

        # B1 (W2-B, live incident): a blocked task only parks the agent while
        # FRESH — grace window = board.blocker_triage_minutes (default 15min),
        # aligned with the lead-triage window (quick lead-unblocks resume
        # in-session with full context). Once the blocked transition is older
        # than the window, poll stops treating it as "working" and the agent
        # becomes claimable for new inbox work — otherwise a stale/zombie
        # blocked task parks the agent forever (a day-old blocked task held
        # Sparky parked while a freshly dispatched task was never offered).
        # in_progress/review tasks are NOT affected — they keep parking
        # unconditionally.
        # Review fix B-1: the age is keyed off task.blocked_at (dedicated
        # →blocked timestamp, maintained by the Task.status listener), NOT
        # updated_at — a generic onupdate column that ANY metadata PATCH
        # (title/priority/labels) resets, which would re-park the agent for
        # another full window indefinitely. updated_at remains only as the
        # fallback for legacy rows blocked before migration 0150.
        if active is not None and active.status == "blocked":
            from app.models.board import Board as _PollBoard

            grace_minutes = 15
            _board_id = active.board_id or agent.board_id
            if _board_id is not None:
                _board_row = await session.get(_PollBoard, _board_id)
                if _board_row is not None and _board_row.blocker_triage_minutes:
                    grace_minutes = _board_row.blocker_triage_minutes
            _blocked_since = active.blocked_at or active.updated_at
            if _blocked_since is not None:
                if _blocked_since.tzinfo is None:
                    _blocked_since = _blocked_since.replace(tzinfo=dt.timezone.utc)
                _age_seconds = (
                    dt.datetime.now(tz=dt.timezone.utc) - _blocked_since
                ).total_seconds()
                if _age_seconds >= grace_minutes * 60:
                    # Grace window expired — stop parking on this blocked
                    # task, fall through to inbox-claim below as if there
                    # were no active task.
                    active = None

        if active is not None:
            if active.ack_at is not None:
                return {"state": "working", "task_id": str(active.id), "new_comments": new_comments}
            # Prompt was never delivered — fall through and deliver it.
            task = active
        else:
            # 4. No active task — look for the next inbox task with satisfied
            # dependencies. The claim path below (`was_inbox → in_progress`)
            # bypasses dispatch.auto_dispatch_task, where dependencies_met()
            # normally applies — so we check explicitly here. Otherwise a
            # polling worker would blindly claim tasks whose predecessors
            # aren't done yet (bug from 2026-04-22).
            from app.services.dispatch import dependencies_met
            candidates_result = await session.exec(
                select(Task)
                .where(Task.assigned_agent_id == agent.id)
                .where(Task.status == "inbox")
                .order_by(Task.created_at.asc())
            )
            task = None
            for candidate in candidates_result.all():
                if await dependencies_met(session, candidate):
                    task = candidate
                    break

            # Runtime-readiness gate (power-managed backends, e.g. PORSCHE
            # unsloth). Don't inject a fresh inbox task into the session while
            # the agent's LLM backend is asleep — the task stays inbox (parked)
            # until the box is woken + the model is serving. Only affects agents
            # bound to a power_managed runtime; every other agent passes through
            # (fail-open). Only the fresh-inbox path is gated — recovery and
            # phase_approval claims above are untouched. See runtime_readiness.py.
            if task is not None:
                from app.services.runtime_readiness import runtime_ready_for_agent
                _rt_ready, _rt_reason = await runtime_ready_for_agent(agent, session)
                if not _rt_ready:
                    return {
                        "state": "idle",
                        "runtime_not_ready": True,
                        "detail": _rt_reason,
                        "new_comments": new_comments,
                    }

    if task is None:
        return {"state": "idle", "new_comments": new_comments}

    # 3. Claim the task. Two paths:
    #    - inbox: only set dispatched_at (if still None) — status stays
    #      "inbox", ack_at stays NULL. The agent must explicitly send the
    #      ACK via PATCH status:in_progress (= Migration 0018
    #      handshake contract).
    #    - already in_progress / blocked / review (direct-dispatch / recovery):
    #      set ack_at so the recovery branch above (active.ack_at != None)
    #      kicks in on the next poll and no re-deliver happens.
    #
    # F1 fix (Plan 26-02 / HERM-10): status no longer flips on poll-claim.
    # Previously (agents.py:2946 old) status="in_progress" + ack_at=now were
    # set in the same atomic write — the LLM session hadn't seen the prompt
    # yet at that point. Status now stays "inbox" until the agent itself
    # does the PATCH (tasks.py:1239-1241 sets started_at + ack_at there).
    #
    # F3 fix (Plan 26-02 / HERM-10): dispatched_at and ack_at can no longer
    # both be set to the same `now` literal value, because they moved into
    # two separate write paths: dispatched_at = poll (here), ack_at = the
    # agent's own PATCH (tasks.py / agent_scoped.py). This guarantees a
    # measurable span `dispatched_at < ack_at`.
    #
    # Duplicate-dispatch concern: as long as the agent hasn't ACKed, poll
    # returns state=new_task again on every call. The bridge (hermes-bridge.py
    # _last_dispatched_task_id, docker poll.sh LAST_DISPATCHED_TASK_ID) already
    # dedupes via task_id cache, so no re-paste into tmux. Plan 26-05
    # hardens the bridge further.
    now = dt.datetime.now(tz=dt.timezone.utc)
    was_inbox = task.status == "inbox"
    if was_inbox:
        # F1 fix (Plan 26-02): only set dispatched_at on first delivery.
        # Status stays "inbox" — flips to in_progress only when the agent
        # explicitly PATCHes status:in_progress (= true ACK per Migration 0018).
        # ack_at is set in that PATCH path, NOT here, so dispatched_at < ack_at
        # is guaranteed (F3 fix).
        if task.dispatched_at is None:
            task.dispatched_at = now
        # NOTE: do NOT set task.ack_at here. Do NOT set task.status here.
    else:
        # in_progress / review / blocked recovery (already-acked task path).
        # Setting ack_at here is the historical "re-deliver prompt" recovery
        # signal — preserved unchanged so the recovery-branch above
        # (active.ack_at != None) catches the next poll.
        task.ack_at = now
    # Set the active-task lock — analogous to comment auto-ACK in
    # agent_scoped.py:3788 and PATCH-ACK in agent_scoped.py:1294.
    # Without this, agent.current_task_id stays None and mc delegate /
    # mc help-request / mc clarification respond with 409 "Kein aktiver Task"
    # for agents that work via push dispatch and want to delegate BEFORE
    # the first comment (live bug Boss 2026-04-25: 6-minute loop on the
    # weather task before the first comment, multiple mc delegate 409s). Same
    # skip condition: workers in subagent mode have parallel sessions
    # and don't need the lock.
    from app.config import settings as _poll_ack_settings
    if not (_poll_ack_settings.use_subagent_dispatch and not agent.is_board_lead):
        if agent.current_task_id != task.id:
            agent.current_task_id = task.id
            session.add(agent)
    # Without dispatch_attempt_id, the next `mc ack`/`mc done` would be
    # rejected with HTTP 409 (enforce_dispatch_attempt_id=True). The direct
    # poll path does NOT call auto_dispatch_task() → we generate the
    # attempt_id here ourselves if none exists yet.
    #
    # Race fix (2026-05-15): conditional UPDATE … WHERE attempt_id IS NULL
    # via set_dispatch_attempt_id(only_if_null=True). Prevents /me/poll
    # and auto_dispatch_task (BackgroundTask) from overwriting each other
    # during a git-clone window. Plus a permanent audit entry.
    session.add(task)
    await session.commit()
    await session.refresh(task)
    from app.services.dispatch_attempt_audit import set_dispatch_attempt_id
    await set_dispatch_attempt_id(
        session, task, str(uuid.uuid4()),
        caller="agent_poll", reason="claim_inbox_task",
        only_if_null=True,
    )

    try:
        prompt = await build_agent_task_prompt(task=task, agent=agent, session=session)
    except Exception as e:
        # Revert on prompt generation failure. Only revert what we just
        # set — otherwise we'd lose the original dispatched_at on
        # direct-dispatch tasks.
        # F1 fix (Plan 26-02): we no longer set status in the inbox path, so
        # no status revert needed here either. Only reset dispatched_at if
        # WE set it for the first time in this call.
        if was_inbox and task.dispatched_at == now:
            task.dispatched_at = None
        else:
            # Recovery path (was_inbox=False): we only set ack_at.
            task.ack_at = None
        # Release the active-task lock again, otherwise it blocks the next poll
        if agent.current_task_id == task.id:
            agent.current_task_id = None
            session.add(agent)
        session.add(task)
        await session.commit()
        raise HTTPException(status_code=500, detail=f"Prompt generation failed: {str(e)}")

    return {
        "state": "new_task",
        "task": {
            "id": str(task.id),
            "title": task.title,
            # NEW (Plan 26-02 / Task 2): expose status explicitly so consumers
            # can read the truthful value. After F1 fix, status="inbox" until
            # the agent's own PATCH sets it to in_progress. Consumers must
            # trust `state` for delivery semantics, `status` for lifecycle.
            "status": task.status,
            # F3 fix (Plan 26-02): expose dispatched_at + ack_at so downstream
            # consumers (bridge, tests) can observe the spread.
            "dispatched_at": task.dispatched_at.isoformat() if task.dispatched_at else None,
            "ack_at": task.ack_at.isoformat() if task.ack_at else None,
            "board_id": str(task.board_id) if task.board_id else None,
            "workspace_path": task.workspace_path,
            "prompt": prompt,
            "slug": getattr(task, "slug", None),
            # Without this value in the response, poll.sh can't write the
            # header to /tmp/mc-context.env and `mc ack` fails with HTTP
            # 409 "Fehlender X-Dispatch-Attempt-Id" (ADR-023 ultrareview).
            "dispatch_attempt_id": task.dispatch_attempt_id,
        },
        "new_comments": new_comments,
    }


@router.get("/agent/me/active-task-recovery")
async def agent_active_task_recovery(
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Agent-initiated recovery (ADR-024): read-only, returns the current
    task prompt + recovery context and generates a fresh
    `dispatch_attempt_id`. **Mutates NO status** — unlike the old
    POST /recover-task which reset the task to inbox (→ dispatch loop
    on crash loops, silent context loss).

    Called by:
    - `mc recover` CLI (agent startup hook, SOUL core rule)
    - poll.sh FIRST_POLL (fallback if the agent itself isn't running `mc recover`)

    Returns:
      - {active: false} if there's no active task
      - {active: true, task: {... prompt, dispatch_attempt_id, ...}}
    """
    from app.services.dispatch import build_agent_task_prompt
    import datetime as _dt

    active_result = await session.exec(
        select(Task)
        .where(Task.assigned_agent_id == agent.id)
        .where(Task.status.in_(["in_progress", "blocked", "review"]))  # type: ignore[union-attr]
        .order_by(Task.updated_at.desc())  # type: ignore[union-attr]
        .limit(1)
    )
    active = active_result.first()
    if active is None:
        return {"active": False, "reason": "no_active_task"}

    # Rate limit: max 1 recovery per task every 30s (Redis TTL key).
    # Protection against poll.sh crash loops + an agent calling `mc recover`
    # in a loop. Backend logs warnings but still serves the cached prompt.
    from app.redis_client import get_redis
    redis = await get_redis()
    cache_key = f"mc:recovery:attempt_id:{active.id}"
    try:
        cached_attempt = await redis.get(cache_key)
    except Exception:
        cached_attempt = None

    if cached_attempt:
        # Reuse the last attempt_id instead of generating a new one — prevents
        # the agent from invalidating its own previous headers.
        active.dispatch_attempt_id = cached_attempt.decode() if isinstance(cached_attempt, bytes) else cached_attempt
    elif active.dispatch_attempt_id:
        # Race fix (2026-05-12): if the task already has an attempt_id
        # (assigned via /me/poll or auto_dispatch_task), don't overwrite it.
        # Otherwise poll.sh would see a different attempt_id on the next poll
        # and re-paste the task. We reuse the existing ID and also cache it
        # in the Redis slot so subsequent recovery calls within the 30s TTL
        # stay consistent.
        try:
            await redis.setex(cache_key, 30, active.dispatch_attempt_id)
        except Exception:
            pass
    else:
        active.dispatch_attempt_id = str(uuid.uuid4())
        try:
            await redis.setex(cache_key, 30, active.dispatch_attempt_id)
        except Exception:
            pass

    active.updated_at = _dt.datetime.now(tz=_dt.timezone.utc)
    session.add(active)
    await session.commit()
    await session.refresh(active)

    try:
        prompt = await build_agent_task_prompt(task=active, agent=agent, session=session)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prompt generation failed: {e}")

    await emit_event(
        session,
        "task.agent_recovery",
        f"{agent.name} Recovery: '{active.title[:60]}'",
        severity="info",
        board_id=active.board_id,
        task_id=active.id,
        agent_id=agent.id,
        detail={"trigger": "agent_initiated_or_poll_first", "cached": bool(cached_attempt)},
    )

    return {
        "active": True,
        "task": {
            "id": str(active.id),
            "title": active.title,
            "status": active.status,
            "board_id": str(active.board_id) if active.board_id else None,
            "workspace_path": active.workspace_path,
            "prompt": prompt,
            "slug": getattr(active, "slug", None),
            "dispatch_attempt_id": active.dispatch_attempt_id,
        },
    }


@router.post("/agent/me/recover-task")
async def agent_recover_task(
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """DEPRECATED (ADR-024): mutates task status → inbox, which can lead to
    dispatch loops on crash loops. New way: `GET /agent/me/active-task-recovery`
    (read-only, generates a fresh attempt_id, no status change).

    Older recovery endpoint for poll runtimes (cli-bridge, host) after a restart.
    poll.sh now calls the new GET endpoint. This POST remains only for
    backward compat and will be removed in the future.

    If an agent container/launchd job restarts while a task is 'in_progress',
    the tmux/claude context is gone, but the backend status stays
    'in_progress' → /agent/me/poll only returns `state=working` without a
    prompt. poll.sh calls this endpoint on the first startup poll, which
    resets the task to 'inbox'. The next poll cycle delivers it as
    `new_task` with the full prompt.
    """
    from app.models.task import TaskComment as _TC, TaskEvent
    import datetime as _dt

    active_result = await session.exec(
        select(Task)
        .where(Task.assigned_agent_id == agent.id)
        .where(Task.status.in_(["in_progress", "blocked"]))  # type: ignore[union-attr]
        .order_by(Task.updated_at.desc())  # type: ignore[union-attr]
        .limit(1)
    )
    active = active_result.first()

    if active is None:
        return {"recovered": False, "reason": "no_active_task"}

    # Rate limit: recovery may be triggered no more than 1x/60s per task.
    # Protection against poll.sh crash loops (FIRST_POLL=true on every restart).
    cooldown_cutoff = _dt.datetime.now(tz=_dt.timezone.utc) - _dt.timedelta(seconds=60)
    recent_recovery = (await session.exec(
        select(TaskEvent)
        .where(TaskEvent.task_id == active.id)
        .where(TaskEvent.reason == "agent_restart_recovery")
        .where(TaskEvent.created_at > cooldown_cutoff)  # type: ignore[union-attr]
        .limit(1)
    )).first()
    if recent_recovery is not None:
        logger.warning(
            "Recovery rate-limited for task %s (agent %s) — last recovery at %s",
            active.id, agent.name, recent_recovery.created_at,
        )
        return {
            "recovered": False,
            "reason": "rate_limited",
            "task_id": str(active.id),
            "last_recovery_at": recent_recovery.created_at.isoformat(),
        }

    old_status = active.status
    active.status = "inbox"
    active.dispatched_at = None
    active.ack_at = None
    active.started_at = None
    # Reset run_control: otherwise an old 'stopped' flag lingers and
    # later blocks the status transition (-> deadlock in the agent).
    active.run_control = None
    active.updated_at = utcnow()
    session.add(active)

    # Recovery system comment (audit trail)
    recovery_comment = _TC(
        task_id=active.id,
        author_type="agent",
        author_agent_id=agent.id,
        content=(
            f"**Recovery triggered** — {agent.name} hat beim Startup einen "
            f"`in_progress` Task ohne Kontext gefunden (Container-/Host-Restart "
            f"oder claude-Crash). Task wird re-dispatched, naechster Poll "
            f"liefert den Prompt neu."
        ),
        comment_type="system",
    )
    session.add(recovery_comment)
    await session.commit()
    await session.refresh(active)

    from app.services.task_lifecycle import record_task_event
    await record_task_event(
        session, active.id, old_status, "inbox",
        changed_by="agent", agent_id=agent.id, reason="agent_restart_recovery",
    )

    await emit_event(
        session, "task.recovery_triggered",
        f"{agent.emoji or '🤖'} {agent.name}: Task '{active.title}' re-dispatched nach Agent-Restart",
        board_id=active.board_id, task_id=active.id, agent_id=agent.id,
        severity="warning",
        detail={"reason": "agent_restart_recovery", "previous_status": old_status},
    )

    return {
        "recovered": True,
        "task_id": str(active.id),
        "task_title": active.title,
        "previous_status": old_status,
    }


@router.post("/agent/me/heartbeat")
async def agent_heartbeat(
    payload: AgentHeartbeatPayload,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Agent signals it's alive. Updates last_seen_at, run_state AND status.

    For non-gateway agents (cli-bridge, host), this is the only way to set
    status to idle/working — session_monitor doesn't apply to them.

    Bug 2 self-heal (2026-05-13): the DB fields `status`, `current_task_id`,
    `last_task_activity_at` regularly drift from the task-table state
    (live bug: Sparky had an `in_progress` task assigned, claude was cooking
    internally for 12 minutes, poll.sh sent `status: idle` because it had no
    NEW task → the agent appeared "idle" to the operator even though it was
    working, Boss/operator could carelessly dispatch it a 2nd task). Fix:
    heartbeat checks the task table for an active assigned task (status
    `in_progress`). If yes → status stays `working`, current_task_id gets
    synced to the task, last_task_activity_at gets stamped. This way the
    agent row converges to the truth by the next heartbeat at the latest.
    `blocked`/`review`/`done`/`failed` are NOT "actively working" — for
    those, the payload status is respected.
    """
    import datetime

    from app.models.task import Task as _Task

    agent.last_seen_at = datetime.datetime.now(tz=datetime.timezone.utc)

    # The task table is the source of truth. We read it here once and
    # derive from it whether the agent is "really" working — independent
    # of what poll.sh reports.
    active_res = await session.exec(
        select(_Task).where(
            _Task.assigned_agent_id == agent.id,
            _Task.status == "in_progress",
        ).limit(1)
    )
    active_task = active_res.first()

    if active_task is not None:
        # current_task_id lock self-heal: derive from the DB independent of
        # the payload. This fixes the original bug 2 (Sparky.current_task_id=
        # None even though a task was assigned) without masking agent state.
        agent.current_task_id = active_task.id
        # status / run_state / last_task_activity_at follow the payload
        # (bug 2 refined 2026-05-13). poll.sh is responsible for sending
        # "working" when claude is really active (bug 13). If poll.sh
        # reports idle on an active task → the operator sees status=idle +
        # current_task_id set in the UI = a clear signal "task assigned but
        # agent not active, what's going on?".
        if payload.status == "working":
            agent.status = "working"
            agent.run_state = "running"
            agent.last_task_activity_at = agent.last_seen_at
        else:
            agent.run_state = "running" if payload.status == "working" else "idle"
            if payload.status in ("idle", "working", "online"):
                agent.status = payload.status
    else:
        # No active task in the task table.
        # Bug 18 fix (2026-05-14): if poll.sh reports "working" but the
        # backend finds no in_progress assigned task → stale state.
        # Example: claude still renders a memory-save in the pane after task
        # done → detect_turn_state says "working" → heartbeat sends working →
        # WITHOUT this fix, agent.status="working" stays forever (seen with
        # Sparky 2026-05-14: status=working + current_task_id=None for
        # hours). Self-heal: force working-without-task → idle.
        if payload.status == "working":
            logger.warning(
                "Bug 18 self-heal: agent %s heartbeated 'working' but no "
                "in_progress task assigned — coercing status to 'idle'",
                agent.name,
            )
            agent.status = "idle"
            agent.run_state = "idle"
        else:
            agent.run_state = "running" if payload.status == "working" else "idle"
            if payload.status in ("idle", "working", "online"):
                agent.status = payload.status
        # Explicitly clear current_task_id — prevents an old pointer from
        # lingering after task done/failed (Sparky symptom 2026-05-14).
        if agent.current_task_id is not None:
            agent.current_task_id = None

    # CTX-01 (Phase 6): Docker self-report context-window usage. Inverts the
    # display formula at line 166 so frontend bars stay accurate.
    if payload.context_pct is not None and agent.context_max:
        agent.context_tokens = round(payload.context_pct / 100 * agent.context_max)

    # Host agents: flip provision_status "provisioning" -> "provisioned" on
    # the first heartbeat that ever arrives (2026-07-10 E2E Lauf 3). The
    # generic host provisioning chain (host_provisioning.py) stages files +
    # starts a poller, but with launchd autoload disabled (the normal case,
    # see /provision's `agent.provision_status = "provisioned" if
    # load["loaded"] else "provisioning"`) nothing else ever transitions it
    # once staging leaves it on "provisioning" — a real, heartbeating agent
    # stayed permanently "not ready" in /health-check. cli-bridge agents are
    # untouched: their own provisioning flow (services/provisioning.py)
    # already flips to "provisioned" once the container starts, independent
    # of any heartbeat — this only ever fires for agent_runtime == "host".
    just_provisioned = agent.agent_runtime == "host" and agent.provision_status == "provisioning"
    if just_provisioned:
        agent.provision_status = "provisioned"
        agent.provisioned_at = agent.last_seen_at

    session.add(agent)
    await session.commit()

    if just_provisioned:
        await emit_event(
            session,
            "agent.provisioned",
            f"{agent.name} (host) provisioned — first heartbeat received",
            severity="info",
            agent_id=agent.id,
            board_id=agent.board_id,
        )

    return {"ok": True, "agent": agent.name}
