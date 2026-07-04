"""Budget warnings on live token/cost data (model_usage_events).

History: before Phase 29 this module polled Gateway sessions (RPC) and wrote
CostEvent snapshots. The Gateway is gone; the Token Harvester (Phase 31,
services/token_harvester.py) now reads JSONL transcripts into
model_usage_events — including cache token splits and list-price cost.

This module only evaluates budget thresholds on that data. The old
collect_session_costs() no-op stub has been removed; the watchdog calls
check_budget_warnings() directly after each harvest.

Cost semantics: cost_usd is the *list-price equivalent* (model_prices table).
Subscription plans (Claude Pro/Max) don't bill per token — the warning exists
to catch runaway consumption (e.g. a looping agent), not to track an invoice.
"""
import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.model_usage import ModelUsageEvent
from app.redis_client import get_redis
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger("mc.cost_collector")


async def check_budget_warnings(session: AsyncSession) -> list[str]:
    """Checks daily/monthly budget thresholds and emits warnings.

    Reads model_usage_events (Token Harvester). Token count = input + output
    + cache_write — cache *reads* are excluded: they dominate raw volume at a
    fraction of the price and would make the threshold meaningless.

    Thresholds come from settings (env-overridable):
      budget_daily_warning_tokens   (default 400M — grounded on 07/2026 fleet
                                     rates: normal days 10-40M, heavy 320M)
      budget_monthly_warning_usd    (default 10000 — list-price equivalent;
                                     30-day run rate 07/2026 was ~$5.3k)

    Returns: list of warning messages (empty = all OK).
    """
    from sqlalchemy import func

    daily_warning_tokens: int = settings.budget_daily_warning_tokens
    monthly_warning_usd: float = settings.budget_monthly_warning_usd

    warnings: list[str] = []
    now = utcnow()
    billable = (
        ModelUsageEvent.input_tokens
        + ModelUsageEvent.output_tokens
        + ModelUsageEvent.cache_write_tokens
    )

    # Daily tokens (all agents)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_result = await session.exec(
        select(func.sum(billable)).where(ModelUsageEvent.ts >= day_start)
    )
    daily_tokens = daily_result.one_or_none() or 0
    if daily_tokens and daily_tokens > daily_warning_tokens:
        warnings.append(
            f"Daily usage: {daily_tokens:,} tokens "
            f"(warning threshold {daily_warning_tokens:,})"
        )

    # Monthly cost (list-price equivalent)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_result = await session.exec(
        select(func.sum(ModelUsageEvent.cost_usd)).where(
            ModelUsageEvent.ts >= month_start,
            ModelUsageEvent.cost_usd.isnot(None),  # type: ignore[union-attr]
        )
    )
    monthly_usd = monthly_result.one_or_none() or 0.0
    if monthly_usd and monthly_usd > monthly_warning_usd:
        warnings.append(
            f"Monthly cost: ${monthly_usd:,.2f} list-price equivalent "
            f"(warning threshold ${monthly_warning_usd:,.2f})"
        )

    # Emit warnings as events (deduplicated via Redis)
    redis = await get_redis()
    for w in warnings:
        dedup_key = f"mc:cost:warning:{hash(w) % 10**8}"
        if not await redis.get(dedup_key):
            await redis.set(dedup_key, "1", ex=3600)  # 1h dedup
            await emit_event(
                session, "cost.budget_warning", w, severity="warning",
            )
            logger.warning("Budget warning: %s", w)

    return warnings
