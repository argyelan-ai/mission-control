"""Tests for Fix 2 — poll orphan-run detection (incident 07.09.2026).

Incident: Agent Alpha ACKed Task D from a stale session; the bridge kept polling
`working` (ack_at was set), but nobody worked — 70 minutes silent, no alarm.

Contract under test (`/agent/me/poll`):
- An acked worker `in_progress` task reports `state=working` ONLY while a
  liveness signal for the run exists within the threshold
  (settings.poll_orphan_run_threshold_seconds, default 600 s):
  agent.last_task_activity_at (working-heartbeat), a harvested
  ModelUsageEvent for the task, or ack_at itself (grace — a JUST-acked run
  has no heartbeat cycle yet).
- Stale/missing signals → the run is ORPHANED: poll returns
  `state=new_task` with a NEW dispatch_attempt_id + cleared ack_at and fires
  event task.orphaned_run_redispatched (severity=warning) with a
  redispatch counter in detail (endlos-redispatch guard, Redis-backed).
- `waiting` tasks park unconditionally (Task 12 hold) — never orphaned.
- Blocked tasks park per the B1 grace window — never orphaned here.

Conventions (tests/conftest.py): in-memory SQLite `test_engine`, `fake_redis`
fixture, `make_board`/`make_agent`/`make_task` factories. No freezegun —
past timestamps are injected into DB rows.
"""
from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from tests.conftest import test_engine


def _session() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


