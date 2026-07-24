"""
Auto-Dispatch Service — automatically assigns new tasks to the matching agent.

Board Lead always has priority (orchestrator principle).
Fallback: first agent with a gateway connection.
Structured dispatch messages give the agent clear context + callback protocol.

Session-reset semantics (IMPORTANT — documented centrally here):
─────────────────────────────────────────────────────────────
- trigger   = normal work impulse, NO session reset (reset_session=False)
- dispatch  = new task to agent, session reset (reset_session=True) → fresh context
- resume    = continue the same task after recovery, NO reset (reset_session=False)
- redispatch = re-dispatch after review rejection, NO reset (reset_session=False)
              developer keeps its existing context
- reset     = explicit special case, only via POST /agents/{id}/reset or watchdog escalation

No normal trigger/redispatch may reset running sessions.
Reset is always explicit, auditable, and separate.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.database import engine
from app.models.agent import Agent
from app.scopes import AgentRole
from app.models.board import Board, Project
from app.utils import utcnow
from app.models.task import Task, TaskDependency
from app.services.activity import emit_event
from app.services.runtime_context import get_session_context_for_runtime

logger = logging.getLogger(__name__)

# API base for agent callbacks — as a shell variable, expanded in the agent context
# Docker agents: MC_API_URL=http://backend:8000 (via docker-compose.agents.yml)
# Host/gateway agents: MC_API_URL=http://localhost (via agent.env / workspace/.env)

# Runtimes that don't need a gateway session — agents actively poll via HTTP.
# Single source of truth: add here when a new poll-based runtime is introduced.
# "host"        — Boss on macOS launchd (ADR-014)
# "cli-bridge"  — Docker agent via poll.sh (ADR-003)
# "free-code-bridge", "claude-code", "manual" — legacy variants, see auto_dispatch_task
NON_GATEWAY_RUNTIMES = frozenset({
    "cli-bridge",
    "host",
    "free-code-bridge",
    "claude-code",
    "manual",
})


# Host paths that the backend container has mounted as a volume (see
# docker-compose.yml backend.volumes). Other host paths (e.g.
# ${HOME_HOST}/Workspace/) are NOT visible in the backend — any write
# attempt there results in `PermissionError: [Errno 13]`. Incident context
# 2026-04-23 (DNA task for Boss): Boss had workspace_path=
# ${HOME_HOST}/Workspace instead of the standardized ${HOME_HOST}/.mc/...
# pattern. The dispatch git-clone call crashed, task blocked with a cryptic
# message. This check catches that early with a clear error message.
# Derived from settings.home_host (not hardcoded) so this works on any
# deployer's machine, not just the original host.
_BACKEND_MOUNTED_ROOTS: tuple[str, ...] = (
    f"{settings.home_host}/.mc/",
    # ~/.openclaw mount removed in Stage-2 decoupling (2026-06-01) —
    # all code now references ~/.mc/... directly.
    f"{settings.home_host}/FreeCode/",
    "/tmp/",  # always writable in the container (in-memory)
)


def is_backend_writable_path(path: str | None) -> bool:
    """Checks whether a host path is writable by the backend container.

    True if the path lies under one of the backend volumes mounted in
    docker-compose.yml. Otherwise False (→ mkdir/clone/write fails).

    Normalized via os.path.normpath to catch `..` traversal tricks —
    a path that no longer lies under a mounted root after normalization
    is not writable.
    """
    if not path:
        return False
    normalized = os.path.normpath(path)
    # Append a trailing slash so `${HOME_HOST}/.mc` matches under
    # `${HOME_HOST}/.mc/` but `${HOME_HOST}/.mcfoo/` does not.
    if not normalized.endswith(os.sep):
        normalized += os.sep
    return any(normalized.startswith(root) for root in _BACKEND_MOUNTED_ROOTS)


def _container_workspace_path(host_path: str | None, agent: "Agent | None") -> str | None:
    """Translate a host-side workspace path to the agent's container view.

    Since ADR-022 (2026-04-21), backend stores host paths in
    `agent.workspace_path` / `task.workspace_path` (e.g.
    `~/.mc/workspaces/rex/projects/xyz/.worktrees/task-abc/`) but the
    Docker-mounted agents see their own workspace as `/workspace/...`.
    This helper rewrites the path so dispatch prompts and SOUL
    references show what the agent actually sees, not the host
    filesystem.

    Non-cli-bridge agents (host, openclaw gateway) see host paths
    directly — no rewrite.

    Security (ADR-023 ultrareview): workspace_path is editable by
    operators with `agents:manage`/`project:write`. Any `..` segment
    escapes the agent's mount. Normalize first and reject traversal.
    """
    if not host_path:
        return host_path
    if not agent or getattr(agent, "agent_runtime", "") != "cli-bridge":
        return host_path
    # Normalize defensively — rejects '..' after the /workspace anchor.
    # `os.path.normpath` collapses segments, so any `..` that survived
    # after the `/.mc/workspaces/<slug>/` anchor would have escaped.
    import os.path as _op
    # New ~/.mc/workspaces/<slug>/... → /workspace/...
    _mc_match = re.match(r"^(?:/[^/]+)+?/\.mc/workspaces/([^/]+)(/.*)?$", host_path)
    if _mc_match:
        suffix = _mc_match.group(2) or ""
        # Reject traversal BEFORE rewriting so the container path can't
        # point outside /workspace. normpath(/workspace/../x) → /x which
        # is outside the mount — if detected, fall back to mount root.
        candidate = _op.normpath(f"/workspace{suffix}")
        if not candidate.startswith("/workspace"):
            logger.warning(
                "_container_workspace_path: rejected traversal in host_path=%r "
                "(agent=%s) — returning /workspace", host_path, getattr(agent, "name", "?"),
            )
            return "/workspace"
        return candidate
    # Legacy ~/.openclaw/workspace-<slug>/... → /workspace/...
    _legacy = re.match(r"^(?:/[^/]+)+?/\.openclaw/workspace-([^/]+)(/.*)?$", host_path)
    if _legacy:
        suffix = _legacy.group(2) or ""
        candidate = _op.normpath(f"/workspace{suffix}")
        if not candidate.startswith("/workspace"):
            logger.warning(
                "_container_workspace_path: rejected traversal in legacy host_path=%r "
                "(agent=%s) — returning /workspace", host_path, getattr(agent, "name", "?"),
            )
            return "/workspace"
        return candidate
    return host_path


async def dependencies_met(session: AsyncSession, task: Task) -> bool:
    """Check whether all dependencies of a task are satisfied (done)."""
    dep_result = await session.exec(
        select(TaskDependency).where(TaskDependency.task_id == task.id)
    )
    deps = dep_result.all()
    if not deps:
        return True
    for dep in deps:
        dep_task = await session.get(Task, dep.depends_on_task_id)
        if not dep_task or dep_task.status != "done":
            return False
    return True


async def find_agent_by_role(
    session: AsyncSession,
    board_id: uuid.UUID,
    role: "AgentRole",
    exclude_agent_id: uuid.UUID | None = None,
) -> "Agent | None":
    """Find an agent with a given role on the board (least-busy strategy).

    With multiple candidates: prefer the agent with the fewest active tasks.
    Fallback: Board Lead.
    """
    from app.scopes import AgentRole
    from sqlalchemy import func as sa_func, or_

    # Phase 30: dispatchable = agent runs on a known poll-based runtime
    # (NON_GATEWAY_RUNTIMES). The legacy `gateway_agent_id IS NOT NULL`
    # filter was a stand-in for "has a delivery channel" — post-Phase 29
    # that channel is the runtime poll loop, not an OpenClaw session.
    query = select(Agent).where(
        Agent.board_id == board_id,
        Agent.role == role.value,
        Agent.agent_runtime.in_(NON_GATEWAY_RUNTIMES),  # type: ignore[union-attr]
    )
    if exclude_agent_id:
        query = query.where(Agent.id != exclude_agent_id)

    result = await session.exec(query)
    candidates = list(result.all())

    if not candidates:
        # Fallback: Board Lead
        lead_result = await session.exec(
            select(Agent).where(
                Agent.board_id == board_id,
                Agent.is_board_lead == True,  # noqa: E712
                Agent.agent_runtime.in_(NON_GATEWAY_RUNTIMES),  # type: ignore[union-attr]
            )
        )
        return lead_result.first()

    if len(candidates) == 1:
        return candidates[0]

    # Least-busy: agent with the fewest active tasks (in_progress + dispatched inbox)
    busy_counts: dict[uuid.UUID, int] = {}
    for agent in candidates:
        active_result = await session.exec(
            select(sa_func.count()).select_from(Task).where(
                Task.assigned_agent_id == agent.id,
                or_(
                    Task.status == "in_progress",
                    (Task.status == "inbox") & (Task.dispatched_at.isnot(None)),  # type: ignore[arg-type]
                ),
            )
        )
        busy_counts[agent.id] = active_result.one()

    candidates.sort(key=lambda a: busy_counts.get(a.id, 0))
    return candidates[0]


async def find_dispatch_target(
    session: AsyncSession,
    task: Task,
    board_id: uuid.UUID,
) -> tuple[Agent | None, str]:
    """Explicit assignment takes priority. Then Board Lead. Fallback: first agent with a gateway.

    Checks whether the agent has an active gateway session (online check).
    Offline agents are skipped — the watchdog picks them up later.

    Returns: (agent, decision_reason) — reason is a short string for logging.
    """
    # Explicit assignment via assigned_agent_id always takes priority over board-lead-first
    # (unless the assigned agent has been archived — archived agents are never
    # dispatch targets, so we fall through to normal selection instead).
    if task.assigned_agent_id:
        assigned = await session.get(Agent, task.assigned_agent_id)
        if assigned and assigned.archived_at is None:
            return assigned, "explicit_assignment"

    result = await session.exec(
        select(Agent).where(Agent.board_id == board_id, Agent.archived_at.is_(None))
    )
    agents = result.all()

    if not agents:
        return None, "no_agents_on_board"

    # Online check (post Phase 30 / Gateway-Sunset):
    # "Online" simply means: agent runs on a poll-based runtime
    # (cli-bridge / host / claude-code / free-code-bridge / manual). These
    # actively pick up tasks via poll.sh / launchd. The gateway-session filter
    # was dropped with Phase 30 — agent runtime is the sole source of truth.
    # Archived agents are guarded here too so any fallback/eligibility path
    # (not just the board query above) never selects one.
    def _is_online(agent: Agent) -> bool:
        if getattr(agent, "archived_at", None) is not None:
            return False
        return getattr(agent, "agent_runtime", None) in NON_GATEWAY_RUNTIMES

    # Orchestrator has the highest priority (Boss via CLI-bridge)
    for agent in agents:
        if agent.role == AgentRole.ORCHESTRATOR and _is_online(agent):
            return agent, "orchestrator"

    # Board Lead as second priority — online preferred
    for agent in agents:
        if agent.is_board_lead and _is_online(agent):
            return agent, "board_lead"

    # Fallback: first ONLINE agent (runtime has a poll channel)
    for agent in agents:
        if _is_online(agent):
            return agent, f"fallback_runtime_agent:{agent.name}"

    # No agent online — Board Lead (even offline) as last resort (watchdog retries later)
    for agent in agents:
        if agent.is_board_lead:
            return agent, "board_lead_offline_fallback"

    return None, "no_runtime_agents"


async def _allocate_port(session: AsyncSession) -> int | None:
    """Allocate the first free port from the range 4200-4299."""
    result = await session.exec(
        select(Task.workspace_port).where(
            Task.workspace_port.isnot(None),  # type: ignore[union-attr]
            Task.status.in_(["inbox", "in_progress", "review", "user_test"]),  # type: ignore[union-attr]
        )
    )
    used_ports = {row for row in result.all() if row is not None}
    for port in range(4200, 4300):
        if port not in used_ports:
            return port
    return None  # All 100 ports taken


async def auto_dispatch_task(
    task_id: uuid.UUID,
    board_id: uuid.UUID,
    extra_recovery_context: str | None = None,
) -> None:
    """Background task: check board, load task, find best agent, assign.

    UNIFIED PUSH: all agents receive tasks via chat_send().
    3 fallback tiers: chat_send → chat_send_isolated → pending_dispatch queue.
    Watchdog redelivers pending tasks once the agent has a session.

    extra_recovery_context: optional caller-supplied recovery text prepended
    to build_recovery_context()'s output and injected VERBATIM into the
    dispatch prompt's recovery block. Used by the waiting-resume path (Task 9)
    to carry the bounded resume recap (open question + operator answer) into
    the re-dispatched prompt — build_recovery_context truncates each comment to
    a single line, so a comment alone would drop the answer.
    """
    from app.services.task_queue import enqueue_task

    # Wait briefly so the request session has committed
    await asyncio.sleep(0.1)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            # Check board
            board = await session.get(Board, board_id)
            if not board or not board.auto_dispatch_enabled:
                return

            # Load task
            task = await session.get(Task, task_id)
            if not task:
                return

            # Planner-mode resolution removed (Phase 6, 2026-04-11).
            # Boss plans on its own via openclaude subagents, no planner intermediate step.
            # planner_mode field stays in the schema for backward compat, no longer read.

            # ── Operational Controls Guard ─────────────────────────────
            from app.services.operations import check_dispatch_allowed
            _guard_agent = None
            if task.assigned_agent_id:
                _guard_agent = await session.get(Agent, task.assigned_agent_id)
            allowed, reason = await check_dispatch_allowed(task, _guard_agent, session)
            if not allowed:
                logger.info("Dispatch blocked: '%s' — %s", task.title, reason)
                return

            # Dependency check — don't dispatch if predecessors aren't done
            if not await dependencies_met(session, task):
                logger.info("Dispatch blocked: '%s' — Dependencies nicht erfuellt", task.title)
                await emit_event(
                    session, "task.dispatch_blocked",
                    f"Task '{task.title}' wartet auf Vorgaenger-Tasks",
                    board_id=board_id, task_id=task.id,
                )
                return  # Gets dispatched via auto-trigger once deps are satisfied

            # Task already assigned? Push directly to that agent
            dispatch_reason = "unknown"
            if task.assigned_agent_id is not None:
                pre_assigned = await session.get(Agent, task.assigned_agent_id)
                # Phase 30: gateway_agent_id branch dropped. The pre-assigned
                # agent simply takes the dispatch — runtime/online-state is
                # handled by downstream delivery (poll.sh / launchd / etc.).
                if pre_assigned:
                    best_agent = pre_assigned
                    dispatch_reason = "pre_assigned"
                else:
                    return  # Assigned agent no longer exists
            else:
                # Find best agent (Board Lead has priority)
                best_agent, dispatch_reason = await find_dispatch_target(session, task, board_id)
                if not best_agent:
                    logger.warning("Auto-dispatch: Kein Agent mit Gateway fuer '%s'", task.title)
                    await emit_event(
                        session,
                        "task.dispatch_failed",
                        f"Kein Agent verfuegbar fuer '{task.title}' — manuelle Zuweisung noetig",
                        board_id=board_id,
                        task_id=task.id,
                        severity="warning",
                        detail={"reason": dispatch_reason},
                    )
                    return

                # Warning when using a fallback agent instead of Board Lead
                if not best_agent.is_board_lead:
                    await emit_event(
                        session,
                        "task.dispatch_fallback",
                        f"Board Lead offline — '{task.title}' an {best_agent.name} (Fallback)",
                        board_id=board_id,
                        task_id=task.id,
                        agent_id=best_agent.id,
                        severity="warning",
                        detail={"reason": "board_lead_unavailable", "fallback_agent": best_agent.name},
                    )

                # Assign the task to the agent
                task.assigned_agent_id = best_agent.id
                session.add(task)
                await session.commit()

            # ── Git Workspace Setup + Worktree Isolation (Bundle 4) ──
            # Extracted to task_context_builder.setup_git_workspace_for_dispatch
            # (REF-01 Step 3). Returns False if the task was blocked
            # (TaskComment + terminal-unassign already committed) — caller MUST
            # return; on success/no-op returns True.
            from app.services.task_context_builder import setup_git_workspace_for_dispatch
            if not await setup_git_workspace_for_dispatch(task, best_agent, session):
                return

            # Phase C (T-1): also create workspace for non-code tasks
            if not task.workspace_path:
                _proj = await session.get(Project, task.project_id) if task.project_id else None
                _agent_ws = best_agent.workspace_path if best_agent else None
                _task_ws = await _ensure_task_workspace(task.id, _proj, _agent_ws)
                if _task_ws:
                    task.workspace_path = _task_ws
                    session.add(task)
                    await session.commit()
                    logger.info("Task %s: Non-Code-Workspace erstellt: %s", task.id, _task_ws)

            # ── Port Allocation ──────────────────────────────────────
            if not task.workspace_port:
                task.workspace_port = await _allocate_port(session)
                if task.workspace_port:
                    session.add(task)
                    await session.commit()

            # ── Dispatch Lock (race-condition protection) ────────────────
            from app.services.task_queue import acquire_dispatch_lock, release_dispatch_lock

            agent_id_str = str(best_agent.id)

            # Workers with isolated sessions: parallel dispatches allowed → no lock needed.
            # IMPORTANT: isolated sessions via chat_send_isolated exist ONLY for gateway agents
            # (openclaw). cli-bridge / host / claude-code have a single tmux session — the busy
            # check MUST stay active, otherwise we'd overwrite their context with the new task
            # (poll.sh sees a new task_id → /clear → worker loses progress on the current task).
            # Bug observed 2026-04-22: Tester lost context when 2 tasks arrived pre-assigned.
            _runtime = getattr(best_agent, "agent_runtime", "openclaw")
            _has_isolated_sessions = _runtime not in NON_GATEWAY_RUNTIMES
            _skip_busy = (
                settings.use_subagent_dispatch
                and not best_agent.is_board_lead
                and _has_isolated_sessions
            )

            if not _skip_busy:
                if not await acquire_dispatch_lock(agent_id_str, ttl=30):
                    # Lock held → queue task instead of dropping it
                    await enqueue_task(agent_id_str, str(task.id))
                    logger.info("Dispatch lock busy: '%s' -> %s (queued)", task.title, best_agent.name)
                    return

            try:
                # ── UNIFIED PUSH MODE (all agents) ─────────────────────

                if not _skip_busy:
                    # Check whether agent is busy → queue
                    # Guard 1: current_task_id (atomic lock)
                    if best_agent.current_task_id and best_agent.current_task_id != task.id:
                        await enqueue_task(agent_id_str, str(task.id))
                        logger.info(
                            "Push-dispatch queued: '%s' -> %s (active_task_lock: %s)",
                            task.title, best_agent.name, best_agent.current_task_id,
                        )
                        await emit_event(
                            session, "task.dispatch_queued",
                            f"Task '{task.title}' in Queue fuer {best_agent.name} (active task lock)",
                            board_id=board_id, task_id=task.id, agent_id=best_agent.id,
                        )
                        return

                    # Guard 2: busy = in_progress OR dispatched-but-not-acked (DB-based)
                    from sqlalchemy import or_
                    active_result = await session.exec(
                        select(Task).where(
                            Task.assigned_agent_id == best_agent.id,
                            Task.id != task.id,
                            or_(
                                Task.status == "in_progress",
                                (Task.status == "inbox") & (Task.dispatched_at.isnot(None)),  # type: ignore[arg-type]
                            ),
                        )
                    )
                    if active_result.first():
                        await enqueue_task(agent_id_str, str(task.id))
                        logger.info("Push-dispatch queued: '%s' -> %s (busy)", task.title, best_agent.name)
                        await emit_event(
                            session, "task.dispatch_queued",
                            f"Task '{task.title}' in Queue fuer {best_agent.name}",
                            board_id=board_id, task_id=task.id, agent_id=best_agent.id,
                        )
                        return

                # Status stays inbox — agent must ACK itself (PATCH status: in_progress)
                # Runtime readiness + delivery (claude-code / cli-bridge / host /
                # openclaw) — extracted to dispatch_delivery.py (REF-01 Step 3).
                # Pitfall A: helper reads rpc + settings via the dispatch namespace so
                # test_dispatch_race + test_subagent_dispatch patches flow through.
                from app.services.dispatch_delivery import (
                    _check_runtime_readiness, _deliver_dispatch_message,
                )
                if not await _check_runtime_readiness(
                    task, best_agent, session, board_id, agent_id_str,
                ):
                    dispatch_mode = "push_pending"
                else:
                    # Generate dispatch_attempt_id BEFORE building the message,
                    # so it can be included in the message sent to the agent.
                    #
                    # Race fix (2026-05-15, post double-dispatch incident):
                    # the old "if not task.dispatch_attempt_id: set" logic was
                    # not atomic — during the git-clone race (5s window),
                    # /agent/me/poll and auto_dispatch_task could both see
                    # NULL at the same time and both set a UUID, with the last
                    # commit winning. set_dispatch_attempt_id(only_if_null=True)
                    # does a conditional UPDATE … WHERE attempt_id IS NULL —
                    # first-writer-wins, race-free. Plus an audit trail in
                    # task_attempt_audit for future forensics.
                    from app.services.dispatch_attempt_audit import (
                        set_dispatch_attempt_id,
                    )
                    await set_dispatch_attempt_id(
                        session, task, str(uuid.uuid4()),
                        caller="auto_dispatch",
                        reason="initial_dispatch",
                        only_if_null=True,
                    )

                    # Recovery context: if the task already has comments
                    # (e.g. after a blocker re-dispatch), include prior progress
                    # + operator response in the dispatch message.
                    _recovery_ctx = await build_recovery_context(session, task)
                    if extra_recovery_context:
                        # Prepend the caller-supplied recap (waiting-resume, Task 9)
                        # so it lands verbatim in the prompt's recovery block.
                        _recovery_ctx = "\n\n".join(
                            p for p in (extra_recovery_context, _recovery_ctx) if p
                        )
                    message = await _build_dispatch_message(
                        task, best_agent, session, recovery_context=_recovery_ctx,
                    )
                    dispatch_mode = await _deliver_dispatch_message(
                        task, best_agent, message, session, board_id, agent_id_str,
                    )

                _decision_reason = dispatch_reason
                logger.info("Push-dispatch: '%s' -> %s (%s, reason=%s)", task.title, best_agent.name, dispatch_mode, _decision_reason)
                await emit_event(
                    session, "task.auto_dispatched",
                    f"Dispatch: '{task.title}' → {best_agent.name} ({_decision_reason})",
                    board_id=board_id, task_id=task.id, agent_id=best_agent.id,
                    detail={"agent_name": best_agent.name, "mode": dispatch_mode, "decision_reason": _decision_reason},
                )
            finally:
                if not _skip_busy:
                    await release_dispatch_lock(agent_id_str)

        except Exception:
            logger.exception("Auto-dispatch failed for task %s", task_id)


async def build_agent_task_prompt(task: Task, agent: Agent, session: AsyncSession) -> str:
    """Public function for HTTP-poll queue — returns prompt string for agent.

    Loads recovery context if the task has already been worked on (via comments
    or checklist items). This way the agent doesn't get the original prompt again
    on re-dispatch after a container/host restart (→ starting over), but instead
    a "you left off here, continue" context.

    build_recovery_context returns None for fresh tasks without history —
    in that case behavior is identical to the old poll path (task prompt only).
    """
    _recovery_ctx = await build_recovery_context(session, task)
    return await _build_dispatch_message(
        task=task, agent=agent, session=session, recovery_context=_recovery_ctx,
    )

# ─────────────────────────────────────────────────────────────────────
# REF-01 Re-Export Shim (Pattern S1, Phase 4 Plan 04-01)
# Race tests patch app.services.dispatch._load_dispatch_context etc.
# task_lifecycle.py + 8 modules import from app.services.dispatch.
# Removal of these shims deferred to v0.6 (per A3 auto-resolution).
# ─────────────────────────────────────────────────────────────────────
from app.services.task_context_builder import (  # noqa: F401
    DispatchContext,
    _load_dispatch_context,
    _ensure_task_workspace,
    get_last_checkpoint,
    build_recovery_context,
)

# ─────────────────────────────────────────────────────────────────────
# REF-01 Step 2 Re-Export Shim (Pattern S1, Phase 4 Plan 04-02)
# 4+ caller modules import these names from app.services.dispatch:
#   - routers/tasks.py + routers/agents.py + 5 test files
# Removal of these shims deferred to v0.6 (per A3 auto-resolution).
# ─────────────────────────────────────────────────────────────────────
from app.services.dispatch_message_builder import (  # noqa: F401
    DispatchSection,
    DISPATCH_TARGET_CHARS,
    DISPATCH_WARN_CHARS,
    DISPATCH_HARD_CHARS,
    MEMORY_AUTO_MAX_CHARS,
    _assemble_with_budget,
    _extract_auth_token,
    _curl,
    _build_review_message,
    _build_test_message,
    _build_dispatch_message,
    _format_dispatch_message,
    build_planning_brief,
)
