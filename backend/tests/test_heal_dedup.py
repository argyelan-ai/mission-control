"""Tests for W0.1 — one heal per card per round (mc:heal claim).

Contract (`app.redis_client.try_claim_heal`):
- SET NX EX one-shot claim on ``mc:heal:{task_id}`` with HEAL_DEDUP_TTL
  (one watchdog tick = 30s). True = caller may heal; False = another
  watchdog healed this card this round and the caller must skip.
- Every healing action gates itself on the claim. Two healers racing on
  the same card in the same round -> exactly ONE healing action + ONE
  event; after the TTL expires the card can be healed again.
- Sabotage probe: without the claim gate two healers both act (test
  verified red against the un-gated behavior).

Conventions (tests/conftest.py): in-memory SQLite ``test_engine``,
``fake_redis`` fixture, ASGI poll client — mirrors
test_poll_orphaned_run.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from tests.conftest import test_engine


def _session() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


def _past(seconds: float) -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(seconds=seconds)


async def _make_agent() -> tuple[str, str, str]:
    """board_id, agent_id, bearer token for a fresh cli-bridge worker."""
    from app.models.agent import Agent
    from app.models.board import Board

    async with _session() as s:
        board = Board(name="Heal Board", slug=f"heal-{uuid.uuid4().hex[:8]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        raw_token, token_hash = generate_agent_token()
        agent = Agent(
            name=f"Worker-{uuid.uuid4().hex[:6]}",
            agent_runtime="cli-bridge",
            agent_token_hash=token_hash,
            board_id=board.id,
            is_board_lead=False,
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return str(board.id), str(agent.id), raw_token


async def _make_task(board_id: str, agent_id: str, **fields) -> uuid.UUID:
    from app.models.task import Task

    async with _session() as s:
        task = Task(
            board_id=uuid.UUID(board_id),
            title="Heal me",
            status="in_progress",
            assigned_agent_id=uuid.UUID(agent_id),
            **fields,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        return task.id


async def _poll(token: str, *, redis=None) -> dict:
    """One poll GET with session+redis overrides (mirrors
    test_poll_orphaned_run._poll). Pass the shared fake_redis so the
    mc:heal claim healer 1 makes is visible to healer 2 (one Redis in prod)."""
    from app.main import app as fastapi_app
    from app.database import get_session
    from app.redis_client import get_redis
    from fakeredis.aioredis import FakeServer, FakeRedis

    if redis is None:
        redis = FakeRedis(server=FakeServer(), decode_responses=True)

    async def override_get_session():
        async with _session() as s:
            yield s

    async def override_get_redis():
        return redis

    import app.routers.agents as agents_mod
    original = agents_mod.get_redis
    agents_mod.get_redis = override_get_redis
    fastapi_app.dependency_overrides[get_redis] = override_get_redis
    fastapi_app.dependency_overrides[get_session] = override_get_session
    try:
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                "/api/v1/agent/me/poll",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200, resp.text
        return resp.json()
    finally:
        agents_mod.get_redis = original
        fastapi_app.dependency_overrides.pop(get_redis, None)
        fastapi_app.dependency_overrides.pop(get_session, None)


async def _count_redispatch_events(task_id) -> int:
    from app.models.activity import ActivityEvent

    async with _session() as s:
        events = list((await s.exec(
            select(ActivityEvent).where(
                ActivityEvent.event_type == "task.orphaned_run_redispatched",
                ActivityEvent.task_id == task_id,
            )
        )).all())
    return len(events)


async def _reseed_staleness(task_id, agent_id) -> None:
    """Re-seed the staleness the orphan-recovery pass needs after the poll
    path has run: updated_at is an onupdate column (any commit rewrites it)
    and the poll's agent auth refreshed last_seen_at. Both go stale-naive,
    matching the model convention (see test_watchdog_dedup_and_renewal)."""
    from app.models.agent import Agent
    from app.models.task import Task
    from sqlmodel import update as _update

    stale_naive = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(minutes=40)
    async with _session() as s:
        await s.exec(_update(Task).where(Task.id == task_id).values(updated_at=stale_naive))
        await s.exec(_update(Agent).where(Agent.id == uuid.UUID(agent_id)).values(last_seen_at=stale_naive))
        await s.commit()



# -- Test 1: two healers in the same round -> ONE action, ONE event -----


@pytest.mark.asyncio
async def test_two_healers_same_round_yield_one_action_and_one_event(fake_redis):
    """First healer (poll-orphan redispatch) claims mc:heal; a second
    healer hitting the same card in the same round must skip: no second
    redispatch, no second event, no inbox reset."""
    import app.redis_client as redis_client
    from app.models.task import Task

    board_id, agent_id, token = await _make_agent()
    task_id = await _make_task(
        board_id, agent_id,
        ack_at=_past(1200), dispatched_at=_past(1260),
        dispatch_attempt_id="aaaaaaaa-1111-2222-3333-444444444444",
    )

    # Healer 1: the poll-orphan redispatch (real path, claims mc:heal on
    # the SHARED fake_redis so healer 2 sees the claim, as in prod).
    with patch("app.services.dispatch.build_agent_task_prompt", return_value="P"):
        body = await _poll(token, redis=fake_redis)
    assert body["orphaned_run_redispatched"] is True
    assert await _count_redispatch_events(task_id) == 1

    # Healer 2 must be ELIGIBLE (stale card, stale heartbeat) so that only
    # the mc:heal claim stands between it and the inbox reset.
    await _reseed_staleness(task_id, agent_id)

    # Healer 2: an orphan-recovery pass on the SAME card in the SAME
    # round -- the claim is held, so it must skip its inbox reset.
    import app.services.watchdog.task_monitor as task_monitor
    monitor = task_monitor.TaskMonitorMixin()
    async with _session() as s:
        with patch.object(task_monitor, "get_redis", return_value=fake_redis):
            recovered = await monitor._recover_orphaned_tasks(s)

    assert recovered == 0, (
        "second healer in the same round must not act on the claimed card"
    )
    # The card was NOT requeued by healer 2.
    async with _session() as s:
        fresh = await s.get(Task, task_id)
    assert fresh.status == "in_progress"
    assert await _count_redispatch_events(task_id) == 1


# -- Test 2: after the TTL expires healing is possible again ------------


@pytest.mark.asyncio
async def test_claim_expires_and_healing_resumes(fake_redis):
    from app.redis_client import HEAL_DEDUP_TTL, RedisKeys, try_claim_heal

    task_id = str(uuid.uuid4())
    assert await try_claim_heal(fake_redis, task_id) is True
    assert await try_claim_heal(fake_redis, task_id) is False

    # Expire the claim (delete ~= TTL elapsed in fakeredis).
    await fake_redis.delete(RedisKeys.task_heal_claim(task_id))
    assert await try_claim_heal(fake_redis, task_id) is True

    # Sanity: a fresh claim really carries the named TTL constant.
    ttl = await fake_redis.ttl(RedisKeys.task_heal_claim(task_id))
    assert 0 < ttl <= HEAL_DEDUP_TTL


# -- Test 3: sabotage probe -- without the claim, both healers act ------
# Verified RED pre-implementation: with the claim gate removed, the
# orphan-recovery pass heals the same card the redispatch just healed.


@pytest.mark.asyncio
async def test_sabotage_without_claim_gate_both_healers_act(fake_redis):
    """Sabotage: neuter the claim (the heal key never sticks) -- then the
    orphan-recovery pass heals the same card the redispatch just healed,
    producing TWO healing actions. This is the bug W0.1 closes."""
    from app.models.agent import Agent
    from app.models.task import Task

    board_id, agent_id, token = await _make_agent()
    task_id = await _make_task(
        board_id, agent_id,
        ack_at=_past(1200), dispatched_at=_past(1260),
        updated_at=_past(2400),
    )

    # Healer 1 acts (claims the key on the shared fake_redis).
    with patch("app.services.dispatch.build_agent_task_prompt", return_value="P"):
        body = await _poll(token, redis=fake_redis)
    assert body["orphaned_run_redispatched"] is True

    await _reseed_staleness(task_id, agent_id)

    # Sabotage: the claim gate is disabled (helper "removed") -- every
    # healer's try_claim_heal reports success, so BOTH act. Patched on the
    # task_monitor namespace: task_monitor.py binds the name at import time
    # (from app.redis_client import try_claim_heal), so patching the source
    # module would not reach the gate. Healer 2 runs on the SHARED
    # fake_redis (patch.object(task_monitor, "get_redis", ...)), otherwise it
    # lands on the autouse-isolated singleton and never sees healer 1's
    # mc:heal key.
    async def _sabotaged_claim(redis, task_id):
        return True

    import app.services.watchdog.task_monitor as task_monitor
    from app.services.watchdog.task_monitor import TaskMonitorMixin
    with (
        patch.object(task_monitor, "try_claim_heal", _sabotaged_claim),
        patch.object(task_monitor, "get_redis", return_value=fake_redis),
    ):
        monitor = TaskMonitorMixin()
        async with _session() as s:
            recovered = await monitor._recover_orphaned_tasks(s)

    assert recovered == 1, (
        "with the claim gate sabotaged, the second healer DOES act -- "
        "the race W0.1 closes"
    )
    async with _session() as s:
        fresh = await s.get(Task, task_id)
    assert fresh.status == "inbox"
