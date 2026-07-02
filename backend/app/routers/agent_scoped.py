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
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status

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
    your_status: str  # "blocked" wenn callback=True, sonst "in_progress"


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
    """Boss fragt den Operator ob er einen neuen CLI-Agent spawnen darf."""
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
    cli_plugins: list[str] | None  # None = alle, [] = keine, [...] = Allowlist
    restart_worker: bool = False   # True → nach Disk-Sync Worker-Session reloaden
                                   # (claude/openclaude liest settings.json nur
                                   # beim Start — ohne Restart aktivieren sich
                                   # neue Plugins erst nach naechstem Container-
                                   # Restart oder /clear). Default false damit
                                   # Boss bewusst entscheidet ob der aktuelle
                                   # Task-Kontext verloren gehen darf.


# VALID_BLOCKER_TYPES is now imported at the top of this module from
# app.services.work_context (Phase 4 REF-02 Plan 04-04). Single source of truth.

# Single Source of Truth: app/comment_types.py (REL-01). Aliasing erhaelt
# den historischen Import-Namen `VALID_COMMENT_TYPES` fuer bestehende Tests
# (test_phase_approval.py etc.). Wer einen neuen comment_type braucht
# → app/comment_types.py editieren, NICHT hier.
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
        # Workers mit isolierten Sessions: current_task_id nicht per Heartbeat setzen
        from app.config import settings as _hb_settings
        if not (_hb_settings.use_subagent_dispatch and not agent.is_board_lead):
            agent.current_task_id = payload.current_task_id
    if payload.status is not None:
        agent.status = payload.status

    # Model Usage Tracking V1 (Theme 4: Wave 2)
    # Speichert nur das aktive Modell als Snapshot — kein kumulierter Counter.
    if payload.model_id:
        try:
            from app.redis_client import get_redis as _get_redis
            _redis = await _get_redis()
            await _redis.set(
                f"mc:agent:{agent.id}:heartbeat_model",
                payload.model_id,
                ex=900,  # 15min TTL — verfaellt wenn Agent offline
            )
        except Exception as e:
            logger.warning("Heartbeat model_id save failed for %s: %s", agent.name, e)

    agent.last_seen_at = utcnow()
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()

    # Agent kommt nach Neustart zurueck
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
    reason: str | None = None  # Warum die Aenderung? (fuer Activity-Log)


@router.put("/config/soul_md")
async def agent_update_own_soul(
    payload: AgentSoulUpdate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """Agent aktualisiert sein eigenes SOUL.md in MC DB + Gateway/Disk.

    Nur fuer Agents mit agents:manage Scope (Board Leads).
    Aenderung wird als Activity-Event geloggt damit der Operator sie sieht.
    """
    old_length = len(agent.soul_md or "")
    agent.soul_md = payload.content
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()

    # Gateway-Sync entfernt (Phase 29). Disk-Persistence (cli-bridge / host)
    # ist die alleinige Wahrheit; openclaw-Runtime wird nicht mehr unterstuetzt.
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

    # Activity-Event (Der Operator sieht die Aenderung)
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
    """Agent liest sein eigenes SOUL.md."""
    return {"content": agent.soul_md or ""}


@router.patch("/me/memory")
async def agent_update_memory(
    payload: AgentMemoryUpdate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.MEMORY_WRITE)),
):
    """Agent aktualisiert seine eigene MEMORY.md in MC DB + Gateway."""
    agent.memory_md = payload.content
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()

    # Gateway-Sync entfernt (Phase 29). MEMORY.md liegt nur in der DB +
    # wird ggf. via sync-config in den Container-Workspace gerendert.

    return {"status": "updated"}


@router.get("/me")
async def agent_get_me(
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_agent),
):
    """Self-Lookup — Agent ruft seine eigene Info ab.

    Convenience-Endpoint fuer Workers die sich orientieren muessen: "wer bin ich,
    welche Rolle, was hab ich an Tools, welche Task laeuft gerade?". Vorher haben
    Agents trial-and-error versucht (GET /agent/agents/{id} → 404), das ist der
    kanonische Weg.

    Keine Scope-Anforderung — jeder auth'd Agent darf sich selbst sehen.
    """
    # Current task summary (wenn vorhanden)
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
    """Agent liest seine eigene MEMORY.md."""
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

    # Fix 1: Agents mit Kontext fuer Orchestrator-Entscheidungen
    agents = (
        await session.exec(
            select(Agent).where(Agent.board_id == board_id)
        )
    ).all()

    # Projekte laden — damit Agent weiss welche Projekte existieren
    projects = (
        await session.exec(
            select(Project)
            .where(Project.board_id == board_id)
            .order_by(Project.created_at.desc())
        )
    ).all()

    # Agent-ID → Name Mapping fuer Tasks
    agent_map = {a.id: a.name for a in agents}

    # Tasks mit agent_name enrichen
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


