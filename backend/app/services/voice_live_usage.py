"""Live voice — cost and brakes (voice PR 6, ADR-091 draft).

What a live voice call costs, and the three brakes that keep it cheap:

- **Usage → cost.** The browser reads the token usage OpenAI reports in each
  ``response.done`` and posts the call's running totals. MC keeps the largest
  value per counter (totals only grow, so a late or repeated post changes
  nothing) and computes the cost with the prices in settings — the browser
  never sends money, only token counts.
- **Daily budget** (``voice_live_daily_budget_usd``, local day in
  ``voice_live_budget_tz``): reached = a new call is refused before anything
  is asked of OpenAI, and a running call is told to hang up.
- **Max length** (``voice_live_max_call_s``): the browser hangs up; MC refuses
  the call's tools afterwards (``voice_live_tools.check_call``) and, when the
  browser reported OpenAI's call id, hangs the call up at OpenAI itself.

The idle brake (silence on both sides) runs in the browser — only it hears
the audio; MC hands it the limits.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.voice_live_call import VoiceLiveCall
from app.utils import utcnow

logger = logging.getLogger("mc.voice_live")

OPENAI_HANGUP_URL = "https://api.openai.com/v1/realtime/calls/{call_id}/hangup"

#: OpenAI's WebRTC call ids look like ``rtc_…``. Anything else is refused so
#: a browser value can never shape the hang-up URL.
OPENAI_CALL_ID_RE = re.compile(r"^rtc_[A-Za-z0-9_-]{1,150}$")

#: A slow last tool call may still land just after the max length.
MAX_LENGTH_GRACE_S = 15

USAGE_FIELDS = (
    "input_text_tokens",
    "input_audio_tokens",
    "cached_text_tokens",
    "cached_audio_tokens",
    "output_text_tokens",
    "output_audio_tokens",
)

END_REASONS = frozenset(
    {
        "hangup",
        "closed",
        "idle",
        "max_length",
        "budget_reached",
        "muted_too_long",
        "network",
        "no_credits",
        "rate_limited",
        "unavailable",
        "server_max_length",
        "server_budget",
    }
)


def ref_hash(call_ref: str) -> str:
    return hashlib.sha256(call_ref.encode("utf-8")).hexdigest()


def _aware(dt: datetime) -> datetime:
    # SQLite hands back naive datetimes; everything here is stored as UTC.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ── prices ───────────────────────────────────────────────────────────────


def cost_usd(usage: dict[str, int]) -> float:
    """Cost of one call's totals. Cached tokens are part of the input counts
    (OpenAI reports them inside ``input_token_details``); they are billed at
    the cached price instead of the full one, never twice."""
    per = 1_000_000.0
    text_in = max(0, usage.get("input_text_tokens", 0) - usage.get("cached_text_tokens", 0))
    audio_in = max(0, usage.get("input_audio_tokens", 0) - usage.get("cached_audio_tokens", 0))
    cached = usage.get("cached_text_tokens", 0) + usage.get("cached_audio_tokens", 0)
    total = (
        text_in * settings.voice_live_price_text_in_per_m
        + audio_in * settings.voice_live_price_audio_in_per_m
        + cached * settings.voice_live_price_cached_in_per_m
        + usage.get("output_text_tokens", 0) * settings.voice_live_price_text_out_per_m
        + usage.get("output_audio_tokens", 0) * settings.voice_live_price_audio_out_per_m
    ) / per
    return round(total, 6)


# ── local day ────────────────────────────────────────────────────────────


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.voice_live_budget_tz or "Europe/Zurich")
    except Exception:  # noqa: BLE001 — a typo in env must not break the voice
        return ZoneInfo("Europe/Zurich")


def day_start(now: datetime | None = None, days_back: int = 0) -> datetime:
    """Local midnight (budget time zone) of today, or ``days_back`` days ago, in UTC."""
    local = _aware(now or utcnow()).astimezone(_tz())
    midnight = datetime(local.year, local.month, local.day, tzinfo=_tz()) - timedelta(days=days_back)
    # Re-attach the zone after the date shift so a DST change lands right.
    midnight = datetime(midnight.year, midnight.month, midnight.day, tzinfo=_tz())
    return midnight.astimezone(timezone.utc)


def next_reset(now: datetime | None = None) -> datetime:
    local = _aware(now or utcnow()).astimezone(_tz())
    tomorrow = (datetime(local.year, local.month, local.day) + timedelta(days=1)).replace(tzinfo=_tz())
    return tomorrow.astimezone(timezone.utc)


# ── rows ─────────────────────────────────────────────────────────────────


def duration_s(row: VoiceLiveCall) -> int:
    end = _aware(row.ended_at or row.last_seen_at)
    secs = int((end - _aware(row.started_at)).total_seconds())
    return max(0, min(secs, settings.voice_live_max_call_s + 60))


async def open_row(
    session: AsyncSession, call_ref: str, agent_id: uuid.UUID, agent_name: str, model: str
) -> VoiceLiveCall:
    row = VoiceLiveCall(
        ref_hash=ref_hash(call_ref), agent_id=agent_id, agent_name=(agent_name or "")[:128], model=model[:64]
    )
    session.add(row)
    await session.commit()
    return row


async def find_row(session: AsyncSession, call_ref: str) -> VoiceLiveCall | None:
    return (
        await session.exec(select(VoiceLiveCall).where(VoiceLiveCall.ref_hash == ref_hash(call_ref)))
    ).first()


async def spent_since(session: AsyncSession, since: datetime) -> float:
    rows = (await session.exec(select(VoiceLiveCall.cost_usd).where(col(VoiceLiveCall.started_at) >= since))).all()
    return round(sum(float(c or 0) for c in rows), 6)


@dataclass(frozen=True)
class BudgetState:
    limit_usd: float
    spent_usd: float
    reached: bool
    resets_at: datetime


async def budget_state(session: AsyncSession, now: datetime | None = None) -> BudgetState:
    now = now or utcnow()
    limit = float(settings.voice_live_daily_budget_usd or 0)
    spent = await spent_since(session, day_start(now))
    return BudgetState(
        limit_usd=limit, spent_usd=spent, reached=limit > 0 and spent >= limit, resets_at=next_reset(now)
    )


def limits() -> dict[str, int]:
    """What the browser enforces itself (it alone hears the audio)."""
    hang = max(10, int(settings.voice_live_idle_hangup_s))
    warn = max(0, min(int(settings.voice_live_idle_warn_s), hang - 5))
    return {"idle_hangup_s": hang, "idle_warn_s": warn, "max_call_s": max(60, int(settings.voice_live_max_call_s))}


def past_max_length(row: VoiceLiveCall, now: datetime | None = None) -> bool:
    elapsed = (_aware(now or utcnow()) - _aware(row.started_at)).total_seconds()
    return elapsed >= limits()["max_call_s"]


@dataclass(frozen=True)
class UsageResult:
    call_cost_usd: float
    today_usd: float
    budget_usd: float
    hang_up: str | None


async def record_usage(
    session: AsyncSession,
    row: VoiceLiveCall,
    usage: dict[str, int],
    *,
    ended: bool,
    end_reason: str | None,
    openai_call_id: str | None,
    now: datetime | None = None,
) -> UsageResult:
    """Merge one usage report into the call's row and say whether the call
    must end now (budget or max length). Totals only grow: each counter keeps
    its largest value, so a repeated or out-of-order post is harmless."""
    now = now or utcnow()
    for f in USAGE_FIELDS:
        setattr(row, f, max(int(getattr(row, f) or 0), int(usage.get(f, 0) or 0)))
    row.cost_usd = cost_usd({f: getattr(row, f) for f in USAGE_FIELDS})
    if row.ended_at is None:
        row.last_seen_at = now
        if ended:
            row.ended_at = now
            row.end_reason = end_reason if end_reason in END_REASONS else "hangup"
    if openai_call_id and not row.openai_call_id and OPENAI_CALL_ID_RE.match(openai_call_id):
        row.openai_call_id = openai_call_id
    row.duration_s = duration_s(row)
    session.add(row)
    await session.commit()

    budget = await budget_state(session, now)
    hang_up: str | None = None
    if not ended and row.ended_at is None:
        if budget.reached:
            hang_up = "budget_reached"
        elif past_max_length(row, now):
            hang_up = "max_length"
    return UsageResult(
        call_cost_usd=row.cost_usd, today_usd=budget.spent_usd, budget_usd=budget.limit_usd, hang_up=hang_up
    )


# ── summary for Insights ─────────────────────────────────────────────────


async def summary(session: AsyncSession, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    today0 = day_start(now)
    week0 = day_start(now, days_back=6)
    rows = (await session.exec(select(VoiceLiveCall).where(col(VoiceLiveCall.started_at) >= week0))).all()

    def bucket(since: datetime) -> dict[str, Any]:
        picked = [r for r in rows if _aware(r.started_at) >= since]
        talked = [r for r in picked if duration_s(r) > 0 or (r.cost_usd or 0) > 0]
        secs = sum(duration_s(r) for r in talked)
        cost = round(sum(float(r.cost_usd or 0) for r in talked), 4)
        return {"calls": len(talked), "seconds": secs, "cost_usd": cost}

    today = bucket(today0)
    week = bucket(week0)
    limit = float(settings.voice_live_daily_budget_usd or 0)
    return {
        "today": today,
        "week": week,
        "budget_usd": limit,
        "budget_reached": limit > 0 and today["cost_usd"] >= limit,
        "per_minute_usd": round(week["cost_usd"] / (week["seconds"] / 60), 4) if week["seconds"] >= 60 else None,
    }


# ── server-side hang-up (max length) ─────────────────────────────────────


def _transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests (httpx.MockTransport). ``None`` = real network."""
    return None


