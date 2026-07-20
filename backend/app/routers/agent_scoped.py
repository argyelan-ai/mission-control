"""Agent-scoped router — Phase 4 split (post-04-08 finalisation).

Originally a 6189-line monolith of agent-authenticated endpoints. Phase 4
(REF-02 across Plans 04-04 → 04-08) split it into:

  - routers/agent_task_status.py  — PATCH /tasks/{id} state-machine + 11
                                     status-related task endpoints
                                     (~2377 lines)
  - routers/agent_comments.py     — POST/GET /tasks/{id}/comments +
                                     reflection→memory pipeline
                                     (~465 lines)
  - routers/agent_git.py          — git/gh side-effect handler library
                                     (NO @router.* — called from
                                     agent_task_status PATCH body, ~207 lines)
  - services/work_context.py      — extracted board-rule + reviewer-finder
                                     validators

This file STAYS as:
  1. Aggregator-by-prefix: declares `router = APIRouter(prefix="/api/v1/agent")`
     and owns the ~37 misc endpoints that have NOT been split out yet
     (heartbeat, /me/*, /agents CRUD, /knowledge, /deliverables, /chat,
      delegate, help-request, clarification, projects, etc.).
  2. Re-export shim — Pattern S1: preserves historical import paths for
     names that test files + sibling modules import via
     `from app.routers.agent_scoped import …`. Specifically:
       • `_find_reviewer`, `_find_last_developer`,
         `_enforce_board_rules_agent`, `enforce_reflection`,
         `VALID_BLOCKER_TYPES` (REF-02 step 1, Plan 04-04)
       • `AgentCommentCreate`, `_extract_reflection_lesson`,
         `_post_subtask_blocker_comment`, `_post_subtask_completion_comment`
         (REF-02 step 3, Plan 04-06)
       • `AgentTaskCreate`, `AgentTaskUpdate`, `ReviewDecisionBody`,
         `ReportBackUpdate`, `CheckpointCreate`,
         `_handle_help_request_resume`, `_handle_callback_resume`,
         `_handle_phase_completion_push`, `dispatch_callback_to_parent`,
         `dispatch_resume_to_agent` (REF-02 step 4, Plan 04-07)
       • `VALID_COMMENT_TYPES` (alias of `app.comment_types.ALL_COMMENT_TYPES`)
       • `handle_review_pr_creation`, `handle_done_pr_merge`,
         `handle_worktree_cleanup` (REF-02 step 2, Plan 04-05 — kept as
         shim though the PATCH caller now lives in agent_task_status.py)

Mount strategy (Pattern 2 — sibling mounts in main.py):
  Plans 04-06 + 04-07 mount `agent_comments.router` and
  `agent_task_status.router` directly in `main.py` alongside
  `agent_scoped.router`. Pattern 1 (`router.include_router(...)` inside
  agent_scoped) was rejected because both child routers carry the
  `/api/v1/agent` prefix → would double-prefix to `/api/v1/agent/api/v1/agent/…`.

Module-size note (Phase 4 A2 auto-resolution):
  This file may stay > 1500 lines (currently ~3271) — accepted in v0.5
  because the misc endpoints inherently want to live together at the
  /api/v1/agent prefix. Further sub-router split (e.g. agent_me.py,
  agent_knowledge.py, agent_delegate.py) is a Phase 5 follow-up.
  agent_task_status.py likewise overflows (~2377 lines) — same A2 logic
  applied; further split into agent_task_create.py + agent_task_review.py
  deferred to Phase 5. Test `test_no_agent_router_over_1500_lines`
  whitelists both files explicitly.

Auth: Bearer <agent-token> via `app.auth.require_agent` +
      scope-gated via `app.scopes.require_scope(Scope.X)` per endpoint.
"""

import logging
import os
import re
import uuid
from datetime import datetime
from typing import Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status

logger = logging.getLogger("mc.agent_scoped")
from pydantic import BaseModel, field_validator
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, or_, and_

from app.auth import require_agent
from app.scopes import Scope, require_scope
from app.database import get_session
from app.models.agent import Agent
from app.models.agent_template import AgentTemplate
from app.models.approval import Approval
from app.models.board import Board, Project
from app.models.chat import ChatMessage
from app.models.memory import BoardMemory
from app.models.task import Task, TaskComment
from app.services.activity import emit_event
from app.utils import utcnow

# ─────────────────────────────────────────────────────────────────────
# REF-02 Step 1 Re-Export Shim (Pattern S1 — Phase 4 Plan 04-04)
# Validators / constant live in app.services.work_context now. The
# underscored aliases below MUST exist on this module because:
#   - task_lifecycle.py:707  imports `_find_reviewer`
#   - task_lifecycle.py:885  imports `_find_last_developer`
#   - test_review_decision.py / test_busy_check.py /
#     test_workflow_scenarios.py / test_reassignment_timestamps.py /
#     test_agent_roles.py     mock.patch the underscored names via
#                             `app.routers.agent_scoped.<name>`.
# `enforce_reflection` is re-exported without underscore — it is new
# (extracted from the inline block) and has no historical callers.
# `VALID_BLOCKER_TYPES` is re-exported because the giant PATCH endpoint
# below still references the bare name.
# ─────────────────────────────────────────────────────────────────────
from app.services.work_context import (  # noqa: F401,E402
    enforce_board_rules_agent as _enforce_board_rules_agent,
    enforce_reflection,
    find_reviewer as _find_reviewer,
    find_last_developer as _find_last_developer,
    VALID_BLOCKER_TYPES,
)

# REF-02 Step 2 (Plan 04-05): git side-effect handlers extracted into
# routers/agent_git.py (handler library, no @router). Leaf-of-graph — safe.
from app.routers.agent_git import (  # noqa: E402
    handle_review_pr_creation,
    handle_done_pr_merge,
    handle_worktree_cleanup,
)


router = APIRouter(prefix="/api/v1/agent", tags=["agent-scoped"])


class HeartbeatPayload(BaseModel):
    context_tokens: int | None = None
    session_message_count: int | None = None
    current_task_id: uuid.UUID | None = None
    status: str | None = None
    model_id: str | None = None  # Theme 4: Model Usage Tracking


class AgentProjectCreate(BaseModel):
    name: str
    description: str | None = None
    project_type: str = "feature"  # feature|website|content|research|automation|design|free
    priority: str = "medium"


class HelpRequestCreate(BaseModel):
    needed_role: str
    title: str
    context: str
    priority: str | None = None


class HelpRequestResponse(BaseModel):
    help_task_id: uuid.UUID
    assigned_to: str
    your_status: str


class DelegateCreate(BaseModel):
    title: str
    description: str
    assigned_agent_id: uuid.UUID
    priority: Literal["low", "medium", "high", "critical"] | None = None
    callback: bool = True  # True = Parent wartet auf Callback; False = Fire-and-Forget


class DelegateResponse(BaseModel):
    subtask_id: uuid.UUID
    assigned_to: str
    your_status: str  # "blocked" if callback=True, otherwise "in_progress"


class ClarificationCreate(BaseModel):
    question: str
    options: list[str] | None = None


class ClarificationResponse(BaseModel):
    approval_id: uuid.UUID
    your_status: str


# ─────────────────────────────────────────────────────────────────────────
# Boss Spawn-Approval + Plugin-Self-Service (Phase 2, 2026-04-11)
# ─────────────────────────────────────────────────────────────────────────


class SpawnAgentRequest(BaseModel):
    """Boss asks the operator whether it may spawn a new CLI agent."""
    name: str
    role: str
    reason: str
    ephemeral: bool = True
    scopes: list[str] | None = None
    skill_filter: list[str] | None = None
    cli_plugins: list[str] | None = None
    soul_md: str | None = None
    model: str | None = None
    template_id: uuid.UUID | None = None  # optional: spawn aus Template


class SpawnApprovalResponse(BaseModel):
    approval_id: uuid.UUID
    status: str


class PluginUpdateRequest(BaseModel):
    cli_plugins: list[str] | None  # None = all, [] = none, [...] = allowlist
    restart_worker: bool = False   # True → reload worker session after disk sync
                                   # (claude/openclaude only reads settings.json
                                   # on start — without a restart, new plugins
                                   # only activate after the next container
                                   # restart or /clear). Default false so
                                   # Boss consciously decides whether the
                                   # current task context may be lost.


# VALID_BLOCKER_TYPES is now imported at the top of this module from
# app.services.work_context (Phase 4 REF-02 Plan 04-04). Single source of truth.

# Single source of truth: app/comment_types.py (REL-01). The alias preserves
# the historical import name `VALID_COMMENT_TYPES` for existing tests
# (test_phase_approval.py etc.). Anyone needing a new comment_type should
# → edit app/comment_types.py, NOT here.
from app.comment_types import ALL_COMMENT_TYPES as VALID_COMMENT_TYPES  # noqa: E402

# ─────────────────────────────────────────────────────────────────────
# REF-02 Step 3 Re-Export Shim (Pattern S1 — Phase 4 Plan 04-06)
# Comment endpoints + reflection pipeline + helpers moved to
# routers/agent_comments.py. Tests + sibling code import the
# underscored helpers + AgentCommentCreate from this module path —
# preserve those names via re-export.
#
# Callers preserved by this shim:
#   - agent_scoped.py PATCH agent_update_task body — calls
#     `_post_subtask_blocker_comment` and `_post_subtask_completion_comment`
#     (bare names, resolved at call time)
#   - tests / sibling modules importing `AgentCommentCreate` or
#     `_extract_reflection_lesson` from `app.routers.agent_scoped`
#     (Pitfall B: Pydantic models must stay re-exportable)
# ─────────────────────────────────────────────────────────────────────
from app.routers.agent_comments import (  # noqa: F401,E402
    AgentCommentCreate,
    _extract_reflection_lesson,
    _post_subtask_blocker_comment,
    _post_subtask_completion_comment,
)

# ─────────────────────────────────────────────────────────────────────
# REF-02 Step 4 Re-Export Shim (Pattern S1 — Phase 4 Plan 04-07)
# 12 status-transition endpoints + 5 cross-task helpers + 5 Pydantic
# models moved to routers/agent_task_status.py. Tests + sibling code
# import these names from `app.routers.agent_scoped` — preserve them.
#
# Callers preserved by this shim:
#   - test_dispatch_gating.py:537 → AgentTaskUpdate
#   - test_help_request_resume.py → _handle_help_request_resume +
#     dispatch_resume_to_agent (mock.patch target)
#   - test_delegate_endpoint.py → _handle_callback_resume +
#     dispatch_callback_to_parent
#   - test_phase_approval.py → _handle_phase_completion_push
#   - test_checkpoint.py → CheckpointCreate
# ─────────────────────────────────────────────────────────────────────
from app.routers.agent_task_status import (  # noqa: F401,E402
    # Pydantic models:
    AgentTaskCreate,
    AgentTaskUpdate,
    ReviewDecisionBody,
    ReportBackUpdate,
    CheckpointCreate,
    # Cross-task helpers (underscored — preserve names tests use):
    _handle_help_request_resume,
    _handle_callback_resume,
    _handle_phase_completion_push,
    dispatch_callback_to_parent,
    dispatch_resume_to_agent,
)

# Note: agent_task_status.router is mounted directly in main.py (line ~492)
# alongside agent_comments.router. Plan 04-08 finalises the main.py mount
# strategy. Mounting via router.include_router() here would double the
# `/api/v1/agent` prefix because both routers carry it.


class MemoryCreate(BaseModel):
    content: str
    title: str | None = None
    tags: list[str] = []
    memory_type: str = "knowledge"
    is_pinned: bool = False


class ApprovalCreate(BaseModel):
    action_type: str
    description: str
    task_id: uuid.UUID | None = None
    payload: dict | None = None
    confidence: float | None = None


class ChatMessageCreate(BaseModel):
    content: str


@router.post("/heartbeat")
async def agent_heartbeat(
    payload: HeartbeatPayload,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.HEARTBEAT)),
):
    old_status = agent.status

    if payload.context_tokens is not None:
        agent.context_tokens = payload.context_tokens
    if payload.session_message_count is not None:
        agent.session_message_count = payload.session_message_count
    if payload.current_task_id is not None:
        # Workers with isolated sessions: don't set current_task_id via heartbeat
        from app.config import settings as _hb_settings
        if not (_hb_settings.use_subagent_dispatch and not agent.is_board_lead):
            agent.current_task_id = payload.current_task_id
    if payload.status is not None:
        agent.status = payload.status

    # Model Usage Tracking V1 (Theme 4: Wave 2)
    # Stores only the active model as a snapshot — no cumulative counter.
    if payload.model_id:
        try:
            from app.redis_client import get_redis as _get_redis
            _redis = await _get_redis()
            await _redis.set(
                f"mc:agent:{agent.id}:heartbeat_model",
                payload.model_id,
                ex=900,  # 15min TTL — expires when the agent is offline
            )
        except Exception as e:
            logger.warning("Heartbeat model_id save failed for %s: %s", agent.name, e)

    agent.last_seen_at = utcnow()
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()

    # Agent comes back after a restart
    if old_status == "restarting" and agent.status != "restarting":
        await emit_event(
            session,
            "agent.status_changed",
            f"{agent.emoji or '🤖'} {agent.name} ist wieder online nach Neustart",
            agent_id=agent.id,
            board_id=agent.board_id,
            detail={"reason": "restart_completed", "old_status": "restarting", "new_status": agent.status},
        )

    # Warn at 70%+ context
    if agent.context_max and agent.context_tokens >= agent.context_max * 0.9:
        await emit_event(
            session,
            "agent.context_warning",
            f"{agent.name}: Context at {round(agent.context_tokens/agent.context_max*100)}%",
            severity="error",
            agent_id=agent.id,
            board_id=agent.board_id,
        )
    elif agent.context_max and agent.context_tokens >= agent.context_max * 0.7:
        await emit_event(
            session,
            "agent.context_warning",
            f"{agent.name}: Context at {round(agent.context_tokens/agent.context_max*100)}%",
            severity="warning",
            agent_id=agent.id,
            board_id=agent.board_id,
        )

    return {"status": "ok", "agent_id": str(agent.id)}


class AgentMemoryUpdate(BaseModel):
    content: str


class AgentSoulUpdate(BaseModel):
    content: str
    reason: str | None = None  # Why the change? (for the activity log)


