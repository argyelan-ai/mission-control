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
from app.models.board import Board
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

# Guard 3 (Live-Turn-Signal, 07.09.2026): the turn signal is
# agent.status == "working" (set by the bridge heartbeater's task-lock, NOT
# by liveness). last_seen_at only gates staleness: a "working" agent whose
# last heartbeat is older than this (seconds) has a bridge that stopped
# heartbeating — the signal is unreliable, so dispatch normally (fail-open,
# no deadlock). The omp-bridge heartbeats every 30 s, so 90 s tolerates up
# to two missed beats.
TURN_SIGNAL_HEARTBEAT_MAX_AGE_SECONDS = 90


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
    fallback_to_lead: bool = True,
) -> "Agent | None":
    """Find an agent with a given role on the board (least-busy strategy).

    Kandidaten-Filter, in dieser Reihenfolge:
      1. Rolle + dispatchfaehige Runtime (NON_GATEWAY_RUNTIMES).
      2. Liveness: ``last_seen_at`` innerhalb des Wrapper-alive Fensters
         (``_liveness_floor_seconds`` = 2x Heartbeat-Interval, min 120s —
         dieselbe Quelle, die task_runner fuer Wrapper-Liveness benutzt).
         Agents OHNE last_seen_at (nie gesehen, keine Daten) passieren den
         Filter — konsistent mit watchdog/session_monitor, das last_seen_at
         ebenfalls nur bei vorhandenem Wert auswertet.
      3. ``exclude_agent_id`` (z.B. Autor der Karte) — gilt auch fuer den
         Board-Lead-Fallback.

    Least-Busy-Last = ``in_progress`` + dispatchte ``inbox`` + gehaltene
    ``review``-Karten (ein Reviewer mit wartenden Reviews ist NICHT frei).

    Bleibt kein Kandidat, wird explizit ``None`` geliefert — kein stiller
    Fallback auf einen offline/belegten Agent. Der Aufrufer muss den
    ``None``-Pfad sichtbar behandeln (Log/Return).

    Fallback auf den Board Lead nur, wenn GAR KEIN Role-Kandidat existiert.
    ``fallback_to_lead=False`` schaltet ihn ganz ab — Caller, die eine
    SPEZIFische Rolle suchen (z.B. find_reviewer), muessen False uebergeben:
    der Board Lead ist kein Stellvertreter fuer die Rolle, und sein stiller
    Einsatz routet die Karte in den Approval-Inbox des Operators statt in
    einen sichtbaren "kein solcher Agent"-Pfad (Vorfall 94fda9f9: Review-
    Karten landeten auf Boss statt Rex, weil dieser Fallback feuerte, bevor
    find_reviewer's eigener Name-Fallback ueberhaupt lief).
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

    if candidates:
        # Liveness: nur Agent mit frischem Heartbeat (Wrapper lebt).
        alive = [a for a in candidates if _agent_is_live(a)]
        if not alive:
            logger.info(
                "find_agent_by_role: %d %s-Kandidaten auf Board %s, aber keiner "
                "lebendig (last_seen_at stale) → explizit None",
                len(candidates), role.value, board_id,
            )
            return None

        if len(alive) == 1:
            return alive[0]

        # Least-Busy: in_progress + dispatched inbox + gehaltene review-Karten
        busy_counts: dict[uuid.UUID, int] = {}
        for agent in alive:
            active_result = await session.exec(
                select(sa_func.count()).select_from(Task).where(
                    Task.assigned_agent_id == agent.id,
                    or_(
                        Task.status == "in_progress",
                        (Task.status == "inbox") & (Task.dispatched_at.isnot(None)),  # type: ignore[arg-type]
                        Task.status == "review",
                    ),
                )
            )
            busy_counts[agent.id] = active_result.one()

        alive.sort(key=lambda a: busy_counts.get(a.id, 0))
        return alive[0]

    # Kein Role-Kandidat → Fallback: Board Lead (gleiche Filter: Runtime,
    # Liveness, exclude). Auch hier: keiner uebrig → explizit None.
    if not fallback_to_lead:
        return None
    lead_query = select(Agent).where(
        Agent.board_id == board_id,
        Agent.is_board_lead == True,  # noqa: E712
        Agent.agent_runtime.in_(NON_GATEWAY_RUNTIMES),  # type: ignore[union-attr]
    )
    if exclude_agent_id:
        lead_query = lead_query.where(Agent.id != exclude_agent_id)
    lead_result = await session.exec(lead_query)
    for lead in lead_result.all():
        if _agent_is_live(lead):
            return lead
    logger.info(
        "find_agent_by_role: kein %s-Kandidat und kein lebendiger Board Lead "
        "auf Board %s → explizit None",
        role.value, board_id,
    )
    return None


def _agent_is_live(agent: "Agent") -> bool:
    """Liveness-Check via Heartbeat (``agents.last_seen_at``).

    Quelle ist dieselbe wie in task_runner._liveness_floor_seconds: ein
    frisches ``last_seen_at`` beweist, dass der poll.sh-Wrapper (und damit
    Container/Host) lebt. Fenster = 2x Heartbeat-Interval, min 120s.
    ``last_seen_at is None`` → keine Daten → nicht als tot gewertet
    (konsistent mit watchdog/session_monitor).
    """
    from app.services.task_runner import _liveness_floor_seconds
    from app.utils import ensure_aware, utcnow

    last_seen = getattr(agent, "last_seen_at", None)
    if last_seen is None:
        return True
    seen_age = (utcnow() - ensure_aware(last_seen)).total_seconds()
    return seen_age < _liveness_floor_seconds(agent)


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


async def redispatch_after_blocker_answer(
    task_id: uuid.UUID,
    board_id: uuid.UUID,
    *,
    expected_status: str = "inbox",
) -> None:
    """Guarded wrapper around auto_dispatch_task for the blocker-answer path.

    Incident 2026-09-13 (card 4c9bb492 / G5): an operator resolved a
    blocker_decision approval, which synchronously checked task.status ==
    "blocked", set the task to "inbox" and scheduled this redispatch as a
    decoupled background task (create_tracked_task). By the time that
    background task actually ran, a different agent had already picked the
    now-"inbox" card up via the normal poll path, worked it, and moved it to
    "review" (PR #554) — auto_dispatch_task itself never re-checks the
    task's current status/run_control before dispatching, so the stale
    redispatch fired anyway and shoved the card back to "inbox", reassigned
    to yet another agent, discarding the in-flight review.

    The synchronous check at approval-resolution time only proves the task
    was dispatchable AT THAT INSTANT — it says nothing about the state by
    the time this deferred call actually executes. This wrapper re-reads
    the task immediately before calling auto_dispatch_task and only
    proceeds if it is still exactly where the blocker resolution left it
    (status == expected_status, normally "inbox", and run_control is None
    — a `mc hold` placed in the gap must also stop the redispatch, same as
    any other hold). Any other status (review, done, waiting, in_progress,
    blocked again, ...) or a run_control set in the meantime means someone
    already acted on the card through a different, more current channel —
    dispatching now would silently overwrite that. Skips visibly (log +
    event) instead of silently doing nothing, so the discarded redispatch
    is not invisible to whoever wonders why the operator's answer had no
    effect.

    The precondition check itself is task_lifecycle.task_still_reactivatable
    — shared with _handle_help_request_resume and _handle_callback_resume
    (agent_task_status.py), the two other reactivation paths carrying the
    same "run_control never checked" gap found the same day.
    """
    from app.services.task_lifecycle import task_still_reactivatable

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, task_id)
        if not task:
            return
        if not task_still_reactivatable(task, expected_status=expected_status):
            logger.warning(
                "Blocker-Redispatch uebersprungen: Task %s ist jetzt "
                "status=%s run_control=%s (erwartet: status=%s, "
                "run_control=None) — Karte wurde inzwischen anderweitig "
                "bearbeitet.",
                task_id, task.status, task.run_control, expected_status,
            )
            await emit_event(
                session,
                "task.blocker_redispatch_skipped",
                f"Blocker-Redispatch uebersprungen: Task ist jetzt "
                f"'{task.status}' (run_control={task.run_control})",
                board_id=board_id,
                task_id=task_id,
                severity="warning",
                detail={
                    "current_status": task.status,
                    "current_run_control": task.run_control,
                    "expected_status": expected_status,
                },
            )
            return

    await auto_dispatch_task(task_id, board_id)


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

            # ── Git Workspace Setup + Non-Code Phase-C (Bundle 4 / PR #568 B2) ──
            # Both steps live in task_context_builder.prepare_agent_workspace_for_task
            # now — this used to be an inline copy of the same two steps, which is
            # exactly why the reassign/handoff callers of that shared function (the
            # dedicated reassign endpoint, the assigned_agent_id PATCH branch, the
            # self-review escalation) drifted from this one the moment either half
            # got a fix without a matching edit here (PR #568 review, B2). Calling
            # the shared function instead of duplicating it means every fix to it
            # — including B1 (stale/foreign task.workspace_path after a reassign) —
            # automatically applies to every first dispatch too, not just to the
            # non-dispatch handoff paths. Returns False if the task was blocked
            # (TaskComment + terminal-unassign already committed) — caller MUST
            # return; on success/no-op returns True.
            from app.services.task_context_builder import prepare_agent_workspace_for_task
            if not await prepare_agent_workspace_for_task(task, best_agent, session):
                return

            # ── Port Allocation ──────────────────────────────────────
            # Agent-independent and idempotent (guarded by `if not task.workspace_port`)
            # — deliberately NOT part of prepare_agent_workspace_for_task's shared
            # sequence, so the reassign/handoff callers above don't reallocate a
            # port a running task already has (PR #568 review N2).
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
                        # Review-park grace (Lauf 7): the locked card may be a
                        # released review card (grace expired / not the real
                        # reviewer / reviewer already commented) — that must
                        # not block a NEW task from being queued behind it
                        # forever. Only reconsider when it's actually status
                        # review; every other lock (in_progress etc.) keeps
                        # queuing exactly like before.
                        _locked_task = await session.get(Task, best_agent.current_task_id)
                        _still_parks = True
                        if _locked_task is not None and _locked_task.status == "review":
                            from app.services.review_park import review_still_parks
                            _still_parks = await review_still_parks(session, _locked_task, best_agent)
                        if _still_parks:
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

                    # Guard 3: Live-Turn-Signal (07.09.2026, widened Bauplan
                    # Lauf 2 Teil 2, 21.09.2026).
                    # Guards 1+2 are DB-state based and blind to a turn that an
                    # agent is STILL executing after its predecessor task went
                    # done (incident 07.09.2026: Task D hung 70 min because omp
                    # was mid-turn and got pasted anyway; Nachpruefung 21.09.:
                    # 60/80 Hand-Starts were the SAME gap for host agents,
                    # because this guard only ever looked at cli-bridge).
                    #
                    # The TURN signal is agent.status == "working": the bridge
                    # heartbeater (bridge.py start_heartbeater → POST
                    # /agent/me/heartbeat, routers/agents.py:3513) derives it
                    # from the task-lock / poll turn detection and the router
                    # self-heals it against the task table — it is true only
                    # while a turn actually runs, unlike last_seen_at which is
                    # just a liveness beat every 30 s (idle agents are fresh
                    # too, PR #452 review). last_seen_at only gates liveness:
                    # a "working" status with a heartbeat older than
                    # TURN_SIGNAL_HEARTBEAT_MAX_AGE_SECONDS (90 s) means the
                    # bridge stopped heartbeating — stale signal, fail-open
                    # dispatch instead of a deadlock.
                    #
                    # The condition is runtime-free by design: the signal
                    # itself (status=="working") is what decides, so ANY
                    # poll-based runtime (host, cli-bridge, future ones) is
                    # covered automatically — an agent that never reports
                    # "working" is simply never touched by this guard. Behind
                    # settings.host_turn_signal_enabled (default True); OFF
                    # restores the old cli-bridge-only gate as a rollback path
                    # that needs no code change.
                    if settings.host_turn_signal_enabled:
                        _guard3_applies = best_agent.status == "working"
                    else:
                        _guard3_applies = (
                            getattr(best_agent, "agent_runtime", None) == "cli-bridge"
                            and best_agent.status == "working"
                        )
                    if _guard3_applies:
                        # Fail-open default: no heartbeat at all → dispatch.
                        _heartbeat_fresh = False
                        _seen_age = -1.0
                        if best_agent.last_seen_at is not None:
                            from app.utils import ensure_aware

                            _seen_age = (
                                utcnow() - ensure_aware(best_agent.last_seen_at)
                            ).total_seconds()
                            _heartbeat_fresh = (
                                _seen_age < TURN_SIGNAL_HEARTBEAT_MAX_AGE_SECONDS
                            )
                        if _heartbeat_fresh:
                            await enqueue_task(agent_id_str, str(task.id))
                            logger.info(
                                "Push-dispatch queued: '%s' -> %s (agent_in_turn, status=working, heartbeat %.0fs old)",
                                task.title, best_agent.name, _seen_age,
                            )
                            await emit_event(
                                session, "task.dispatch_queued",
                                f"Task '{task.title}' in Queue fuer {best_agent.name} (Agent im Zug)",
                                board_id=board_id, task_id=task.id, agent_id=best_agent.id,
                                detail={"reason": "agent_in_turn", "heartbeat_age_seconds": round(_seen_age)},
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