async def _make_agent(*, last_task_activity_at=None, is_board_lead=False):
    from app.models.agent import Agent
    from app.models.board import Board

    async with _session() as s:
        board = Board(name="Orphan Board", slug=f"orphan-{uuid.uuid4().hex[:8]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        raw_token, token_hash = generate_agent_token()
        agent = Agent(
            name=f"Worker-{uuid.uuid4().hex[:6]}",
            agent_runtime="cli-bridge",
            agent_token_hash=token_hash,
            board_id=board.id,
            is_board_lead=is_board_lead,
            last_task_activity_at=last_task_activity_at,
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return board.id, agent.id, raw_token


async def _make_task(board_id, agent_id, *, status="in_progress", ack_at=None,
                     dispatched_at=None, bind_current_task=False):
    from app.models.agent import Agent
    from app.models.task import Task

    async with _session() as s:
        task = Task(
            board_id=board_id,
            assigned_agent_id=agent_id,
            title="Orphan probe",
            status=status,
            dispatched_at=dispatched_at,
            ack_at=ack_at,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        if bind_current_task:
            agent = await s.get(Agent, agent_id)
            agent.current_task_id = task.id
            s.add(agent)
            await s.commit()
        return task.id


async def _set_ack(task_id, when):
    from app.models.task import Task

    async with _session() as s:
        await s.exec(update(Task).where(Task.id == task_id).values(ack_at=when))
        await s.commit()


async def _poll(token, *, redis=None):
    """One poll GET. Pass a shared ``redis`` (FakeRedis) to observe counters
    across multiple polls in one test; a fresh fakeredis per call otherwise."""
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
        return resp.json(), redis
    finally:
        agents_mod.get_redis = original
        fastapi_app.dependency_overrides.pop(get_redis, None)
        fastapi_app.dependency_overrides.pop(get_session, None)


def _past(seconds: int) -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_acked_with_fresh_heartbeat_reports_working():
    """acked + working-heartbeat fresher than the threshold → working."""
    board_id, agent_id, token = await _make_agent(
        last_task_activity_at=_past(30),
    )
    task_id = await _make_task(
        board_id, agent_id,
        ack_at=_past(120), dispatched_at=_past(180),
    )

    body, _ = await _poll(token)
    assert body["state"] == "working", body
    assert body["task_id"] == str(task_id)


@pytest.mark.asyncio
async def test_acked_with_fresh_model_event_reports_working():
    """acked + harvested ModelUsageEvent for the task → working."""
    board_id, agent_id, token = await _make_agent(last_task_activity_at=None)
    task_id = await _make_task(
        board_id, agent_id,
        ack_at=_past(300), dispatched_at=_past(360),
    )

    from app.models.model_usage import ModelUsageEvent

    async with _session() as s:
        s.add(ModelUsageEvent(
            agent_id=agent_id, task_id=task_id,
            harness="claude-code", model="claude-sonnet-4",
            session_id="sess-1", message_uuid=f"u-{uuid.uuid4().hex[:12]}",
            ts=_past(45),
            source_file=f"/tmp/transcripts/{uuid.uuid4().hex[:8]}.jsonl",
        ))
        await s.commit()

    body, _ = await _poll(token)
    assert body["state"] == "working", body


@pytest.mark.asyncio
async def test_acked_without_signal_within_threshold_redispatches():
    """acked, NO liveness signal at all, older than threshold → orphan:
    new_task with a NEW dispatch_attempt_id + warning event + counter."""
    board_id, agent_id, token = await _make_agent(last_task_activity_at=None)
    old_attempt = "aaaaaaaa-1111-2222-3333-444444444444"
    task_id = await _make_task(
        board_id, agent_id,
        ack_at=_past(1200), dispatched_at=_past(1260),
    )
    from app.models.task import Task
    from sqlmodel import update
    from sqlmodel.ext.asyncio.session import AsyncSession
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await s.exec(update(Task).where(Task.id == task_id).values(
            dispatch_attempt_id=old_attempt,
        ))
        await s.commit()

    with patch("app.services.dispatch.build_agent_task_prompt", return_value="P"):
        body, redis = await _poll(token)

    assert body["state"] == "new_task", body
    assert body["orphaned_run_redispatched"] is True
    assert body["redispatch_count"] == 1
    task = body["task"]
    assert task["dispatch_attempt_id"] not in (None, old_attempt)
    assert task["prompt"] == "P"

    # Event: warning severity with the counter in detail. The alert lives
    # in ActivityEvent (status transitions go to TaskEvent).
    from sqlmodel import select
    from app.models.activity import ActivityEvent
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        alerts = list((await s.exec(
            select(ActivityEvent).where(
                ActivityEvent.event_type == "task.orphaned_run_redispatched",
                ActivityEvent.task_id == task_id,
            )
        )).all())
    assert len(alerts) == 1
    assert alerts[0].severity == "warning"
    assert alerts[0].detail["redispatch_count"] == 1
    assert alerts[0].detail["threshold_seconds"] == 600

    # ack_at cleared in DB.
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task_id)
    assert fresh.ack_at is None

    # Redis counter rides along (endlos-redispatch guard).
    count = await redis.get(f"mc:poll:orphan_redispatch_count:{task_id}")
    assert int(count) == 1


@pytest.mark.asyncio
async def test_stale_signal_beyond_threshold_redispatches():
    """last_task_activity_at older than the threshold counts as orphaned."""
    board_id, agent_id, token = await _make_agent(
        last_task_activity_at=_past(700),
    )
    task_id = await _make_task(
        board_id, agent_id,
        ack_at=_past(1200), dispatched_at=_past(1260),
    )

    with patch("app.services.dispatch.build_agent_task_prompt", return_value="P"):
        body, _ = await _poll(token)

    assert body["state"] == "new_task", body
    assert body["orphaned_run_redispatched"] is True


@pytest.mark.asyncio
async def test_redispatch_counter_increments_across_orphans():
    """Second orphan detection on the same task reports count=2 — the
    endlos-redispatch guard is observable (shared Redis across polls)."""
    from fakeredis.aioredis import FakeServer, FakeRedis

    board_id, agent_id, token = await _make_agent(last_task_activity_at=None)
    task_id = await _make_task(
        board_id, agent_id,
        ack_at=_past(1200), dispatched_at=_past(1260),
    )
    shared = FakeRedis(server=FakeServer(), decode_responses=True)

    with patch("app.services.dispatch.build_agent_task_prompt", return_value="P"):
        first, _ = await _poll(token, redis=shared)
    assert first["redispatch_count"] == 1

    # Re-orphan: the redispatched run again shows no liveness (fresh ack_at
    # from the redispatch is a grace signal — age it past the threshold).
    await _set_ack(task_id, _past(1200))
    with patch("app.services.dispatch.build_agent_task_prompt", return_value="P"):
        second, _ = await _poll(token, redis=shared)
    assert second["redispatch_count"] == 2


@pytest.mark.asyncio
async def test_waiting_task_parks_unconditionally():
    """Guardrail: waiting tasks (mc ask --blocking) are never orphaned —
    poll holds the session even with zero liveness signals."""
    board_id, agent_id, token = await _make_agent(last_task_activity_at=None)
    task_id = await _make_task(
        board_id, agent_id,
        status="waiting",
        ack_at=_past(7200), dispatched_at=_past(7260),
        # Production: the ACK PATCH binds current_task_id; the Task 12 hold
        # is keyed off that pointer.
        bind_current_task=True,
    )

    body, _ = await _poll(token)
    assert body["state"] == "working", body
    assert body["task_id"] == str(task_id)


@pytest.mark.asyncio
async def test_blocked_task_not_orphaned_by_poll():
    """Guardrail: blocked tasks park per the B1 grace window — the orphan
    path must not touch them."""
    board_id, agent_id, token = await _make_agent(last_task_activity_at=None)
    task_id = await _make_task(
        board_id, agent_id,
        status="blocked",
        ack_at=_past(120), dispatched_at=_past(180),
    )

    body, _ = await _poll(token)
    assert body["state"] == "working", body
    assert body["task_id"] == str(task_id)


@pytest.mark.asyncio
async def test_board_lead_task_not_orphaned():
    """Board Leads orchestrate between polls — never orphan their task."""
    board_id, agent_id, token = await _make_agent(
        last_task_activity_at=None, is_board_lead=True,
    )
    task_id = await _make_task(
        board_id, agent_id,
        ack_at=_past(7200), dispatched_at=_past(7260),
    )

    body, _ = await _poll(token)
    assert body["state"] == "working", body


@pytest.mark.asyncio
async def test_just_acked_run_not_redispatched():
    """A JUST-acked run has no heartbeat cycle yet — ack_at itself is the
    grace signal, no instant redispatch on the next poll."""
    board_id, agent_id, token = await _make_agent(last_task_activity_at=None)
    task_id = await _make_task(
        board_id, agent_id,
        ack_at=_past(20), dispatched_at=_past(60),
    )

    body, _ = await _poll(token)
    assert body["state"] == "working", body
    assert "orphaned_run_redispatched" not in body


@pytest.mark.asyncio
async def test_settings_threshold_exposed():
    """The threshold is configurable via settings, default 600 s."""
    from app.config import settings
    assert settings.poll_orphan_run_threshold_seconds == 600