@router.put("/config/soul_md")
async def agent_update_own_soul(
    payload: AgentSoulUpdate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """Agent updates its own SOUL.md in the MC DB + gateway/disk.

    Only for agents with agents:manage scope (Board Leads).
    The change is logged as an activity event so the operator sees it.
    """
    old_length = len(agent.soul_md or "")
    agent.soul_md = payload.content
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()

    # Gateway sync removed (Phase 29). Disk persistence (cli-bridge / host)
    # is the sole source of truth; the openclaw runtime is no longer supported.
    gateway_synced = False

    # Disk sync — depends on runtime:
    #   cli-bridge: write to ~/.mc/agents/<slug>/claude-config/SOUL.md
    #               (docker container reads it via volume-mount + start-claude.sh)
    #   host:       write to agent.workspace_path/claude-config/SOUL.md
    #               (Boss + Hermes — host process reads it natively)
    disk_synced = False
    if agent.agent_runtime == "cli-bridge":
        try:
            import os
            from pathlib import Path
            home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
            slug = agent.name.lower().replace(" ", "-")
            soul_path = Path(home) / ".mc" / "agents" / slug / "claude-config" / "SOUL.md"
            if soul_path.parent.exists():
                soul_path.write_text(payload.content, encoding="utf-8")
                disk_synced = True
        except Exception as e:
            logger.warning("SOUL.md Disk-Sync fehlgeschlagen fuer %s: %s", agent.name, e)
    elif agent.agent_runtime == "host" and agent.workspace_path:
        try:
            from pathlib import Path
            soul_path = Path(agent.workspace_path) / "claude-config" / "SOUL.md"
            soul_path.parent.mkdir(parents=True, exist_ok=True)
            soul_path.write_text(payload.content, encoding="utf-8")
            disk_synced = True
        except Exception as e:
            logger.warning("SOUL.md Host-Disk-Sync fehlgeschlagen fuer %s: %s", agent.name, e)

    # Activity event (the operator sees the change)
    await emit_event(
        session,
        "agent.soul_updated",
        f"{agent.name} hat eigenes SOUL.md aktualisiert"
        + (f": {payload.reason}" if payload.reason else ""),
        agent_id=agent.id,
        board_id=agent.board_id,
        detail={
            "reason": payload.reason,
            "old_length": old_length,
            "new_length": len(payload.content),
            "gateway_synced": gateway_synced,
            "disk_synced": disk_synced,
        },
    )

    logger.info(
        "SOUL.md self-update: %s (%d→%d chars) reason=%s gw=%s disk=%s",
        agent.name, old_length, len(payload.content),
        payload.reason, gateway_synced, disk_synced,
    )
    return {
        "status": "updated",
        "gateway_synced": gateway_synced,
        "disk_synced": disk_synced,
    }


@router.get("/config/soul_md")
async def agent_get_own_soul(
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """Agent reads its own SOUL.md."""
    return {"content": agent.soul_md or ""}


@router.patch("/me/memory")
async def agent_update_memory(
    payload: AgentMemoryUpdate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.MEMORY_WRITE)),
):
    """Agent updates its own MEMORY.md in the MC DB + gateway."""
    agent.memory_md = payload.content
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()

    # Gateway sync removed (Phase 29). MEMORY.md lives only in the DB and
    # is optionally rendered into the container workspace via sync-config.

    return {"status": "updated"}


@router.get("/me")
async def agent_get_me(
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_agent),
):
    """Self-lookup — agent retrieves its own info.

    Convenience endpoint for workers that need to orient themselves: "who am
    I, what role, what tools do I have, what task is currently running?".
    Agents used to try trial-and-error (GET /agent/agents/{id} → 404) — this
    is the canonical way.

    No scope requirement — any authenticated agent may look up itself.
    """
    # Current task summary (if present)
    current_task = None
    if agent.current_task_id:
        task = await session.get(Task, agent.current_task_id)
        if task:
            current_task = {
                "id": str(task.id),
                "title": task.title,
                "status": task.status,
                "board_id": str(task.board_id) if task.board_id else None,
            }

    return {
        "id": str(agent.id),
        "name": agent.name,
        "emoji": agent.emoji,
        "role": agent.role,
        "is_board_lead": bool(agent.is_board_lead),
        "board_id": str(agent.board_id) if agent.board_id else None,
        "agent_runtime": agent.agent_runtime,
        "model": agent.model,
        "scopes": agent.scopes or [],  # [] = alle Scopes (backward compat)
        "cli_skills": agent.cli_skills,  # None = alle, [] = keine, [...] = Allowlist
        "cli_plugins": agent.cli_plugins,
        "skill_filter": agent.skill_filter,
        "current_task": current_task,
        "provision_status": agent.provision_status,
    }


@router.get("/me/memory")
async def agent_get_memory(
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.MEMORY_READ)),
):
    """Agent reads its own MEMORY.md."""
    return {"content": agent.memory_md or ""}


async def _resolve_active_task_for_agent(
    agent: Agent,
    body_task_id: uuid.UUID | None,
    session: AsyncSession,
    *,
    required: bool = True,
) -> "Task | None":
    """Resolve the current task for an agent — used by /me/* endpoints.

    Resolution chain:
      1. body_task_id (explicit, with ownership + board check)
      2. agent.current_task_id (Board Lead path)
      3. Reverse-lookup via task.spawn_session_key (Worker/cli-bridge path)
      4. Raise 422 if required=True, return None if required=False

    Workers dispatched via USE_SUBAGENT_DISPATCH=true get spawn_session_key
    set by dispatch_delivery.py. maxConcurrent=1 guarantees uniqueness.
    """
    if body_task_id is not None:
        task = await session.get(Task, body_task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task {body_task_id} nicht gefunden.")
        _same_board_lead = agent.is_board_lead and agent.board_id == task.board_id
        if (
            task.assigned_agent_id != agent.id
            and task.owner_agent_id != agent.id
            and not _same_board_lead
        ):
            raise HTTPException(
                status_code=403,
                detail="Du bist nicht der zugewiesene Agent dieses Tasks (und nicht Board Lead desselben Boards).",
            )
        return task

    if agent.current_task_id is not None:
        task = await session.get(Task, agent.current_task_id)
        if task is not None:
            return task

    # Phase 30: gateway_agent_id session-key pattern lookup dropped. The
    # OpenClaw-session pattern `agent:{slug}:task:%:work` does not exist
    # post-Phase-29 (no more OpenClaw sessions). agent.current_task_id +
    # body task_id are the canonical sources now; spawn_session_key on the
    # Task itself drives the subagent-dispatch routing.

    if not required:
        return None

    raise HTTPException(
        status_code=422,
        detail=(
            "Keine aktive Task gefunden. Entweder task_id im Body mitgeben "
            "oder sicherstellen dass der Task via Subagent-Dispatch gestartet wurde "
            "(spawn_session_key wird dabei automatisch gesetzt)."
        ),
    )


@router.get("/me/memory/search")
async def agent_memory_search(
    q: str,
    limit: int = 10,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.MEMORY_READ)),
):
    """Semantic memory search — thin GET wrapper around `run_memory_query`.

    Used by `mc memory search` (Workstream A3). Queries the 3 Qdrant layers
    (semantic + agent + episodic) scoped to the caller's agent/board, and
    returns the flattened Top-N hits. Falls back to keyword search when the
    embedding service is offline.
    """
    # Security SEC-6: cap query length. Embedding service charges per token
    # and a stuck agent could loop with a giant prompt. 1000 chars is a
    # generous upper bound for legitimate semantic queries.
    if len(q) > 1000:
        raise HTTPException(
            status_code=400,
            detail="Query zu lang (max 1000 Zeichen). Suche mit kuerzeren Keywords.",
        )
    from app.services.memory_query import run_memory_query, InvalidQueryError

    try:
        result = await run_memory_query(
            session=session,
            query=q,
            layers=["semantic", "agent", "episodic"],
            top_k=max(1, min(limit, 25)),
            agent_id=str(agent.id),
            board_id=str(agent.board_id) if agent.board_id else None,
        )
    except InvalidQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Flatten layers into a single ranked list for CLI convenience.
    hits: list[dict] = []
    for layer_name, layer_data in (result or {}).items():
        if not isinstance(layer_data, dict):
            continue
        for item in layer_data.get("hits", []) or []:
            hits.append({
                "layer": layer_name,
                "score": item.get("score"),
                "title": item.get("title") or item.get("memory_type"),
                "content": item.get("content", "")[:400],
                "memory_id": item.get("id"),
                "created_at": item.get("created_at"),
            })
    hits.sort(key=lambda h: (h.get("score") or 0.0), reverse=True)
    return {"query": q, "hits": hits[:limit]}


@router.get("/boards/{board_id}")
async def agent_get_board(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    board = await session.get(Board, board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Board not found")

    tasks = (
        await session.exec(
            select(Task).where(Task.board_id == board_id, Task.status != "done")
        )
    ).all()

    # Fix 2: Memory sortiert — gepinnte zuerst, neueste zuerst, weniger Rauschen
    memory = (
        await session.exec(
            select(BoardMemory)
            .where(BoardMemory.board_id == board_id)
            .order_by(BoardMemory.is_pinned.desc(), BoardMemory.created_at.desc())
            .limit(20)
        )
    ).all()

    # Fix 1: agents with context for orchestrator decisions
    agents = (
        await session.exec(
            select(Agent).where(Agent.board_id == board_id)
        )
    ).all()

    # Load projects — so the agent knows which projects exist
    projects = (
        await session.exec(
            select(Project)
            .where(Project.board_id == board_id)
            .order_by(Project.created_at.desc())
        )
    ).all()

    # Agent-ID → name mapping for tasks
    agent_map = {a.id: a.name for a in agents}

    # Enrich tasks with agent_name
    enriched_tasks = []
    for t in tasks:
        td = t.model_dump()
        td["assigned_agent_name"] = agent_map.get(t.assigned_agent_id)
        enriched_tasks.append(td)

    return {
        "board": board,
        "tasks": enriched_tasks,
        "memory": memory,
        "projects": [
            {
                "id": str(p.id),
                "name": p.name,
                "status": p.status,
                "priority": p.priority,
                "project_type": p.project_type,
                "progress_pct": p.progress_pct,
                "description": p.description,
                "github_repo_url": p.github_repo_url,
                "workspace_path": p.workspace_path,
            }
            for p in projects
        ],
        "agents": [
            {
                "id": str(a.id),
                "name": a.name,
                "role": a.role,
                "emoji": a.emoji,
                "status": a.status,
                "model": a.model,
                "is_board_lead": a.is_board_lead,
                "provision_status": a.provision_status,
            }
            for a in agents
        ],
    }


# Priority ordering for pull dispatch
@router.get("/boards/{board_id}/agents")
async def agent_list_board_agents(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    """List all agents of a board — for delegation (assigned_agent_id)."""
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    agents = (
        await session.exec(
            select(Agent)
            .where(Agent.board_id == board_id)
            .order_by(Agent.name)
        )
    ).all()

    return [
        {
            "id": str(a.id),
            "name": a.name,
            "role": a.role,
            "emoji": a.emoji,
            "is_board_lead": a.is_board_lead,
            "provision_status": a.provision_status,
        }
        for a in agents
    ]


# ── Agent-Auth Task CRUD (Board Lead / Coordinator Endpoints) ─────────────────


# ── Agent-Auth Agent-Inspection Endpoints ───────────────────────────────────


@router.get("/agents/list")
async def agent_list_all_agents(
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """List all agents in the system — only for Board Leads with agents:manage."""
    result = await session.exec(select(Agent).order_by(Agent.name))
    agents = result.all()
    return [
        {
            "id": str(a.id),
            "name": a.name,
            "role": a.role,
            "emoji": a.emoji,
            "status": a.status,
            "agent_runtime": a.agent_runtime,
            "is_board_lead": a.is_board_lead,
            "board_id": str(a.board_id) if a.board_id else None,
            "model": a.model,
            "provision_status": a.provision_status,
            "scopes": a.scopes,
            "cli_plugins": a.cli_plugins,
            "cli_skills": a.cli_skills,
        }
        for a in agents
    ]


@router.get("/agents/{agent_id}/detail")
async def agent_get_agent_detail(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """Read agent detail — config, scopes, plugins, skills."""
    target = await session.get(Agent, agent_id)
    if not target:
        raise HTTPException(status_code=404, detail="Agent not found")

    return {
        "id": str(target.id),
        "name": target.name,
        "role": target.role,
        "emoji": target.emoji,
        "status": target.status,
        "agent_runtime": target.agent_runtime,
        "is_board_lead": target.is_board_lead,
        "board_id": str(target.board_id) if target.board_id else None,
        "model": target.model,
        "provision_status": target.provision_status,
        "scopes": target.scopes,
        "skills": target.skills,
        "skill_filter": target.skill_filter,
        "cli_plugins": target.cli_plugins,
        "cli_skills": target.cli_skills,
        "current_task_id": str(target.current_task_id) if target.current_task_id else None,
        "workspace_path": target.workspace_path,
        "last_seen_at": target.last_seen_at.isoformat() if target.last_seen_at else None,
        "created_at": target.created_at.isoformat() if target.created_at else None,
    }


@router.get("/boards/{board_id}/projects")
async def agent_list_projects(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    """List all projects of a board."""
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    projects = (
        await session.exec(
            select(Project)
            .where(Project.board_id == board_id)
            .order_by(Project.created_at.desc())
        )
    ).all()

    return projects


@router.post("/boards/{board_id}/projects", status_code=status.HTTP_201_CREATED)
async def agent_create_project(
    board_id: uuid.UUID,
    payload: AgentProjectCreate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_CREATE)),
):
    """Board Lead can create projects to bundle related tasks."""
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    board = await session.get(Board, board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Board not found")

    # ── Duplicate Project Guard ──────────────────────────────────
    # Prevents duplicate projects from agent retries/double calls.
    # Same name + board + 60s window → 409 with existing_project_id.
    from datetime import timedelta

    _norm_name = re.sub(r"\s+", " ", (payload.name or "").strip().lower())
    if _norm_name:
        _dup_query = select(Project).where(
            Project.board_id == board_id,
            Project.created_at > utcnow() - timedelta(seconds=60),
        )
        _dup_result = await session.exec(_dup_query)
        for _existing in _dup_result.all():
            _existing_norm = re.sub(r"\s+", " ", (_existing.name or "").strip().lower())
            if _existing_norm == _norm_name:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "duplicate_project",
                        "existing_project_id": str(_existing.id),
                        "message": f"Projekt mit gleichem Namen existiert bereits (erstellt vor "
                                   f"{int((utcnow() - _existing.created_at).total_seconds())}s)",
                    },
                )

    project = Project(
        board_id=board_id,
        name=payload.name,
        description=payload.description,
        project_type=payload.project_type,
        priority=payload.priority,
        status="active",
        created_by="agent",
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)

    await emit_event(
        session, "project.created",
        f"Agent {agent.name} hat Projekt erstellt: {project.name}",
        board_id=board_id, agent_id=agent.id,
        detail={"project_id": str(project.id), "project_name": project.name},
    )

    return project


@router.post("/boards/{board_id}/help-request", status_code=status.HTTP_201_CREATED)
async def agent_help_request(
    board_id: uuid.UUID,
    payload: HelpRequestCreate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_HELP)),
):
    """Agent asks another agent for help. Creates a subtask, blocks the requester."""
    from app.services.dispatch import auto_dispatch_task

    # 1. Find the agent's current task
    current_task_id = agent.current_task_id
    if not current_task_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Kein aktiver Task — Help Request nur aus aktiver Arbeit heraus moeglich.",
        )
    current_task = await session.get(Task, current_task_id)
    if not current_task or current_task.status != "in_progress":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task ist nicht in_progress — Help Request nur waehrend aktiver Arbeit moeglich.",
        )

    # 2. Tiefenlimit
    if current_task.help_request_from is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Help Requests koennen nicht verschachtelt werden (max. 1 Ebene).",
        )

    # 3. Find a helper agent
    helper_query = select(Agent).where(
        Agent.board_id == board_id,
        Agent.role == payload.needed_role,
        Agent.provision_status == "provisioned",
    )
    helpers = (await session.exec(helper_query)).all()
    if not helpers:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Kein provisionierter Agent mit Rolle '{payload.needed_role}' auf diesem Board.",
        )

    helper = next((h for h in helpers if h.current_task_id is None), helpers[0])

    # 4. Create subtask
    subtask = Task(
        id=uuid.uuid4(),
        board_id=board_id,
        project_id=current_task.project_id,
        parent_task_id=current_task.id,
        title=payload.title,
        description=payload.context,
        status="inbox",
        priority=payload.priority or current_task.priority,
        task_type="story",
        assigned_agent_id=helper.id,
        owner_agent_id=agent.id,
        help_request_from=agent.id,
        is_auto_created=True,
        auto_reason=f"help_request from {agent.name}",
    )
    session.add(subtask)

    # 5. Absender blockieren
    current_task.status = "blocked"
    current_task.blocked_by_task_id = subtask.id
    session.add(current_task)

    await session.commit()
    await session.refresh(subtask)

    # 6. Dispatch (async, fire-and-forget)
    import asyncio as _aio
    _aio.create_task(auto_dispatch_task(subtask.id, board_id))

    # 7. Activity Event
    await emit_event(
        session,
        event_type="task.help_request.created",
        title=f"{agent.name} bittet {helper.name} um Hilfe: {payload.title}",
        severity="info",
        board_id=board_id,
        task_id=current_task.id,
        agent_id=agent.id,
        detail={
            "help_task_id": str(subtask.id),
            "needed_role": payload.needed_role,
            "helper_agent": helper.name,
        },
    )

    logger.info(
        "Help Request: %s → %s (subtask %s, parent %s blocked)",
        agent.name, helper.name, subtask.id, current_task.id,
    )

    return HelpRequestResponse(
        help_task_id=subtask.id,
        assigned_to=helper.name,
        your_status="blocked",
    )


