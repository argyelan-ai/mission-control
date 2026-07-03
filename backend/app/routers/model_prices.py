"""Model Prices CRUD — admin-only.

GET    /api/v1/model-prices                  → List (sorted by priority DESC, pattern)
POST   /api/v1/model-prices                  → Create
GET    /api/v1/model-prices/unmatched        → Models in events with no matching pattern
POST   /api/v1/model-prices/recompute        → Recompute cost_usd for all events
PATCH  /api/v1/model-prices/{id}             → Update (partial fields)
DELETE /api/v1/model-prices/{id}             → Delete

IMPORTANT: /unmatched and /recompute must be defined BEFORE /{id} —
FastAPI matches routes top-down, otherwise "unmatched" would get parsed as a UUID.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import asc, desc, func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.database import get_session
from app.models.model_usage import ModelPrice, ModelUsageEvent

router = APIRouter(prefix="/api/v1", tags=["model-prices"])


# ── Schemas ────────────────────────────────────────────────────────────────

class ModelPriceCreate(BaseModel):
    model_pattern: str
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0
    currency: str = "USD"
    valid_from: datetime
    priority: int = 0
    note: Optional[str] = None


class ModelPriceUpdate(BaseModel):
    model_pattern: Optional[str] = None
    input_per_mtok: Optional[float] = None
    output_per_mtok: Optional[float] = None
    cache_read_per_mtok: Optional[float] = None
    cache_write_per_mtok: Optional[float] = None
    currency: Optional[str] = None
    valid_from: Optional[datetime] = None
    priority: Optional[int] = None
    note: Optional[str] = None


class RecomputeRequest(BaseModel):
    from_ts: Optional[datetime] = None  # optional: only events from this point on


def _price_to_dict(p: ModelPrice) -> dict:
    """Serializes a ModelPrice object to a dict."""
    return {
        "id": str(p.id),
        "model_pattern": p.model_pattern,
        "input_per_mtok": p.input_per_mtok,
        "output_per_mtok": p.output_per_mtok,
        "cache_read_per_mtok": p.cache_read_per_mtok,
        "cache_write_per_mtok": p.cache_write_per_mtok,
        "currency": p.currency,
        "valid_from": p.valid_from.isoformat(),
        "priority": p.priority,
        "note": p.note,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/model-prices")
async def list_prices(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """List all prices (sorted: priority DESC, model_pattern ASC)."""
    result = await session.exec(
        select(ModelPrice).order_by(
            desc(ModelPrice.priority),
            asc(ModelPrice.model_pattern),
        )
    )
    return [_price_to_dict(p) for p in result.all()]


@router.post("/model-prices", status_code=201)
async def create_price(
    payload: ModelPriceCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Create a new price."""
    vf = payload.valid_from
    if vf.tzinfo is None:
        vf = vf.replace(tzinfo=timezone.utc)

    price = ModelPrice(
        id=uuid.uuid4(),
        model_pattern=payload.model_pattern,
        input_per_mtok=payload.input_per_mtok,
        output_per_mtok=payload.output_per_mtok,
        cache_read_per_mtok=payload.cache_read_per_mtok,
        cache_write_per_mtok=payload.cache_write_per_mtok,
        currency=payload.currency,
        valid_from=vf,
        priority=payload.priority,
        note=payload.note,
    )
    session.add(price)
    await session.commit()
    await session.refresh(price)
    return _price_to_dict(price)


@router.get("/model-prices/unmatched")
async def get_unmatched_models(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Models in model_usage_events that don't match any price pattern.

    Uses match_price (same logic as the harvester) — shows real gaps.
    Returns per model: event_count, total_input_tokens, total_output_tokens.
    """
    from app.services.token_harvester import match_price
    from app.utils import utcnow

    # Load all prices (for matching)
    prices_result = await session.exec(select(ModelPrice))
    all_prices = list(prices_result.all())

    # DISTINCT models with aggregates
    result = await session.exec(
        select(
            ModelUsageEvent.model,
            func.count(ModelUsageEvent.id).label("event_count"),
            func.sum(ModelUsageEvent.input_tokens).label("total_input"),
            func.sum(ModelUsageEvent.output_tokens).label("total_output"),
        ).group_by(ModelUsageEvent.model)
    )

    now = utcnow()
    unmatched = []
    for row in result.all():
        price_info = match_price(row.model, now, all_prices)
        if price_info is None:
            unmatched.append({
                "model": row.model,
                "event_count": row.event_count,
                "total_input_tokens": row.total_input or 0,
                "total_output_tokens": row.total_output or 0,
            })

    # Sorted by event_count DESC
    unmatched.sort(key=lambda x: x["event_count"], reverse=True)
    return unmatched


@router.post("/model-prices/recompute")
async def recompute_costs(
    payload: RecomputeRequest,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Recompute cost_usd for all events (from from_ts if given) using the current price table.

    Returns {"updated": N}.
    """
    from app.services.token_harvester import match_price, _compute_cost_usd

    # Load prices
    prices_result = await session.exec(select(ModelPrice))
    all_prices = list(prices_result.all())

    # Load events (optionally from from_ts)
    query = select(ModelUsageEvent)
    if payload.from_ts:
        from_ts = payload.from_ts
        if from_ts.tzinfo is None:
            from_ts = from_ts.replace(tzinfo=timezone.utc)
        query = query.where(ModelUsageEvent.ts >= from_ts)

    result = await session.exec(query)
    events = result.all()

    updated = 0
    for event in events:
        price_info = match_price(event.model, event.ts, all_prices)
        if price_info is not None:
            new_cost = _compute_cost_usd(
                price_info,
                event.input_tokens,
                event.output_tokens,
                event.cache_read_tokens,
                event.cache_write_tokens,
            )
            event.cost_usd = new_cost
            session.add(event)
            updated += 1

    await session.commit()
    return {"updated": updated}


@router.patch("/model-prices/{price_id}")
async def update_price(
    price_id: uuid.UUID,
    payload: ModelPriceUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Partially update a price."""
    price = await session.get(ModelPrice, price_id)
    if not price:
        raise HTTPException(status_code=404, detail="Price not found")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(price, field, value)

    session.add(price)
    await session.commit()
    await session.refresh(price)
    return _price_to_dict(price)


@router.delete("/model-prices/{price_id}", status_code=204)
async def delete_price(
    price_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Delete a price."""
    price = await session.get(ModelPrice, price_id)
    if not price:
        raise HTTPException(status_code=404, detail="Price not found")
    await session.delete(price)
    await session.commit()
