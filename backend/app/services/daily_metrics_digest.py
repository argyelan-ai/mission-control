"""Daily Metrics Digest — "Vier Messzahlen, taeglich" (Lauf 3).

Einmal pro Tag (ab settings.daily_metrics_hour, UTC-Stunde — kein
Zeitzonen-Setting im System, Container laufen auf UTC) vier Messzahlen als
Report verschicken:

  M1 stale_cards               — offene Karten ohne Bewegung > 4h
  M2 reviews_to_lead / total   — task.review_handoff-Events letzte 24h,
                                  wie viele gingen an einen Board-Lead
  M3 double_dispatch/healer    — Dispatch-Events (< 15 min Abstand) je Task:
                                  beide auto_dispatched -> double_dispatch,
                                  sonst -> healer_repeat
  M4 hand_status_changes       — task_events mit changed_by='user' letzte
                                  24h, gruppiert nach reason

Muster: app/services/intelligence.py (Singleton, asyncio-Loop, Redis-Dedup
fuer "einmal pro Tag").
"""

import asyncio
import logging
import unicodedata
from datetime import timedelta

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.database import async_session_maker
from app.models.activity import ActivityEvent
from app.models.agent import Agent
from app.models.task import Task, TaskComment, TaskEvent
from app.redis_client import get_redis
from app.services.operator_reports import send_report
from app.utils import ensure_aware, utcnow

logger = logging.getLogger("mc.daily_metrics_digest")

_OPEN_STATUSES = ("in_progress", "review", "waiting", "blocked", "user_test")
_STALE_THRESHOLD = timedelta(hours=4)
_LOOKBACK_24H = timedelta(hours=24)
_DISPATCH_EVENT_TYPES = (
    "task.auto_dispatched",
    "task.orphaned_run_redispatched",
    "task.undispatched_recovery",
)
_DISPATCH_PAIR_WINDOW = timedelta(minutes=15)


async def compute_daily_metrics(session: AsyncSession | None, *, now=None) -> dict:
    """Berechnet die vier Messzahlen.

    ``session`` wird verwendet, wenn vorhanden (Aufrufer verwaltet die
    Lebensdauer selbst); ist keine Session gegeben, wird eine eigene ueber
    ``async_session_maker()`` geoeffnet und danach geschlossen. Reine
    SQLModel-``select()`` + Python-Nachbearbeitung — laeuft unveraendert auf
    SQLite (Tests) und Postgres.
    """
    now = ensure_aware(now) if now is not None else utcnow()
    if session is not None:
        return await _compute(session, now)
    async with async_session_maker() as own_session:
        return await _compute(own_session, now)


async def _compute(session: AsyncSession, now) -> dict:
    stale_cards = await _stale_cards(session, now)
    reviews_to_lead, reviews_total = await _reviews_to_lead(session, now)
    double_dispatch, healer_repeats = await _dispatch_pairs(session, now)
    hand_total, hand_by_reason = await _hand_status_changes(session, now)
    return {
        "stale_cards": stale_cards,
        "reviews_to_lead_24h": reviews_to_lead,
        "reviews_total_24h": reviews_total,
        "double_dispatch_24h": double_dispatch,
        "healer_repeats_24h": healer_repeats,
        "hand_status_changes_24h": hand_total,
        "hand_status_changes_by_reason": hand_by_reason,
    }


async def _stale_cards(session: AsyncSession, now) -> list[dict]:
    """M1: offene Karten, deren letzte Bewegung (updated_at, letztes
    TaskEvent, letzter TaskComment) laenger als 4h zurueckliegt."""
    result = await session.exec(select(Task).where(Task.status.in_(_OPEN_STATUSES)))
    tasks = result.all()
    if not tasks:
        return []
    task_ids = [t.id for t in tasks]

    event_max_result = await session.exec(
        select(TaskEvent.task_id, func.max(TaskEvent.created_at))
        .where(TaskEvent.task_id.in_(task_ids))
        .group_by(TaskEvent.task_id)
    )
    event_max = dict(event_max_result.all())

    comment_max_result = await session.exec(
        select(TaskComment.task_id, func.max(TaskComment.created_at))
        .where(TaskComment.task_id.in_(task_ids))
        .group_by(TaskComment.task_id)
    )
    comment_max = dict(comment_max_result.all())

    entries: list[dict] = []
    for task in tasks:
        candidates = [ensure_aware(task.updated_at)]
        ev_ts = event_max.get(task.id)
        if ev_ts is not None:
            candidates.append(ensure_aware(ev_ts))
        co_ts = comment_max.get(task.id)
        if co_ts is not None:
            candidates.append(ensure_aware(co_ts))
        last_movement = max(candidates)
        hours_still = (now - last_movement).total_seconds() / 3600
        if hours_still > _STALE_THRESHOLD.total_seconds() / 3600:
            entries.append(
                {
                    "id": str(task.id),
                    "title": task.title,
                    "status": task.status,
                    "hours_still": round(hours_still, 1),
                }
            )
    entries.sort(key=lambda e: e["hours_still"], reverse=True)
    return entries


