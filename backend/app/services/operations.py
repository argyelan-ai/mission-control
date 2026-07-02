"""
Operational Controls — System Mode, Dispatch Guards, Stop/Resume.

Zentrale Stelle fuer alle Betriebssteuerungs-Logik:
- System Mode (active/draining/halted) via Redis
- Dispatch Guard (check_dispatch_allowed) — DRY fuer alle 5 Dispatch-Einstiegspunkte
- Stop Run / Resume Task Run
- Continuation Flow Detection (explizit per dispatch_intent)
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
    """Aktuellen System Mode aus Redis lesen. Fail-Open: 'active' bei Fehler."""
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
    """System Mode setzen + Meta-Daten speichern."""
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
    """System Mode Meta-Daten lesen."""
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
    """Explizit: nur automatische Flows (subtask, review_handoff, review_rework).

    manual_redispatch ist KEINE Continuation — Drain darf nicht still umgangen werden.
    """
    return getattr(task, "dispatch_intent", "root") in CONTINUATION_INTENTS


# ── Dispatch Guard ─────────────────────────────────────────────────────

async def check_dispatch_allowed(
    task: Task,
    agent: Agent | None,
    session: AsyncSession | None = None,
) -> tuple[bool, str]:
    """Zentrale Dispatch-Prüfung. Gibt (erlaubt, grund) zurück.

    Prioritätsreihenfolge:
    1. System HALTED → blockiert alles
    2. Task run_control → blockiert diesen Task
    3. Agent PAUSED → blockiert diesen Agent
    3.5 Runtime-Readiness → power-managed Backend (PORSCHE) muss wach sein
    4. System DRAINING → blockiert nicht-Continuation Flows

    `session` ist optional: nur wenn übergeben greift das Runtime-Readiness-Gate
    (es braucht einen DB-Lookup). Ohne session bleibt das Verhalten unverändert —
    bestehende Unit-Tests rufen ohne session auf.
    """
    system_mode = await get_system_mode()

    # 1. System HALTED
    if system_mode == "halted":
        return False, "System HALTED"

    # 2. Task run_control
    if task.run_control in ("manual_hold", "stopped"):
        return False, f"Task run_control: {task.run_control}"

    # 2.5 Pre-Dispatch Gate — Planning-Tasks werden nicht dispatcht
    # AUSNAHME: Review-Handoff und Review-Rework duerfen passieren,
    # da sie interne System-Flows sind (Developer→Reviewer bzw. Reviewer→Developer).
    # Subtask-Dispatch bleibt blockiert — das ist der Kern des Gatings.
    _REVIEW_INTENTS = {"review_handoff", "review_rework"}
    if settings.enable_dispatch_gating and getattr(task, "dispatch_phase", None) == "planning":
        intent = getattr(task, "dispatch_intent", "root")
        if intent not in _REVIEW_INTENTS:
            return False, "Task in planning phase — dispatch blocked"

    # 3. Agent PAUSED
    if agent and agent.operational_mode == "paused":
        return False, f"Agent {agent.name} PAUSED"

    # 3.5 Runtime-Readiness Gate — ein power-managed Backend (z.B. PORSCHE
    # unsloth) muss wach + am Servieren sein, bevor ein Task injiziert wird.
    # Greift nur wenn eine session da ist UND der Agent an eine power_managed
    # Runtime gebunden ist — jeder andere Agent ist unberührt (fail-open bei
    # Fehlern). Siehe services/runtime_readiness.py.
    if session is not None and agent is not None:
        from app.services.runtime_readiness import runtime_ready_for_agent
        rt_ready, rt_reason = await runtime_ready_for_agent(agent, session)
        if not rt_ready:
            return False, rt_reason or "Runtime nicht bereit"

    # 4. Agent Liveness Check — ist der Agent erreichbar?
    # Phase 30: Gateway-Session-Gating entfernt. Heartbeat ist nur noch ein
    # weiches Signal — Cold-Start + Stale-Agents werden zum Dispatch
    # zugelassen (poll-basierte Runtimes nehmen Tasks aktiv ab; Watchdog +
    # ACK-Timeout im task_runner uebernehmen die echte Liveness-Eskalation).
    if agent and agent.last_seen_at:
        seconds_since_heartbeat = (utcnow() - agent.last_seen_at).total_seconds()
        if seconds_since_heartbeat > 900:  # > 15 Min
            logger.warning(
                "Dispatch warning: Agent %s hat seit %.0f Min keinen Heartbeat "
                "(Dispatch trotzdem erlaubt — poll-runtime holt sich Tasks aktiv)",
                agent.name, seconds_since_heartbeat / 60,
            )
        elif seconds_since_heartbeat > 300:  # > 5 Min
            logger.info(
                "Dispatch info: Agent %s Heartbeat ist %.0f Min alt",
                agent.name, seconds_since_heartbeat / 60,
            )

    # 5. System DRAINING — nur automatische Continuation Flows
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
    """Aktiven Task-Run stoppen. Nur für Tasks mit aktivem Run."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Validierung: Nur aktive Runs stoppen
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

    # Phase 29: Gateway-Session-Reset entfaellt mit dem Gateway-Sunset.
    # spawn_session_key bleibt im Modell bis Phase 30 (DB-Drop). cli-bridge /
    # host / claude-code Runtimes haben kein remote-session-Konzept — die
    # Re-Dispatch-Logik in dispatch.py setzt neue Worker-Sessions auf.
    # TODO Phase 30: drop spawn_session_key column.

    # 2. Task updaten
    old_status = task.status
    task.run_control = "stopped"
    task.status = "blocked"
    task.dispatched_at = None
    task.ack_at = None
    # Invalidiert alle ausstehenden Agent-Updates (audit trail).
    from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
    await clear_dispatch_attempt_id(
        session, task, caller="user_stop", reason="manual_stop",
    )
    task.review_decision = None  # Alte Review-Entscheidung irrelevant nach Stop
    task.review_decided_at = None
    clear_spawn_tracking(task)
    task.updated_at = utcnow()

    # 3. Agent-Lock freigeben aber assigned_agent_id BEHALTEN. Manual-Stop
    # ist temporaer, beim Resume soll der Task wieder an denselben Agent.
    #
    # Historischer Kontext: hier stand frueher `apply_terminal_unassign` —
    # Begruendung "Verhindert Cancel-Schleife im agent_poll". Die Cancel-
    # Schleife wurde inzwischen via state="stopped" in agents.py:2635 sauber
    # geloest: der Agent-Poll returnt state="stopped" sobald Task.assigned_
    # agent_id == agent.id UND Task.run_control == "stopped". Der Poll.sh
    # terminiert dann die Session ESC + /clear + context reset ohne den Task
    # als failed zu behandeln. Dazu MUSS assigned_agent_id aber gesetzt
    # bleiben — sonst sieht der Agent den Stop gar nicht + Task wird beim
    # Resume orphaned.
    # Live-Bug 2026-04-24: Der Operator stoppte Sparky-Task + restartete Container,
    # beim Resume landete Task in inbox mit assigned_agent_id=None.
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

    # Kein fake User-Comment mehr (ADR-024 / Ultrareview PS). Stop ist ein
    # Lifecycle-Event, kein Operator-Kommentar. Die ActivityEvent-Audit-Trail
    # unten plus der neue poll-state="stopped" geben dem Agent + UI den
    # nötigen Kontext. Optional: reason wird im Event-detail festgehalten.
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
    """Gestoppten/gehaltenen Task wieder freigeben. Status → inbox für normalen Dispatch-Flow."""
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

    # 1. Vollständiger Reset + frische dispatch_attempt_id. Wir generieren
    # die ID hier sofort (statt auf den nächsten Dispatch zu warten), damit
    # poll.sh sie im Response lesen und in /tmp/mc-context.env schreiben
    # kann. Ohne das würde der Agent mit alter attempt_id senden → 409.
    task.run_control = None
    task.status = "inbox"
    task.dispatched_at = None
    task.ack_at = None
    from app.services.dispatch_attempt_audit import set_dispatch_attempt_id
    await set_dispatch_attempt_id(
        session, task, str(uuid.uuid4()),
        caller="user_resume", reason="manual_resume",
    )
    task.review_decision = None  # Alte Review-Entscheidung irrelevant nach Resume
    task.review_decided_at = None
    clear_spawn_tracking(task)
    task.updated_at = utcnow()
    session.add(task)

    # 2. Audit Trail
    await record_task_event(
        session, task.id, old_status, "inbox",
        changed_by="user", reason="manual_resume",
    )

    # Kein fake User-Comment mehr. Resume = frische Prompt-Lieferung via
    # regulärem /me/poll inbox-claim Pfad — der Agent bekommt die volle
    # Dispatch-Message (nicht nur ein "Der Operator hat kommentiert"-Wrapper).
    await emit_event(
        session, "task.run_resumed",
        f"Task '{task.title}' resumed",
        board_id=task.board_id, task_id=task.id,
        detail={"resumed_by": user_id, "old_status": old_status, "old_run_control": old_run_control},
    )

    # Auto-Re-Dispatch: wenn der Task einen Assignee hat (neuer Pfad seit
    # Fix 2026-04-24), schicken wir sofort eine frische Dispatch-Message an
    # den Agent. Ohne das muss der Agent-Poll zufaellig im naechsten Cycle
    # aufnehmen und verlaesst sich auf die inbox-claim Logik — der
    # Dispatch-Prompt kommt aber schneller + verlaesslicher via direktem
    # auto_dispatch_task. Wenn kein Assignee → find_dispatch_target waehlt
    # einen (Board-Lead-First wie bei frischem Task).
    # Commit ist noch nicht passiert — caller commitet. Dispatch oeffnet
    # eigene session intern.
    await session.commit()  # Ensure resume-state persisted before dispatch reads

    from app.services.dispatch import auto_dispatch_task
    try:
        await auto_dispatch_task(task.id, task.board_id)
    except Exception as e:
        logger.warning("Auto-dispatch nach Resume fehlgeschlagen fuer %s: %s", task.id, e)

    return task