async def hangup_upstream(openai_call_id: str) -> bool:
    """Ends the call at OpenAI with the server key. Best effort: a call that
    already ended answers 404 — that is fine too. Never logs the key or the
    upstream body."""
    if not OPENAI_CALL_ID_RE.match(openai_call_id or ""):
        return False
    from app.services.secrets_helper import effective_secret

    api_key = await effective_secret("openai_api_key")
    if not api_key:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=_transport()) as client:
            resp = await client.post(
                OPENAI_HANGUP_URL.format(call_id=openai_call_id),
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("voice_live hangup: upstream unreachable (%s)", type(exc).__name__)
        return False
    logger.info("voice_live hangup: upstream %s", resp.status_code)
    return resp.status_code < 300 or resp.status_code == 404


#: row id → the timer that ends that call at its max length.
_DEADLINES: dict[uuid.UUID, asyncio.Task] = {}


async def enforce_deadline(session: AsyncSession, row_id: uuid.UUID, now: datetime | None = None) -> bool:
    """The max-length check the timer runs: a call that is still open past its
    max length is hung up at OpenAI and closed in the row. True = hung up."""
    row = await session.get(VoiceLiveCall, row_id)
    if row is None or row.ended_at is not None or not row.openai_call_id:
        return False
    if not past_max_length(row, now):
        return False
    await hangup_upstream(row.openai_call_id)
    row.ended_at = _aware(now or utcnow())
    row.end_reason = "server_max_length"
    row.duration_s = duration_s(row)
    session.add(row)
    await session.commit()
    logger.info("voice_live: call %s hung up server-side at max length", row.id)
    return True


def schedule_deadline(row: VoiceLiveCall, session_factory: Callable[[], AsyncSession]) -> None:
    """Starts the max-length timer for a call once OpenAI's call id is known.
    In-process (one backend worker, like ``voice_live_tools._CALLS``): a
    backend restart drops it, and the browser's own timer still ends the call."""
    if row.id in _DEADLINES or not row.openai_call_id or row.ended_at is not None:
        return
    delay = (
        _aware(row.started_at) + timedelta(seconds=limits()["max_call_s"] + 5) - utcnow()
    ).total_seconds()

    async def _run() -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            async with session_factory() as s:
                await enforce_deadline(s, row.id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a timer must never take the backend down
            logger.warning("voice_live: max-length timer failed", exc_info=True)
        finally:
            _DEADLINES.pop(row.id, None)

    try:
        _DEADLINES[row.id] = asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        pass


def cancel_deadline(row_id: uuid.UUID) -> None:
    task = _DEADLINES.pop(row_id, None)
    if task is not None:
        task.cancel()