async def _reviews_to_lead(session: AsyncSession, now) -> tuple[int, int]:
    """M2: task.review_handoff-Events letzte 24h, Anteil an einen Board-Lead.

    N4 (Pruefbericht): Cutoff laeuft direkt in der SQL-WHERE-Klausel, nicht
    mehr erst nach dem Laden der kompletten Event-Historie in Python — die
    Tabelle waechst unbegrenzt weiter (Live: 18k+ activity_events)."""
    cutoff = now - _LOOKBACK_24H
    result = await session.exec(
        select(ActivityEvent).where(
            ActivityEvent.event_type == "task.review_handoff",
            ActivityEvent.created_at >= cutoff,
        )
    )
    events = result.all()
    if not events:
        return 0, 0

    lead_result = await session.exec(select(Agent.id).where(Agent.is_board_lead == True))  # noqa: E712
    lead_ids = set(lead_result.all())
    reviews_to_lead = sum(1 for e in events if e.agent_id in lead_ids)
    return reviews_to_lead, len(events)


async def _dispatch_pairs(session: AsyncSession, now) -> tuple[int, int]:
    """M3: Dispatch-Events letzte 24h je Task chronologisch; aufeinander-
    folgende Paare <= 15 min: beide auto_dispatched -> double_dispatch,
    sonst -> healer_repeat."""
    cutoff = now - _LOOKBACK_24H
    result = await session.exec(
        select(ActivityEvent).where(
            ActivityEvent.event_type.in_(_DISPATCH_EVENT_TYPES),
            ActivityEvent.created_at >= cutoff,
        )
    )
    events = [e for e in result.all() if e.task_id is not None]

    by_task: dict = {}
    for e in events:
        by_task.setdefault(e.task_id, []).append(e)

    double_dispatch = 0
    healer_repeats = 0
    for task_events in by_task.values():
        task_events.sort(key=lambda e: ensure_aware(e.created_at))
        for prev, cur in zip(task_events, task_events[1:]):
            diff = ensure_aware(cur.created_at) - ensure_aware(prev.created_at)
            if diff <= _DISPATCH_PAIR_WINDOW:
                if (
                    prev.event_type == "task.auto_dispatched"
                    and cur.event_type == "task.auto_dispatched"
                ):
                    double_dispatch += 1
                else:
                    healer_repeats += 1
    return double_dispatch, healer_repeats


async def _hand_status_changes(session: AsyncSession, now) -> tuple[int, dict[str, int]]:
    """M4: task_events mit changed_by='user' letzte 24h, gruppiert nach reason
    (kein Grund -> "ohne_grund")."""
    cutoff = now - _LOOKBACK_24H
    result = await session.exec(
        select(TaskEvent).where(
            TaskEvent.changed_by == "user",
            TaskEvent.created_at >= cutoff,
        )
    )
    events = result.all()

    by_reason: dict[str, int] = {}
    for e in events:
        reason = e.reason or "ohne_grund"
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return len(events), by_reason


_UMLAUT_MAP = str.maketrans(
    {
        "ä": "ae", "ö": "oe", "ü": "ue",
        "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
        "ß": "ss",
    }
)


