"""Cost Collector — budget warnings (post-Gateway-Sunset).

Before Phase 29 (OpenClaw Gateway sunset): collect_session_costs() read token
counts from Gateway sessions via the old sessions-list RPC. After the sunset
there are no more Gateway sessions — the function is stubbed to a no-op.

TODO Phase 31: re-implement cost extraction for cli-bridge agents (e.g. from
container logs or Hermes heartbeats). check_budget_warnings() keeps working
since it operates exclusively on CostEvent rows in the DB.
"""
import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.cost_event import CostEvent
from app.redis_client import get_redis
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger("mc.cost_collector")

# Budget warnings (daily/monthly)
DAILY_WARNING_TOKENS = 500_000  # 500k tokens/day warning
MONTHLY_WARNING_USD = 50.0  # $50/month warning


async def collect_session_costs(session: AsyncSession) -> dict:
    """No-op after Gateway sunset (Phase 29, D-11).

    Before Phase 29: aggregated token deltas from Gateway sessions.
    After Phase 29: Gateway is gone, no sessions left to poll.

    TODO Phase 31: implement cli-bridge cost extraction (container logs
    or Hermes heartbeat tokens). Until then the function stays a no-op
    stub so watchdog/scheduler can keep calling it.

    Returns: {"collected": 0, "events_created": 0, "errors": 0}
    """
    return {"collected": 0, "events_created": 0, "errors": 0}


async def check_budget_warnings(session: AsyncSession) -> list[str]:
    """Checks daily/monthly budget thresholds and emits warnings.

    Reads exclusively CostEvent rows from the DB — Gateway-independent.

    Returns: list of warning messages (empty = all OK).
    """
    from sqlalchemy import func

    warnings: list[str] = []
    now = utcnow()

    # Daily tokens (all agents)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_result = await session.exec(
        select(
            func.sum(CostEvent.tokens_in + CostEvent.tokens_out)
        ).where(CostEvent.created_at >= day_start)
    )
    daily_tokens = daily_result.one_or_none() or 0
    if daily_tokens and daily_tokens > DAILY_WARNING_TOKENS:
        msg = f"Tagesverbrauch: {daily_tokens:,} Tokens (Warnung ab {DAILY_WARNING_TOKENS:,})"
        warnings.append(msg)

    # Monthly cost
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_result = await session.exec(
        select(func.sum(CostEvent.cost_usd)).where(
            CostEvent.created_at >= month_start,
            CostEvent.cost_usd.isnot(None),  # type: ignore[union-attr]
        )
    )
    monthly_usd = monthly_result.one_or_none() or 0.0
    if monthly_usd and monthly_usd > MONTHLY_WARNING_USD:
        msg = f"Monatskosten: ${monthly_usd:.2f} (Warnung ab ${MONTHLY_WARNING_USD:.2f})"
        warnings.append(msg)

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
