"""The update drain in Postgres (ADR-094 decision 7, update plan PR 4c).

While an MC update runs, the engine on the host sets one row in
``system_state`` (``key='update_drain'``, value ``{"run_id", "expires_at",
"set_by"}``). From then on MC hands out no new work — dispatch, the poll
claim, the watchdog's re-dispatch, new group-chat and loop rounds, due
scheduled jobs — and the Docker gateway refuses container changes. Held-back
dispatches and jobs are recorded and caught up once the drain is gone.

Plan sabotage probes covered here:
  (a) Redis flushed during the run → still no dispatch (the drain is not in Redis)
  (b) drain check removed from the loop runner → test_loop_runner_starts_no_round_in_drain red
"""
from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.group import AgentGroup, GroupRound
from app.models.loop import Loop
from app.models.scheduled_job import ScheduledJob
from app.models.scheduled_job_run import ScheduledJobRun
from app.models.system_update import DRAIN_KEY, SystemState
from app.models.task import Task
from app.services import docker_gateway, update_drain
from app.services.operations import UPDATE_DRAIN_REASON, check_dispatch_allowed
from tests.conftest import test_engine

RUN_ID = "20261019T120000Z-abc123"


def _iso(ts: datetime) -> str:
    # Exactly the engine's format (scripts/mc_update.py _iso).
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _set_drain(*, minutes: float = 30, run_id: str = RUN_ID, expires: str | None = None) -> None:
    value = {
        "run_id": run_id,
        "expires_at": expires if expires is not None else _iso(datetime.now(timezone.utc) + timedelta(minutes=minutes)),
        "set_by": "mc_update",
    }
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(SystemState, DRAIN_KEY)
        if row is None:
            s.add(SystemState(key=DRAIN_KEY, value=value))
        else:
            row.value = value
            s.add(row)
        await s.commit()


async def _clear_drain() -> None:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(SystemState, DRAIN_KEY)
        if row is not None:
            await s.delete(row)
            await s.commit()


async def _deferred_keys() -> list[str]:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = await s.exec(select(SystemState).where(SystemState.key.startswith("deferred:")))
        return sorted(r.key for r in rows.all())


@pytest.fixture(autouse=True)
def _isolated_drain(monkeypatch):
    """Every test starts with an empty drain cache, a gateway on the
    settings-only source, and the module's engine pointed at the test DB."""
    update_drain.reset_cache()
    monkeypatch.setattr(update_drain, "engine", test_engine)
    previous = docker_gateway.set_drain_source(docker_gateway.SettingsDrainSource())
    yield
    update_drain.reset_cache()
    docker_gateway.set_drain_source(previous)
    docker_gateway.reset_drain_cache()


# ── the row and its expiry ─────────────────────────────────────────────────


def test_drain_row_counts_only_until_it_expires():
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    live = update_drain.parse_row({"run_id": RUN_ID, "expires_at": "2026-10-19T12:30:00Z"})
    gone = update_drain.parse_row({"run_id": RUN_ID, "expires_at": "2026-10-19T11:59:59Z"})
    assert live.state(now).active is True and live.state(now).run_id == RUN_ID
    assert gone.state(now).active is False


def test_unreadable_expiry_fails_closed():
    """A drain row whose expiry cannot be read still drains: handing out work
    in the middle of an update is the worse error (the bridge lifts it)."""
    assert update_drain.parse_row({"run_id": RUN_ID, "expires_at": "soon"}).state().active is True
    assert update_drain.parse_row({"run_id": RUN_ID}).state().active is True


@pytest.mark.asyncio
async def test_current_reads_the_row_fresh_with_a_session(session):
    assert (await update_drain.current(session)).active is False
    await _set_drain()
    state = await update_drain.current(session)
    assert state.active is True and state.run_id == RUN_ID
    # The cache followed the fresh read (the Docker gateway serves from it).
    assert update_drain.cached_state().active is True
    await _clear_drain()
    assert (await update_drain.current(session)).active is False


@pytest.mark.asyncio
async def test_expired_drain_hands_out_work_again(session):
    await _set_drain(minutes=-1)
    assert (await update_drain.current(session)).active is False