def _to_ascii(text: str) -> str:
    """Schreibt einen String nach ASCII um statt daran zu scheitern.

    Pruefbericht B1: Kartentitel kommen aus der Live-DB und enthalten oft
    Umlaute (live 482/1134 Titel) — ein `assert text.isascii()` liess den
    Bericht in der Praxis nie zustande kommen. Erst Umlaute gezielt
    ersetzen (aeoeue/ss bleibt lesbar statt zu verschwinden), dann den Rest
    (z.B. Emoji) per NFKD-Normalisierung best-effort wegfalten."""
    text = text.translate(_UMLAUT_MAP)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def format_digest(metrics: dict, *, now=None) -> str:
    """Formatiert die Messzahlen als kurzen, deutschen ASCII-Report
    (<= 14 Zeilen — Telegram/Slack-tauglich, keine Umlaute)."""
    now = ensure_aware(now) if now is not None else utcnow()
    today = now.strftime("%Y-%m-%d")
    lines: list[str] = [f"Taeglicher Messzahlen-Digest ({today})"]

    lines.append("")
    lines.append("M1: Karten ueber 4 h still")
    stale_cards = metrics.get("stale_cards") or []
    if not stale_cards:
        lines.append("  keine")
    else:
        for card in stale_cards[:3]:
            title = _to_ascii(card["title"])[:40]
            lines.append(
                f"  {title} ({card['status']}, {card['hours_still']} h)"
            )

    lines.append(
        f"M2: {metrics.get('reviews_to_lead_24h', 0)} von "
        f"{metrics.get('reviews_total_24h', 0)} Reviews beim Lead"
    )

    lines.append(
        f"M3: {metrics.get('double_dispatch_24h', 0)} Doppel-Dispatch, "
        f"{metrics.get('healer_repeats_24h', 0)} Healer-Wiederholungen"
    )

    by_reason = metrics.get("hand_status_changes_by_reason") or {}
    top_reasons = sorted(by_reason.items(), key=lambda kv: kv[1], reverse=True)[:3]
    reasons_str = (
        ", ".join(f"{_to_ascii(r)}: {c}" for r, c in top_reasons) if top_reasons else "keine"
    )
    lines.append(
        f"M4: {metrics.get('hand_status_changes_24h', 0)} Hand-Statuswechsel "
        f"({reasons_str})"
    )

    # Belt-and-braces: alles, was noch nicht durch _to_ascii lief (z.B. ein
    # kuenftiges Feld), faellt hier still auf ASCII zurueck statt den
    # gesamten Versand zu verschlucken (frueher: assert -> AssertionError,
    # die in _maybe_send abgefangen wurde und den ganzen Tag stumm blieb).
    return _to_ascii("\n".join(lines))


class DailyMetricsDigest:
    """Singleton: einmal taeglich (ab settings.daily_metrics_hour, UTC) den
    Messzahlen-Digest berechnen und verschicken. Redis-Dedup pro Kalendertag
    (Muster: intelligence._maybe_daily_destillation)."""

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("DailyMetricsDigest started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("DailyMetricsDigest stopped")

    async def _run_loop(self) -> None:
        try:
            while self._running:
                try:
                    await self._maybe_send(utcnow())
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.warning("DailyMetricsDigest tick failed (non-critical): %s", e)
                await asyncio.sleep(600)  # alle 10 Minuten pruefen
        except asyncio.CancelledError:
            pass

    async def _maybe_send(self, now) -> None:
        """now.hour wird gegen settings.daily_metrics_hour geprueft — beides
        UTC (kein TZ-Setting im System, Container laufen auf UTC; siehe
        Feld-Kommentar in config.py)."""
        try:
            if not settings.daily_metrics_enabled:
                return
            if now.hour < settings.daily_metrics_hour:
                return

            redis = await get_redis()
            dedup_key = f"mc:daily_metrics_digest:{now.strftime('%Y-%m-%d')}"

            # N1 (Pruefbericht): Schluessel VOR dem Berechnen/Senden atomar
            # reservieren (SET NX). Vorher wurde erst NACH dem Senden
            # gesetzt — fiel nur das `set` aus (z.B. Redis-Blip zwischen GET
            # und SET), ging der Bericht bei jedem 10-Minuten-Tick erneut
            # raus. NX macht das Reservieren selbst schon die Dedup-Grenze.
            acquired = await redis.set(dedup_key, "1", nx=True, ex=20 * 3600)
            if not acquired:
                return

            try:
                metrics = await compute_daily_metrics(None, now=now)
                delivered, results = await send_report(format_digest(metrics, now=now))
                # N2 (Pruefbericht): Rueckgabe von send_report auswerten.
                # Ohne konfigurierten Kanal liefert send_report (False, [])
                # OHNE eigenes Log — der Tag zaehlt trotzdem als "erledigt"
                # (kein Kanal = kein Spam morgen frueh), aber sichtbar bleibt
                # es nur, wenn wir selbst warnen.
                if not delivered:
                    logger.warning(
                        "DailyMetricsDigest: send_report hat nichts zugestellt (results=%s)",
                        results,
                    )
            except Exception:
                # Reservierung zuruecknehmen, damit der naechste Tick (in
                # <=10 min) es erneut versucht statt bis morgen zu warten.
                try:
                    await redis.delete(dedup_key)
                except Exception as cleanup_err:
                    logger.warning(
                        "DailyMetricsDigest: Dedup-Key-Cleanup fehlgeschlagen: %s",
                        cleanup_err,
                    )
                raise
        except Exception as e:
            logger.warning("DailyMetricsDigest send failed (non-critical): %s", e)


daily_metrics_digest = DailyMetricsDigest()