# Priority ordering fuer Pull-Dispatch
@router.get("/boards/{board_id}/agents")
async def agent_list_board_agents(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    """Alle Agents eines Boards auflisten — fuer Delegation (assigned_agent_id)."""
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


# ── Agent-Auth Task CRUD (Board Lead / Koordinator Endpoints) ─────────────────


# ── Agent-Auth Agent-Inspection Endpoints ─────────────────────────────────────


@router.get("/agents/list")
async def agent_list_all_agents(
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """Alle Agents im System auflisten — nur fuer Board Leads mit agents:manage."""
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
    """Agent-Detail lesen — Config, Scopes, Plugins, Skills."""
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
    """Alle Projekte eines Boards auflisten."""
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
    """Board Lead kann Projekte erstellen um zusammengehoerige Tasks zu buendeln."""
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    board = await session.get(Board, board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Board not found")

    # ── Duplicate Project Guard ──────────────────────────────────
    # Verhindert doppelte Projekte durch Agent-Retry/Doppelaufruf.
    # Gleicher Name + Board + 60s Fenster → 409 mit existing_project_id.
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
    """Agent bittet einen anderen Agent um Hilfe. Erstellt Subtask, blockiert Absender."""
    from app.services.dispatch import auto_dispatch_task

    # 1. Aktuellen Task des Agents finden
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

    # 3. Helfer-Agent finden
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

    # 4. Subtask erstellen
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
    """Orchestrator-Delegation: Subtask erstellen + explizit warten auf Callback.

    Atomare Alternative zu 'mc task-create + mc blocked getrennt'. Erzeugt KEINE
    Operator-Approval — reine Orchestration.
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
    # Board-Isolation: Target muss auf demselben Board sein (verhindert Cross-Board-Leak)
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

    # Subtask in-memory konstruieren (noch nicht persistieren)
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
        # Callback-Pattern: Subtask zeigt zurueck auf den delegierenden Agent
        callback_agent_id=agent.id if payload.callback else None,
        is_auto_created=True,
        auto_reason=f"delegation from {agent.name}",
    )

    # Dispatch-Guard VOR Commit — kein Zombie-Subtask wenn System/Agent gerade nicht dispatchbar
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
        # Explicit flush vor dem UPDATE des current_task. Ohne flush kann
        # SQLAlchemy beim folgenden emit_event() (das intern session.commit()
        # macht, activity.py:41) die Operationen falsch ordnen — current_task
        # UPDATE mit blocked_by_task_id wird vor INSERT subtask ausgefuehrt
        # und die FK fk_tasks_blocked_by_task_id (nicht deferrable) kracht.
        # Reflexive FKs (tasks → tasks) verwirren SQLAlchemys topological sort.
        # Live-Bug Boss 2026-04-25: HTTP 500 bei mc delegate --callback.
        await session.flush()
        current_task.status = "blocked"
        current_task.blocked_by_task_id = subtask.id
        current_task.callback_agent_id = agent.id
        session.add(current_task)

    # Progress-Comment mit Delegation-Kontext
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

    # Dispatch (async, fire-and-forget) — Guard oben hat bereits bestaetigt dass dispatchbar
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
    """Agent stellt dem Operator eine Klaerungsfrage. Task wird blockiert bis der Operator antwortet."""

    # 1. Aktuellen Task des Agents pruefen
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

    # 2. Projekt-Name fuer Kontext laden
    project_name = None
    if current_task.project_id:
        project = await session.get(Project, current_task.project_id)
        if project:
            project_name = project.name

    # 3. Approval erstellen
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
    )
    session.add(approval)

    # 4. Task blockieren
    current_task.status = "blocked"
    session.add(current_task)

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
    """Agent registriert ein Deliverable — Ergebnis-Artefakt."""
    deliverable_type: Literal["screenshot", "file", "url", "artifact", "document", "data"]
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
    """DeliverableCreate + optionales task_id-Override fuer /me/deliverable."""
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
    """Agent registriert ein Deliverable — Screenshot, File, URL, Artifact oder Document."""
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

    # Dedup-Check: verhindert doppelte Registrierung wenn Agent den gleichen
    # Content nochmal submittet (z.B. weil er dachte der erste Call waere
    # fehlgeschlagen — Incident 2026-04-23 Root-Cause Bug A: Researcher hat
    # 4x dasselbe Deliverable registriert weil der LIST-Endpoint kein content
    # zurueckgab). Match-Kriterium: (task_id, path) wenn path vorhanden, sonst
    # (task_id, title) fuer inline-only Deliverables. Same-Agent-Only — Cross-
    # Agent-Duplikate sind legitime separate Beitraege.
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
    # (board_memory), damit Research-Resultate durchsuchbar sind und nicht verloren
    # gehen — ohne dass der Agent einen zweiten POST machen muss.
    from app.models.memory import BoardMemory

    _TYPE_TO_MEMORY_TYPE = {
        "document": "knowledge",
        "data": "knowledge",
        "url": "reference",
        "file": "reference",
        "artifact": "reference",
        "screenshot": "reference",
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
        # Memory-Write-Fehler darf den Deliverable-Flow nicht blocken — nur loggen.
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
    """Alle Deliverables fuer einen Task lesen.

    Query-Params:
      include_content: Wenn true, wird das `content`-Feld (volle Markdown/Text-
          Body) mitgeliefert. Default false um Response-Size klein zu halten.
      include_subtasks: Wenn true, werden auch Deliverables aller Descendant-
          Subtasks (rekursiv bis `depth` Ebenen) mitgeliefert. Jedes Subtask-
          Deliverable bekommt `source_task_id` + `source_task_title` + `depth`
          (0=self, 1=direct child, etc.) fuer UI-Gruppierung. Orchestrator-
          Parent-Tasks koennen damit den gesamten Output-Tree auf einen Blick
          sehen ohne jeden Subtask einzeln abzufragen.
      depth: Max Subtask-Tiefe (1=direkte Kinder, 2=Enkel, ...). Default 2,
          Maximum 5 als Response-Size-Schutz.
    """
    from app.models.deliverable import TaskDeliverable
    from sqlmodel import col as _col

    # Depth clamp (server-side safety)
    effective_depth = max(1, min(int(depth or 2), 5))

    # Task-IDs + Titel sammeln per BFS (falls include_subtasks).
    # Map: task_id -> (task_title, depth)
    task_meta: dict[uuid.UUID, tuple[str, int]] = {task_id: ("", 0)}

    # Titel des Root-Tasks holen (fuer konsistente source_task_title-Ausgabe)
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

    # Deliverables fuer alle gesammelten task_ids
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
            # Nur bei include_subtasks, sonst wird LIST-Response-Shape unnoetig
            # geaendert fuer Aufrufer die die alten Feldnamen erwarten.
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
    """Einzelnes Deliverable mit vollem `content`-Feld lesen.

    Closes verification-gap: der LIST-Endpoint blendet `content` standardmaessig
    aus (Response-Size). Agents (Boss, FreeCode, Planner) brauchen aber den
    vollen Markdown/Text-Body um Follow-Up-Arbeit zu machen — dieser Endpoint
    liefert ihn. Scope: TASKS_READ (gleich wie LIST).

    Incident-Context 2026-04-23: ohne diesen Endpoint haben Agents aus der
    `content_length=0`-Abwesenheit im LIST-Response faelschlich geschlossen
    dass content fehlt — was zu doppelten Re-Registrierungen und kaputten
    phase_rewrite_requests fuehrte.
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

    # Board-Check via Task: verhindert Cross-Board-Leak
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
# Boss Agent-Spawn Request — der Operator muss approven (Phase 2, 2026-04-11)
# ─────────────────────────────────────────────────────────────────────────


@router.post("/agents/request-spawn", response_model=SpawnApprovalResponse)
async def agent_request_spawn(
    payload: SpawnAgentRequest,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """Boss erstellt eine Spawn-Approval. Operator approved → Agent wird erstellt.

    Nur Board-Leads (Boss, Henry) duerfen das — Scope AGENTS_MANAGE.
    Der eigentliche Spawn passiert im resolve_approval() Handler.
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
    # Verhindert dass zwei parallele POST-Requests beide durchkommen. Der
    # bestehende SELECT+INSERT-Pfad darunter bleibt als Defense-in-Depth.
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
        # Weiter zum DB-Check — nicht blocken wenn Redis down ist

    # Duplikat-Check: kein pending spawn mit gleichem Namen
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
# Boss Plugin-Self-Service — Boss darf eigene + Worker-Plugins toggeln
# ─────────────────────────────────────────────────────────────────────────


@router.get("/plugins")
async def agent_list_plugins(
    agent: Agent = Depends(require_scope(Scope.AGENTS_MANAGE)),
):
    """Shared-Cache Plugins auflisten fuer Plugin-Zuweisung an Worker.

    Nur Board Leads — die einzigen, die Plugins auch zuweisen duerfen
    (siehe PATCH /agents/{id}/plugins). Reine Read-Operation, kein Install.
    Install neuer Plugins laeuft weiter via POST /install-requests (Operator-
    Approval-Gate, Supply-Chain-Schutz).
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
    """Aktuell zugewiesene cli_plugins eines Worker-Agents lesen.

    Komplement zu PATCH — Boss kann pruefen welche Plugins ein Worker heute
    hat bevor er zuweist/entfernt. None = alle installierten, [] = keine,
    Liste = Allowlist.
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
    """Boss/Board-Lead darf cli_plugins fuer sich selbst oder Worker setzen.

    Triggert sync_agent_plugins_to_disk() — settings.json + installed_plugins.json
    werden neu gerendert. Worker-Restart ist nicht noetig, Next-Start liest neu.
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
    # Board Leads duerfen sich gegenseitig NICHT Plugins setzen (Privilege-Guard):
    # Boss soll Henry's Plugin-Config nicht aendern koennen und umgekehrt.
    if target.is_board_lead and target.id != agent.id:
        raise HTTPException(
            status_code=403,
            detail="Board-Lead-Agents koennen einander keine Plugins setzen (nur self)",
        )

    target.cli_plugins = payload.cli_plugins
    session.add(target)
    await session.commit()
    await session.refresh(target)

    # Sync auf Disk — fail-soft, DB ist Source of Truth
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

    # Worker-Restart optional — claude/openclaude liest settings.json nur beim
    # Start. Ohne Restart wirken neue Plugins erst beim naechsten Neustart.
    # Nur fuer CLI-Bridge-Agents — host-Runtime (Boss) hat keinen Worker.
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
    """Board-Lead darf Worker-Session eines CLI-Bridge Agents neu starten.

    Sinnvoll wenn: neue Plugins zugewiesen (ohne restart_worker=true), settings
    haben sich geaendert, Worker steckt in altem Zustand. Kill + Neustart der
    claude-Session in tmux Window 0 — Container bleibt up, poll.sh laeuft weiter.

    WARNUNG: laufender Task-Kontext geht verloren. Boss sollte pruefen
    (current_task_id) bevor er restarted.
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
    """Board-Lead darf Workflow-Flags fuer Worker setzen.

    Primaerer Use-Case: Boss erkennt dass ein Design/Writer/Research-Worker
    wegen Git-Commit-Gate blocked steht → setzt requires_git_workflow=false
    statt den Operator zu fragen. Autonomer Unblock ohne Mensch im Loop.

    Privilege-Guard: Board Leads koennen einander keine Flags setzen (nur self
    oder Worker). Analog zu /plugins Endpoint.
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
    """Hybrid Vektor-/Keyword-Suche ueber 3 Memory-Layer.

    Layers:
    - semantic: wiederverwendbares Wissen (knowledge, decision, concept, reference, research)
    - agent: Agent-Lessons (gefiltert nach agent_id)
    - episodic: zeitgebundene Eintraege (journal, weekly_review, insight)

    Special values:
    - agent_id="self" → eigene agent_id einsetzen
    - board_id="current" → agent.board_id einsetzen
    """
    # Resolve special values auf den Agent-Kontext VOR dem Helper-Call
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

    # Theme 3: Autonomy-Check — L1/L2 brauchen kein Approval
    autonomy_level = await resolve_autonomy(payload.action_type)

    if autonomy_level == "L1":
        # Auto-execute: kein Approval noetig
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

    # L3: Normaler Approval-Flow
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
    Liest Knowledge-Base-Eintraege die fuer den Agent relevant sind:
    - Eigene Eintraege (agent_id = this agent)
    - Board-Memory des eigenen Boards
    - Globale Knowledge (board_id=null, agent_id=null)
    """
    # Scope-Filter: eigene + board + global
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
    Schreibt einen Eintrag in die Knowledge Base.
    scope="agent"  → agent_id gesetzt, kein board_id (nur dieser Agent sieht es)
    scope="board"  → board_id gesetzt (alle Board-Agents sehen es)
    scope="global" → weder board_id noch agent_id (alle sehen es)
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


# ── Content Pipeline Agent-Callback: lebt jetzt im News-Studio-Vertical ──────
# (app/verticals/news_studio/routers/content_agent.py — gleicher Prefix)


# ── Agent-Erstellung (nur Board Lead) ─────────────────────────────────────────


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
    """Listet alle verfuegbaren Agent-Templates auf. Nur fuer Agents mit agents:manage Scope."""
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
    Erstellt einen neuen Agent aus einem Template — nur fuer Board Leads.
    Provisioning erfolgt automatisch im Hintergrund.
    Rueckgabe: { agent, token } — Token einmalig sichtbar!
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

    # Provisioning im Hintergrund starten
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
    Erstellt einen neuen Agent ohne Template — nur fuer Board Leads.
    Provisioning erfolgt automatisch im Hintergrund.
    Rueckgabe: { agent, token } — Token einmalig sichtbar!
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

    # Vault-Write mc_token_{slug} fuer /internal/bootstrap (Fresh-Install-Fix).
    from app.services.secrets_helper import upsert_agent_token_secret
    await upsert_agent_token_secret(session, new_agent.name, raw_token)

    await emit_event(
        session,
        "agent.created",
        f"Agent {new_agent.name} erstellt von {agent.name} (Custom)",
        agent_id=new_agent.id,
        board_id=new_agent.board_id,
    )

    # Provisioning im Hintergrund starten
    background_tasks.add_task(_provision_agent_background, new_agent.id)

    return {
        "agent": new_agent,
        "token": raw_token,
    }


# ── Checklist CRUD (T-1) ─────────────────────────────────────────────────────

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
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """Agent legt Checklist-Items für einen Task an (bulk)."""
    from app.models.checklist import TaskChecklistItem

    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task nicht gefunden")

    items = []
    for item_data in payload.items:
        item = TaskChecklistItem(
            task_id=task_id,
            agent_id=agent.id,
            title=item_data.title,
            sort_order=item_data.sort_order,
        )
        session.add(item)
        items.append(item)

    # Zähler aktualisieren
    task.checklist_total = task.checklist_total + len(items)
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
    """Agent setzt Status eines Checklist-Items."""
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
    """Agent liest Checklist eines Tasks (für Recovery)."""
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
    """Agent sendet Text-Nachricht in einen Discord Channel."""
    from app.services.discord import send_to_discord_channel

    await send_to_discord_channel(body.channel_id, content=body.content)


class VisualVerifyInteraction(BaseModel):
    """Einzelne Browser-Interaktion vor dem Screenshot (click/fill/wait_for/...).

    Pydantic laesst zusaetzliche Felder durch — das Schema muss mit
    InteractionSpec in mc-playwright/service.py synchron bleiben.
    """
    action: str  # click | fill | wait_for | scroll_to | evaluate | press
    selector: str | None = None
    value: str | None = None
    script: str | None = None
    wait_after_ms: int = 300


class VisualVerifyLogin(BaseModel):
    """Inline-Form-Login (Alternative zu credential_id)."""
    url: str
    username: str
    password: str
    username_selector: str | None = None
    password_selector: str | None = None
    submit_selector: str | None = None
    wait_for_url: str | None = None
    wait_for_selector: str | None = None


class VisualVerifyBody(BaseModel):
    """Body fuer /agent/tasks/{task_id}/visual-verify — ruft mc-playwright + Telegram."""
    url: str
    viewports: list[str] = ["desktop", "mobile"]
    scroll: bool = True
    metrics: bool = True
    send_to_telegram: bool = True
    caption: str | None = None  # HTML-Caption auf das erste Bild

    # --- Interaktions-Mode (2026-04-23) ---------------------------------------
    credential_id: uuid.UUID | None = None   # Vault-Resolve (Login) — Backend
    auth_token: str | None = None            # JWT direkt in localStorage
    login: VisualVerifyLogin | None = None   # Inline-Form-Login ohne Vault
    interactions: list[VisualVerifyInteraction] = []
    wait_for_selector: str | None = None     # Finale Wartezeit vor Screenshot
    full_page: bool = True                   # False: Viewport-only (fuer Modals)
    force_telegram_resend: bool = False      # Override per-task Dedup — sendet auch bei 2.+ run


class TelegramReportBody(BaseModel):
    """Body fuer agenten-initiierten Telegram-Report an den Operator."""
    text: str
    # Optional: Task-Kontext fuer Flag-Setzen. Im Subagent-Dispatch-Modus haben Worker
    # kein agent.current_task_id — deswegen muss die CLI die Task-ID mitschicken
    # (aus /tmp/mc-context.env das poll.sh schreibt). Fuer Board Leads reicht der
    # Fallback auf current_task_id.
    task_id: uuid.UUID | None = None
    # Optional: Screenshot-Deliverable an Telegram als sendPhoto anhaengen (statt
    # plain sendMessage). text wird zur Photo-Caption (1024 Zeichen max — laenger
    # wird truncated). Deliverable muss type=screenshot sein und ein resolvable
    # File haben. Use case: Tester/Deployer hat per MCP einen Screenshot gemacht
    # → mc deliverable registriert → mc telegram --photo <id> schickt das echte
    # Bild an den Reports-Chat des Operators (statt nur Text-Beschreibung).
    deliverable_id: uuid.UUID | None = None
    # Optional: File-Deliverable (PDF/Excel/PowerPoint/Word/ZIP/...) als
    # sendDocument anhaengen. Mutex zu deliverable_id (man kann nicht beides
    # gleichzeitig schicken). Akzeptiert alle deliverable_types ausser `url`
    # und `data` (haben keinen aufloesbaren Pfad). Anders als sendPhoto wird
    # die Datei nicht komprimiert — ideal fuer Office-Dokumente.
    document_deliverable_id: uuid.UUID | None = None
    # Optional: vault-relative Pfad zu einem Deliverable-Wrapper unter
    # ~/.mc/vault/agents/{slug}/deliverables/*.md. Backend liest das Wrapper-
    # Frontmatter, resolved attachment_path zur echten Binary unter
    # vault/attachments/{files,images,audio}/ und sendet sie via sendDocument.
    # Use case: Voice-Concierge bekommt vault_search-Hits zurueck und kennt
    # nur den Wrapper-Pfad, nicht die deliverable_id — dieser Pfad gibt ihm
    # den Telegram-Versand in einem Call. Mutex zu deliverable_id und
    # document_deliverable_id.
    vault_path: str | None = None


class PdfGenerateBody(BaseModel):
    """Body fuer Agent-initiierte PDF-Generierung via mc-playwright."""
    title: str
    # Entweder markdown ODER html (nicht beide). Default-Stylesheet ist auf
    # Research-Reports / Deliverable-Docs optimiert (Geist-like Typography,
    # neutrale Farben, kein AI-slop).
    markdown: str | None = None
    html: str | None = None
    custom_css: str | None = None
    filename_prefix: str = "report"
    description: str | None = None


class MePdfBody(PdfGenerateBody):
    """PdfGenerateBody + optionales task_id-Override fuer /me/pdf."""
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
    """Markdown oder HTML → PDF via zentralen mc-playwright Sidecar.

    Zero-Setup-Alternative zu lokalem puppeteer/chromium im Agent-Container.
    Vermeidet ARM-Rosetta-Konflikte (Incident 2026-04-23: FreeCode hing 2+h
    in Download-Kaskade weil x86-Binaries im ARM-Container scheiterten).

    Der Sidecar rendert das PDF mit page.pdf() in Chromium (ARM-nativ, immer
    verfuegbar) + schreibt nach /shared-deliverables/<task_id>/. Backend
    registriert das PDF automatisch als TaskDeliverable (type=file).

    Response: {deliverable_id, path, title, bytes, pages}
    """
    # Task-Check (Agent darf nur eigene/assignend Tasks adressieren)
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

    # Content-Length fuer Response-Payload neu lesen (das File auf Disk ist
    # die source of truth)
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
    """Screenshots + Metrics via mc-playwright + optional Telegram-Foto-Anhang.

    Ruft den zentralen mc-playwright Service (kein eigenes Browser-Setup
    pro Agent mehr noetig). Registriert jedes Screenshot als TaskDeliverable.
    Wenn send_to_telegram=True, sendet die Screenshots zusaetzlich als
    Media-Group an den Reports-Chat des Operators.
    """
    from app.services.visual_verifier import (
        verify_url, register_screenshots_as_deliverables,
        send_screenshots_to_telegram, format_metrics_summary,
    )

    # Task laden + Ownership-Check
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task nicht gefunden")
    _same_board = agent.board_id == task.board_id
    _is_owner = task.assigned_agent_id == agent.id or task.owner_agent_id == agent.id
    if not (_is_owner or (agent.is_board_lead and _same_board)):
        raise HTTPException(
            403, "Du bist nicht assigned, owner oder same-board Lead auf diesem Task."
        )

    # --- Vault-Resolve bei credential_id ------------------------------------
    # Wenn der Agent einen Credential-Verweis mitschickt, resolven wir die
    # verschluesselte Credential zu einem Form-Login-Dict. Der Agent darf das
    # nur wenn er CREDENTIALS_READ Scope hat — sonst 403.
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

    # Inline-Login gewinnt gegen resolved_login (falls beide gesetzt)
    login_payload: dict | None = None
    if body.login is not None:
        login_payload = body.login.model_dump(exclude_none=True)
    elif resolved_login is not None:
        login_payload = resolved_login

    interactions_payload = [i.model_dump(exclude_none=True) for i in body.interactions] or None

    # Aufruf mc-playwright — kann 10-60s dauern (Login + Interactions)
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

    # Bug B (2026-04-23): Login-Success-Check vom mc-playwright Service
    # auswerten. Wenn der Form-Login faktisch fehlschlug (Page blieb auf
    # Login-URL), ist der Screenshot wertlos — Agent wuerde sonst die
    # Login-Maske als "eingeloggter Zustand" interpretieren (siehe Bug A).
    # Hier hart abbrechen mit klarer Fehlermeldung statt das Bild + ok=true
    # zurueckzugeben.
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

    # --- Telegram Send mit per-task Dedup -----------------------------------
    # Problem (2026-04-23 Bug): Agents machen oft mehrere visual-verify Calls
    # pro Task (Selbst-Verifikation, fresh re-take, retry nach Timeout). Jeder
    # Call sendete default an Telegram → der Operator bekam das gleiche Modal 3-5x.
    # Fix: Redis-Dedup pro Task. Erste Call sendet, Folge-Calls loggen skip.
    # Opt-out via `force_telegram_resend=True` fuer legitime Re-Sends.
    tg_result = None
    tg_sent = False
    tg_skipped_reason: str | None = None
    if body.send_to_telegram:
        # Redis-Dedup — Fail-open bei Redis-Fehlern (Container-Issue / Tests),
        # damit Telegram-Send nicht grundlos ausfaellt.
        redis = None
        already_sent = False
        try:
            from app.redis_client import get_redis as _get_redis
            redis = await _get_redis()
            dedup_key = f"mc:visual_verify:telegram_sent:{task_id}"
            already_sent = bool(await redis.get(dedup_key))
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
        else:
            caption_html = body.caption or ""
            metrics_block = format_metrics_summary(result)
            if metrics_block:
                caption_html = (caption_html + "\n\n" + metrics_block).strip() if caption_html else metrics_block
            tg_result = await send_screenshots_to_telegram(result, caption=caption_html or None)
            tg_sent = tg_result is not None and (tg_result.get("ok") if isinstance(tg_result, dict) else False)
            if tg_sent and redis is not None:
                # TTL 24h — lang genug fuer normalen Task-Lifecycle, kurz genug
                # dass Tasks die nach Langzeit reopened werden trotzdem 1 send kriegen.
                try:
                    await redis.set(dedup_key, "1", ex=24 * 3600)
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
    """Agent sendet Report-Nachricht an den Reports-Telegram-Chat des Operators.

    Nutze diesen Endpoint fuer Info-Delivery am Task-Ende (Summary, Deliverable-
    Liste, Empfehlung). KEIN Status-Spam — nur final-relevante Reports.
    HTML Parse-Mode: nutze `<b>`, `<i>`, `<code>`, `<a href="...">...</a>`.

    Side-Effect: wenn Agent einen aktiven Task hat (current_task_id), wird
    `task.report_sent_to_telegram = True` gesetzt — damit weiss der `mc done`
    Guard dass der Report-Back-Contract erfuellt ist.
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

    # Routing-Regel "wer dispatcht, der sendet" (Subtask-Send-Guard, 2026-05-17):
    # Wenn der Agent an einem Subtask arbeitet (parent_task_id NOT NULL), gehoert
    # der finale Telegram-Hit zum Orchestrator (Boss), nicht zum Worker. Sonst
    # bekommt der Operator eine Worker-Nachricht UND eine Boss-Konsolidier-Nachricht =
    # Duplikat. Ausnahme: long-running Watch-Tasks bei denen Boss explizit
    # autonomous_telegram=True setzt — dann darf der Worker autonom melden.
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

    # Flag-Claim VOR Telegram-Send (Race-Schutz C4 + Duplicate-Schutz C5):
    # Atomic UPDATE WHERE flag=false — Rowcount sagt ob wir "gewonnen" haben.
    # Wenn Flag schon true: Send trotzdem durchlassen (Idempotenz), aber
    # nicht nochmal setzen.
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
        """Flag zuruecknehmen damit Agent retryen kann. Best-effort, nie werfen."""
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

    # Wenn deliverable_id mitgegeben: Photo-Send statt Plain-Text. Ermoeglicht
    # Agents (Tester etc.) das echte Bild an den Operator anzuhaengen, nicht nur eine
    # Text-Beschreibung. Caption ist auf 1024 Zeichen begrenzt (Telegram-Limit
    # — send_photo truncated automatisch).
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
        # Ownership-Check: Agent darf nur eigene Deliverables (oder seines Boards) senden
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

    # Wenn document_deliverable_id mitgegeben: sendDocument (PDF/Office/ZIP/...).
    # Anders als sendPhoto wird die Datei NICHT komprimiert — ideal fuer alle
    # Nicht-Bild-Dateien wo Pixel-Genauigkeit oder Original-Format wichtig sind.
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
        # url/data haben keinen Pfad — sinnlos als Document zu schicken
        if deliverable.deliverable_type in ("url", "data"):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"deliverable_id verweist auf type='{deliverable.deliverable_type}' — "
                    "url/data haben keinen File-Pfad und koennen nicht als "
                    "Telegram-Document gesendet werden."
                ),
            )
        # Ownership-Check: analog zum Photo-Pfad
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

    # Wenn vault_path mitgegeben: Wrapper-basierter Versand. Voice-Concierge
    # kennt aus vault_search nur den Wrapper-Pfad, nicht die deliverable_id.
    # Wir lesen das Wrapper-Frontmatter, resolven attachment_path zur echten
    # Binary unter /vault/attachments/{files,images,audio}/ und schicken sie
    # via sendDocument. body.text wird zur Caption.
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

    # Telegram-Send NACH Flag-Claim — try/except faengt auch httpx-Exceptions (B1-Fix)
    try:
        if photo_path is not None:
            # Caption = text (truncated auf 1024 von telegram_reports.send_photo)
            result = await telegram_reports.send_photo(photo_path, caption=text)
        elif document_path is not None:
            result = await telegram_reports.send_document(document_path, caption=text)
        else:
            result = await telegram_reports.send(text)
    except Exception as send_exc:
        # Network/Timeout/HTTP-Level Errors — Flag zurueckrollen, damit Agent retryen kann
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
# Agent muss keine Task-UUID mehr manuell in die URL einsetzen.
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
    """PDF generieren — aktive Task automatisch aufloesen.

    Kein board_id/task_id in der URL noetig. Backend resolviert via
    current_task_id (Board Leads) oder spawn_session_key (Workers/cli-bridge).
    Optional: task_id im Body als explizites Override (mit Ownership-Check).
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
    """Deliverable registrieren — aktive Task automatisch aufloesen.

    Kein board_id/task_id in der URL noetig. Backend resolviert via
    current_task_id (Board Leads) oder spawn_session_key (Workers/cli-bridge).
    Optional: task_id im Body als explizites Override (mit Ownership-Check).
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
    """Telegram-Report senden — /me/*-konsistenter Pfad.

    Identisches Verhalten wie POST /telegram/send (Task-Resolution via
    body.task_id > current_task_id > spawn_session_key bereits dort integriert).
    Dieser Endpoint existiert fuer API-Konsistenz — alle drei Delivery-Aktionen
    unter /me/*.
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
    """Agent listet alle verfuegbaren Credentials (maskiert, alphabetisch sortiert).

    Credentials sind global (nicht board-scoped) — board_id nur fuer URL-Konsistenz.
    Gibt data_masked zurueck (letzte 4 Zeichen sichtbar, Rest Sternchen).
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
    """Agent holt eine einzelne Credential vollstaendig entschluesselt.

    Gibt das entschluesselte data-Dict zurueck (kein Masking).
    404 wenn Credential nicht existiert.
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
