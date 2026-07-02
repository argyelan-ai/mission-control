"""
Auto-Dispatch Service — weist neue Tasks automatisch dem passenden Agent zu.

Board Lead hat immer Prioritaet (Orchestrator-Prinzip).
Fallback: Erster Agent mit Gateway-Anbindung.
Structured Dispatch Messages geben dem Agent klaren Kontext + Callback-Protokoll.

Session-Reset Semantik (WICHTIG — hier zentral dokumentiert):
─────────────────────────────────────────────────────────────
- trigger   = normaler Arbeitsimpuls, KEIN Session-Reset (reset_session=False)
- dispatch  = neuer Task an Agent, Session-Reset (reset_session=True) → frischer Kontext
- resume    = denselben Task fortsetzen nach Recovery, KEIN Reset (reset_session=False)
- redispatch = Re-Dispatch nach Review-Rejection, KEIN Reset (reset_session=False)
              Developer behaelt bisherigen Kontext
- reset     = expliziter Sonderfall, nur via POST /agents/{id}/reset oder Watchdog-Eskalation

Kein normaler Trigger/Redispatch darf laufende Sessions resetten.
Reset ist immer explizit, auditierbar und getrennt.
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

# API Base fuer Agent-Callbacks — als Shell-Variable, wird im Agent-Context expandiert
# Docker-Agents: MC_API_URL=http://backend:8000 (via docker-compose.agents.yml)
# Host/Gateway-Agents: MC_API_URL=http://localhost (via agent.env / workspace/.env)

# Runtimes die keine Gateway-Session brauchen — Agents pollen aktiv per HTTP.
# Single source of truth: wenn ein neuer Poll-based Runtime hinzukommt, hier ergaenzen.
# "host"        — Boss auf macOS launchd (ADR-014)
# "cli-bridge"  — Docker-Agent via poll.sh (ADR-003)
# "free-code-bridge", "claude-code", "manual" — Legacy-Varianten, siehe auto_dispatch_task
NON_GATEWAY_RUNTIMES = frozenset({
    "cli-bridge",
    "host",
    "free-code-bridge",
    "claude-code",
    "manual",
})


# Host-Pfade die der Backend-Container als Volume gemountet hat (siehe
# docker-compose.yml backend.volumes). Andere Host-Pfade (z.B.
# ${HOME_HOST}/Workspace/) sind im Backend NICHT sichtbar — jeder Schreib-
# Versuch dort fuehrt zu `PermissionError: [Errno 13]`. Incident-Context
# 2026-04-23 (DNA-Task fuer Boss): Boss hatte workspace_path=
# ${HOME_HOST}/Workspace statt des standardisierten ${HOME_HOST}/.mc/...
# Pattern. Dispatch-Git-Clone-Call crashte, Task blockierte mit kryptischer
# Meldung. Dieser Check fangt das frueh ab mit klarer Error-Message.
# Derived from settings.home_host (not hardcoded) so this works on any
# deployer's machine, not just the original host.
_BACKEND_MOUNTED_ROOTS: tuple[str, ...] = (
    f"{settings.home_host}/.mc/",
    # ~/.openclaw Mount in Stage-2-Entkopplung entfernt (2026-06-01) —
    # aller Code referenziert jetzt direkt ~/.mc/...
    f"{settings.home_host}/FreeCode/",
    "/tmp/",  # immer im Container beschreibbar (in-memory)
)


def is_backend_writable_path(path: str | None) -> bool:
    """Prueft ob ein Host-Pfad vom Backend-Container beschreibbar ist.

    True, wenn der Pfad unter einem der in docker-compose.yml gemounteten
    Backend-Volumes liegt. Sonst False (→ mkdir/clone/write schlaegt fehl).

    Normalisiert per os.path.normpath um `..` Traversal-Tricks abzufangen
    — ein Pfad der nach Normalisierung nicht mehr unter einem mounted root
    liegt ist nicht beschreibbar.
    """
    if not path:
        return False
    normalized = os.path.normpath(path)
    # Trailing-Slash anhaengen damit `${HOME_HOST}/.mc` unter
    # `${HOME_HOST}/.mc/` matched aber `${HOME_HOST}/.mcfoo/` nicht.
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
    """Pruefen ob alle Dependencies einer Task erfuellt (done) sind."""
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
    """Agent mit bestimmter Rolle im Board finden (Least-Busy-Strategie).

    Bei mehreren Kandidaten: Agent mit wenigsten aktiven Tasks bevorzugen.
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

    # Least-Busy: Agent mit wenigsten aktiven Tasks (in_progress + dispatched inbox)
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
    """Explizite Zuweisung hat Vorrang. Danach Board Lead. Fallback: erster Agent mit Gateway.

    Prueft ob der Agent eine aktive Gateway-Session hat (Online-Check).
    Offline-Agents werden uebersprungen — Watchdog greift spaeter.

    Returns: (agent, decision_reason) — reason ist ein kurzer String fuer Logging.
    """
    # Explizite Zuweisung via assigned_agent_id hat immer Vorrang vor Board-Lead-First
    if task.assigned_agent_id:
        assigned = await session.get(Agent, task.assigned_agent_id)
        if assigned:
            return assigned, "explicit_assignment"

    result = await session.exec(
        select(Agent).where(Agent.board_id == board_id)
    )
    agents = result.all()

    if not agents:
        return None, "no_agents_on_board"

    # Online-Check (post Phase 30 / Gateway-Sunset):
    # "Online" bedeutet schlicht: Agent laeuft auf einem Poll-based Runtime
    # (cli-bridge / host / claude-code / free-code-bridge / manual). Diese
    # liefern Tasks aktiv via poll.sh / launchd ab. Gateway-Session-Filter
    # ist mit Phase 30 entfallen — Agent-Runtime ist die einzige Wahrheit.
    def _is_online(agent: Agent) -> bool:
        return getattr(agent, "agent_runtime", None) in NON_GATEWAY_RUNTIMES

    # Orchestrator hat hoechste Prioritaet (Boss via CLI-bridge)
    for agent in agents:
        if agent.role == AgentRole.ORCHESTRATOR and _is_online(agent):
            return agent, "orchestrator"

    # Board Lead als zweite Prioritaet — online bevorzugt
    for agent in agents:
        if agent.is_board_lead and _is_online(agent):
            return agent, "board_lead"

    # Fallback: erster ONLINE Agent (Runtime hat ein Poll-Channel)
    for agent in agents:
        if _is_online(agent):
            return agent, f"fallback_runtime_agent:{agent.name}"

    # Kein Agent online — Board Lead (auch offline) als letzte Option (Watchdog retry spaeter)
    for agent in agents:
        if agent.is_board_lead:
            return agent, "board_lead_offline_fallback"

    return None, "no_runtime_agents"


