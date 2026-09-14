"""Recovery Tier 2 (process restart) is skipped for ACP / opted-out agents.

Option B of the 2026-09-14 decision: measured over 7 days, Tier 2 failed in
48 % of runs and — for ACP agents — the restart itself killed the running
turn (double review, phantom delivery). Until the liveness-based restart
(option A) lands, ACP agents and explicitly opted-out slugs are REPORTED
(Tier 3 resume + Tier 4 operator notification) but never restarted.

Three probes, each one a sabotage of the other:
  * docker agent whose slug is in OMP_ACP_AGENT_SLUGS   -> restart NOT called
  * host agent whose slug is in RECOVERY_TIER2_SKIP_*   -> lifecycle NOT called
  * docker agent in neither list                         -> restart called (unchanged)
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _fixtures(make_board, make_agent, make_task, *, runtime: str, name: str):
    board = await make_board(name=f"Tier2 skip {runtime}", slug=f"t2s-{runtime}-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(
        name=name, board_id=board.id, is_board_lead=False,
        role="developer", agent_runtime=runtime,
    )
    task = await make_task(
        board_id=board.id, title=f"stale on {name}", status="in_progress",
        assigned_agent_id=agent.id,
    )
    return board, agent, task


@asynccontextmanager
async def _session():
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


async def _run_recovery(fake_redis, agent, task):
    """Mirror test_tiered_recovery.py: singleton runner, fake redis via get_redis, 30 s wait skipped."""
    from app.services.task_runner import task_runner

    with patch("app.services.task_runner.get_redis", AsyncMock(return_value=fake_redis)), \
         patch("app.services.task_runner.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_runner.logger"), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        async with _session() as session:
            await task_runner._run_tiered_recovery(session, task, agent)


@pytest.mark.asyncio
async def test_tier2_skipped_for_acp_agent(fake_redis, make_board, make_agent, make_task, monkeypatch):
    """Slug listed in OMP_ACP_AGENT_SLUGS -> restart_docker_agent_container is never called."""
    _, agent, task = await _fixtures(make_board, make_agent, make_task, runtime="docker", name="AcpWorker")
    from app.services.fs_service import agent_slug
    from app.config import settings
    assert agent_slug(agent)
    monkeypatch.setattr(settings, "omp_acp_agent_slugs", agent_slug(agent))
    monkeypatch.setattr(settings, "recovery_tier2_skip_agent_slugs", "")

    restart_spy = MagicMock(return_value={"status": "restarted"})
    with patch("app.services.docker_agent_sync.restart_docker_agent_container", restart_spy):
        await _run_recovery(fake_redis, agent, task)
    assert restart_spy.call_count == 0, "ACP agent must not be restarted by Tier 2"


@pytest.mark.asyncio
async def test_tier2_skipped_for_explicit_optout_host_agent(fake_redis, make_board, make_agent, make_task, monkeypatch):
    """Slug listed in RECOVERY_TIER2_SKIP_AGENT_SLUGS (host runtime) -> _host_agent_lifecycle never called."""
    _, agent, task = await _fixtures(make_board, make_agent, make_task, runtime="host", name="HostOptout")
    from app.services.fs_service import agent_slug
    from app.config import settings
    monkeypatch.setattr(settings, "omp_acp_agent_slugs", "")
    monkeypatch.setattr(settings, "recovery_tier2_skip_agent_slugs", f"other-agent, {agent_slug(agent)}")

    lifecycle_spy = AsyncMock(return_value=None)
    with patch("app.routers.cli_terminal._host_agent_lifecycle", lifecycle_spy):
        await _run_recovery(fake_redis, agent, task)
    assert lifecycle_spy.await_count == 0, "opted-out host agent must not be restarted by Tier 2"


@pytest.mark.asyncio
async def test_tier2_still_restarts_plain_docker_agent(fake_redis, make_board, make_agent, make_task, monkeypatch):
    """Sabotage of the two tests above: an agent in NEITHER list is restarted exactly as before."""
    _, agent, task = await _fixtures(make_board, make_agent, make_task, runtime="docker", name="PlainWorker")
    from app.config import settings
    monkeypatch.setattr(settings, "omp_acp_agent_slugs", "someone-else")
    monkeypatch.setattr(settings, "recovery_tier2_skip_agent_slugs", "")

    restart_spy = MagicMock(return_value={"status": "restarted"})
    with patch("app.services.docker_agent_sync.restart_docker_agent_container", restart_spy):
        await _run_recovery(fake_redis, agent, task)
    assert restart_spy.call_count >= 1, "non-ACP docker agent must still get the Tier 2 restart"
