"""Lead-first Blocker-Triage (Autonomy Hardening, Fix A).

Eskalations-Leiter fuer Agent-Blocker:

  Stufe 1 (Lead-Triage):  Blocker → Board-Lead bekommt einen actionable
                          Kommentar und darf selbst loesen (resolution +
                          PATCH in_progress). KEIN Operator-Approval,
                          KEIN Telegram.
  Stufe 2 (Operator):     Erst wenn das Triage-Fenster ablaeuft (Watchdog)
                          oder der Lead explizit eskaliert
                          (comment_type=escalate_to_operator), entsteht das
                          blocker_decision-Approval + Telegram — wie frueher.

Direkt zu Stufe 2 gehen:
  - blocker_type in {decision_needed, permission_needed} (echte Operator-Entscheide)
  - Boards mit blocker_triage_minutes == 0 (Triage abgeschaltet)
  - Boards ohne (anderen) Lead — niemand da, der triagieren koennte

Das Blocker-Payload der Stufe 1 lebt in Redis (kein Approval-Row existiert
noch). Faellt Redis aus, rekonstruiert die Eskalation das Payload aus dem
letzten Blocker-Kommentar — gleiche Degradation wie der Alt-Flow.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.approval import Approval
from app.models.task import Task, TaskComment
from app.redis_client import get_redis
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger("mc.blocker_triage")

# Blocker-Typen, die immer ein Operator-Entscheid sind — keine Lead-Triage.
OPERATOR_ONLY_BLOCKER_TYPES = {"decision_needed", "permission_needed"}

TRIAGE_KEY_PREFIX = "mc:blocker:triage"
# TTL deutlich groesser als jedes plausible Triage-Fenster; verwaiste Keys
# (Task verliess blocked ohne Eskalation) verfallen von selbst.
TRIAGE_PAYLOAD_TTL = 48 * 3600


def triage_key(task_id: uuid.UUID | str) -> str:
    return f"{TRIAGE_KEY_PREFIX}:{task_id}"


async def store_triage_payload(task_id: uuid.UUID, payload: dict) -> None:
    try:
        redis = await get_redis()
        await redis.set(triage_key(task_id), json.dumps(payload), ex=TRIAGE_PAYLOAD_TTL)
    except Exception as exc:  # noqa: BLE001 — Redis ist optional, Fallback existiert
        logger.warning("Triage-Payload konnte nicht gespeichert werden (%s): %s", task_id, exc)


async def load_triage_payload(task_id: uuid.UUID) -> dict | None:
    try:
        redis = await get_redis()
        raw = await redis.get(triage_key(task_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Triage-Payload konnte nicht gelesen werden (%s): %s", task_id, exc)
        return None


async def clear_triage_payload(task_id: uuid.UUID) -> None:
    try:
        redis = await get_redis()
        await redis.delete(triage_key(task_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Triage-Payload delete fehlgeschlagen (%s): %s", task_id, exc)


def is_lead_agent(agent: Agent) -> bool:
    """Einheitliche Lead-Erkennung fuer Triage-Rechte (Gate-Ausnahme,
    escalate_to_operator). `is_board_lead` UND role='lead' zaehlen — die
    Triage-Benachrichtigung laeuft ueber find_agent_by_role(LEAD), das auf
    `role` matcht; wer benachrichtigt wird, muss auch loesen duerfen
    (sonst 403 fuer den zustaendigen Lead = Incident-Reproduktion)."""
    if agent.is_board_lead:
        return True
    return (agent.role or "").strip().lower() == "lead"


async def find_board_lead(
    session: AsyncSession, board_id: uuid.UUID,
) -> Agent | None:
    result = await session.exec(
        select(Agent).where(
            Agent.board_id == board_id,
            Agent.is_board_lead == True,  # noqa: E712
        )
    )
    return result.first()


async def start_lead_triage(
    session: AsyncSession,
    *,
    task: Task,
    agent: Agent,
    lead: Agent,
    blocker_payload: dict,
    triage_minutes: int,
) -> None:
    """Stufe 1: Blocker-Payload parken + Lead actionable informieren."""
    await store_triage_payload(task.id, blocker_payload)

    question = blocker_payload.get("question") or ""
    description = blocker_payload.get("description") or ""
    msg = (
        f"BLOCKER (Lead-Triage): {agent.name} bei \"{task.title}\"\n\n"
        f"Typ: {blocker_payload.get('blocker_type', 'other')}\n"
        f"Frage: {question}\n"
        + (f"Detail: {description}\n" if description else "")
        + f"Task-ID: {task.id}\n\n"
        f"DU bist zustaendig — es gibt (noch) kein Operator-Approval.\n"
        f"1. Loesen: `resolution`-Kommentar auf den Task posten + Task via PATCH auf "
        f"`in_progress` setzen (du darfst das als Lead) → Auto-Redispatch.\n"
        f"2. Eskalieren (nur wenn wirklich ein Operator-Entscheid noetig ist): "
        f"Kommentar mit `comment_type: escalate_to_operator` auf den Task posten.\n"
        f"Loest du nicht innerhalb von {triage_minutes} Minuten, eskaliert der "
        f"Watchdog automatisch an den Operator."
    )
    session.add(TaskComment(
        task_id=task.id,
        author_type="system",
        content=msg,
        comment_type="blocker_lead_notify",
    ))
    await session.commit()

    await emit_event(
        session,
        "blocker.lead_notified",
        f"Blocker-Triage: {agent.name} blockiert bei \"{task.title}\" → Lead {lead.name} ({triage_minutes}min Fenster)",
        board_id=task.board_id,
        task_id=task.id,
        agent_id=agent.id,
        severity="info",
        detail={
            "blocker_type": blocker_payload.get("blocker_type"),
            "lead_id": str(lead.id),
            "triage_minutes": triage_minutes,
        },
    )
    logger.info(
        "Lead-Triage gestartet: task=%s agent=%s lead=%s fenster=%dmin",
        task.id, agent.name, lead.name, triage_minutes,
    )


async def _payload_from_comments(
    session: AsyncSession, task: Task,
) -> dict:
    """Fallback: Blocker-Payload aus dem letzten Blocker-Kommentar rekonstruieren."""
    cmt = (await session.exec(
        select(TaskComment)
        .where(TaskComment.task_id == task.id)
        .where(TaskComment.comment_type.in_(("blocker", "blocker_lead_notify")))  # type: ignore[union-attr]
        .order_by(TaskComment.created_at.desc())
        .limit(1)
    )).first()
    text = cmt.content[:2000] if cmt else "Kein Blocker-Kommentar"
    return {
        "blocker_type": "other",
        "question": text[:1000],
        "description": "",
        "blocker_comment": text,
    }


async def escalate_blocker_to_operator(
    session: AsyncSession,
    *,
    task: Task,
    reason: str,
    blocker_payload: dict | None = None,
    lead_context: bool = True,
) -> Approval | None:
    """Stufe 2: blocker_decision-Approval erstellen + Telegram + Warning-Event.

    ``reason``: "direct" (Typ/Board erzwingt Operator), "triage_timeout"
    (Watchdog), "lead_escalated" (expliziter Lead-Entscheid).
    Idempotent: existiert bereits ein pending Approval, passiert nichts.
    """
    existing = (await session.exec(
        select(Approval).where(
            Approval.task_id == task.id,
            Approval.action_type == "blocker_decision",
            Approval.status == "pending",
        )
    )).first()
    if existing:
        return None

    if blocker_payload is None:
        blocker_payload = await load_triage_payload(task.id)
    if blocker_payload is None:
        blocker_payload = await _payload_from_comments(session, task)

    agent = await session.get(Agent, task.assigned_agent_id) if task.assigned_agent_id else None
    agent_name = agent.name if agent else "unbekannt"

    # Lead-Kontext anreichern: was hat der Lead waehrend der Triage kommentiert?
    if lead_context:
        lead = await find_board_lead(session, task.board_id)
        if lead:
            lead_comments = (await session.exec(
                select(TaskComment)
                .where(
                    TaskComment.task_id == task.id,
                    TaskComment.author_agent_id == lead.id,
                )
                .order_by(TaskComment.created_at.desc())
                .limit(2)
            )).all()
            if lead_comments:
                blocker_payload["lead_triage_notes"] = [
                    c.content[:500] for c in lead_comments
                ]

    blocker_payload.setdefault("task_title", task.title)
    blocker_payload["escalation_reason"] = reason
    if agent:
        blocker_payload.setdefault("blocked_agent_id", str(agent.id))
        blocker_payload.setdefault("blocked_agent_name", agent.name)

    approval = Approval(
        board_id=task.board_id,
        task_id=task.id,
        agent_id=task.assigned_agent_id,
        action_type="blocker_decision",
        description=f"{agent_name} ist blockiert bei \"{task.title}\"",
        payload=blocker_payload,
        expires_at=utcnow() + timedelta(hours=24),
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)
    await clear_triage_payload(task.id)

    try:
        from app.services.telegram_bot import telegram_bot
        await telegram_bot.send_approval_telegram(
            approval.id, agent_name, task.title,
            blocker_payload.get("blocker_comment") or blocker_payload.get("question") or "",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Telegram approval failed: %s", e)

    await emit_event(
        session,
        "blocker.escalated_to_operator" if reason != "direct" else "approval.created",
        f"Blocker-Approval: {agent_name} blockiert bei \"{task.title}\" ({reason})",
        board_id=task.board_id,
        task_id=task.id,
        agent_id=task.assigned_agent_id,
        severity="warning",
        detail={"escalation_reason": reason},
    )
    logger.info(
        "Blocker eskaliert an Operator: task=%s reason=%s", task.id, reason,
    )
    return approval
