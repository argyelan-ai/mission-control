"""Cost Collector — Budget-Warnungen (post-Gateway-Sunset).

Vor Phase 29 (OpenClaw-Gateway-Sunset): collect_session_costs() las Token-Counts
aus Gateway-Sessions ueber die alte Sessions-List-RPC. Nach dem Sunset gibt es
keine Gateway-Sessions mehr — die Funktion ist auf einen No-Op stubbed.

TODO Phase 31: Re-implementiere Cost-Extraction fuer cli-bridge-Agents (z.B.
aus Container-Logs oder Hermes-Heartbeats). check_budget_warnings() funktioniert
weiter, da es ausschliesslich auf CostEvent-Rows in der DB arbeitet.
"""
import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.cost_event import CostEvent
from app.redis_client import get_redis
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger("mc.cost_collector")

# Budget-Warnungen (taeglich/monatlich)
DAILY_WARNING_TOKENS = 500_000  # 500k Tokens/Tag Warnung
MONTHLY_WARNING_USD = 50.0  # $50/Monat Warnung


async def collect_session_costs(session: AsyncSession) -> dict:
    """No-op nach Gateway-Sunset (Phase 29, D-11).

    Vor Phase 29: aggregierte Token-Deltas aus Gateway-Sessions.
    Nach Phase 29: Gateway ist weg, keine Sessions zu pollen.

    TODO Phase 31: cli-bridge Cost-Extraction implementieren (Container-Logs
    oder Hermes-Heartbeat-Tokens). Bis dahin bleibt die Funktion als
    No-Op-Stub erhalten, damit Watchdog/Scheduler weiterhin aufrufen koennen.

    Returns: {"collected": 0, "events_created": 0, "errors": 0}
    """
    return {"collected": 0, "events_created": 0, "errors": 0}


async def check_budget_warnings(session: AsyncSession) -> list[str]:
    """Prueft taegl./monatl. Budget-Schwellen und emittiert Warnungen.

    Liest ausschliesslich CostEvent-Rows aus der DB — Gateway-unabhaengig.

    Returns: Liste von Warn-Messages (leer = alles OK).
    """
    from sqlalchemy import func

    warnings: list[str] = []
    now = utcnow()

    # Taegliche Tokens (alle Agents)
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

    # Monatliche Kosten
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

    # Warnungen als Events emittieren (dedupliziert via Redis)
    redis = await get_redis()
    for w in warnings:
        dedup_key = f"mc:cost:warning:{hash(w) % 10**8}"
        if not await redis.get(dedup_key):
            await redis.set(dedup_key, "1", ex=3600)  # 1h Dedup
            await emit_event(
                session, "cost.budget_warning", w, severity="warning",
            )
            logger.warning("Budget warning: %s", w)

    return warnings
