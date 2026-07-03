"""
Operational Controls — System Mode, Dispatch Guards, Stop/Resume.

Central place for all operational-control logic:
- System Mode (active/draining/halted) via Redis
- Dispatch Guard (check_dispatch_allowed) — DRY for all 5 dispatch entry points
- Stop Run / Resume Task Run
- Continuation Flow Detection (explicit via dispatch_intent)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.task import Task, TaskComment
from app.redis_client import RedisKeys, get_redis
from app.config import settings
from app.services.activity import emit_event
from app.services.task_lifecycle import clear_spawn_tracking, record_task_event
from app.utils import utcnow

logger = logging.getLogger(__name__)

# ── System Mode ────────────────────────────────────────────────────────

VALID_SYSTEM_MODES = {"active", "draining", "halted"}


async def get_system_mode() -> str:
    """Read current System Mode from Redis. Fail-open: 'active' on error."""
    try:
        redis = await get_redis()
        if redis is None:
            return "active"
        mode = await redis.get(RedisKeys.system_mode())
        if mode and mode in VALID_SYSTEM_MODES:
            return mode
        return "active"
    except Exception:
        logger.warning("Redis nicht erreichbar — System Mode defaults auf 'active'")
        return "active"


async def set_system_mode(mode: str, changed_by: str, reason: str = "") -> dict:
    """Set System Mode + store metadata."""
    if mode not in VALID_SYSTEM_MODES:
        raise ValueError(f"Ungültiger System Mode: {mode}")

    redis = await get_redis()
    if redis is None:
        raise RuntimeError("Redis nicht verfügbar")

    previous_mode = await get_system_mode()
    now = utcnow().isoformat()

    meta = {
        "mode": mode,
        "previous_mode": previous_mode,
        "changed_by": changed_by,
        "changed_at": now,
        "reason": reason,
    }

    await redis.set(RedisKeys.system_mode(), mode)
    await redis.set(RedisKeys.system_mode_meta(), json.dumps(meta))

    logger.info("System Mode: %s → %s (by %s, reason: %s)", previous_mode, mode, changed_by, reason)
    return meta


async def get_system_mode_meta() -> dict:
    """Read System Mode metadata."""
    try:
        redis = await get_redis()
        if redis is None:
            return {"mode": "active", "previous_mode": None, "changed_by": None, "changed_at": None, "reason": ""}
        raw = await redis.get(RedisKeys.system_mode_meta())
        if raw:
            return json.loads(raw)
        return {"mode": await get_system_mode(), "previous_mode": None, "changed_by": None, "changed_at": None, "reason": ""}
    except Exception:
        return {"mode": "active", "previous_mode": None, "changed_by": None, "changed_at": None, "reason": ""}


# ── Continuation Flow ──────────────────────────────────────────────────

CONTINUATION_INTENTS = {"subtask", "review_handoff", "review_rework"}


def is_continuation_flow(task: Task) -> bool:
    """Explicit: only automatic flows (subtask, review_handoff, review_rework).

    manual_redispatch is NOT a continuation — Drain must not be silently bypassed.
    """
    return getattr(task, "dispatch_intent", "root") in CONTINUATION_INTENTS


# ── Dispatch Guard ─────────────────────────────────────────────────────

async def check_dispatch_allowed(
    task: Task,
    agent: Agent | None,
    session: AsyncSession | None = None,
) -> tuple[bool, str]:
    """Central dispatch check. Returns (allowed, reason).

    Priority order:
    1. System HALTED → blocks everything
    2. Task run_control → blocks this task
    3. Agent PAUSED → blocks this agent
    3.5 Runtime-Readiness → power-managed backend (PORSCHE) must be awake
    4. System DRAINING → blocks non-continuation flows

    `session` is optional: the runtime-readiness gate only kicks in when it's
    passed (it needs a DB lookup). Without a session, behavior is unchanged —
    existing unit tests call this without a session.
    """
    system_mode = await get_system_mode()

    # 1. System HALTED
    if system_mode == "halted":
        return False, "System HALTED"

    # 2. Task run_control
    if task.run_control in ("manual_hold", "stopped"):
        return False, f"Task run_control: {task.run_control}"

    # 2.5 Pre-Dispatch Gate — planning tasks are not dispatched
    # EXCEPTION: review-handoff and review-rework are allowed through,
    # since they're internal system flows (Developer→Reviewer resp. Reviewer→Developer).
    # Subtask dispatch stays blocked — that's the core of the gating.
    _REVIEW_INTENTS = {"review_handoff", "review_rework"}
    if settings.enable_dispatch_gating and getattr(task, "dispatch_phase", None) == "planning":
        intent = getattr(task, "dispatch_intent", "root")
        if intent not in _REVIEW_INTENTS:
            return False, "Task in planning phase — dispatch blocked"

    # 3. Agent PAUSED
    if agent and agent.operational_mode == "paused":
        return False, f"Agent {agent.name} PAUSED"

    # 3.5 Runtime-Readiness Gate — a power-managed backend (e.g. PORSCHE
    # unsloth) must be awake + serving before a task is injected.
    # Only kicks in when a session is present AND the agent is bound to a
    # power_managed runtime — every other agent is unaffected (fail-open on
    # errors). See services/runtime_readiness.py.
    if session is not None and agent is not None:
        from app.services.runtime_readiness import runtime_ready_for_agent
        rt_ready, rt_reason = await runtime_ready_for_agent(agent, session)
        if not rt_ready:
            return False, rt_reason or "Runtime nicht bereit"

    # 4. Agent Liveness Check — is the agent reachable?
    # Phase 30: Gateway-session gating removed. Heartbeat is now just a soft
    # signal — cold-start + stale agents are still allowed to dispatch
    # (poll-based runtimes pull tasks actively; watchdog + ACK-timeout in
    # task_runner handle the real liveness escalation).
    if agent and agent.last_seen_at:
        seconds_since_heartbeat = (utcnow() - agent.last_seen_at).total_seconds()
        if seconds_since_heartbeat > 900:  # > 15 min
            logger.warning(
                "Dispatch warning: Agent %s hat seit %.0f Min keinen Heartbeat "
                "(Dispatch trotzdem erlaubt — poll-runtime holt sich Tasks aktiv)",
                agent.name, seconds_since_heartbeat / 60,
            )
        elif seconds_since_heartbeat > 300:  # > 5 min
            logger.info(
                "Dispatch info: Agent %s Heartbeat ist %.0f Min alt",
                agent.name, seconds_since_heartbeat / 60,
            )

    # 5. System DRAINING — only automatic continuation flows
    if system_mode == "draining" and not is_continuation_flow(task):
        return False, "System DRAINING — nur automatische Flows erlaubt"

    return True, ""


# ── Stop Run ───────────────────────────────────────────────────────────

async def stop_task_run(
    session: AsyncSession,
    task_id: uuid.UUID,
    user_id: str,
    reason: str = "",
) -> Task:
    """Stop an active task run. Only for tasks with an active run."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Validation: only stop active runs
    has_active_run = (
        task.status == "in_progress"
        or (task.status == "inbox" and task.dispatched_at is not None)
        or task.status == "review"
    )
    if not has_active_run:
        raise HTTPException(
            status_code=409,
            detail=f"Task hat keinen aktiven Run (status={task.status}, dispatched_at={task.dispatched_at}). "
                   "Für nicht-gestartete Tasks: Hold/Unhold verwenden."
        )

    agent = await session.get(Agent, task.assigned_agent_id) if task.assigned_agent_id else None

    # Phase 29: Gateway session reset no longer applies with the gateway sunset.
    # spawn_session_key stays on the model until Phase 30 (DB drop). cli-bridge /
    # host / claude-code runtimes have no remote-session concept — the
    # re-dispatch logic in dispatch.py sets up new worker sessions.
    # TODO Phase 30: drop spawn_session_key column.

    # 2. Update task
    old_status = task.status
    task.run_control = "stopped"
    task.status = "blocked"
    task.dispatched_at = None
    task.ack_at = None
    # Invalidates all pending agent updates (audit trail).
    from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
    await clear_dispatch_attempt_id(
        session, task, caller="user_stop", reason="manual_stop",
    )
    task.review_decision = None  # Old review decision is irrelevant after stop
    task.review_decided_at = None
    clear_spawn_tracking(task)
    task.updated_at = utcnow()

    # 3. Release the agent lock but KEEP assigned_agent_id. Manual stop
    # is temporary — on resume the task should go back to the same agent.
    #
    # Historical context: this used to have `apply_terminal_unassign` —
    # rationale "prevents cancel loop in agent_poll". The cancel loop has
    # since been cleanly resolved via state="stopped" in agents.py:2635: the
    # agent poll returns state="stopped" as soon as Task.assigned_
    # agent_id == agent.id AND Task.run_control == "stopped". poll.sh then
    # terminates the session (ESC + /clear + context reset) without treating
    # the task as failed. For that, assigned_agent_id MUST stay set —
    # otherwise the agent never sees the stop and the task gets orphaned on
    # resume.
    # Live bug 2026-04-24: the operator stopped a Sparky task + restarted the container,
    # on resume the task ended up in inbox with assigned_agent_id=None.
    if agent:
        agent.run_state = "idle"
        if agent.current_task_id == task.id:
            agent.current_task_id = None
        session.add(agent)

    # 5. Audit Trail
    await record_task_event(
        session, task.id, old_status, "blocked",
        changed_by="user", reason="manual_stop",
    )

    # No more fake user comment (ADR-024 / Ultrareview PS). Stop is a
    # lifecycle event, not an operator comment. The ActivityEvent audit trail
    # below plus the new poll state="stopped" give the agent + UI the
    # necessary context. Optional: reason is recorded in the event detail.
    session.add(task)

    await emit_event(
        session, "task.run_stopped",
        f"Task '{task.title}' Run gestoppt" + (f" — {reason}" if reason else ""),
        board_id=task.board_id, task_id=task.id,
        agent_id=task.assigned_agent_id,
        detail={"stopped_by": user_id, "reason": reason, "old_status": old_status},
    )

    return task