@router.post("/boards/{board_id}/delegate", status_code=status.HTTP_201_CREATED)
async def agent_delegate_task(
    board_id: uuid.UUID,
    payload: DelegateCreate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_CREATE)),
):
    """Orchestrator delegation: create subtask + explicitly wait for callback.

    Atomic alternative to 'mc task-create + mc blocked separately'. Creates NO
    operator approval — pure orchestration.
    """
    from app.services.dispatch import auto_dispatch_task
    from app.services.operations import check_dispatch_allowed

    current_task_id = agent.current_task_id
    if not current_task_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Kein aktiver Task — Delegation nur aus aktiver Arbeit heraus moeglich.",
        )
    current_task = await session.get(Task, current_task_id)
    if not current_task or current_task.status != "in_progress":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Aktiver Task ist nicht in_progress — Delegation blockiert.",
        )

    if current_task.board_id != board_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Task gehoert nicht zu diesem Board.",
        )

    target_agent = await session.get(Agent, payload.assigned_agent_id)
    if not target_agent:
        raise HTTPException(status_code=404, detail="Ziel-Agent nicht gefunden.")
    # Board isolation: target must be on the same board (prevents cross-board leak)
    if target_agent.board_id != board_id:
        raise HTTPException(
            status_code=422,
            detail=f"Ziel-Agent '{target_agent.name}' gehoert nicht zu diesem Board.",
        )
    if target_agent.status == "archived":
        raise HTTPException(
            status_code=422,
            detail=f"Agent '{target_agent.name}' ist archiviert und kann keine neuen Tasks erhalten.",
        )
    if target_agent.provision_status != "provisioned":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Agent '{target_agent.name}' ist nicht provisioniert "
                f"(status: {target_agent.provision_status}). Provision zuerst."
            ),
        )
    if target_agent.id == agent.id:
        raise HTTPException(
            status_code=422,
            detail="Selbst-Delegation ist nicht erlaubt. Eigenarbeit direkt am Task machen.",
        )

    # Construct subtask in-memory (not persisted yet)
    subtask = Task(
        id=uuid.uuid4(),
        board_id=board_id,
        project_id=current_task.project_id,
        parent_task_id=current_task.id,
        title=payload.title,
        description=payload.description,
        status="inbox",
        priority=payload.priority or current_task.priority,
        task_type="story",
        assigned_agent_id=target_agent.id,
        owner_agent_id=agent.id,
        # Callback pattern: subtask points back to the delegating agent
        callback_agent_id=agent.id if payload.callback else None,
        is_auto_created=True,
        auto_reason=f"delegation from {agent.name}",
    )

    # Dispatch guard BEFORE commit — no zombie subtask if the system/agent isn't dispatchable right now
    allowed, reason = await check_dispatch_allowed(subtask, target_agent, session)
    if not allowed:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Delegation blockiert: {reason}. "
                f"Versuche es spaeter erneut, wenn der Ziel-Agent bereit ist."
            ),
        )

    session.add(subtask)

    if payload.callback:
        # Explicit flush before the current_task UPDATE. Without a flush,
        # SQLAlchemy could order the operations wrong in the following
        # emit_event() call (which internally does session.commit(),
        # activity.py:41) — the current_task UPDATE with blocked_by_task_id
        # runs before the subtask INSERT and the non-deferrable FK
        # fk_tasks_blocked_by_task_id blows up.
        # Reflexive FKs (tasks → tasks) confuse SQLAlchemy's topological sort.
        # Live bug Boss 2026-04-25: HTTP 500 on mc delegate --callback.
        await session.flush()
        current_task.status = "blocked"
        current_task.blocked_by_task_id = subtask.id
        current_task.callback_agent_id = agent.id
        session.add(current_task)

    # Progress comment with delegation context
    comment = TaskComment(
        id=uuid.uuid4(),
        task_id=current_task.id,
        author_type="agent",
        author_agent_id=agent.id,
        content=(
            f"Delegiert an **{target_agent.name}**: {payload.title}\n\n"
            f"Subtask-ID: {subtask.id}\n"
            + ("Warte auf Callback — Parent reaktiviert sich automatisch wenn Subtask `done` ist."
               if payload.callback
               else "Fire-and-Forget — Parent laeuft weiter.")
        ),
        comment_type="progress",
    )
    session.add(comment)

    await emit_event(
        session,
        event_type="task.delegated",
        title=f"{agent.name} delegiert an {target_agent.name}: {payload.title}",
        severity="info",
        board_id=board_id,
        task_id=current_task.id,
        agent_id=agent.id,
        detail={
            "subtask_id": str(subtask.id),
            "target_agent": target_agent.name,
            "callback": payload.callback,
        },
    )

    await session.commit()
    await session.refresh(subtask)

    # Dispatch (async, fire-and-forget) — guard above already confirmed it's dispatchable
    import asyncio as _aio
    _aio.create_task(auto_dispatch_task(subtask.id, board_id))

    logger.info(
        "Delegate: %s → %s (subtask %s, parent %s %s)",
        agent.name, target_agent.name, subtask.id, current_task.id,
        "blocked" if payload.callback else "in_progress",
    )

    return DelegateResponse(
        subtask_id=subtask.id,
        assigned_to=target_agent.name,
        your_status="blocked" if payload.callback else "in_progress",
    )


@router.post("/boards/{board_id}/clarification", status_code=status.HTTP_201_CREATED)
async def agent_clarification(
    board_id: uuid.UUID,
    payload: ClarificationCreate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_HELP)),
):
    """Agent asks the operator a clarifying question. Task is blocked until the operator answers."""

    # 1. Check the agent's current task
    current_task_id = agent.current_task_id
    if not current_task_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Kein aktiver Task — Klaerungsfrage nur aus aktiver Arbeit heraus moeglich.",
        )
    current_task = await session.get(Task, current_task_id)
    if not current_task or current_task.status != "in_progress":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task ist nicht in_progress — Klaerungsfrage nur waehrend aktiver Arbeit moeglich.",
        )

    # 2. Load project name for context
    project_name = None
    if current_task.project_id:
        project = await session.get(Project, current_task.project_id)
        if project:
            project_name = project.name

    # 3. Create approval — mit expires_at, damit das Fix-E-Renewal (24h-
    # Reminder statt stilles Vergessen) auch fuer Klaerungsfragen greift.
    from datetime import timedelta as _timedelta
    approval = Approval(
        id=uuid.uuid4(),
        board_id=board_id,
        task_id=current_task.id,
        agent_id=agent.id,
        action_type="clarification_question",
        description=f"Klaerungsfrage von {agent.name}: {payload.question[:300]}",
        payload={
            "question": payload.question,
            "options": payload.options,
            "task_title": current_task.title,
            "project_name": project_name,
            "agent_name": agent.name,
        },
        status="pending",
        expires_at=utcnow() + _timedelta(hours=24),
    )
    session.add(approval)

    # 4. Block task
    from app.services.task_lifecycle import record_task_event
    await record_task_event(
        session, current_task.id, current_task.status, "blocked",
        changed_by="agent", agent_id=agent.id, reason="clarification_question",
    )
    current_task.status = "blocked"
    session.add(current_task)

    # 5. Lead-FYI (G1): Der Lead darf antworten, wenn er die Antwort kennt —
    # sein Unblock supersedet das Approval (approval_cleanup). Die Frage
    # selbst bleibt ein Operator-Fall (Telegram laeuft wie bisher).
    from app.services.blocker_triage import find_board_lead
    _lead = await find_board_lead(session, board_id)
    if _lead is not None and _lead.id != agent.id:
        from app.models.task import TaskComment as _TC
        session.add(_TC(
            task_id=current_task.id,
            author_type="system",
            content=(
                f"KLAERUNGSFRAGE: {agent.name} bei \"{current_task.title}\"\n\n"
                f"**Frage:** {payload.question[:1000]}\n\n"
                f"**Task-ID:** {current_task.id}\n\n"
                f"Der Operator wurde gefragt. Kennst DU die Antwort, darfst du "
                f"sie als `resolution`-Kommentar posten und den Task via PATCH "
                f"auf `in_progress` setzen — das Approval schliesst sich dann "
                f"automatisch."
            ),
            comment_type="blocker_lead_notify",
        ))

    await session.commit()
    await session.refresh(approval)

    # 5. Activity Event
    await emit_event(
        session,
        event_type="clarification.created",
        title=f"{agent.name} fragt: {payload.question[:80]}",
        severity="info",
        board_id=board_id,
        task_id=current_task.id,
        agent_id=agent.id,
        detail={
            "approval_id": str(approval.id),
            "question": payload.question,
            "options": payload.options,
        },
    )

    logger.info(
        "Clarification: %s asks '%s' (approval %s, task %s blocked)",
        agent.name, payload.question[:60], approval.id, current_task.id,
    )

    return ClarificationResponse(
        approval_id=approval.id,
        your_status="blocked",
    )


# ─────────────────────────────────────────────────────────────────────────
# mc ask (Task 7, Interaction Model 2.0) — thread-native question, replaces
# the Approval-based /boards/{board_id}/clarification for new callers.
# Two stages: non-blocking (fire a question message, keep working) and
# blocking (question message + status -> waiting, session stays alive —
# see app/task_status.py VALID_TRANSITIONS + Task 6).
# ─────────────────────────────────────────────────────────────────────────


class AskCreate(BaseModel):
    question: str
    blocking: bool = False
    to: str = "boss"
    priority: str = "medium"
    options: list[str] | None = None
    default: str | None = None
    deadline: str | None = None

    @field_validator("to")
    @classmethod
    def _validate_to(cls, v: str) -> str:
        from app.comm_constants import QUESTION_TARGETS

        if v not in QUESTION_TARGETS:
            raise ValueError(f"to must be one of {QUESTION_TARGETS}")
        return v

    @field_validator("priority")
    @classmethod
    def _validate_priority(cls, v: str) -> str:
        from app.comm_constants import QUESTION_PRIORITIES

        if v not in QUESTION_PRIORITIES:
            raise ValueError(f"priority must be one of {QUESTION_PRIORITIES}")
        return v


class AskResponse(BaseModel):
    message_id: uuid.UUID
    thread_id: uuid.UUID
    your_status: str


@router.post("/tasks/current/ask", status_code=status.HTTP_201_CREATED)
async def agent_ask(
    payload: AskCreate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.CHAT_WRITE)),
):
    """Agent asks a question on the task thread — non-blocking by default.

    Non-blocking: posts message_type="question" with question_meta.awaiting
    True, task status untouched. Blocking: additionally moves the task to
    `waiting` (Task 6) and posts a `system` line — the worker's session
    stays alive, paused on an answer.
    """
    from app.services.messaging import ensure_task_thread, post_message
    from app.services.task_lifecycle import record_task_event
    from app.task_status import TaskStatus, is_valid_transition

    current_task_id = agent.current_task_id
    if not current_task_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Kein aktiver Task — ask nur aus aktiver Arbeit heraus moeglich.",
        )
    current_task = await session.get(Task, current_task_id)
    if not current_task:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Kein aktiver Task — ask nur aus aktiver Arbeit heraus moeglich.",
        )

    # Task 12 (final-review A2, defense-in-depth): a blocking ask parks the
    # task in `waiting` until an answer is delivered — but answer delivery is
    # gated on the comm_v2 pilot. A non-pilot agent parking here could never be
    # released (dead task). Reject blocking asks from non-pilots; non-blocking
    # asks are harmless (the question lands in the thread, visible in web).
    if payload.blocking and not getattr(agent, "comm_v2", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="messaging v2 pilot required for blocking asks",
        )

    if payload.blocking and not is_valid_transition(current_task.status, TaskStatus.WAITING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Task-Status '{current_task.status}' erlaubt keinen Wechsel zu "
                f"'waiting' — blocking ask nur waehrend aktiver Arbeit moeglich."
            ),
        )

    thread = await ensure_task_thread(session, current_task)

    message = await post_message(
        session,
        thread_id=thread.id,
        sender_type="agent",
        sender_id=agent.id,
        message_type="question",
        body=payload.question,
        question_meta={
            "awaiting": True,
            "blocking": payload.blocking,
            "to": payload.to,
            "priority": payload.priority,
            "options": payload.options,
            "default": payload.default,
            "deadline": payload.deadline,
        },
    )

    your_status = current_task.status
    if payload.blocking:
        await record_task_event(
            session, current_task.id, current_task.status, TaskStatus.WAITING,
            changed_by="agent", agent_id=agent.id, reason="ask_blocking",
        )
        current_task.status = TaskStatus.WAITING
        session.add(current_task)
        await session.commit()
        await session.refresh(current_task)
        your_status = current_task.status

        await post_message(
            session,
            thread_id=thread.id,
            sender_type="system",
            message_type="system",
            body=f"⏸ {agent.name} wartet auf Antwort (blocking)",
        )

    logger.info(
        "Ask: %s asks '%s' (blocking=%s, task %s -> %s)",
        agent.name, payload.question[:60], payload.blocking, current_task.id, your_status,
    )

    return AskResponse(
        message_id=message.id,
        thread_id=thread.id,
        your_status=your_status,
    )


