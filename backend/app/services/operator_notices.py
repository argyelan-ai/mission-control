"""Passive notices for watchdog-generated STOERUNGSMELDUNGEN.

dispatch_escalation, lead_escalation, review_stuck and dependency_zombie are
watchdog-detected problems, not operator decisions — they get no yes/no
question anymore (see approval_cleanup.NOTICE_ONLY_ACTION_TYPES). Instead
they raise a passive `operator.notice` activity event plus a plain-text
report, best effort.

Two Redis markers (TTL 6h, mirrors the review_feedback: a marker outlives
any single watchdog tick):
- ``RedisKeys.notice_marker(task_id, action_type)`` — per-type, SET NX.
  Caps ``send_report`` to once per action_type per card per TTL window
  (Pruefbericht Punkt 4, spam guard) while the ``operator.notice`` activity
  event is still written every tick (audit trail).
- ``RedisKeys.notice_marker_any(task_id)`` — collective, refreshed on every
  ``raise_notice`` call. Read by ``notice_active()`` so the silent-card
  watchdog and the retraction check can treat "Approval pending OR
  notice_active" the same, without a SCAN over per-type keys
  (Pruefbericht B1/B2).
"""

import logging

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import Task
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.services.approval_cleanup import NOTICE_ONLY_ACTION_TYPES
from app.services.operator_reports import send_report

logger = logging.getLogger("mc.operator_notices")

NOTICE_MARKER_TTL_SECONDS = 6 * 3600


async def raise_notice(
    session: AsyncSession,
    *,
    action_type: str,
    task: Task,
    title: str,
    body: str,
    agent_id=None,
    board_id=None,
) -> None:
    """Emit an `operator.notice` activity event and send a plain-text report.

    The event is written every call (audit trail). The report is sent at
    most once per action_type per card per NOTICE_MARKER_TTL_SECONDS window
    (Pruefbericht Punkt 4 — review_stuck/dependency_zombie tick every 2h/4h
    and would otherwise flood the reports channel).

    Best effort: a report-backend failure is logged, never raised — a
    watchdog check must not blow up because Telegram/Slack is down.
    """
    assert action_type in NOTICE_ONLY_ACTION_TYPES, (
        f"raise_notice: action_type={action_type!r} is not one of the "
        f"notice-only types {sorted(NOTICE_ONLY_ACTION_TYPES)} — a new "
        f"creation site was wired up without adding it there first."
    )

    resolved_board_id = board_id if board_id is not None else task.board_id

    await emit_event(
        session,
        "operator.notice",
        title,
        severity="warning",
        board_id=resolved_board_id,
        task_id=task.id,
        agent_id=agent_id,
        detail={"action_type": action_type, "title": title, "body": body},
    )

    try:
        redis = await get_redis()
        # Collective marker: refreshed on every call, no NX — it just needs
        # to outlive the TTL window while ANY notice type is active on this
        # card, for notice_active() below.
        await redis.set(
            RedisKeys.notice_marker_any(str(task.id)),
            "1", ex=NOTICE_MARKER_TTL_SECONDS,
        )
        # Per-type marker: SET NX — only the FIRST call within the TTL
        # window wins and sends the report; later calls in the same window
        # still emit the event above but skip the report (anti-spam).
        is_first_in_window = bool(await redis.set(
            RedisKeys.notice_marker(str(task.id), action_type),
            "1", nx=True, ex=NOTICE_MARKER_TTL_SECONDS,
        ))
    except Exception:
        logger.exception(
            "raise_notice: redis marker failed for action_type=%s task_id=%s "
            "— sending report anyway (fail open, not closed)",
            action_type, task.id,
        )
        is_first_in_window = True

    if not is_first_in_window:
        return

    try:
        card = f"#{str(task.id)[:8]}"
        text = f"{title}\n{card} {task.title}\n{body}"
        delivered, _results = await send_report(text)
        if not delivered:
            logger.warning(
                "raise_notice: send_report did not deliver (no backend "
                "configured or all failed) for action_type=%s task_id=%s",
                action_type, task.id,
            )
    except Exception:
        logger.exception(
            "raise_notice: send_report failed for action_type=%s task_id=%s",
            action_type, task.id,
        )


async def retract_notice(
    session: AsyncSession,
    *,
    action_type: str,
    task: Task,
    title: str,
    body: str,
    board_id=None,
) -> None:
    """Retract a previously raised notice-only escalation (Pruefbericht B2).

    The notice path leaves no Approval row for
    ``_check_silent_card_retractions`` to supersede, so that watchdog would
    otherwise leave the operator with the original "ESKALATION" and never
    the "Entwarnung" once the card moves again. Called instead, in that
    same place, when no pending Approval exists but a notice was active.

    Deletes both markers (per-action_type and collective). Simplification
    over tracking "any OTHER type still active" (which would need a
    per-task set, not just two plain keys): the collective marker's only
    other reader is the silent-card watchdog's "already has a case"
    suppression. If a DIFFERENT notice type is genuinely still active on
    this card, the collective marker stays gone until that type's own
    watchdog fires its next tick and calls raise_notice() again — governed
    by that type's own dedup gate (2h review_stuck, 4h dependency_zombie,
    24h dispatch_escalation), so the gap can be as long as that gate. The
    risk is small: retraction only runs when the card genuinely moved
    (evidence-based, see _silent_card_last_activity_at), which is rare
    relative to the dedup window, and a stale gap only means one extra
    stage-1 notify at worst, not a lost escalation.
    """
    resolved_board_id = board_id if board_id is not None else task.board_id

    try:
        redis = await get_redis()
        await redis.delete(RedisKeys.notice_marker(str(task.id), action_type))
        await redis.delete(RedisKeys.notice_marker_any(str(task.id)))
    except Exception:
        logger.exception(
            "retract_notice: redis marker delete failed for action_type=%s task_id=%s",
            action_type, task.id,
        )

    await emit_event(
        session,
        "operator.notice_retracted",
        title,
        severity="info",
        board_id=resolved_board_id,
        task_id=task.id,
        detail={"action_type": action_type, "title": title, "body": body},
    )

    try:
        card = f"#{str(task.id)[:8]}"
        text = f"{title}\n{card} {task.title}\n{body}"
        delivered, _results = await send_report(text)
        if not delivered:
            logger.warning(
                "retract_notice: send_report did not deliver for "
                "action_type=%s task_id=%s",
                action_type, task.id,
            )
    except Exception:
        logger.exception(
            "retract_notice: send_report failed for action_type=%s task_id=%s",
            action_type, task.id,
        )


async def notice_active(task_id) -> bool:
    """True if any notice-only escalation is currently active on this card
    (collective marker still within its TTL window). Used by the
    silent-card watchdog (treat "Approval pending OR notice_active" the
    same) and by the retraction check (emit a notice-retraction instead of
    superseding an Approval row that no longer exists)."""
    try:
        redis = await get_redis()
        return bool(await redis.get(RedisKeys.notice_marker_any(str(task_id))))
    except Exception:
        logger.exception("notice_active: redis read failed for task_id=%s", task_id)
        return False