async def _allocate_port(session: AsyncSession) -> int | None:
    """Ersten freien Port aus Range 4200-4299 allokieren."""
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
    return None  # Alle 100 Ports belegt


async def auto_dispatch_task(task_id: uuid.UUID, board_id: uuid.UUID) -> None:
    """Background Task: Board pruefen, Task laden, besten Agent finden, zuweisen.

    UNIFIED PUSH: Alle Agents bekommen Tasks via chat_send().
    3 Fallback-Stufen: chat_send → chat_send_isolated → pending_dispatch Queue.
    Watchdog liefert pending Tasks nach, sobald Agent eine Session hat.
    """
    from app.services.task_queue import enqueue_task

    # Kurz warten damit die Request-Session committed hat
    await asyncio.sleep(0.1)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            # Board pruefen
            board = await session.get(Board, board_id)
            if not board or not board.auto_dispatch_enabled:
                return

            # Task laden
            task = await session.get(Task, task_id)
            if not task:
                return

            # Planner-Modus Aufloesung entfernt (Phase 6, 2026-04-11).
            # Boss plant selbst via openclaude-Subagents, kein Planner-Zwischenschritt.
            # planner_mode Feld bleibt im Schema fuer Backward-Compat, wird nicht mehr gelesen.

            # ── Operational Controls Guard ─────────────────────────────
            from app.services.operations import check_dispatch_allowed
            _guard_agent = None
            if task.assigned_agent_id:
                _guard_agent = await session.get(Agent, task.assigned_agent_id)
            allowed, reason = await check_dispatch_allowed(task, _guard_agent, session)
            if not allowed:
                logger.info("Dispatch blocked: '%s' — %s", task.title, reason)
                return

            # Dependency-Check — nicht dispatchen wenn Vorgaenger nicht fertig
            if not await dependencies_met(session, task):
                logger.info("Dispatch blocked: '%s' — Dependencies nicht erfuellt", task.title)
                await emit_event(
                    session, "task.dispatch_blocked",
                    f"Task '{task.title}' wartet auf Vorgaenger-Tasks",
                    board_id=board_id, task_id=task.id,
                )
                return  # Wird via Auto-Trigger dispatcht wenn deps erfuellt

            # Task bereits zugewiesen? Direkt an diesen Agent pushen
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
                    return  # Zugewiesener Agent existiert nicht mehr
            else:
                # Besten Agent finden (Board Lead hat Prioritaet)
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

                # Warning wenn Fallback-Agent statt Board Lead
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

                # Task dem Agent zuweisen
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

            # Phase C (T-1): Workspace auch fuer Non-Code-Tasks erstellen
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

            # ── Dispatch Lock (Race-Condition-Schutz) ────────────────
            from app.services.task_queue import acquire_dispatch_lock, release_dispatch_lock

            agent_id_str = str(best_agent.id)

            # Workers mit isolierten Sessions: parallele Dispatches erlaubt → kein Lock noetig.
            # WICHTIG: Isolierte Sessions via chat_send_isolated gibt's NUR fuer Gateway-Agents
            # (openclaw). cli-bridge / host / claude-code haben single-tmux-session — busy-check
            # MUSS aktiv bleiben, sonst ueberschreiben wir deren Context mit dem neuen Task
            # (poll.sh sieht neue task_id → /clear → Worker verliert Arbeit am aktuellen Task).
            # Bug beobachtet 2026-04-22: Tester verlor Kontext als 2 Tasks pre-assigned kamen.
            _runtime = getattr(best_agent, "agent_runtime", "openclaw")
            _has_isolated_sessions = _runtime not in NON_GATEWAY_RUNTIMES
            _skip_busy = (
                settings.use_subagent_dispatch
                and not best_agent.is_board_lead
                and _has_isolated_sessions
            )

            if not _skip_busy:
                if not await acquire_dispatch_lock(agent_id_str, ttl=30):
                    # Lock belegt → Task in Queue statt droppen
                    await enqueue_task(agent_id_str, str(task.id))
                    logger.info("Dispatch lock busy: '%s' -> %s (queued)", task.title, best_agent.name)
                    return

            try:
                # ── UNIFIED PUSH MODE (alle Agents) ─────────────────────

                if not _skip_busy:
                    # Pruefen ob Agent beschaeftigt → Queue
                    # Guard 1: current_task_id (atomares Lock)
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

                    # Guard 2: Busy = in_progress ODER dispatched-but-not-acked (DB-basiert)
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

                # Status bleibt inbox — Agent muss selbst ACKen (PATCH status: in_progress)
                # Runtime-Readiness + Auslieferung (claude-code / cli-bridge / host /
                # openclaw) — extrahiert nach dispatch_delivery.py (REF-01 Step 3).
                # Pitfall A: Helper liest rpc + settings via dispatch-namespace damit
                # test_dispatch_race + test_subagent_dispatch Patches durchschlagen.
                from app.services.dispatch_delivery import (
                    _check_runtime_readiness, _deliver_dispatch_message,
                )
                if not await _check_runtime_readiness(
                    task, best_agent, session, board_id, agent_id_str,
                ):
                    dispatch_mode = "push_pending"
                else:
                    # dispatch_attempt_id VOR Message-Build generieren,
                    # damit sie in der Message an den Agent mitgegeben werden kann.
                    #
                    # Race-Fix (2026-05-15, post doppelter-dispatch incident):
                    # die alte "if not task.dispatch_attempt_id: set" Logik war
                    # nicht atomar — bei git-clone Race (5s Fenster) konnten
                    # /agent/me/poll und auto_dispatch_task beide gleichzeitig
                    # NULL sehen und beide eine UUID setzen, letzter Commit
                    # gewann. set_dispatch_attempt_id(only_if_null=True) macht
                    # einen conditional UPDATE … WHERE attempt_id IS NULL —
                    # first-writer-wins, race-frei. Plus Audit-Trail in
                    # task_attempt_audit für künftige Forensik.
                    from app.services.dispatch_attempt_audit import (
                        set_dispatch_attempt_id,
                    )
                    await set_dispatch_attempt_id(
                        session, task, str(uuid.uuid4()),
                        caller="auto_dispatch",
                        reason="initial_dispatch",
                        only_if_null=True,
                    )

                    # Recovery-Kontext: wenn der Task bereits Kommentare hat
                    # (z.B. nach Blocker-Re-Dispatch), vorherigen Fortschritt
                    # + Operator-Antwort in die Dispatch-Message einbauen.
                    _recovery_ctx = await build_recovery_context(session, task)
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
    """Public function for HTTP-Poll Queue — returns prompt string for agent.

    Laedt Recovery-Context wenn der Task bereits bearbeitet wurde (via Comments
    oder Checklist-Items). So bekommt der Agent beim Re-Dispatch nach einem
    Container-/Host-Restart nicht den Original-Prompt wieder (→ neu anfangen),
    sondern einen "Du hast hier aufgehoert, setze fort" Kontext.

    build_recovery_context returned None fuer frische Tasks ohne History —
    dann ist das Verhalten identisch zum alten Poll-Pfad (nur Task-Prompt).
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