class MessageCreate(BaseModel):
    """Agent posts a plain message/status/decision onto its task thread.

    Questions are NOT allowed here — they carry awaiting semantics and go
    through POST /tasks/current/ask. `system` lines are backend-authored.
    """
    body: str
    message_type: str = "message"
    reply_to: uuid.UUID | None = None

    @field_validator("body")
    @classmethod
    def _body_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body darf nicht leer sein")
        return v

    @field_validator("message_type")
    @classmethod
    def _validate_message_type(cls, v: str) -> str:
        allowed = ("message", "status", "decision")
        if v not in allowed:
            raise ValueError(f"message_type muss eines von {allowed} sein (Fragen → /ask)")
        return v


class MessageResponse(BaseModel):
    message_id: uuid.UUID
    thread_id: uuid.UUID
    your_status: str


@router.post("/tasks/current/messages", status_code=status.HTTP_201_CREATED)
async def agent_post_message(
    payload: MessageCreate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.CHAT_WRITE)),
):
    """Agent posts a plain message on its active task thread (§3.3).

    The first inbound Message on a dispatched task claims it via the shared
    ACK handshake (same effect as the legacy first-comment ACK) — no other
    lifecycle side-effects. Delivery to peers/operator rides the poll path.
    """
    from app.services.messaging import ensure_task_thread, post_message
    from app.services.task_lifecycle import apply_ack_handshake

    current_task_id = agent.current_task_id
    if not current_task_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Kein aktiver Task — message nur aus aktiver Arbeit heraus moeglich.",
        )
    current_task = await session.get(Task, current_task_id)
    if not current_task:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Kein aktiver Task — message nur aus aktiver Arbeit heraus moeglich.",
        )

    # First inbound agent Message ACKs the task (idempotent). Non-committing —
    # the post_message commit below persists the handshake mutations too.
    apply_ack_handshake(session, current_task, agent)

    thread = await ensure_task_thread(session, current_task)
    message = await post_message(
        session,
        thread_id=thread.id,
        sender_type="agent",
        sender_id=agent.id,
        message_type=payload.message_type,
        body=payload.body,
        reply_to=payload.reply_to,
    )

    await session.refresh(current_task)
    logger.info(
        "Message: %s posts on task %s (type=%s, status=%s)",
        agent.name, current_task.id, payload.message_type, current_task.status,
    )
    return MessageResponse(
        message_id=message.id,
        thread_id=thread.id,
        your_status=current_task.status,
    )


# PATCH /boards/{board_id}/tasks/{task_id} (the 600-line state-machine
# endpoint), `_find_reviewer` / `_find_last_developer` etc. now live in
# routers/agent_task_status.py + services/work_context.py. The shim
# block at the top of this module preserves all imported names.
# Phase 4 REF-02 Plan 04-07.


# Status-transition endpoints (events / report-back / review / checkpoint)
# live in routers/agent_task_status.py (REF-02 step 4 — Plan 04-07).
# Their Pydantic models (ReviewDecisionBody, ReportBackUpdate,
# CheckpointCreate) are re-exported via the shim block at the top of this
# module for test compatibility.


# ── Deliverable Endpoints ─────────────────────────────────────────────────

class DeliverableCreate(BaseModel):
    """Agent registers a deliverable — a result artifact."""
    deliverable_type: Literal["screenshot", "file", "url", "artifact", "document", "data", "video"]
    title: str
    path: str | None = None
    description: str | None = None
    # V2 fields
    content: str | None = None         # Text-Inhalt direkt (Markdown etc.)
    scope: str = "task"                # task | phase | project
    tags: list[str] | None = None      # Für Suche
    is_pinned: bool = False            # Immer in Agent-Kontext injiziert
    is_reusable: bool = False          # Cross-Project wiederverwendbar
    git_commit: bool = False           # Deliverable als Datei in Git committen

    @field_validator("title")
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title darf nicht leer sein")
        if len(v) > 500:
            raise ValueError("title max 500 Zeichen")
        return v

    @field_validator("scope")
    @classmethod
    def scope_valid(cls, v: str) -> str:
        if v not in ("task", "phase", "project"):
            raise ValueError("scope muss task | phase | project sein")
        return v


class MeDeliverableCreate(DeliverableCreate):
    """DeliverableCreate + optional task_id override for /me/deliverable."""
    task_id: uuid.UUID | None = None


@router.post(
    "/boards/{board_id}/tasks/{task_id}/deliverables",
    status_code=status.HTTP_201_CREATED,
)
async def agent_create_deliverable(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: DeliverableCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_WRITE)),
):
    """Agent registers a deliverable — screenshot, file, URL, artifact, or document."""
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    if payload.deliverable_type in ("document", "data") and not (payload.content and payload.content.strip()):
        raise HTTPException(
            status_code=400,
            detail=(
                f"deliverable_type='{payload.deliverable_type}' requires inline 'content' "
                "(Markdown/Text). 'path' alone is not readable from the UI because it "
                "points into the agent container filesystem. Include the full text in 'content'."
            ),
        )

    from app.services.deliverable_paths import validate_deliverable_path
    validate_deliverable_path(payload.path, payload.content, task_id)

    from app.models.deliverable import TaskDeliverable

    # Dedup check: prevents duplicate registration when an agent resubmits
    # the same content (e.g. because it thought the first call had failed —
    # Incident 2026-04-23 root cause Bug A: Researcher registered the same
    # deliverable 4x because the LIST endpoint returned no content). Match
    # criterion: (task_id, path) when path is present, otherwise
    # (task_id, title) for inline-only deliverables. Same-agent-only — cross-
    # agent duplicates are legitimate separate contributions.
    from sqlmodel import and_ as _and_
    dedup_query = select(TaskDeliverable).where(
        TaskDeliverable.task_id == task_id,
        TaskDeliverable.agent_id == agent.id,
    )
    if payload.path:
        dedup_query = dedup_query.where(TaskDeliverable.path == payload.path)
    else:
        dedup_query = dedup_query.where(
            _and_(
                TaskDeliverable.path.is_(None),  # type: ignore[union-attr]
                TaskDeliverable.title == payload.title,
            )
        )
    existing_dup = (await session.exec(dedup_query)).first()
    if existing_dup is not None:
        logger.info(
            "Deliverable-Dedup: Agent %s re-submitted (task=%s, path=%s, title=%s) — returning existing %s",
            agent.name, task_id, payload.path, payload.title, existing_dup.id,
        )
        return {
            "id": str(existing_dup.id),
            "task_id": str(existing_dup.task_id),
            "agent_id": str(existing_dup.agent_id),
            "deliverable_type": existing_dup.deliverable_type,
            "title": existing_dup.title,
            "path": existing_dup.path,
            "content_length": len(existing_dup.content) if existing_dup.content else 0,
            "duplicate": True,
            "message": (
                "Deliverable existiert bereits (gleicher Pfad/Titel + Agent + Task). "
                "Nutze GET /boards/{board_id}/tasks/{task_id}/deliverables/{id} um "
                "content zu verifizieren, statt neu zu registrieren."
            ),
        }

    git_commit_hash: str | None = None

    # Optional: Deliverable als Datei in Git committen
    if payload.git_commit and payload.content and task.project_id:
        project = await session.get(Project, task.project_id)
        if project and project.workspace_path:
            try:
                import re
                from app.services.git_service import GitService
                from app.models.project_phase import ProjectPhase
                git = GitService()
                filename = re.sub(r"[^a-z0-9\-]", "-", payload.title.lower()[:50]) + ".md"
                phase_slug = "general"
                if task.phase_id:
                    phase = await session.get(ProjectPhase, task.phase_id)
                    if phase:
                        phase_slug = phase.git_branch.replace("phase/", "") if phase.git_branch else "general"
                git_commit_hash = await git.commit_deliverable(
                    project_dir=project.workspace_path,
                    phase_slug=phase_slug,
                    filename=filename,
                    content=payload.content,
                    task_id=str(task_id)[:8],
                    title=payload.title,
                )
            except Exception as e:
                logger.warning("Git-Commit für Deliverable fehlgeschlagen: %s", e)

    deliverable = TaskDeliverable(
        task_id=task_id,
        agent_id=agent.id,
        deliverable_type=payload.deliverable_type,
        title=payload.title,
        path=payload.path,
        description=payload.description,
        content=payload.content,
        scope=payload.scope,
        tags=payload.tags,
        is_pinned=payload.is_pinned,
        is_reusable=payload.is_reusable,
        git_commit_hash=git_commit_hash,
    )
    session.add(deliverable)
    await session.commit()
    await session.refresh(deliverable)

    # Auto-Memory-Write: jedes Deliverable landet im 3-schichtigen Memory-System
    # (board_memory) so research results are searchable and don't get lost —
    # without the agent having to make a second POST.
    from app.models.memory import BoardMemory

    _TYPE_TO_MEMORY_TYPE = {
        "document": "knowledge",
        "data": "knowledge",
        "url": "reference",
        "file": "reference",
        "artifact": "reference",
        "screenshot": "reference",
        "video": "reference",
    }
    memory_type = _TYPE_TO_MEMORY_TYPE.get(payload.deliverable_type, "reference")

    memory_body_parts: list[str] = []
    if payload.description:
        memory_body_parts.append(payload.description)
    if payload.content:
        memory_body_parts.append(payload.content)
    elif payload.path:
        memory_body_parts.append(f"Datei: `{payload.path}`")
    memory_body = "\n\n".join(memory_body_parts).strip() or payload.title

    memory_tags = list(payload.tags or [])
    memory_tags.extend([f"task:{task_id}", f"deliverable:{deliverable.id}"])

    try:
        memory_entry = BoardMemory(
            board_id=task.board_id,
            agent_id=agent.id,
            memory_type=memory_type,
            title=payload.title,
            content=memory_body[:20000],
            tags=memory_tags,
            source=agent.name,
            auto_generated=True,
        )
        session.add(memory_entry)
        await session.commit()
    except Exception as e:
        # A memory-write failure must not block the deliverable flow — just log it.
        logger.warning("Auto-memory-write fuer deliverable %s fehlgeschlagen: %s", deliverable.id, e)

    logger.info(
        "Deliverable created: task=%s agent=%s type=%s title='%s' scope=%s pinned=%s memory_type=%s",
        task_id, agent.name, payload.deliverable_type, payload.title[:60],
        payload.scope, payload.is_pinned, memory_type,
    )

    # Phase A vault-as-brain: write a Markdown wrapper into ~/.mc/vault so
    # the deliverable is FTS5/Qdrant-searchable and reachable to other agents
    # via Read /vault/agents/.../deliverables/*.md. Background-tasked because
    # the sync involves a filesystem hardlink + frontmatter write — we don't
    # want the agent to wait for that.
    background_tasks.add_task(_sync_deliverable_to_vault_bg, deliverable.id)

    return {
        "id": str(deliverable.id),
        "created_at": str(deliverable.created_at),
        "git_commit_hash": git_commit_hash,
    }


async def _sync_deliverable_to_vault_bg(deliverable_id: uuid.UUID) -> None:
    """Background-task helper: opens its own DB session, calls the wrapper
    sync service, logs the outcome. Errors NEVER bubble — vault sync is a
    best-effort enrichment, not a deliverable-create blocker."""
    from app.database import engine
    from app.services.deliverable_wrapper import sync_deliverable_id

    try:
        async with AsyncSession(engine, expire_on_commit=False) as bg_session:
            res = await sync_deliverable_id(deliverable_id, bg_session)
        if res.error:
            logger.warning("Vault wrapper sync failed for %s: %s", deliverable_id, res.error)
        elif res.skipped:
            logger.debug("Vault wrapper skipped for %s: %s", deliverable_id, res.reason)
        else:
            logger.info("Vault wrapper synced (bg) for %s → %s", deliverable_id, res.wrapper_path)
    except Exception as exc:
        logger.warning("Vault wrapper background sync crashed for %s: %s", deliverable_id, exc)


