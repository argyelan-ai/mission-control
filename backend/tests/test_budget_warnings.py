"""Budget warnings on model_usage_events (cost_collector rewrite, 07/2026).

check_budget_warnings() reads the Token-Harvester table instead of the dead
gateway-era cost_events archive. Token count = input + output + cache_write
(cache reads excluded); thresholds come from settings.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.model_usage import ModelUsageEvent
from app.utils import utcnow


async def _mk_agent(session: AsyncSession) -> Agent:
    agent = Agent(name=f"BudgetBot-{uuid.uuid4().hex[:6]}", emoji="💸")
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


def _mk_event(agent_id, *, tokens_in=0, tokens_out=0, cache_write=0, cache_read=0,
              cost=None, ts=None) -> ModelUsageEvent:
    return ModelUsageEvent(
        agent_id=agent_id,
        harness="cli-bridge",
        model="claude-sonnet-4-6",
        provider="anthropic",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        message_uuid=str(uuid.uuid4()),
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cache_write_tokens=cache_write,
        cache_read_tokens=cache_read,
        cost_usd=cost,
        ts=ts or utcnow(),
        source_file="/tmp/test-transcript.jsonl",
    )


@pytest.mark.asyncio
async def test_no_warning_below_thresholds(session: AsyncSession) -> None:
    from app.services.cost_collector import check_budget_warnings

    agent = await _mk_agent(session)
    session.add(_mk_event(agent.id, tokens_in=1000, tokens_out=500, cost=0.05))
    await session.commit()

    warnings = await check_budget_warnings(session)
    assert warnings == []


@pytest.mark.asyncio
async def test_daily_token_warning_fires(session: AsyncSession, monkeypatch, fake_redis) -> None:
    from app.config import settings
    from app.services import cost_collector
    from app.services.cost_collector import check_budget_warnings

    async def _fake_get_redis():
        return fake_redis
    monkeypatch.setattr(cost_collector, "get_redis", _fake_get_redis)

    async def _noop_emit(*a, **k):
        return None
    monkeypatch.setattr(cost_collector, "emit_event", _noop_emit)

    monkeypatch.setattr(settings, "budget_daily_warning_tokens", 1_000)
    agent = await _mk_agent(session)
    session.add(_mk_event(agent.id, tokens_in=900, tokens_out=200))
    await session.commit()

    warnings = await check_budget_warnings(session)
    assert len(warnings) == 1
    assert "Daily usage" in warnings[0]


@pytest.mark.asyncio
async def test_cache_reads_do_not_count(session: AsyncSession, monkeypatch) -> None:
    """Cache reads dominate raw volume at a fraction of the price — they must
    not trip the daily token threshold."""
    from app.config import settings
    from app.services.cost_collector import check_budget_warnings

    monkeypatch.setattr(settings, "budget_daily_warning_tokens", 1_000)
    agent = await _mk_agent(session)
    session.add(_mk_event(agent.id, tokens_in=100, tokens_out=100, cache_read=50_000))
    await session.commit()

    warnings = await check_budget_warnings(session)
    assert warnings == []


@pytest.mark.asyncio
async def test_cache_writes_do_count(session: AsyncSession, monkeypatch, fake_redis) -> None:
    from app.config import settings
    from app.services import cost_collector
    from app.services.cost_collector import check_budget_warnings

    async def _fake_get_redis():
        return fake_redis
    monkeypatch.setattr(cost_collector, "get_redis", _fake_get_redis)

    async def _noop_emit(*a, **k):
        return None
    monkeypatch.setattr(cost_collector, "emit_event", _noop_emit)

    monkeypatch.setattr(settings, "budget_daily_warning_tokens", 1_000)
    agent = await _mk_agent(session)
    session.add(_mk_event(agent.id, tokens_in=100, tokens_out=100, cache_write=2_000))
    await session.commit()

    warnings = await check_budget_warnings(session)
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_monthly_usd_warning_fires(session: AsyncSession, monkeypatch, fake_redis) -> None:
    from app.config import settings
    from app.services import cost_collector
    from app.services.cost_collector import check_budget_warnings

    async def _fake_get_redis():
        return fake_redis
    monkeypatch.setattr(cost_collector, "get_redis", _fake_get_redis)

    async def _noop_emit(*a, **k):
        return None
    monkeypatch.setattr(cost_collector, "emit_event", _noop_emit)

    monkeypatch.setattr(settings, "budget_monthly_warning_usd", 10.0)
    agent = await _mk_agent(session)
    session.add(_mk_event(agent.id, tokens_in=10, tokens_out=10, cost=11.5))
    await session.commit()

    warnings = await check_budget_warnings(session)
    assert any("Monthly cost" in w for w in warnings)


@pytest.mark.asyncio
async def test_events_before_day_start_ignored(session: AsyncSession, monkeypatch) -> None:
    from app.config import settings
    from app.services.cost_collector import check_budget_warnings

    monkeypatch.setattr(settings, "budget_daily_warning_tokens", 1_000)
    agent = await _mk_agent(session)
    yesterday = utcnow() - timedelta(days=1, hours=1)
    session.add(_mk_event(agent.id, tokens_in=5_000, ts=yesterday))
    await session.commit()

    warnings = await check_budget_warnings(session)
    assert all("Daily usage" not in w for w in warnings)


def test_cost_collector_has_no_stub_left() -> None:
    """The gateway-era no-op stub is gone; only check_budget_warnings remains."""
    from app.services import cost_collector

    assert not hasattr(cost_collector, "collect_session_costs")