# ── dispatch ───────────────────────────────────────────────────────────────


async def _board_task(*, intent: str = "root", status: str = "inbox", auto: bool = True):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="Drain", slug=f"drain-{uuid.uuid4().hex[:6]}", auto_dispatch_enabled=auto)
        s.add(board)
        await s.commit()
        task = Task(board_id=board.id, title="drain probe", status=status, dispatch_intent=intent)
        s.add(task)
        await s.commit()
        await s.refresh(task)
        return board, task


@pytest.mark.asyncio
async def test_dispatch_is_held_in_the_drain(session, fake_redis):
    _, task = await _board_task()
    assert await check_dispatch_allowed(task, None, session) == (True, "")
    await _set_drain()
    assert await check_dispatch_allowed(task, None, session) == (False, UPDATE_DRAIN_REASON)


@pytest.mark.asyncio
async def test_continuation_flows_are_held_too(session, fake_redis):
    """Unlike the Redis DRAINING mode, the update drain also holds review
    hand-offs: the code switch follows once running work reaches zero."""
    _, task = await _board_task(intent="review_handoff")
    await _set_drain()
    allowed, reason = await check_dispatch_allowed(task, None, session)
    assert allowed is False and reason == UPDATE_DRAIN_REASON


@pytest.mark.asyncio
async def test_redis_flush_does_not_lift_the_drain(session, fake_redis):
    """Sabotage probe (a): a FLUSHALL during the run must not hand out work."""
    _, task = await _board_task()
    await _set_drain()
    await fake_redis.flushall()
    allowed, _ = await check_dispatch_allowed(task, None, session)
    assert allowed is False


@pytest.mark.asyncio
async def test_auto_dispatch_in_the_drain_defers_and_assigns_nothing(fake_redis, monkeypatch):
    from app.services import dispatch

    monkeypatch.setattr(dispatch, "engine", test_engine)
    board, task = await _board_task()
    await _set_drain()
    with patch("app.services.dispatch.find_dispatch_target", new_callable=AsyncMock) as target:
        await dispatch.auto_dispatch_task(task.id, board.id)
    target.assert_not_awaited()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
    assert fresh.assigned_agent_id is None and fresh.status == "inbox"
    assert await _deferred_keys() == [f"deferred:dispatch:{task.id}"]


@pytest.mark.asyncio
async def test_catch_up_dispatches_held_work_once_the_drain_is_gone(fake_redis, monkeypatch):
    _, task = await _board_task()
    await update_drain.defer(update_drain.KIND_DISPATCH, task.id, bind=test_engine, run_id=RUN_ID)
    await update_drain.defer(update_drain.KIND_DISPATCH, task.id, bind=test_engine, run_id=RUN_ID)  # idempotent
    job_id = str(uuid.uuid4())
    await update_drain.defer(update_drain.KIND_SCHEDULE, job_id, bind=test_engine, run_id=RUN_ID)
    assert len(await _deferred_keys()) == 2

    dispatched = AsyncMock()
    executed = AsyncMock()
    monkeypatch.setattr("app.services.dispatch.auto_dispatch_task", dispatched)
    from app.services.scheduler import scheduler
    monkeypatch.setattr(scheduler, "execute_deferred", executed)

    # Still drained: nothing happens, nothing is consumed.
    await _set_drain()
    counts = await update_drain.catch_up(test_engine)
    assert counts["dispatch"] == 0 and counts["schedule"] == 0
    assert len(await _deferred_keys()) == 2
    dispatched.assert_not_awaited()

    await _clear_drain()
    counts = await update_drain.catch_up(test_engine)
    assert counts == {"dispatch": 1, "schedule": 1, "skipped": 0}
    dispatched.assert_awaited_once_with(task.id, task.board_id)
    executed.assert_awaited_once_with(job_id)
    assert await _deferred_keys() == []
    # Consumed: a second pass does nothing.
    assert (await update_drain.catch_up(test_engine))["dispatch"] == 0