@router.get("/boards/{board_id}/tasks/{task_id}/deliverables")
async def agent_list_deliverables(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    include_content: bool = False,
    include_subtasks: bool = False,
    depth: int = 2,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    """Read all deliverables for a task.

    Query params:
      include_content: If true, the `content` field (full markdown/text
          body) is included. Default false to keep the response size small.
      include_subtasks: If true, deliverables of all descendant subtasks
          (recursively up to `depth` levels) are included too. Each subtask
          deliverable gets `source_task_id` + `source_task_title` + `depth`
          (0=self, 1=direct child, etc.) for UI grouping. This lets
          orchestrator parent tasks see the entire output tree at a glance
          without querying each subtask individually.
      depth: Max subtask depth (1=direct children, 2=grandchildren, ...).
          Default 2, maximum 5 as a response-size guard.
    """
    from app.models.deliverable import TaskDeliverable
    from sqlmodel import col as _col

    # Depth clamp (server-side safety)
    effective_depth = max(1, min(int(depth or 2), 5))

    # Collect task IDs + titles via BFS (if include_subtasks).
    # Map: task_id -> (task_title, depth)
    task_meta: dict[uuid.UUID, tuple[str, int]] = {task_id: ("", 0)}

    # Fetch the root task's title (for consistent source_task_title output)
    root_task = await session.get(Task, task_id)
    if root_task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if root_task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not in this board")
    task_meta[task_id] = (root_task.title or "", 0)

    if include_subtasks:
        current_level = [task_id]
        for d in range(1, effective_depth + 1):
            if not current_level:
                break
            children_result = await session.exec(
                select(Task).where(_col(Task.parent_task_id).in_(current_level))
            )
            children = list(children_result.all())
            next_level: list[uuid.UUID] = []
            for child in children:
                if child.id not in task_meta:
                    task_meta[child.id] = (child.title or "", d)
                    next_level.append(child.id)
            current_level = next_level

    # Deliverables for all collected task_ids
    deliverables_result = await session.exec(
        select(TaskDeliverable)
        .where(_col(TaskDeliverable.task_id).in_(list(task_meta.keys())))
        .order_by(TaskDeliverable.created_at.desc())  # type: ignore[union-attr]
    )
    deliverables = deliverables_result.all()

    def _serialize(d: TaskDeliverable) -> dict:
        source_title, source_depth = task_meta.get(d.task_id, ("", 0))
        row = {
            "id": str(d.id),
            "task_id": str(d.task_id),
            "agent_id": str(d.agent_id),
            "deliverable_type": d.deliverable_type,
            "title": d.title,
            "path": d.path,
            "description": d.description,
            "created_at": str(d.created_at),
            "content_length": len(d.content) if d.content else 0,
        }
        if include_content:
            row["content"] = d.content
        if include_subtasks:
            # Only when include_subtasks is set, otherwise the LIST-response
            # shape would needlessly change for callers expecting the old field names.
            row["source_task_id"] = str(d.task_id)
            row["source_task_title"] = source_title
            row["source_depth"] = source_depth  # 0=self, 1=direct child, ...
        return row

    return [_serialize(d) for d in deliverables]


@router.get("/boards/{board_id}/tasks/{task_id}/deliverables/{deliverable_id}")
async def agent_get_deliverable(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    """Read a single deliverable with the full `content` field.

    Closes a verification gap: the LIST endpoint omits `content` by default
    (response size). Agents (Boss, FreeCode, Planner) need the full
    markdown/text body to do follow-up work — this endpoint provides it.
    Scope: TASKS_READ (same as LIST).

    Incident context 2026-04-23: without this endpoint, agents wrongly
    concluded from the absent `content_length=0` in the LIST response that
    content was missing — which led to duplicate re-registrations and
    broken phase_rewrite_requests.
    """
    from app.models.deliverable import TaskDeliverable

    deliverable = await session.get(TaskDeliverable, deliverable_id)
    if not deliverable:
        raise HTTPException(status_code=404, detail="Deliverable not found")
    if deliverable.task_id != task_id:
        raise HTTPException(
            status_code=404,
            detail=f"Deliverable {deliverable_id} gehoert nicht zu Task {task_id}",
        )

    # Board check via task: prevents cross-board leak
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    return {
        "id": str(deliverable.id),
        "task_id": str(deliverable.task_id),
        "agent_id": str(deliverable.agent_id),
        "deliverable_type": deliverable.deliverable_type,
        "title": deliverable.title,
        "path": deliverable.path,
        "description": deliverable.description,
        "content": deliverable.content,
        "content_length": len(deliverable.content) if deliverable.content else 0,
        "created_at": str(deliverable.created_at),
    }


# ─────────────────────────────────────────────────────────────────────────
# Boss agent-spawn request — the operator must approve it (Phase 2, 2026-04-11)
# ─────────────────────────────────────────────────────────────────────────


@router.post("/agents/request-spawn", response_model=SpawnApprovalResponse)
async def agent_request_spawn(
    payload: SpawnAgentRequest,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """Boss creates a spawn approval. Operator approves → agent gets created.

    Only Board Leads (Boss, Henry) may do this — scope AGENTS_MANAGE.
    The actual spawn happens in the resolve_approval() handler.
    """
    if not agent.is_board_lead:
        raise HTTPException(
            status_code=403,
            detail="Nur Board Leads duerfen Agent-Spawns anfragen",
        )
    if not payload.name or not payload.role or not payload.reason:
        raise HTTPException(status_code=400, detail="name, role, reason sind Pflicht")
    if agent.board_id is None:
        raise HTTPException(status_code=400, detail="Requester hat kein board_id")

    # Race-safe Duplicate-Check via Redis SET NX Lock (60s TTL).
    # Prevents two parallel POST requests from both getting through. The
    # existing SELECT+INSERT path below remains as defense-in-depth.
    _spawn_lock_key = f"mc:spawn_request:{payload.name.strip().lower()}"
    _lock_acquired = False
    try:
        from app.redis_client import get_redis
        _redis = await get_redis()
        _lock_acquired = bool(await _redis.set(_spawn_lock_key, "1", ex=60, nx=True))
        if not _lock_acquired:
            raise HTTPException(
                status_code=409,
                detail=f"Spawn fuer '{payload.name}' gerade in Bearbeitung (Race-Lock)",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Redis spawn-lock nicht verfuegbar, fallback auf DB-Check: %s", e)
        # Continue to the DB check — don't block if Redis is down

    # Duplicate check: no pending spawn with the same name
    existing_result = await session.exec(
        select(Approval).where(
            Approval.action_type == "spawn_agent",
            Approval.status == "pending",
        )
    )
    for appr in existing_result.all():
        if appr.payload and appr.payload.get("name") == payload.name:
            raise HTTPException(
                status_code=409,
                detail=f"Spawn fuer '{payload.name}' haengt bereits in Approval {appr.id}",
            )

    approval = Approval(
        board_id=agent.board_id,
        task_id=agent.current_task_id,
        agent_id=agent.id,
        action_type="spawn_agent",
        description=f"Boss will neuen Agent '{payload.name}' ({payload.role}) spawnen: {payload.reason[:120]}",
        payload=payload.model_dump(mode="json"),
        status="pending",
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)

    await emit_event(
        session,
        event_type="approval.spawn_requested",
        title=f"Spawn-Approval: '{payload.name}' ({payload.role})",
        severity="info",
        board_id=agent.board_id,
        agent_id=agent.id,
        detail={
            "approval_id": str(approval.id),
            "agent_name": payload.name,
            "role": payload.role,
            "ephemeral": payload.ephemeral,
        },
    )
    logger.info(
        "Spawn-Approval %s erstellt von %s fuer '%s' (%s)",
        approval.id, agent.name, payload.name, payload.role,
    )
    return SpawnApprovalResponse(approval_id=approval.id, status="pending")


# ─────────────────────────────────────────────────────────────────────────
# Boss Plugin-Self-Service — Boss may toggle its own + worker plugins
# ─────────────────────────────────────────────────────────────────────────


@router.get("/plugins")
async def agent_list_plugins(
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """List shared-cache plugins for plugin assignment to workers.

    Board Leads only — the only ones allowed to assign plugins too
    (see PATCH /agents/{id}/plugins). Pure read operation, no install.
    Installing new plugins still runs via POST /install-requests (operator
    approval gate, supply-chain protection).
    """
    if not agent.is_board_lead:
        raise HTTPException(
            status_code=403,
            detail="Nur Board Leads duerfen Plugins auflisten",
        )
    from app.services.plugin_manager import list_available_plugins
    plugins = list_available_plugins()
    return {
        "plugins": [p.model_dump() for p in plugins],
        "total": len(plugins),
    }


@router.get("/agents/{target_agent_id}/plugins")
async def agent_get_plugins(
    target_agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """Read the cli_plugins currently assigned to a worker agent.

    Complement to PATCH — Boss can check which plugins a worker currently
    has before assigning/removing. None = all installed, [] = none,
    list = allowlist.
    """
    if not agent.is_board_lead:
        raise HTTPException(
            status_code=403,
            detail="Nur Board Leads duerfen Plugin-Zuweisungen lesen",
        )
    target = await session.get(Agent, target_agent_id)
    if not target:
        raise HTTPException(status_code=404, detail="Ziel-Agent nicht gefunden")
    if target.board_id != agent.board_id:
        raise HTTPException(
            status_code=403,
            detail="Ziel-Agent gehoert zu einem anderen Board",
        )
    return {
        "agent_id": str(target.id),
        "agent_name": target.name,
        "cli_plugins": target.cli_plugins,
    }


@router.patch("/agents/{target_agent_id}/plugins")
async def agent_patch_plugins(
    target_agent_id: uuid.UUID,
    payload: PluginUpdateRequest,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """Boss/Board-Lead may set cli_plugins for itself or a worker.

    Triggers sync_agent_plugins_to_disk() — settings.json + installed_plugins.json
    are re-rendered. A worker restart isn't required, the next start reads fresh.
    """
    if not agent.is_board_lead:
        raise HTTPException(
            status_code=403,
            detail="Nur Board Leads duerfen Plugins anderer Agents setzen",
        )
    target = await session.get(Agent, target_agent_id)
    if not target:
        raise HTTPException(status_code=404, detail="Ziel-Agent nicht gefunden")
    if target.board_id != agent.board_id:
        raise HTTPException(
            status_code=403,
            detail="Ziel-Agent gehoert zu einem anderen Board",
        )
    # Board Leads may NOT set plugins on each other (privilege guard):
    # Boss shouldn't be able to change Henry's plugin config and vice versa.
    if target.is_board_lead and target.id != agent.id:
        raise HTTPException(
            status_code=403,
            detail="Board-Lead-Agents koennen einander keine Plugins setzen (nur self)",
        )

    target.cli_plugins = payload.cli_plugins
    session.add(target)
    await session.commit()
    await session.refresh(target)

    # Sync to disk — fail-soft, DB is the source of truth
    synced: dict[str, bool] = {}
    slug = (target.name or "").lower().replace(" ", "-")
    try:
        from app.services.plugin_manager import sync_agent_plugins_to_disk
        synced = sync_agent_plugins_to_disk(
            agent_slug=slug,
            system_prompt=target.soul_md or "",
            model=target.model or "",
            cli_plugins=target.cli_plugins,
        )
    except Exception as e:
        logger.warning("Plugin sync to disk failed for %s: %s", target.name, e)

    # Worker restart optional — claude/openclaude only reads settings.json on
    # start. Without a restart, new plugins only take effect on the next restart.
    # Only for CLI-bridge agents — host runtime (Boss) has no worker.
    worker_restarted: bool | None = None
    if payload.restart_worker:
        if target.agent_runtime != "cli-bridge":
            logger.info("restart_worker ignoriert fuer %s — agent_runtime=%s hat keinen Worker", target.name, target.agent_runtime)
            worker_restarted = False
        else:
            try:
                from app.routers.cli_terminal import _bridge_post
                restart_result = _bridge_post(f"/worker/{slug}/restart", {})
                worker_restarted = bool(restart_result and restart_result.get("ok"))
            except Exception as e:
                logger.warning("Worker-Restart fuer %s fehlgeschlagen: %s", target.name, e)
                worker_restarted = False

    await emit_event(
        session,
        event_type="agent.plugins_updated",
        title=f"Plugins aktualisiert fuer {target.name}",
        severity="info",
        board_id=agent.board_id,
        agent_id=agent.id,
        detail={
            "target_agent_id": str(target.id),
            "cli_plugins": target.cli_plugins,
            "disk_sync": synced,
            "worker_restarted": worker_restarted,
            "changed_by": agent.name,
        },
    )
    logger.info(
        "Plugins fuer %s von %s auf %s gesetzt (restart=%s)",
        target.name, agent.name, target.cli_plugins, worker_restarted,
    )
    return {
        "agent_id": str(target.id),
        "cli_plugins": target.cli_plugins,
        "disk_sync": synced,
        "worker_restarted": worker_restarted,
    }


@router.post("/agents/{target_agent_id}/worker/restart")
async def agent_restart_worker(
    target_agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """Board Lead may restart a CLI-bridge agent's worker session.

    Useful when: new plugins were assigned (without restart_worker=true),
    settings changed, or the worker is stuck in a stale state. Kills and
    restarts the claude session in tmux window 0 — the container stays up,
    poll.sh keeps running.

    WARNING: in-flight task context is lost. Boss should check
    (current_task_id) before restarting.
    """
    if not agent.is_board_lead:
        raise HTTPException(
            status_code=403,
            detail="Nur Board Leads duerfen Worker restarten",
        )
    target = await session.get(Agent, target_agent_id)
    if not target:
        raise HTTPException(status_code=404, detail="Ziel-Agent nicht gefunden")
    if target.board_id != agent.board_id:
        raise HTTPException(
            status_code=403,
            detail="Ziel-Agent gehoert zu einem anderen Board",
        )
    if target.is_board_lead and target.id != agent.id:
        raise HTTPException(
            status_code=403,
            detail="Board-Lead-Agents koennen einander nicht restarten (nur self)",
        )
    if target.agent_runtime != "cli-bridge":
        raise HTTPException(
            status_code=400,
            detail=f"Worker-Restart nicht verfuegbar fuer agent_runtime={target.agent_runtime}",
        )

    slug = (target.name or "").lower().replace(" ", "-")
    from app.routers.cli_terminal import _bridge_post
    result = _bridge_post(f"/worker/{slug}/restart", {})
    if result is None:
        raise HTTPException(status_code=503, detail="CLI-Bridge nicht erreichbar")
    ok = bool(result.get("ok"))

    await emit_event(
        session,
        event_type="agent.worker_restarted",
        title=f"Worker-Session neu gestartet fuer {target.name}",
        severity="info" if ok else "warning",
        board_id=agent.board_id,
        agent_id=agent.id,
        detail={
            "target_agent_id": str(target.id),
            "ok": ok,
            "changed_by": agent.name,
            "bridge_result": result,
        },
    )
    return {
        "agent_id": str(target.id),
        "ok": ok,
        "bridge_result": result,
    }


class WorkflowFlagsUpdate(BaseModel):
    requires_git_workflow: bool | None = None


@router.patch("/agents/{target_agent_id}/workflow-flags")
async def agent_patch_workflow_flags(
    target_agent_id: uuid.UUID,
    payload: WorkflowFlagsUpdate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """Board Lead may set workflow flags for a worker.

    Primary use case: Boss notices a Design/Writer/Research worker is blocked
    on the git-commit gate → sets requires_git_workflow=false instead of
    asking the operator. Autonomous unblock with no human in the loop.

    Privilege guard: Board Leads cannot set flags on each other (only self
    or a worker). Same pattern as the /plugins endpoint.
    """
    if not agent.is_board_lead:
        raise HTTPException(
            status_code=403,
            detail="Nur Board Leads duerfen workflow-flags setzen",
        )
    target = await session.get(Agent, target_agent_id)
    if not target:
        raise HTTPException(status_code=404, detail="Ziel-Agent nicht gefunden")
    if target.board_id != agent.board_id:
        raise HTTPException(
            status_code=403,
            detail="Ziel-Agent gehoert zu einem anderen Board",
        )
    if target.is_board_lead and target.id != agent.id:
        raise HTTPException(
            status_code=403,
            detail="Board-Lead-Agents koennen einander keine workflow-flags setzen (nur self)",
        )

    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Keine Flags zum Aendern")

    for k, v in changes.items():
        setattr(target, k, v)
    target.updated_at = utcnow()
    session.add(target)
    await session.commit()
    await session.refresh(target)

    await emit_event(
        session,
        "agent.workflow_flags_updated",
        title=f"Workflow-Flags aktualisiert fuer {target.name}: {changes}",
        severity="info",
        board_id=agent.board_id,
        agent_id=agent.id,
        detail={
            "target_agent_id": str(target.id),
            "changes": changes,
            "changed_by": agent.name,
        },
    )
    logger.info(
        "Workflow-Flags fuer %s von %s gesetzt: %s",
        target.name, agent.name, changes,
    )
    return {
        "agent_id": str(target.id),
        "requires_git_workflow": target.requires_git_workflow,
    }


# ─────────────────────────────────────────────────────────────────────────
# Memory Query — 3-stufiges Memory-System (Phase 3, 2026-04-11)
# ─────────────────────────────────────────────────────────────────────────

class MemoryQueryRequest(BaseModel):
    query: str
    layers: list[str] = ["semantic", "agent", "episodic"]
    top_k: int = 5
    agent_id: str | None = None  # "self" | <uuid> | None
    board_id: str | None = None  # "current" | <uuid> | None


@router.post("/memory/query")
async def agent_memory_query(
    payload: MemoryQueryRequest,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.MEMORY_READ)),
):
    """Hybrid vector/keyword search across 3 memory layers.

    Layers:
    - semantic: reusable knowledge (knowledge, decision, concept, reference, research)
    - agent: agent lessons (filtered by agent_id)
    - episodic: time-bound entries (journal, weekly_review, insight)

    Special values:
    - agent_id="self" → substitute the caller's own agent_id
    - board_id="current" → substitute agent.board_id
    """
    # Resolve special values against the agent context BEFORE the helper call
    resolved_agent_id: str | None = None
    if payload.agent_id == "self":
        resolved_agent_id = str(agent.id)
    elif payload.agent_id:
        resolved_agent_id = payload.agent_id

    resolved_board_id: str | None = None
    if payload.board_id == "current":
        resolved_board_id = str(agent.board_id) if agent.board_id else None
    elif payload.board_id:
        resolved_board_id = payload.board_id

    from app.services.memory_query import run_memory_query, InvalidQueryError
    try:
        return await run_memory_query(
            session=session,
            query=payload.query,
            layers=payload.layers,
            top_k=payload.top_k,
            agent_id=resolved_agent_id,
            board_id=resolved_board_id,
        )
    except InvalidQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/boards/{board_id}/memory", status_code=status.HTTP_201_CREATED)
async def agent_create_memory(
    board_id: uuid.UUID,
    payload: MemoryCreate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.MEMORY_WRITE)),
):
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    memory = BoardMemory(board_id=board_id, source=agent.name, **payload.model_dump())
    session.add(memory)
    await session.commit()
    await session.refresh(memory)
    try:
        from app.services.memory_indexing import index_memory
        await index_memory(memory)
    except Exception:
        pass
    return memory


@router.post("/boards/{board_id}/approvals", status_code=status.HTTP_201_CREATED)
async def agent_request_approval(
    board_id: uuid.UUID,
    payload: ApprovalCreate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.APPROVALS_CREATE)),
):
    from datetime import timedelta
    from app.services.autonomy import resolve_autonomy

    # Theme 3: autonomy check — L1/L2 need no approval
    autonomy_level = await resolve_autonomy(payload.action_type)

    if autonomy_level == "L1":
        # Auto-execute: no approval needed
        return {"id": None, "status": "auto_approved", "autonomy_level": "L1"}

    if autonomy_level == "L2":
        # Notify + auto-execute
        await emit_event(
            session, f"autonomy.l2.{payload.action_type}",
            f"[L2] {agent.name}: {payload.description}",
            severity="info",
            board_id=board_id, agent_id=agent.id,
        )
        return {"id": None, "status": "auto_approved", "autonomy_level": "L2"}

    # L3: normal approval flow
    approval = Approval(
        board_id=board_id,
        agent_id=agent.id,
        expires_at=utcnow() + timedelta(hours=24),
        autonomy_level="L3",
        **payload.model_dump(),
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)

    await emit_event(
        session, "approval.created",
        f"{agent.name} requests approval: {payload.description}",
        severity="warning",
        board_id=board_id, agent_id=agent.id,
    )
    return approval


@router.post("/boards/{board_id}/chat", status_code=status.HTTP_201_CREATED)
async def agent_post_chat(
    board_id: uuid.UUID,
    payload: ChatMessageCreate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.CHAT_WRITE)),
):
    msg = ChatMessage(
        channel_type="board",
        board_id=board_id,
        sender_type="agent",
        sender_agent_id=agent.id,
        content=payload.content,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    return msg


# ── Knowledge Base ─────────────────────────────────────────────────────────────

@router.get("/knowledge")
async def agent_list_knowledge(
    memory_type: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.KNOWLEDGE_READ)),
):
    """
    Reads knowledge-base entries relevant to the agent:
    - Own entries (agent_id = this agent)
    - Board memory of the agent's own board
    - Global knowledge (board_id=null, agent_id=null)
    """
    # Scope filter: own + board + global
    scope_conditions = [
        BoardMemory.agent_id == agent.id,  # eigene Eintraege
        and_(BoardMemory.board_id.is_(None), BoardMemory.agent_id.is_(None)),  # type: ignore[attr-defined]  # globale Eintraege
    ]
    if agent.board_id:
        scope_conditions.append(BoardMemory.board_id == agent.board_id)  # Board-Memory
    scope_filter = or_(*scope_conditions)

    query = select(BoardMemory).where(scope_filter)

    if memory_type:
        query = query.where(BoardMemory.memory_type == memory_type)
    if search:
        query = query.where(
            BoardMemory.content.ilike(f"%{search}%")  # type: ignore[attr-defined]
        )

    query = query.order_by(BoardMemory.is_pinned.desc(), BoardMemory.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
    result = await session.exec(query)
    return result.all()


class KnowledgeCreate(BaseModel):
    content: str
    title: str | None = None
    tags: list[str] = []
    memory_type: str = "knowledge"
    is_pinned: bool = False
    scope: str = "agent"  # "agent" | "board" | "global"


@router.post("/knowledge", status_code=status.HTTP_201_CREATED)
async def agent_create_knowledge(
    payload: KnowledgeCreate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.KNOWLEDGE_WRITE)),
):
    """
    Writes an entry to the knowledge base.
    scope="agent"  → agent_id set, no board_id (only this agent sees it)
    scope="board"  → board_id set (all board agents see it)
    scope="global" → neither board_id nor agent_id (everyone sees it)
    """
    board_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None

    if payload.scope == "board":
        if not agent.board_id:
            raise HTTPException(status_code=400, detail="Agent has no board assigned")
        board_id = agent.board_id
    elif payload.scope == "global":
        pass  # beide bleiben None
    else:
        agent_id = agent.id  # default: agent-scoped

    data = payload.model_dump(exclude={"scope"})
    entry = BoardMemory(
        board_id=board_id,
        agent_id=agent_id,
        source=agent.name,
        **data,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    try:
        from app.services.memory_indexing import index_memory
        await index_memory(entry)
    except Exception as e:
        logger.warning("agent_create_knowledge index failed: %s", e)
    return entry


# ── Content Pipeline Agent-Callback: now lives in the News-Studio vertical ──
# (app/verticals/news_studio/routers/content_agent.py — same prefix)


# ── Agent Creation (Board Lead only) ─────────────────────────────────────────


class AgentInstantiateByAgentRequest(BaseModel):
    name: str | None = None
    board_id: uuid.UUID | None = None  # Default: current_agent.board_id


class AgentCreateByAgentRequest(BaseModel):
    name: str
    emoji: str = "🤖"
    role: str | None = None
    model: str | None = None
    skills: list[str] = []
    soul_md: str | None = None
    board_id: uuid.UUID | None = None  # Default: current_agent.board_id

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


@router.get("/templates")
async def agent_list_templates(
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """Lists all available agent templates. Only for agents with agents:manage scope."""
    result = await session.exec(select(AgentTemplate).order_by(AgentTemplate.name))
    return result.all()


@router.post("/templates/{template_id}/instantiate", status_code=status.HTTP_201_CREATED)
async def agent_instantiate_template(
    template_id: uuid.UUID,
    body: AgentInstantiateByAgentRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """
    Creates a new agent from a template — Board Leads only.
    Provisioning happens automatically in the background.
    Returns: { agent, token } — token is visible only once!
    """
    from app.routers.agent_templates import _do_instantiate
    from app.services.provisioning import provision_agent_background as _provision_agent_background

    if not agent.is_board_lead:
        raise HTTPException(status_code=403, detail="Nur Board Leads duerfen neue Agents erstellen")

    template = await session.get(AgentTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template nicht gefunden")

    effective_board_id = body.board_id or agent.board_id

    new_agent, raw_token = await _do_instantiate(
        template=template,
        board_id=effective_board_id,
        name=body.name,
        model=None,
        session=session,
    )

    await emit_event(
        session,
        "agent.created",
        f"Agent {new_agent.name} erstellt von {agent.name} (Template: {template.name})",
        agent_id=new_agent.id,
        board_id=new_agent.board_id,
    )

    # Start provisioning in the background
    background_tasks.add_task(_provision_agent_background, new_agent.id)

    return {
        "agent": new_agent,
        "token": raw_token,
    }


@router.post("/agents", status_code=status.HTTP_201_CREATED)
async def agent_create_custom(
    body: AgentCreateByAgentRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """
    Creates a new agent without a template — Board Leads only.
    Provisioning happens automatically in the background.
    Returns: { agent, token } — token is visible only once!
    """
    from app.auth import generate_agent_token
    from app.routers.agents import _generate_tools_md
    from app.services.provisioning import provision_agent_background as _provision_agent_background

    if not agent.is_board_lead:
        raise HTTPException(status_code=403, detail="Nur Board Leads duerfen neue Agents erstellen")

    effective_board_id = body.board_id or agent.board_id
    board_id_str = str(effective_board_id) if effective_board_id else None

    raw_token, token_hash = generate_agent_token()
    tools_md = _generate_tools_md(body.name, body.emoji, raw_token, board_id_str, scopes=[])

    new_agent = Agent(
        name=body.name,
        emoji=body.emoji,
        role=body.role,
        model=body.model,
        soul_md=body.soul_md,
        skills=body.skills,
        scopes=[],  # Custom Agents: alle Scopes (backward compat)
        board_id=effective_board_id,
        tools_md=tools_md,
        agent_token_hash=token_hash,
        provision_status="local",
    )
    session.add(new_agent)
    await session.commit()
    await session.refresh(new_agent)

    # Vault write mc_token_{slug} for /internal/bootstrap (fresh-install fix).
    from app.services.secrets_helper import upsert_agent_token_secret
    await upsert_agent_token_secret(session, new_agent, raw_token)

    await emit_event(
        session,
        "agent.created",
        f"Agent {new_agent.name} erstellt von {agent.name} (Custom)",
        agent_id=new_agent.id,
        board_id=new_agent.board_id,
    )

    # Start provisioning in the background
    background_tasks.add_task(_provision_agent_background, new_agent.id)

    return {
        "agent": new_agent,
        "token": raw_token,
    }


# ── Checklist CRUD (T-1) ────────────────────────────────────────────────────

class ChecklistItemCreate(BaseModel):
    title: str
    sort_order: int = 0


class ChecklistBulkCreate(BaseModel):
    items: list[ChecklistItemCreate]


class ChecklistItemUpdate(BaseModel):
    status: Literal["pending", "in_progress", "done", "blocked", "skipped"]


@router.post(
    "/boards/{board_id}/tasks/{task_id}/checklist",
    status_code=201,
    dependencies=[Depends(require_scope(Scope.TASKS_WRITE))],
)
async def agent_create_checklist(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: ChecklistBulkCreate,
    response: Response,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Agent creates checklist items for a task (bulk).

    Idempotency guard (2026-07-08 incident fix): a re-dispatched agent
    (container restart, manual resume, blocked→in_progress unblock) gets
    re-instructed to "create its checklist" and used to replay the exact
    same `mc checklist add` calls, duplicating rows (one item created 3x,
    others 2x — which later made `mc finish` fail with a wall of open
    items). Items whose title already exists for this task are treated as
    a no-op and the existing row is returned instead of inserting a
    duplicate. Genuinely new titles (agent discovered an extra step, or
    this is the very first create) still insert normally. If the whole
    payload turned out to be a pure replay, respond 200 instead of 201 so
    callers can tell "nothing changed" from "created new items" without
    parsing the body.
    """
    from app.models.checklist import TaskChecklistItem

    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task nicht gefunden")

    # Dedup only against NON-TERMINAL items (pending/in_progress). An already
    # done/skipped item with the same title must NOT swallow a genuinely new
    # round of that step (e.g. a second "Run tests" pass) — otherwise the new
    # work would be invisible to `mc finish` and recovery. This is only the
    # backstop for a real double-POST of still-open items; the prompt gate in
    # dispatch_message_builder stops re-dispatch from replaying the whole
    # checklist in the first place.
    _NON_TERMINAL = ("pending", "in_progress")
    existing_items = (
        await session.exec(
            select(TaskChecklistItem).where(TaskChecklistItem.task_id == task_id)
        )
    ).all()
    existing_by_title = {
        i.title.strip().lower(): i
        for i in existing_items
        if i.status in _NON_TERMINAL
    }

    items = []
    new_count = 0
    for item_data in payload.items:
        key = item_data.title.strip().lower()
        dup = existing_by_title.get(key)
        if dup is not None:
            items.append(dup)
            continue
        item = TaskChecklistItem(
            task_id=task_id,
            agent_id=agent.id,
            title=item_data.title,
            sort_order=item_data.sort_order,
        )
        session.add(item)
        items.append(item)
        existing_by_title[key] = item  # dedup within the same payload too
        new_count += 1

    if new_count == 0:
        # Pure replay — nothing new to persist, tell the agent it's a no-op.
        response.status_code = status.HTTP_200_OK
        return items

    # Update counters
    task.checklist_total = task.checklist_total + new_count
    session.add(task)
    await session.commit()
    for item in items:
        await session.refresh(item)
    return items


@router.patch(
    "/boards/{board_id}/tasks/{task_id}/checklist/{item_id}",
    dependencies=[Depends(require_scope(Scope.TASKS_WRITE))],
)
async def agent_update_checklist_item(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: ChecklistItemUpdate,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Agent sets the status of a checklist item."""
    from app.models.checklist import TaskChecklistItem

    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    # Validate task belongs to this board
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task nicht gefunden")

    item = await session.get(TaskChecklistItem, item_id)
    if not item or item.task_id != task_id:
        raise HTTPException(status_code=404, detail="Checklist-Item nicht gefunden")

    old_status = item.status
    item.status = payload.status
    if payload.status == "done" and old_status != "done":
        item.completed_at = utcnow()
    elif payload.status != "done":
        item.completed_at = None

    session.add(item)

    # Flush so the updated status is visible to the counter query
    await session.flush()

    # Recalculate counters from DB (post-flush, so updated status is visible)
    result = await session.exec(
        select(TaskChecklistItem).where(TaskChecklistItem.task_id == task_id)
    )
    all_items = result.all()
    task.checklist_total = len(all_items)
    task.checklist_done = sum(1 for i in all_items if i.status == "done")
    session.add(task)

    await session.commit()
    await session.refresh(item)
    return item


@router.get(
    "/boards/{board_id}/tasks/{task_id}/checklist",
    dependencies=[Depends(require_scope(Scope.TASKS_READ))],
)
async def agent_get_checklist(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Agent reads a task's checklist (for recovery)."""
    from app.models.checklist import TaskChecklistItem

    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task nicht gefunden")

    result = await session.exec(
        select(TaskChecklistItem)
        .where(TaskChecklistItem.task_id == task_id)
        .order_by(TaskChecklistItem.sort_order)
    )
    return result.all()


# ── Discord Delivery ──────────────────────────────────────────────────────────

class DiscordSendBody(BaseModel):
    channel_id: str
    content: str


@router.post("/discord/send", dependencies=[Depends(require_scope(Scope.CHAT_WRITE))])
async def agent_send_discord(
    body: DiscordSendBody,
    agent: Agent = Depends(require_agent),
):
    """Agent sends a text message to a Discord channel."""
    from app.services.discord import send_to_discord_channel

    await send_to_discord_channel(body.channel_id, content=body.content)


class VisualVerifyInteraction(BaseModel):
    """A single browser interaction before the screenshot (click/fill/wait_for/...).

    Pydantic lets extra fields through — this schema must stay in sync with
    InteractionSpec in mc-playwright/service.py.
    """
    action: str  # click | fill | wait_for | scroll_to | evaluate | press
    selector: str | None = None
    value: str | None = None
    script: str | None = None
    wait_after_ms: int = 300


class VisualVerifyLogin(BaseModel):
    """Inline form login (alternative to credential_id)."""
    url: str
    username: str
    password: str
    username_selector: str | None = None
    password_selector: str | None = None
    submit_selector: str | None = None
    wait_for_url: str | None = None
    wait_for_selector: str | None = None


class VisualVerifyBody(BaseModel):
    """Body for /agent/tasks/{task_id}/visual-verify — calls mc-playwright + Telegram."""
    url: str
    viewports: list[str] = ["desktop", "mobile"]
    scroll: bool = True
    metrics: bool = True
    send_to_telegram: bool = True
    caption: str | None = None  # HTML caption on the first image

    # --- Interaktions-Mode (2026-04-23) ---------------------------------------
    credential_id: uuid.UUID | None = None   # Vault-Resolve (Login) — Backend
    auth_token: str | None = None            # JWT direkt in localStorage
    login: VisualVerifyLogin | None = None   # Inline form login without vault
    interactions: list[VisualVerifyInteraction] = []
    wait_for_selector: str | None = None     # Final wait before screenshot
    full_page: bool = True                   # False: viewport-only (for modals)
    force_telegram_resend: bool = False      # Override per-task Dedup — sendet auch bei 2.+ run


class TelegramReportBody(BaseModel):
    """Body for an agent-initiated Telegram report to the operator."""
    text: str
    # Optional: task context for flag setting. In subagent-dispatch mode,
    # workers have no agent.current_task_id — so the CLI must send the task
    # ID along (from /tmp/mc-context.env that poll.sh writes). For Board
    # Leads, the fallback to current_task_id is sufficient.
    task_id: uuid.UUID | None = None
    # Optional: attach a screenshot deliverable to Telegram as sendPhoto (instead
    # of plain sendMessage). text becomes the photo caption (1024 chars max — longer
    # will be truncated). Deliverable must be type=screenshot and have a
    # resolvable file. Use case: Tester/Deployer took a screenshot via MCP
    # → registered via mc deliverable → mc telegram --photo <id> sends the
    # actual image to the operator's reports chat (instead of just a text
    # description).
    deliverable_id: uuid.UUID | None = None
    # Optional: attach a file deliverable (PDF/Excel/PowerPoint/Word/ZIP/...)
    # as sendDocument. Mutually exclusive with deliverable_id (can't send
    # both at once). Accepts all deliverable_types except `url` and `data`
    # (they have no resolvable path). Unlike sendPhoto, the file is not
    # compressed — ideal for office documents.
    document_deliverable_id: uuid.UUID | None = None
    # Optional: vault-relative path to a deliverable wrapper under
    # ~/.mc/vault/agents/{slug}/deliverables/*.md. Backend reads the wrapper
    # frontmatter, resolves attachment_path to the actual binary under
    # vault/attachments/{files,images,audio}/ and sends it via sendDocument.
    # Use case: Voice-Concierge gets vault_search hits back and only knows
    # the wrapper path, not the deliverable_id — this path lets it do the
    # Telegram send in one call. Mutually exclusive with deliverable_id and
    # document_deliverable_id.
    vault_path: str | None = None


class PdfGenerateBody(BaseModel):
    """Body for agent-initiated PDF generation via mc-playwright."""
    title: str
    # Either markdown OR html (not both). Default stylesheet is optimized
    # for research reports / deliverable docs (Geist-like typography,
    # neutral colors, no AI-slop).
    markdown: str | None = None
    html: str | None = None
    custom_css: str | None = None
    filename_prefix: str = "report"
    description: str | None = None


class MePdfBody(PdfGenerateBody):
    """PdfGenerateBody + optional task_id override for /me/pdf."""
    task_id: uuid.UUID | None = None


@router.post(
    "/boards/{board_id}/tasks/{task_id}/pdf",
    dependencies=[Depends(require_scope(Scope.TASKS_WRITE))],
)
async def agent_generate_pdf(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    body: PdfGenerateBody,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Markdown or HTML → PDF via the central mc-playwright sidecar.

    Zero-setup alternative to local puppeteer/chromium in the agent
    container. Avoids ARM/Rosetta conflicts (Incident 2026-04-23: FreeCode
    hung for 2+h in a download cascade because x86 binaries failed in the
    ARM container).

    The sidecar renders the PDF with page.pdf() in Chromium (ARM-native,
    always available) and writes to /shared-deliverables/<task_id>/. The
    backend automatically registers the PDF as a TaskDeliverable (type=file).

    Response: {deliverable_id, path, title, bytes, pages}
    """
    # Task check (agent may only address its own/assigned tasks)
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} nicht gefunden.")
    # Board-Isolation
    if agent.board_id != task.board_id:
        raise HTTPException(status_code=403, detail="Task gehoert nicht zu deinem Board.")
    # Ownership: assigned_agent, owner, oder Board-Lead
    if (
        task.assigned_agent_id != agent.id
        and task.owner_agent_id != agent.id
        and not agent.is_board_lead
    ):
        raise HTTPException(
            status_code=403,
            detail="Du bist nicht der zugewiesene Agent dieses Tasks (und nicht Board Lead).",
        )

    if not body.markdown and not body.html:
        raise HTTPException(status_code=422, detail="Entweder 'markdown' ODER 'html' mitschicken.")
    if body.markdown and body.html:
        raise HTTPException(status_code=422, detail="'markdown' und 'html' schliessen sich aus.")

    from app.services.pdf_generator import generate_and_register_pdf

    try:
        deliverable = await generate_and_register_pdf(
            session,
            task_id=task_id,
            agent_id=agent.id,
            markdown=body.markdown,
            html=body.html,
            title=body.title,
            filename_prefix=body.filename_prefix,
            custom_css=body.custom_css,
            description=body.description,
        )
    except httpx.HTTPStatusError as e:
        detail = e.response.text[:500] if e.response is not None else str(e)
        raise HTTPException(
            status_code=502,
            detail=f"mc-playwright Sidecar antwortete {e.response.status_code if e.response else '?'}: {detail}",
        )
    except Exception as e:
        logger.exception("PDF generation failed task=%s: %s", task_id, e)
        raise HTTPException(
            status_code=503,
            detail=f"PDF-Generation fehlgeschlagen ({type(e).__name__}: {e}). "
                   f"Pruefe ob mc-playwright Sidecar laeuft (docker ps | grep mc-playwright).",
        )

    logger.info(
        "PDF erzeugt: task=%s agent=%s title=%s deliverable=%s",
        task_id, agent.name, body.title, deliverable.id,
    )

    # Re-read content length for the response payload (the file on disk is
    # the source of truth)
    import os as _os
    try:
        file_bytes = _os.path.getsize(deliverable.path) if deliverable.path else 0
    except OSError:
        file_bytes = 0

    return {
        "ok": True,
        "deliverable_id": str(deliverable.id),
        "path": deliverable.path,
        "title": deliverable.title,
        "bytes": file_bytes,
    }


@router.post(
    "/tasks/{task_id}/visual-verify",
    dependencies=[Depends(require_scope(Scope.CHAT_WRITE))],
)
async def agent_visual_verify(
    task_id: uuid.UUID,
    body: VisualVerifyBody,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Screenshots + metrics via mc-playwright + optional Telegram photo attachment.

    Calls the central mc-playwright service (no more per-agent browser
    setup needed). Registers each screenshot as a TaskDeliverable.
    If send_to_telegram=True, also sends the screenshots as a media group
    to the operator's reports chat.
    """
    from app.services.visual_verifier import (
        verify_url, register_screenshots_as_deliverables,
        send_screenshots_to_telegram, format_metrics_summary,
    )

    # Load task + ownership check
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task nicht gefunden")
    _same_board = agent.board_id == task.board_id
    _is_owner = task.assigned_agent_id == agent.id or task.owner_agent_id == agent.id
    if not (_is_owner or (agent.is_board_lead and _same_board)):
        raise HTTPException(
            403, "Du bist nicht assigned, owner oder same-board Lead auf diesem Task."
        )

    # --- Vault resolve on credential_id --------------------------------------
    # If the agent sends a credential reference, we resolve the encrypted
    # credential to a form-login dict. The agent may only do this if it has
    # the CREDENTIALS_READ scope — otherwise 403.
    resolved_login: dict | None = None
    if body.credential_id is not None:
        from app.scopes import Scope, get_agent_effective_scopes
        effective = get_agent_effective_scopes(agent)
        if Scope.CREDENTIALS_READ.value not in effective:
            raise HTTPException(
                403,
                "credential_id benoetigt Scope 'credentials:read'.",
            )
        import json as _json
        from app.models.credential import Credential
        from app.services.encryption import safe_decrypt

        cred = await session.get(Credential, body.credential_id)
        if cred is None:
            raise HTTPException(404, f"Credential {body.credential_id} nicht gefunden")
        if cred.credential_type != "login":
            raise HTTPException(
                422,
                f"Credential ist type='{cred.credential_type}' — nur 'login' wird fuer visual-verify unterstuetzt.",
            )
        decrypted = safe_decrypt(cred.encrypted_data)
        data = _json.loads(decrypted) if decrypted else {}
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            raise HTTPException(500, "Credential hat kein username/password Feld.")
        if not cred.url:
            raise HTTPException(
                422,
                "Credential hat keine url — wird fuer Login-Page benoetigt. "
                "Setze credential.url oder nutze login.url inline.",
            )
        resolved_login = {
            "url": cred.url,
            "username": username,
            "password": password,
        }

    # Inline login wins over resolved_login (if both are set)
    login_payload: dict | None = None
    if body.login is not None:
        login_payload = body.login.model_dump(exclude_none=True)
    elif resolved_login is not None:
        login_payload = resolved_login

    interactions_payload = [i.model_dump(exclude_none=True) for i in body.interactions] or None

    # Call mc-playwright — can take 10-60s (login + interactions)
    try:
        result = await verify_url(
            url=body.url,
            task_id=task_id,
            viewports=body.viewports,
            scroll=body.scroll,
            metrics=body.metrics,
            auth_token=body.auth_token,
            login=login_payload,
            interactions=interactions_payload,
            wait_for_selector=body.wait_for_selector,
            full_page=body.full_page,
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            502,
            f"mc-playwright Service-Fehler: {e.response.status_code} — {e.response.text[:200]}",
        )
    except httpx.HTTPError as e:
        raise HTTPException(503, f"mc-playwright unreachable: {e}")

    # Bug B (2026-04-23): evaluate the login-success check from the
    # mc-playwright service. If the form login actually failed (page stayed
    # on the login URL), the screenshot is worthless — the agent would
    # otherwise interpret the login mask as a "logged-in state" (see Bug A).
    # Hard-abort here with a clear error message instead of returning the
    # image + ok=true.
    login_info = result.get("login") if isinstance(result, dict) else None
    if login_info and login_info.get("succeeded") is False:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "form_login_failed",
                "message": (
                    "Form-Login an mc-playwright fehlgeschlagen — Page blieb auf "
                    "der Login-URL. Vermutlich wurden Username/Password vom "
                    "Backend abgelehnt, oder der Auth-Guard hat redirected. "
                    "Der Screenshot zeigt vermutlich die Login-Maske statt "
                    "der gewuenschten Page."
                ),
                "final_url": login_info.get("final_url"),
                "reason": login_info.get("reason"),
                "fix_hints": [
                    "Pruefe ob die Vault-Credential das richtige Password fuer den User hat.",
                    "Pruefe ob Backend das Login akzeptiert: curl -X POST <login_url> ...",
                    "Bei false-positive (Login klappte aber Path-Check zu pessimistisch): "
                    "setze login.wait_for_url oder login.wait_for_selector im Body.",
                ],
            },
        )

    # Deliverables registrieren
    deliverables = await register_screenshots_as_deliverables(
        session, task_id, agent.id, result,
    )

    # --- Telegram send with per-task dedup -----------------------------------
    # Problem (2026-04-23 bug): agents often make multiple visual-verify calls
    # per task (self-verification, fresh re-take, retry after timeout). Each
    # call defaulted to sending to Telegram → the operator got the same modal 3-5x.
    # Fix: Redis dedup per task. First call sends, follow-up calls log a skip.
    # Opt-out via `force_telegram_resend=True` for legitimate re-sends.
    tg_result = None
    tg_sent = False
    tg_skipped_reason: str | None = None

    # G4 (Incident 2026-07-04): Operator bekam dieselben Screenshots doppelt —
    # zwei Agenten (Selbst-Check nach Deploy + offizielle QA) verifizierten
    # dieselbe URL im Abstand von 2 Minuten; der Dedup war nur pro Task.
    # 1) Rollen-Default: Telegram nur vom designierten Verifier (QA/Test/
    #    Review-Rolle oder Board-Lead). Selbst-Checks anderer Agenten laufen
    #    silent — die Screenshots sind trotzdem als Deliverables registriert.
    # 2) URL-Fenster: gleiche URL im selben Board → max. 1 Telegram-Meldung
    #    pro 30min, egal von welchem Task.
    # Override fuer beide: force_telegram_resend=true.
    def _is_designated_verifier() -> bool:
        if agent.is_board_lead:
            return True
        role_text = (agent.role or "").lower()
        return any(k in role_text for k in ("test", "qa", "review", "verify"))

    if body.send_to_telegram and not _is_designated_verifier() and not body.force_telegram_resend:
        tg_skipped_reason = "not_verifier"
        logger.info(
            "visual-verify telegram skipped: agent=%s ist kein designierter "
            "Verifier (Selbst-Check laeuft silent). "
            "Use force_telegram_resend=true to override.",
            agent.name,
        )
    elif body.send_to_telegram:
        # Redis dedup — fail-open on Redis errors (container issue / tests),
        # so the Telegram send doesn't fail without reason.
        import hashlib as _hashlib
        redis = None
        already_sent = False
        url_recently_sent = False
        url_key = None
        try:
            from app.redis_client import get_redis as _get_redis
            redis = await _get_redis()
            dedup_key = f"mc:visual_verify:telegram_sent:{task_id}"
            already_sent = bool(await redis.get(dedup_key))
            _url_hash = _hashlib.sha256(body.url.encode()).hexdigest()[:16]
            _board_scope = agent.board_id or "global"
            url_key = f"mc:visual_verify:telegram_url:{_board_scope}:{_url_hash}"
            url_recently_sent = bool(await redis.get(url_key))
        except Exception as e:  # noqa: BLE001
            logger.warning("visual-verify dedup redis unavailable — sending anyway: %s", e)
            redis = None

        if already_sent and not body.force_telegram_resend:
            tg_skipped_reason = "already_sent"
            logger.info(
                "visual-verify telegram dedup: task=%s already sent (agent=%s), skipping. "
                "Use force_telegram_resend=true to override.",
                task_id, agent.name,
            )
        elif url_recently_sent and not body.force_telegram_resend:
            tg_skipped_reason = "url_recently_sent"
            logger.info(
                "visual-verify telegram dedup: URL wurde in den letzten 30min "
                "bereits an Telegram gemeldet (board-weit), skipping. agent=%s",
                agent.name,
            )
        else:
            caption_html = body.caption or ""
            metrics_block = format_metrics_summary(result)
            if metrics_block:
                caption_html = (caption_html + "\n\n" + metrics_block).strip() if caption_html else metrics_block
            tg_result = await send_screenshots_to_telegram(result, caption=caption_html or None)
            tg_sent = tg_result is not None and (tg_result.get("ok") if isinstance(tg_result, dict) else False)
            if tg_sent and redis is not None:
                # Task-Key TTL 24h — long enough for a normal task lifecycle,
                # short enough that tasks reopened after a long time still get
                # 1 send. URL-Key TTL 30min (board-weites Doppel-Fenster, G4).
                try:
                    await redis.set(dedup_key, "1", ex=24 * 3600)
                    if url_key is not None:
                        await redis.set(url_key, "1", ex=30 * 60)
                except Exception as e:  # noqa: BLE001
                    logger.warning("visual-verify dedup redis.set failed: %s", e)

    return {
        "ok": True,
        "screenshots": result.get("screenshots", []),
        "scroll_shots": result.get("scroll_shots", []),
        "metrics": result.get("metrics"),
        "deliverables_registered": len(deliverables),
        "telegram_sent": tg_sent,
        "telegram_skipped": tg_skipped_reason,
    }


@router.post(
    "/telegram/send",
    dependencies=[Depends(require_scope(Scope.CHAT_WRITE))],
)
async def agent_send_telegram_report(
    body: TelegramReportBody,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Agent sends a report message to the operator's reports Telegram chat.

    Use this endpoint for info delivery at task end (summary, deliverable
    list, recommendation). NO status spam — only reports relevant at the end.
    HTML parse mode: use `<b>`, `<i>`, `<code>`, `<a href="...">...</a>`.

    Side effect: if the agent has an active task (current_task_id),
    `task.report_sent_to_telegram = True` is set — so the `mc done` guard
    knows the report-back contract is fulfilled.
    """
    from app.services.telegram_reports import telegram_reports

    if not telegram_reports.configured:
        raise HTTPException(
            status_code=503,
            detail=(
                "Reports-Bot nicht konfiguriert. Der Operator muss "
                "TELEGRAM_REPORTS_BOT_TOKEN + TELEGRAM_REPORTS_CHAT_ID setzen."
            ),
        )

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text darf nicht leer sein.")
    if len(text) > 4000:
        raise HTTPException(
            status_code=422,
            detail="Telegram-Limit: max. 4000 Zeichen pro Message.",
        )

    _attachment_fields = sum(
        1 for v in (body.deliverable_id, body.document_deliverable_id, body.vault_path) if v
    )
    if _attachment_fields > 1:
        raise HTTPException(
            status_code=422,
            detail=(
                "deliverable_id (Photo), document_deliverable_id (File) und vault_path "
                "(Vault-Wrapper) schliessen sich aus — bitte nur eines mitgeben."
            ),
        )

    # Task-Context aufloesen: body_task_id > current_task_id (Board Lead) > spawn_session_key (Worker)
    resolved_task: Task | None = await _resolve_active_task_for_agent(
        agent, body.task_id, session, required=False
    )

    # Routing rule "whoever dispatches, sends" (subtask-send guard, 2026-05-17):
    # If the agent is working on a subtask (parent_task_id NOT NULL), the
    # final Telegram send belongs to the orchestrator (Boss), not the
    # worker. Otherwise the operator gets a worker message AND a Boss
    # consolidation message = duplicate. Exception: long-running watch tasks
    # where Boss explicitly sets autonomous_telegram=True — then the worker
    # may report autonomously.
    if (
        resolved_task is not None
        and resolved_task.parent_task_id is not None
        and not resolved_task.autonomous_telegram
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Subtask sendet kein Telegram an den Operator — der Orchestrator (Parent-Task) "
                "konsolidiert + sendet final. Liefere stattdessen: `mc deliverable` + "
                "Reflection-Comment + `mc done`. Wenn Boss bewusst Autonomie wollte, "
                "muesste der Brief das Flag `autonomous_telegram=true` enthalten."
            ),
        )

    # Flag claim BEFORE Telegram send (race protection C4 + duplicate protection C5):
    # Atomic UPDATE WHERE flag=false — rowcount tells us whether we "won".
    # If the flag is already true: let the send through anyway (idempotency),
    # but don't set it again.
    from sqlalchemy import update as _sa_update
    claimed = False
    if resolved_task is not None and not resolved_task.report_sent_to_telegram:
        upd = await session.exec(
            _sa_update(Task)
            .where(Task.id == resolved_task.id, Task.report_sent_to_telegram == False)  # noqa: E712
            .values(report_sent_to_telegram=True)
        )
        await session.commit()
        claimed = upd.rowcount == 1

    async def _rollback_claim() -> None:
        """Take back the flag so the agent can retry. Best-effort, never raises."""
        if not claimed or resolved_task is None:
            return
        try:
            await session.exec(
                _sa_update(Task)
                .where(Task.id == resolved_task.id)
                .values(report_sent_to_telegram=False)
            )
            await session.commit()
        except Exception as _rb_exc:
            logger.warning(
                "Claim-Rollback fehlgeschlagen fuer Task %s: %s — Agent kann Gate nicht retryen",
                resolved_task.id, _rb_exc,
            )

    # If deliverable_id is given: photo send instead of plain text. Lets
    # agents (Tester etc.) attach the actual image to the operator instead of
    # just a text description. Caption is limited to 1024 characters
    # (Telegram limit — send_photo truncates automatically).
    photo_path: str | None = None
    if body.deliverable_id is not None:
        from app.models.deliverable import TaskDeliverable
        from app.routers.tasks import _resolve_deliverable_fs_path

        deliverable = await session.get(TaskDeliverable, body.deliverable_id)
        if deliverable is None:
            raise HTTPException(
                status_code=404,
                detail=f"Deliverable {body.deliverable_id} nicht gefunden.",
            )
        if deliverable.deliverable_type != "screenshot":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"deliverable_id verweist auf type='{deliverable.deliverable_type}' — "
                    "nur 'screenshot' kann als Telegram-Photo angehaengt werden."
                ),
            )
        # Ownership check: agent may only send its own deliverables (or its board's)
        if (
            deliverable.agent_id != agent.id
            and not agent.is_board_lead
        ):
            raise HTTPException(
                status_code=403,
                detail="Du darfst nur eigene Deliverables als Telegram-Photo senden (oder Board Lead sein).",
            )
        photo_path = await _resolve_deliverable_fs_path(deliverable, session)
        if not photo_path:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Deliverable {body.deliverable_id} hat keinen aufloesbaren File-Pfad "
                    f"(path={deliverable.path!r}). Telegram-Photo nicht moeglich."
                ),
            )

    # If document_deliverable_id is given: sendDocument (PDF/Office/ZIP/...).
    # Unlike sendPhoto, the file is NOT compressed — ideal for all non-image
    # files where pixel accuracy or the original format matters.
    document_path: str | None = None
    if body.document_deliverable_id is not None:
        from app.models.deliverable import TaskDeliverable
        from app.routers.tasks import _resolve_deliverable_fs_path

        deliverable = await session.get(TaskDeliverable, body.document_deliverable_id)
        if deliverable is None:
            raise HTTPException(
                status_code=404,
                detail=f"Deliverable {body.document_deliverable_id} nicht gefunden.",
            )
        # url/data have no path — pointless to send as a document
        if deliverable.deliverable_type in ("url", "data"):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"deliverable_id verweist auf type='{deliverable.deliverable_type}' — "
                    "url/data haben keinen File-Pfad und koennen nicht als "
                    "Telegram-Document gesendet werden."
                ),
            )
        # Ownership check: same as the photo path
        if (
            deliverable.agent_id != agent.id
            and not agent.is_board_lead
        ):
            raise HTTPException(
                status_code=403,
                detail="Du darfst nur eigene Deliverables als Telegram-Document senden (oder Board Lead sein).",
            )
        document_path = await _resolve_deliverable_fs_path(deliverable, session)
        if not document_path:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Deliverable {body.document_deliverable_id} hat keinen aufloesbaren "
                    f"File-Pfad (path={deliverable.path!r}). Telegram-Document nicht moeglich."
                ),
            )

    # If vault_path is given: wrapper-based delivery. Voice-Concierge only
    # knows the wrapper path from vault_search, not the deliverable_id.
    # We read the wrapper frontmatter, resolve attachment_path to the actual
    # binary under /vault/attachments/{files,images,audio}/ and send it
    # via sendDocument. body.text becomes the caption.
    if body.vault_path is not None:
        from app.config import settings as _settings
        from app.helpers.vault_frontmatter import FrontmatterError, parse_frontmatter
        from app.routers.vault import _safe_path as _vault_safe_path

        vault_root = _settings.vault_path.resolve()
        wrapper_abs = _vault_safe_path(body.vault_path, _settings.vault_path)
        if not wrapper_abs.exists() or not wrapper_abs.is_file():
            raise HTTPException(status_code=404, detail=f"Vault-Wrapper nicht gefunden: {body.vault_path}")
        try:
            post = parse_frontmatter(wrapper_abs)
        except FrontmatterError as exc:
            raise HTTPException(status_code=422, detail=f"Wrapper-Frontmatter ungueltig: {exc}")

        attachment_rel = post.metadata.get("attachment_path")
        if not attachment_rel:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Wrapper hat keinen attachment_path — kinds 'document' und 'url' "
                    "haben keine Binary zum Versand. Voice soll Inhalt inline lesen."
                ),
            )

        candidate = (wrapper_abs.parent / str(attachment_rel)).resolve()
        try:
            candidate.relative_to(vault_root)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="attachment_path verlaesst den Vault-Root",
            )
        if not candidate.exists() or not candidate.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"attachment fehlt auf der Disk: {attachment_rel}",
            )

        document_path = str(candidate)

    # Telegram send AFTER flag claim — try/except also catches httpx exceptions (B1-Fix)
    try:
        if photo_path is not None:
            # Caption = text (truncated to 1024 by telegram_reports.send_photo)
            result = await telegram_reports.send_photo(photo_path, caption=text)
        elif document_path is not None:
            result = await telegram_reports.send_document(document_path, caption=text)
        else:
            result = await telegram_reports.send(text)
    except Exception as send_exc:
        # Network/timeout/HTTP-level errors — roll back the flag so the agent can retry
        logger.warning(
            "Telegram-Send Exception (%s): %s — rolling back flag claim",
            type(send_exc).__name__, send_exc,
        )
        await _rollback_claim()
        raise HTTPException(
            status_code=503,
            detail=f"Telegram-Send fehlgeschlagen ({type(send_exc).__name__}). Retry moeglich.",
        )

    send_failed = result is None or not result.get("ok")
    if send_failed:
        await _rollback_claim()
        if result is None:
            raise HTTPException(status_code=503, detail="Telegram-Send fehlgeschlagen.")
        raise HTTPException(
            status_code=422,
            detail=f"Telegram API: {result.get('description', 'unbekannter Fehler')}",
        )

    logger.info(
        "Telegram-Report von %s gesendet (%d chars, task=%s, claimed=%s)",
        agent.name, len(text),
        resolved_task.id if resolved_task else None,
        claimed,
    )
    return {
        "ok": True,
        "message_id": result.get("result", {}).get("message_id"),
    }


# ── /me/* Auto-Task-Resolution Endpoints ─────────────────────────────────────
# Agent no longer has to manually put a task UUID into the URL.
# Backend resolviert via: body.task_id > current_task_id > spawn_session_key.

@router.post(
    "/me/pdf",
    dependencies=[Depends(require_scope(Scope.TASKS_WRITE))],
)
async def agent_me_generate_pdf(
    body: MePdfBody,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Generate a PDF — active task resolved automatically.

    No board_id/task_id needed in the URL. Backend resolves via
    current_task_id (Board Leads) or spawn_session_key (workers/cli-bridge).
    Optional: task_id in the body as an explicit override (with ownership check).
    """
    task = await _resolve_active_task_for_agent(agent, body.task_id, session)
    return await agent_generate_pdf(
        board_id=task.board_id,
        task_id=task.id,
        body=body,
        agent=agent,
        session=session,
    )


@router.post(
    "/me/deliverable",
    status_code=status.HTTP_201_CREATED,
)
async def agent_me_create_deliverable(
    payload: MeDeliverableCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_WRITE)),
):
    """Register a deliverable — active task resolved automatically.

    No board_id/task_id needed in the URL. Backend resolves via
    current_task_id (Board Leads) or spawn_session_key (workers/cli-bridge).
    Optional: task_id in the body as an explicit override (with ownership check).
    """
    task = await _resolve_active_task_for_agent(agent, payload.task_id, session)
    return await agent_create_deliverable(
        board_id=task.board_id,
        task_id=task.id,
        payload=payload,
        background_tasks=background_tasks,
        session=session,
        agent=agent,
    )


@router.post(
    "/me/telegram",
    dependencies=[Depends(require_scope(Scope.CHAT_WRITE))],
)
async def agent_me_send_telegram_report(
    body: TelegramReportBody,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Send a Telegram report — /me/*-consistent path.

    Identical behavior to POST /telegram/send (task resolution via
    body.task_id > current_task_id > spawn_session_key is already integrated
    there). This endpoint exists for API consistency — all three delivery
    actions under /me/*.
    """
    return await agent_send_telegram_report(body=body, agent=agent, session=session)


# ── Credentials Read (agent-scoped) ──────────────────────────────────────────

@router.get(
    "/boards/{board_id}/credentials",
    dependencies=[Depends(require_scope(Scope.CREDENTIALS_READ))],
)
async def agent_list_credentials(
    board_id: uuid.UUID,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Agent lists all available credentials (masked, sorted alphabetically).

    Credentials are global (not board-scoped) — board_id is only for URL consistency.
    Returns data_masked (last 4 characters visible, rest asterisks).
    """
    import json
    from app.models.credential import Credential
    from app.services.encryption import safe_decrypt
    from app.routers.credentials import _mask_data

    result = await session.exec(select(Credential).order_by(Credential.name))
    credentials = result.all()

    items = []
    for c in credentials:
        decrypted = safe_decrypt(c.encrypted_data)
        data = json.loads(decrypted) if decrypted else {}
        items.append({
            "id": str(c.id),
            "name": c.name,
            "credential_type": c.credential_type,
            "data_masked": _mask_data(data, c.credential_type),
            "url": c.url,
            "notes": c.notes,
        })
    return items


@router.get(
    "/boards/{board_id}/credentials/{credential_id}",
    dependencies=[Depends(require_scope(Scope.CREDENTIALS_READ))],
)
async def agent_get_credential(
    board_id: uuid.UUID,
    credential_id: uuid.UUID,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Agent fetches a single credential, fully decrypted.

    Returns the decrypted data dict (no masking).
    404 if the credential doesn't exist.
    """
    import json
    from app.models.credential import Credential
    from app.services.encryption import safe_decrypt

    credential = await session.get(Credential, credential_id)
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")

    decrypted = safe_decrypt(credential.encrypted_data)
    data = json.loads(decrypted) if decrypted else {}

    return {
        "id": str(credential.id),
        "name": credential.name,
        "credential_type": credential.credential_type,
        "data": data,
        "url": credential.url,
        "notes": credential.notes,
    }