# ── Resume Task Run ────────────────────────────────────────────────────

async def resume_task_run(
    session: AsyncSession,
    task_id: uuid.UUID,
    user_id: str,
) -> Task:
    """Release a stopped/held task again. Status → inbox for normal dispatch flow."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.run_control not in ("stopped", "manual_hold"):
        raise HTTPException(
            status_code=409,
            detail=f"Task ist nicht gestoppt/gehalten (run_control={task.run_control})"
        )

    old_status = task.status
    old_run_control = task.run_control

    # 1. Full reset + fresh dispatch_attempt_id. We generate the ID here
    # immediately (instead of waiting for the next dispatch) so poll.sh can
    # read it from the response and write it to /tmp/mc-context.env. Without
    # this, the agent would send with the old attempt_id → 409.
    task.run_control = None
    task.status = "inbox"
    task.dispatched_at = None
    task.ack_at = None
    from app.services.dispatch_attempt_audit import set_dispatch_attempt_id
    await set_dispatch_attempt_id(
        session, task, str(uuid.uuid4()),
        caller="user_resume", reason="manual_resume",
    )
    task.review_decision = None  # Old review decision is irrelevant after resume
    task.review_decided_at = None
    clear_spawn_tracking(task)
    task.updated_at = utcnow()
    session.add(task)

    # 2. Audit Trail
    await record_task_event(
        session, task.id, old_status, "inbox",
        changed_by="user", reason="manual_resume",
    )

    # No more fake user comment. Resume = fresh prompt delivery via the
    # regular /me/poll inbox-claim path — the agent gets the full
    # dispatch message (not just an "operator commented" wrapper).
    await emit_event(
        session, "task.run_resumed",
        f"Task '{task.title}' resumed",
        board_id=task.board_id, task_id=task.id,
        detail={"resumed_by": user_id, "old_status": old_status, "old_run_control": old_run_control},
    )

    # Auto re-dispatch: if the task has an assignee (new path since
    # fix 2026-04-24), we immediately send a fresh dispatch message to
    # the agent. Without this, the agent poll would have to happen to pick
    # it up in the next cycle and rely on the inbox-claim logic — the
    # dispatch prompt arrives faster + more reliably via direct
    # auto_dispatch_task. If there's no assignee → find_dispatch_target picks
    # one (board-lead-first, same as for a fresh task).
    # Commit hasn't happened yet — the caller commits. Dispatch opens its
    # own session internally.
    await session.commit()  # Ensure resume-state persisted before dispatch reads

    from app.services.dispatch import auto_dispatch_task
    try:
        await auto_dispatch_task(task.id, task.board_id)
    except Exception as e:
        logger.warning("Auto-dispatch nach Resume fehlgeschlagen fuer %s: %s", task.id, e)

    return task
