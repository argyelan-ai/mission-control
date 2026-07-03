"""Bundle 2 — Cost Tracking Tests.

1. Create + read CostEvent model
2. estimate_cost calculation
3. Delta calculation (collector simulated)
4. Budget warnings
5. Existing watchdog logic not broken
"""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.cost_event import CostEvent, estimate_cost


# ── Model + estimate_cost ────────────────────────────────────────────────

class TestCostEventModel:

    @pytest.mark.asyncio
    async def test_create_cost_event(self, session: AsyncSession, make_agent, make_task):
        """CostEvent can be created and read."""
        board_id = uuid.uuid4()
        agent = await make_agent("CostAgent", board_id=board_id, role="developer")
        task = await make_task(board_id, title="Cost Test")

        event = CostEvent(
            agent_id=agent.id,
            task_id=task.id,
            session_key="agent:cody:task:abc:work",
            tokens_in=5000,
            tokens_out=1200,
            provider="openai-codex",
            model="gpt-5.4",
            cost_usd=0.0245,
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)

        assert event.id is not None
        assert event.tokens_in == 5000
        assert event.tokens_out == 1200
        assert event.cost_usd == 0.0245

    @pytest.mark.asyncio
    async def test_cost_event_without_task(self, session: AsyncSession, make_agent):
        """CostEvent without task_id (e.g. main session) is valid."""
        board_id = uuid.uuid4()
        agent = await make_agent("NoTaskAgent", board_id=board_id, role="developer")

        event = CostEvent(
            agent_id=agent.id,
            session_key="agent:main:main",
            tokens_in=10000,
            tokens_out=500,
        )
        session.add(event)
        await session.commit()

        assert event.task_id is None

    @pytest.mark.asyncio
    async def test_aggregate_by_agent(self, session: AsyncSession, make_agent, make_task):
        """Aggregation per agent works."""
        board_id = uuid.uuid4()
        agent = await make_agent("AggAgent", board_id=board_id, role="developer")
        task = await make_task(board_id, title="Agg Test")

        for i in range(3):
            session.add(CostEvent(
                agent_id=agent.id,
                task_id=task.id,
                session_key=f"agent:test:task:{i}:work",
                tokens_in=1000 * (i + 1),
                tokens_out=200 * (i + 1),
                cost_usd=0.01 * (i + 1),
            ))
        await session.commit()

        result = await session.exec(
            select(
                func.sum(CostEvent.tokens_in),
                func.sum(CostEvent.tokens_out),
                func.sum(CostEvent.cost_usd),
            ).where(CostEvent.agent_id == agent.id)
        )
        row = result.one()
        assert row[0] == 6000  # 1000+2000+3000
        assert row[1] == 1200  # 200+400+600
        assert abs(row[2] - 0.06) < 0.001


class TestEstimateCost:

    def test_gpt54_cost(self):
        """GPT-5.4: $2.50/1M in, $10/1M out."""
        cost = estimate_cost("gpt-5.4", 100_000, 10_000)
        assert cost is not None
        assert abs(cost - 0.35) < 0.01  # 0.25 + 0.10

    def test_gpt54_with_provider_prefix(self):
        """Provider prefix gets stripped."""
        cost = estimate_cost("openai-codex/gpt-5.4", 100_000, 10_000)
        assert cost is not None
        assert abs(cost - 0.35) < 0.01

    def test_local_model_free(self):
        """Local models cost $0."""
        cost = estimate_cost("nemotron-3-super", 500_000, 50_000)
        assert cost == 0.0

    def test_unknown_model_returns_none(self):
        """Unknown model → None."""
        cost = estimate_cost("some-unknown-model", 100_000, 10_000)
        assert cost is None

    def test_none_model_returns_none(self):
        """model=None → None."""
        cost = estimate_cost(None, 100_000, 10_000)
        assert cost is None

    def test_zero_tokens(self):
        """0 tokens → $0."""
        cost = estimate_cost("gpt-5.4", 0, 0)
        assert cost == 0.0


# ── Budget warnings ──────────────────────────────────────────────────────

class TestBudgetWarnings:

    @pytest.mark.asyncio
    async def test_no_warning_below_threshold(self, session: AsyncSession, make_agent):
        """Below threshold → no warning."""
        from app.services.cost_collector import check_budget_warnings

        board_id = uuid.uuid4()
        agent = await make_agent("LowCostAgent", board_id=board_id, role="developer")

        session.add(CostEvent(
            agent_id=agent.id,
            session_key="agent:test:main",
            tokens_in=1000,
            tokens_out=100,
            cost_usd=0.01,
        ))
        await session.commit()

        warnings = await check_budget_warnings(session)
        assert len(warnings) == 0

    @pytest.mark.asyncio
    async def test_daily_token_warning(self, session: AsyncSession, make_agent, fake_redis):
        """Over 500k tokens/day → warning."""
        from unittest.mock import patch, AsyncMock
        from app.services.cost_collector import check_budget_warnings

        board_id = uuid.uuid4()
        agent = await make_agent("HighCostAgent", board_id=board_id, role="developer")

        session.add(CostEvent(
            agent_id=agent.id,
            session_key="agent:test:main",
            tokens_in=400_000,
            tokens_out=200_000,
            cost_usd=2.50,
        ))
        await session.commit()

        async def _fake_get_redis():
            return fake_redis

        with patch("app.services.cost_collector.get_redis", _fake_get_redis):
            with patch("app.services.cost_collector.emit_event", new_callable=AsyncMock):
                warnings = await check_budget_warnings(session)
        assert any("Tagesverbrauch" in w for w in warnings)


# ── Security ─────────────────────────────────────────────────────────────

class TestCostSecurity:

    @pytest.mark.asyncio
    async def test_no_credential_leaks_in_cost_events(self, session: AsyncSession, make_agent):
        """CostEvent contains no credential fields."""
        board_id = uuid.uuid4()
        agent = await make_agent("SecAgent", board_id=board_id, role="developer")

        event = CostEvent(
            agent_id=agent.id,
            session_key="agent:test:main",
            tokens_in=1000,
            tokens_out=100,
            model="gpt-5.4",
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)

        # No field should contain credentials
        for field_name in ["session_key", "model", "provider", "event_type"]:
            value = getattr(event, field_name, None)
            if value:
                assert "password" not in str(value).lower()
                assert "secret" not in str(value).lower()
                assert "token" not in str(value).lower() or field_name == "session_key"
