"""Tests for runtime_seeder — ensures JSON import is idempotent."""
import pytest

from app.services.runtime_seeder import seed_runtimes
from app.models.runtime import Runtime
from sqlalchemy import select


@pytest.mark.asyncio
async def test_seed_inserts_all_from_json(async_session):
    inserted, skipped = await seed_runtimes(async_session)
    assert inserted >= 1  # at least one runtime in runtimes.json seed
    assert skipped == 0

    rows = (await async_session.exec(select(Runtime))).scalars().all()
    slugs = {r.slug for r in rows}
    # Seeded runtimes from runtimes.json
    assert "nemotron-super" in slugs or "qwen-general" in slugs


@pytest.mark.asyncio
async def test_seed_is_idempotent(async_session):
    first_inserted, _ = await seed_runtimes(async_session)
    second_inserted, second_skipped = await seed_runtimes(async_session)
    assert second_inserted == 0
    assert second_skipped == first_inserted


@pytest.mark.asyncio
async def test_seed_maps_role_tags(async_session):
    await seed_runtimes(async_session)
    rows = (await async_session.exec(select(Runtime))).scalars().all()
    for r in rows:
        assert isinstance(r.role_tags, list)


@pytest.mark.asyncio
async def test_agent_runtime_fk_nullable(async_session):
    """Agent.runtime_id defaults to None and survives without a runtime."""
    from app.models.agent import Agent

    agent = Agent(name="Test", agent_runtime="cli-bridge")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    assert agent.runtime_id is None