@pytest.mark.asyncio
async def test_catch_up_skips_a_task_someone_already_moved(fake_redis, monkeypatch):
    _, task = await _board_task(status="done")
    await update_drain.defer(update_drain.KIND_DISPATCH, task.id, bind=test_engine)
    dispatched = AsyncMock()
    monkeypatch.setattr("app.services.dispatch.auto_dispatch_task", dispatched)
    counts = await update_drain.catch_up(test_engine)
    assert counts["skipped"] == 1
    dispatched.assert_not_awaited()
    assert await _deferred_keys() == []


# ── poll claim ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_does_not_claim_a_fresh_inbox_task_in_the_drain(client, async_session):
    from app.auth import generate_agent_token

    board = Board(name="P", slug=f"p-{uuid.uuid4().hex[:6]}")
    async_session.add(board)
    await async_session.commit()
    raw, token_hash = generate_agent_token()
    agent = Agent(name=f"Worker-{uuid.uuid4().hex[:6]}", agent_runtime="host",
                  agent_token_hash=token_hash, board_id=board.id)
    async_session.add(agent)
    await async_session.commit()
    task = Task(board_id=board.id, assigned_agent_id=agent.id, title="fresh", status="inbox")
    async_session.add(task)
    await async_session.commit()

    await _set_drain()
    with patch("app.services.dispatch.build_agent_task_prompt", return_value="prompt"):
        r = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "idle" and r.json().get("update_drain") is True
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert (await s.get(Task, task.id)).dispatched_at is None

    await _clear_drain()
    with patch("app.services.dispatch.build_agent_task_prompt", return_value="prompt"):
        r = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {raw}"})
    assert r.json()["state"] == "new_task", r.json()


# ── loop runner / group runner ─────────────────────────────────────────────


