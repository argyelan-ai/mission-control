"""Bauplan Lauf 2 Teil 3 (analyse.md, 21.09.2026): the working->idle
heartbeat flank drains the agent's Redis task queue when a host agent's
turn ends.

Problem: `drain_agent_task_queue` today only fires on task completion
(task_lifecycle.py:1047/1412), but Hermes marks its task `done`/`review`
BEFORE its ACP turn actually finishes (it keeps writing a summary/posting
comments) — so a card Guard 3 (Teil 2) queued during that turn sits until
the 15-minute pending escalation fires. This heartbeat flank is the
missing trigger: `agent_heartbeat` (routers/agents.py) must notice the
status transition working -> idle (captured BEFORE any mutation, in
`_prev_status`) and, AFTER the commit, fire
`create_tracked_task(drain_agent_task_queue(str(agent.id)))`.

RED (21.09.2026): none of this exists yet — `drain_agent_task_queue` is
never called from the heartbeat handler, so all four tests fail because
the patched mock is never invoked (or, for the failure-tolerance test,
because there is nothing calling it that could raise).

CI-Flake fix (22.09.2026): `create_tracked_task` is fire-and-forget by
design (asyncio.create_task) — in production that is exactly the point,
but in this test file it used to leave a REAL background task running
past the end of each test (only bridged by an old `_settle()` busy-loop
that yielded the event loop a few times, hoping the task finished in
time). On CI that task occasionally survived into the NEXT test and
touched the shared StaticPool SQLite connection there, breaking an
unrelated test (tests/test_vault_cleanup_batch.py) with a timezone
StatementError. Fix: `app.utils.create_tracked_task` is patched (module
attribute — agents.py does a local `from app.utils import
create_tracked_task` on every call, so it always picks up whatever is
currently on the module) with a stub that still schedules a real
asyncio.Task (agents.py calls it synchronously, no `await`, so the stub
cannot be a coroutine either) but also COLLECTS it, so each test can
`await` it explicitly via the new `_settle()` helper before the test
ends — deterministic, not a hopeful sleep loop. An autouse fixture
additionally asserts, after every test in this file, that no extra
asyncio Task survives it.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _make_agent_with_active_task(*, status: str):
    """Host agent with ONE in_progress task assigned — mirrors a real
    Hermes turn (task stays in_progress while the bridge is mid-turn), so
    the heartbeat self-heal in routers/agents.py keeps `status` following
    the payload for both 'working' and 'idle' instead of the Bug-18
    no-active-task coercion path."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    raw_token, token_hash = generate_agent_token()
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(
            id=board_id, name=f"HBDrain-{uuid.uuid4().hex[:6]}",
            slug=f"hbdrain-{uuid.uuid4().hex[:6]}",
        ))
        await s.commit()

        agent = Agent(
            id=agent_id,
            name=f"HermesLike-{uuid.uuid4().hex[:6]}",
            role="developer",
            board_id=board_id,
            agent_runtime="host",
            status=status,
            agent_token_hash=token_hash,
            scopes=["heartbeat", "tasks:read"],
            provision_status="provisioned",
        )
        s.add(agent)
        await s.commit()

        task = Task(
            board_id=board_id,
            title="Turn-end drain probe",
            status="in_progress",
            assigned_agent_id=agent_id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(agent)

    return agent, raw_token


@pytest.fixture
def tracked_tasks(monkeypatch):
    """Patches `app.utils.create_tracked_task` (module attribute — agents.py
    does a LOCAL `from app.utils import create_tracked_task` on every
    heartbeat, so it always picks up whatever is currently on the module)
    with a stub that still schedules a REAL asyncio.Task — agents.py calls
    it synchronously (no `await create_tracked_task(...)`), so the stub
    must be a plain function too, not a coroutine, or it leaks an
    unawaited-coroutine warning of its own — but also COLLECTS the task
    here so the test can await it explicitly via `_settle()` below instead
    of it surviving past the test (CI-Flake fix, 22.09.2026: the previous
    `_settle()` busy-loop only yielded the event loop a few times and
    hoped the real background task had finished by then; on CI it
    sometimes hadn't, and the task went on to touch the shared StaticPool
    SQLite connection in the NEXT test — tests/test_vault_cleanup_batch.py
    failed with a timezone StatementError two runs in a row)."""
    tasks: list[asyncio.Task] = []

    def _create_and_track(coro, name: str | None = None):
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    monkeypatch.setattr("app.utils.create_tracked_task", _create_and_track)
    return tasks


async def _settle(tasks: list) -> None:
    """Await every tracked background task to completion. `return_exceptions`
    mirrors create_tracked_task's own contract — an unhandled exception in
    the background task is logged (real code's `_on_done`), never raised
    to whoever scheduled it, so the test asserting a swallowed drain
    failure (test_drain_failure_does_not_break_heartbeat) still passes."""
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture(autouse=True)
async def _no_leaked_tasks():
    """CI-Flake fix (22.09.2026): proof, not just hope — after every test
    in this file, no asyncio Task other than the currently-running one may
    still be alive. Catches a future regression of the same leak pattern,
    not just the one this fix addresses. Async fixture (not sync) so its
    teardown runs while this test's event loop is still the running one —
    a sync fixture's teardown here saw 'no running event loop' instead."""
    yield
    current = asyncio.current_task()
    leaked = [
        t for t in asyncio.all_tasks()
        if t is not current and not t.done()
    ]
    assert leaked == [], f"leaked background task(s) after test: {leaked}"


@pytest.mark.asyncio
async def test_working_to_idle_drains_queue(client: AsyncClient, tracked_tasks):
    agent, token = await _make_agent_with_active_task(status="working")
    headers = {"Authorization": f"Bearer {token}"}

    with patch(
        "app.services.task_queue.drain_agent_task_queue", new_callable=AsyncMock
    ) as drain:
        resp = await client.post(
            "/api/v1/agent/me/heartbeat", json={"status": "idle"}, headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await _settle(tracked_tasks)

    drain.assert_called_once_with(str(agent.id))


@pytest.mark.asyncio
async def test_idle_to_idle_does_not_drain(client: AsyncClient, tracked_tasks):
    agent, token = await _make_agent_with_active_task(status="idle")
    headers = {"Authorization": f"Bearer {token}"}

    with patch(
        "app.services.task_queue.drain_agent_task_queue", new_callable=AsyncMock
    ) as drain:
        resp = await client.post(
            "/api/v1/agent/me/heartbeat", json={"status": "idle"}, headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await _settle(tracked_tasks)

    drain.assert_not_called()


@pytest.mark.asyncio
async def test_working_to_working_does_not_drain(client: AsyncClient, tracked_tasks):
    agent, token = await _make_agent_with_active_task(status="working")
    headers = {"Authorization": f"Bearer {token}"}

    with patch(
        "app.services.task_queue.drain_agent_task_queue", new_callable=AsyncMock
    ) as drain:
        resp = await client.post(
            "/api/v1/agent/me/heartbeat", json={"status": "working"}, headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await _settle(tracked_tasks)

    drain.assert_not_called()


@pytest.mark.asyncio
async def test_idle_to_working_does_not_drain(client: AsyncClient, tracked_tasks):
    """Only the working->idle flank drains — the reverse edge (turn just
    STARTED) must never trigger a drain."""
    agent, token = await _make_agent_with_active_task(status="idle")
    headers = {"Authorization": f"Bearer {token}"}

    with patch(
        "app.services.task_queue.drain_agent_task_queue", new_callable=AsyncMock
    ) as drain:
        resp = await client.post(
            "/api/v1/agent/me/heartbeat", json={"status": "working"}, headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await _settle(tracked_tasks)

    drain.assert_not_called()


@pytest.mark.asyncio
async def test_drain_failure_does_not_break_heartbeat(client: AsyncClient, tracked_tasks):
    """A drain that raises must never surface as a broken heartbeat — the
    background task swallows/logs it (create_tracked_task's own
    contract), the HTTP response stays 200."""
    agent, token = await _make_agent_with_active_task(status="working")
    headers = {"Authorization": f"Bearer {token}"}

    with patch(
        "app.services.task_queue.drain_agent_task_queue",
        new_callable=AsyncMock,
        side_effect=RuntimeError("redis is on fire"),
    ) as drain:
        resp = await client.post(
            "/api/v1/agent/me/heartbeat", json={"status": "idle"}, headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await _settle(tracked_tasks)

    drain.assert_called_once_with(str(agent.id))


@pytest.mark.asyncio
async def test_switch_off_keeps_legacy_no_drain(
    client: AsyncClient, monkeypatch, tracked_tasks,
):
    """Schalter (Bauplan 'Schalter', shared by Teil 2-4): with
    host_turn_signal_enabled=False, the working->idle flank must NOT drain
    — the rollback path needs no code change beyond this flag.

    Runde 2 (Pruefbericht Punkt 4): monkeypatch.setattr instead of a hard
    `= True` in `finally` — restores the pre-test value, not an assumption."""
    from app.config import settings

    agent, token = await _make_agent_with_active_task(status="working")
    headers = {"Authorization": f"Bearer {token}"}

    monkeypatch.setattr(settings, "host_turn_signal_enabled", False)
    with patch(
        "app.services.task_queue.drain_agent_task_queue", new_callable=AsyncMock
    ) as drain:
        resp = await client.post(
            "/api/v1/agent/me/heartbeat", json={"status": "idle"}, headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await _settle(tracked_tasks)

    drain.assert_not_called()