async def _loop_tick() -> None:
    from app.services.loop_runner import LoopRunnerService

    with patch("app.services.loop_runner.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_create.emit_event", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await LoopRunnerService().tick(s)


@pytest.mark.asyncio
async def test_loop_runner_starts_no_round_in_drain(fake_redis):
    """Sabotage probe (b): without the drain check in loop_runner this is red."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="L", slug=f"l-{uuid.uuid4().hex[:6]}", auto_dispatch_enabled=False)
        s.add(board)
        await s.commit()
        loop = Loop(board_id=board.id, name="Polish", goal="better", backlog_source="markdown",
                    backlog_md="- [ ] A", status="running", max_rounds=3)
        s.add(loop)
        await s.commit()
        await s.refresh(loop)

    await _set_drain()
    await _loop_tick()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        held = await s.get(Loop, loop.id)
        assert held.current_round_no == 0 and held.current_task_id is None
        assert held.status == "running"

    await _clear_drain()
    await _loop_tick()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert (await s.get(Loop, loop.id)).current_round_no == 1


@pytest.mark.asyncio
async def test_group_runner_starts_no_new_round_in_drain(fake_redis, monkeypatch, tmp_path):
    from app.services import group_service
    from app.services.group_runner import GroupRunnerService, start_group

    monkeypatch.setattr("app.services.reference_ingest.references_root", lambda: str(tmp_path))
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agents = []
        for name in ("Alpha", "Beta"):
            a = Agent(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:4]}", agent_runtime="cli-bridge",
                      comm_v2=True)
            s.add(a)
            await s.commit()
            await s.refresh(a)
            agents.append(a)
        group = await group_service.create_group(
            s, name="Room", goal="decide", member_ids=[a.id for a in agents], lead_agent_id=agents[0].id,
        )
        group = await start_group(s, group)

    await _set_drain()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await GroupRunnerService().tick(s)
        rounds = (await s.exec(select(GroupRound).where(GroupRound.group_id == group.id))).all()
        assert rounds == []
        assert (await s.get(AgentGroup, group.id)).status == "running"

    await _clear_drain()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await GroupRunnerService().tick(s)
        rounds = (await s.exec(select(GroupRound).where(GroupRound.group_id == group.id))).all()
        assert len(rounds) == 1


# ── scheduler ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduled_job_due_in_the_drain_is_deferred_not_run(fake_redis, monkeypatch):
    from app.services import scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "engine", test_engine)
    monkeypatch.setattr("app.services.sse.broadcast", AsyncMock())
    svc = sched_mod.SchedulerService()
    monkeypatch.setattr(svc, "_do_create_task", AsyncMock(return_value=(True, None, {})))
    monkeypatch.setattr(svc, "_update_next_run", AsyncMock())
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        job = ScheduledJob(name="Morning", schedule_type="daily", schedule_time="07:00",
                           action_type="create_task", enabled=True)
        s.add(job)
        await s.commit()
        await s.refresh(job)

    await _set_drain()
    await svc._execute_job(str(job.id))
    svc._do_create_task.assert_not_awaited()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert (await s.exec(select(ScheduledJobRun))).all() == []
    assert await _deferred_keys() == [f"deferred:schedule:{job.id}"]

    # After the update: the catch-up runs it once, through the normal path.
    await _clear_drain()
    monkeypatch.setattr(sched_mod, "scheduler", svc)
    counts = await update_drain.catch_up(test_engine)
    assert counts["schedule"] == 1
    svc._do_create_task.assert_awaited_once()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert len((await s.exec(select(ScheduledJobRun))).all()) == 1


# ── watchdog ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watchdog_redispatches_nothing_in_the_drain(fake_redis, monkeypatch):
    from app.services.watchdog import core as wd_core
    from app.services.watchdog import watchdog

    monkeypatch.setattr(wd_core, "engine", test_engine)
    stubs = {}
    for name in dir(watchdog):
        if name.startswith(("_check_", "_recover_", "_collect_")) and name != "_check_all":
            stub = AsyncMock(return_value=(0.0, 0.0) if name == "_check_system_health" else 0)
            monkeypatch.setattr(watchdog, name, stub)
            stubs[name] = stub
    monkeypatch.setattr("app.services.discord_notify.flush_digest", AsyncMock(return_value=0))
    monkeypatch.setattr("app.services.dispatch_gating.process_planned_tasks",
                        AsyncMock(return_value={"checked": 0, "promoted": 0, "approval": 0, "manual": 0}))
    monkeypatch.setattr(watchdog, "_checks_total", 1)  # skip the every-5th-tick harvester

    await _set_drain()
    await watchdog._check_all()
    stubs["_check_undispatched_tasks"].assert_not_awaited()
    stubs["_check_agent_health"].assert_awaited()  # health checks still run

    await _clear_drain()
    await watchdog._check_all()
    stubs["_check_undispatched_tasks"].assert_awaited()


# ── Docker gateway ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gateway_refuses_container_changes_in_the_postgres_drain(monkeypatch):
    """The gateway (PR 4a) now reads the Postgres drain: in the drain a
    recreate never reaches docker; reads stay free."""
    update_drain.install_gateway_source()
    calls: list = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))

    await update_drain.prime(test_engine)
    docker_gateway.run(["compose", "-p", "mission-control", "up", "-d", "--force-recreate", "agent"])
    assert len(calls) == 1  # no drain: transparent

    await _set_drain()
    await update_drain.prime(test_engine)
    with pytest.raises(docker_gateway.DockerDeferred) as exc:
        docker_gateway.run(["compose", "-p", "mission-control", "up", "-d", "--force-recreate", "agent"])
    assert exc.value.run_id == RUN_ID
    assert len(calls) == 1  # nothing reached docker
    docker_gateway.run(["ps", "-q"])
    assert len(calls) == 2  # reads stay free


def test_gateway_source_before_the_first_read_keeps_the_guard_state():
    update_drain.reset_cache()
    with pytest.raises(RuntimeError):
        update_drain.PostgresDrainSource().read()


def test_install_gateway_source_is_idempotent():
    update_drain.install_gateway_source()
    first = docker_gateway._guard.source
    update_drain.install_gateway_source()
    assert docker_gateway._guard.source is first
    assert any(isinstance(s, update_drain.PostgresDrainSource) for s in first.sources)
    assert any(isinstance(s, docker_gateway.SettingsDrainSource) for s in first.sources)
