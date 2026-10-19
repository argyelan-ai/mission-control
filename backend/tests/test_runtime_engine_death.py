"""Engine death (services/engine_death.py, wired into runtime_watcher).

The live case behind it (18./19.10.2026): a recipe's memory guard killed the
engine container (exit 137). The watcher wrote "endpoint unreachable" three
times — slot row, the recipe that ran, and a recipe that never ran on the same
port — the box looked free, running tasks retried a dead endpoint until they
ended blocked, and nothing said why it did not come back.

These tests pin the new contract: one honest event with a reason, a
"crashed" status with time, running tasks back in the inbox, and none of it
for planned silence (stop through MC, eviction by a start).
"""
from __future__ import annotations

import json
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.models.activity import ActivityEvent
from app.models.agent import Agent
from app.models.board import Board
from app.models.runtime import Runtime
from app.models.task import Task, TaskComment
from app.redis_client import RedisKeys
from app.services import engine_death, runtime_grace
from app.services import sse as sse_mod
from app.services.agent_runtime_switch import ProbedModel
from app.services.host_resolver import ResolvedHost
from app.services.runtime_watcher import UNREACHABLE_EVENT_THRESHOLD, RuntimeWatcher

BOX = ResolvedHost(ssh_host="192.0.2.10", ssh_user="mc", kind="ssh", source="registry")
ENDPOINT = "http://192.0.2.10:8000/v1"


class FakeBox:
    """`docker inspect` / `docker logs` / `pgrep` of one box, configurable."""

    def __init__(self, *, inspect: str | None = "exited|137|false|2026-10-18T17:20:17Z|2026-10-18T09:00:00Z",
                 logs: str = "", pgrep_running: bool = False):
        self.inspect = inspect
        self.logs = logs
        self.pgrep_running = pgrep_running
        self.commands: list[str] = []

    async def run(self, command, *, host=None, timeout=None):
        self.commands.append(command)
        if command.startswith("docker inspect"):
            if self.inspect is None:
                return ("", "Error: No such object", 1)
            return (self.inspect, "", 0)
        if command.startswith("docker logs"):
            return (self.logs, "", 0)
        if command.startswith("pgrep"):
            return ("", "", 0 if self.pgrep_running else 1)
        return ("", "", 0)


class Probe:
    """What the port answers this tick (None = down)."""

    def __init__(self, served):
        self.served = served

    async def __call__(self, runtime):
        return ProbedModel(model_id=self.served, context_len=None)


def _stack(box: FakeBox, fake_redis, probe: Probe):
    async def _get_redis():
        return fake_redis

    stack = ExitStack()
    for ctx in (
        patch("app.services.runtime_watcher.probe_runtime_model_info", new=probe),
        patch("app.services.runtime_watcher.get_redis", _get_redis),
        patch("app.services.runtime_grace.get_redis", _get_redis),
        patch("app.redis_client.get_redis", _get_redis),
        patch.object(sse_mod, "get_redis", _get_redis),
        patch("app.services.runtime_watcher.resolve_host_for_runtime", new=AsyncMock(return_value=BOX)),
        patch("app.services.runtime_manager._ssh_run", new=box.run),
        patch("app.services.runtime_watcher.RuntimeWatcher._maybe_auto_recover", new=AsyncMock(return_value=None)),
        patch("app.services.runtime_watcher.RuntimeWatcher._served_answer_is_own", new=AsyncMock(return_value=True)),
        patch("app.services.runtime_watcher.host_memory_prep.finish_for_host", new=AsyncMock(return_value=False)),
        patch("app.services.runtime_watcher.host_memory_prep.recover_orphaned_preps", new=AsyncMock(return_value=[])),
        patch("app.services.runtime_watcher.sync_pending_agents", new=AsyncMock(return_value=None)),
        patch("app.services.runtime_watcher.recreate_pending_agents", new=AsyncMock(return_value=None)),
        patch("app.services.runtime_watcher.mark_agents_for_sync", new=AsyncMock(return_value=None)),
    ):
        stack.enter_context(ctx)
    return stack


async def _rt(session, *, slug, model, is_slot=False, runtime_type="ssh_process", process="dsv-tf") -> Runtime:
    rt = Runtime(
        slug=slug, display_name=slug, runtime_type=runtime_type, endpoint=ENDPOINT,
        model_identifier=model, enabled=True, is_slot=is_slot,
        process_name=None if is_slot else process,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _events(session, event_type: str) -> list[ActivityEvent]:
    return list((await session.exec(select(ActivityEvent).where(ActivityEvent.event_type == event_type))).all())


async def _serve_then_die(watcher, session, probe, model):
    """One serving tick, then exactly enough silent ticks to hit the threshold."""
    probe.served = model
    await watcher.tick(session=session)
    probe.served = None
    for _ in range(UNREACHABLE_EVENT_THRESHOLD):
        await watcher.tick(session=session)


@pytest.mark.asyncio
async def test_engine_death_is_reported_once_with_reason_and_time(async_session, fake_redis):
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    box = FakeBox()
    probe = Probe(None)
    with _stack(box, fake_redis, probe):
        await _serve_then_die(RuntimeWatcher(interval=90), async_session, probe, "DeepSeek-V4.1")

    died = await _events(async_session, "runtime.engine_died")
    assert len(died) == 1
    assert died[0].severity == "error"
    assert died[0].detail["reason_code"] == engine_death.KILLED
    assert died[0].detail["exit_code"] == 137
    assert "SIGKILL" in died[0].title
    # The generic alarm is replaced, not doubled.
    assert await _events(async_session, "runtime.unreachable") == []
    # Box autostart is off here → the event says MC will not restart it.
    assert "Box-Autostart ist aus" in died[0].title

    live = json.loads(await fake_redis.get(RedisKeys.runtime_live(rt.slug)))
    assert live["status"] == "crashed"
    assert live["crash"]["reason_code"] == engine_death.KILLED
    assert live["crash"]["at"] == "2026-10-18T17:20:17Z"  # Docker's own finish time
    await async_session.refresh(rt)
    assert rt.serving_since is None


@pytest.mark.asyncio
async def test_crashed_status_survives_further_ticks_and_ends_when_serving(async_session, fake_redis):
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(None)
    watcher = RuntimeWatcher(interval=90)
    with _stack(FakeBox(), fake_redis, probe):
        await _serve_then_die(watcher, async_session, probe, "DeepSeek-V4.1")
        await watcher.tick(session=async_session)  # 4th silent tick
        live = json.loads(await fake_redis.get(RedisKeys.runtime_live(rt.slug)))
        assert live["status"] == "crashed"
        assert len(await _events(async_session, "runtime.engine_died")) == 1

        probe.served = "DeepSeek-V4.1"
        await watcher.tick(session=async_session)

    live = json.loads(await fake_redis.get(RedisKeys.runtime_live(rt.slug)))
    assert live.get("status") != "crashed"
    assert "crash" not in live
    assert await fake_redis.get(runtime_grace.crash_key(rt.slug)) is None


@pytest.mark.asyncio
async def test_shared_port_yields_one_event_naming_the_model_that_ran(async_session, fake_redis):
    """Live 19.10.: slot + DeepSeek + GLM on one port → three alarms. Now one."""
    slot = await _rt(async_session, slug="box-slot", model="DeepSeek-V4.1", is_slot=True,
                     runtime_type="openai_compatible")
    dsv = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    glm = await _rt(async_session, slug="glm", model="GLM-5.3", process="glm-tf")
    probe = Probe(None)
    watcher = RuntimeWatcher(interval=90)
    with _stack(FakeBox(), fake_redis, probe):
        await _serve_then_die(watcher, async_session, probe, "DeepSeek-V4.1")
        # The slot row defers to the recipe row, which mirrors its crash doc
        # onto it; the slot's snapshot picks it up on its next silent probe.
        await watcher.tick(session=async_session)

    died = await _events(async_session, "runtime.engine_died")
    assert len(died) == 1
    assert died[0].detail["slug"] == dsv.slug
    assert await _events(async_session, "runtime.unreachable") == []

    glm_live = json.loads(await fake_redis.get(RedisKeys.runtime_live(glm.slug)))
    assert glm_live.get("status") != "crashed"  # GLM never ran
    slot_live = json.loads(await fake_redis.get(RedisKeys.runtime_live(slot.slug)))
    assert slot_live["status"] == "crashed"     # the box's address is dark too


@pytest.mark.asyncio
async def test_stop_through_mc_is_not_a_death(async_session, fake_redis):
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(None)
    watcher = RuntimeWatcher(interval=90)
    with _stack(FakeBox(), fake_redis, probe):
        probe.served = "DeepSeek-V4.1"
        await watcher.tick(session=async_session)
        await runtime_grace.mark_stopped(rt.slug)
        probe.served = None
        for _ in range(UNREACHABLE_EVENT_THRESHOLD):
            await watcher.tick(session=async_session)

    assert await _events(async_session, "runtime.engine_died") == []
    assert len(await _events(async_session, "runtime.unreachable")) == 1  # unchanged behaviour
    live = json.loads(await fake_redis.get(RedisKeys.runtime_live(rt.slug)))
    assert live.get("status") != "crashed"


@pytest.mark.asyncio
async def test_stopping_the_recipe_keeps_the_slot_row_quiet_too(async_session, fake_redis):
    """Stop button on the recipe → the box's slot row goes dark as planned:
    no crash on the slot card, no task of a slot-bound agent handed back."""
    slot = await _rt(async_session, slug="box-slot", model="DeepSeek-V4.1", is_slot=True,
                     runtime_type="openai_compatible")
    dsv = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    board, agent = await _board_agent(async_session, slot)
    running = Task(board_id=board.id, title="running", status="in_progress",
                   assigned_agent_id=agent.id, dispatched_at=datetime.now(timezone.utc))
    async_session.add(running)
    await async_session.commit()
    probe = Probe(None)
    watcher = RuntimeWatcher(interval=90)
    with _stack(FakeBox(), fake_redis, probe):
        probe.served = "DeepSeek-V4.1"
        await watcher.tick(session=async_session)
        await runtime_grace.mark_stopped(dsv.slug)
        probe.served = None
        for _ in range(UNREACHABLE_EVENT_THRESHOLD):
            await watcher.tick(session=async_session)

    assert await _events(async_session, "runtime.engine_died") == []
    slot_live = json.loads(await fake_redis.get(RedisKeys.runtime_live(slot.slug)))
    assert slot_live.get("status") != "crashed"
    await async_session.refresh(running)
    assert running.status == "in_progress"


@pytest.mark.asyncio
async def test_eviction_by_a_start_is_not_a_death(async_session, fake_redis):
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(None)
    watcher = RuntimeWatcher(interval=90)
    with _stack(FakeBox(), fake_redis, probe):
        probe.served = "DeepSeek-V4.1"
        await watcher.tick(session=async_session)
        await runtime_grace.mark_evicted(rt.slug, by="glm")
        probe.served = None
        for _ in range(UNREACHABLE_EVENT_THRESHOLD):
            await watcher.tick(session=async_session)

    assert await _events(async_session, "runtime.engine_died") == []


@pytest.mark.asyncio
async def test_a_silent_but_running_engine_is_not_a_death(async_session, fake_redis):
    """Container still running, port silent (busy or stuck engine): no
    "crashed", no task handed back — only the old unreachable alarm."""
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    board, agent = await _board_agent(async_session, rt)
    running = Task(board_id=board.id, title="running", status="in_progress",
                   assigned_agent_id=agent.id, dispatched_at=datetime.now(timezone.utc))
    async_session.add(running)
    await async_session.commit()
    probe = Probe(None)
    with _stack(FakeBox(inspect="running|0|false|0001-01-01T00:00:00Z|x"), fake_redis, probe):
        await _serve_then_die(RuntimeWatcher(interval=90), async_session, probe, "DeepSeek-V4.1")
    assert await _events(async_session, "runtime.engine_died") == []
    assert len(await _events(async_session, "runtime.unreachable")) == 1
    live = json.loads(await fake_redis.get(RedisKeys.runtime_live(rt.slug)))
    assert live.get("status") != "crashed"
    await async_session.refresh(running)
    assert running.status == "in_progress"


@pytest.mark.asyncio
async def test_kill_switch_restores_old_behaviour(async_session, fake_redis):
    await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(None)
    with _stack(FakeBox(), fake_redis, probe), \
            patch("app.services.engine_death.settings.runtime_engine_death_enabled", False):
        await _serve_then_die(RuntimeWatcher(interval=90), async_session, probe, "DeepSeek-V4.1")
    assert await _events(async_session, "runtime.engine_died") == []
    assert len(await _events(async_session, "runtime.unreachable")) == 1


async def _board_agent(session, runtime: Runtime) -> tuple[Board, Agent]:
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}", auto_dispatch_enabled=False)
    session.add(board)
    await session.commit()
    agent = Agent(name=f"A-{uuid.uuid4().hex[:4]}", board_id=board.id, role="developer",
                  runtime_id=runtime.id, status="busy")
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return board, agent


@pytest.mark.asyncio
async def test_running_tasks_of_bound_agents_go_back_to_the_inbox(async_session, fake_redis):
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    other = await _rt(async_session, slug="other", model="X", process="x")
    other.endpoint = "http://192.0.2.11:8000/v1"  # another box: its work is not touched
    async_session.add(other)
    await async_session.commit()
    board, agent = await _board_agent(async_session, rt)
    _, bystander = await _board_agent(async_session, other)
    now = datetime.now(timezone.utc)
    running = Task(board_id=board.id, title="running", status="in_progress",
                   assigned_agent_id=agent.id, dispatched_at=now, ack_at=now,
                   dispatch_attempt_id=str(uuid.uuid4()))
    delivered = Task(board_id=board.id, title="delivered", status="inbox",
                     assigned_agent_id=agent.id, dispatched_at=now)
    finished = Task(board_id=board.id, title="finished", status="done", assigned_agent_id=agent.id)
    elsewhere = Task(board_id=board.id, title="elsewhere", status="in_progress",
                     assigned_agent_id=bystander.id, dispatched_at=now, ack_at=now)
    for t in (running, delivered, finished, elsewhere):
        async_session.add(t)
    await async_session.commit()
    agent.current_task_id = running.id
    async_session.add(agent)
    await async_session.commit()

    probe = Probe(None)
    with _stack(FakeBox(), fake_redis, probe):
        await _serve_then_die(RuntimeWatcher(interval=90), async_session, probe, "DeepSeek-V4.1")

    for t in (running, delivered, finished, elsewhere):
        await async_session.refresh(t)
    await async_session.refresh(agent)
    assert running.status == "inbox"
    assert running.dispatched_at is None and running.ack_at is None
    assert running.dispatch_attempt_id is None
    assert agent.current_task_id is None
    assert delivered.status == "inbox" and delivered.dispatched_at is None
    assert finished.status == "done"
    assert elsewhere.status == "in_progress"

    notes = (await async_session.exec(
        select(TaskComment).where(TaskComment.task_id == running.id)
    )).all()
    assert any("Modell ausgefallen" in c.content for c in notes)
    requeued = await _events(async_session, "task.requeued_runtime_down")
    assert {e.task_id for e in requeued} == {running.id, delivered.id}
    died = (await _events(async_session, "runtime.engine_died"))[0]
    assert set(died.detail["requeued_task_ids"]) == {str(running.id), str(delivered.id)}
    # The card says how many went back (crash doc read by the live snapshot).
    live = json.loads(await fake_redis.get(RedisKeys.runtime_live(rt.slug)))
    assert live["crash"]["requeued_tasks"] == 2


@pytest.mark.asyncio
async def test_requeue_switch_off_leaves_tasks_alone(async_session, fake_redis):
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    board, agent = await _board_agent(async_session, rt)
    running = Task(board_id=board.id, title="running", status="in_progress",
                   assigned_agent_id=agent.id, dispatched_at=datetime.now(timezone.utc))
    async_session.add(running)
    await async_session.commit()
    probe = Probe(None)
    with _stack(FakeBox(), fake_redis, probe), \
            patch("app.services.engine_death.settings.runtime_engine_death_requeue_enabled", False):
        await _serve_then_die(RuntimeWatcher(interval=90), async_session, probe, "DeepSeek-V4.1")
    await async_session.refresh(running)
    assert running.status == "in_progress"
    assert len(await _events(async_session, "runtime.engine_died")) == 1


# ── diagnose(): the reason read from the box ─────────────────────────────

def _row(runtime_type="ssh_process", process="dsv-tf", container=None):
    return Runtime(slug="r", display_name="r", runtime_type=runtime_type, endpoint=ENDPOINT,
                   process_name=process, container_name=container)


@pytest.mark.asyncio
@pytest.mark.parametrize("inspect,logs,expected", [
    ("exited|137|true|2026-10-18T17:20:17Z|x", "", engine_death.OOM),
    ("exited|137|false|2026-10-18T17:20:17Z|x", "memguard: MemAvailable under floor, killing", engine_death.MEMGUARD),
    ("exited|137|false|2026-10-18T17:20:17Z|x", "", engine_death.KILLED),
    ("exited|1|false|2026-10-18T17:20:17Z|x", "ValueError: KV cache too small", engine_death.EXITED_ERROR),
    ("exited|0|false|2026-10-18T17:20:17Z|x", "", engine_death.EXITED_CLEAN),
    ("running|0|false|0001-01-01T00:00:00Z|x", "", engine_death.HUNG),
])
async def test_diagnose_reads_the_reason_from_docker(inspect, logs, expected):
    box = FakeBox(inspect=inspect, logs=logs)
    with patch("app.services.runtime_manager._ssh_run", new=box.run):
        diag = await engine_death.diagnose(_row(), BOX)
    assert diag["reason_code"] == expected
    if expected == engine_death.EXITED_ERROR:
        assert "KV cache" in diag["log_line"]
    if expected == engine_death.HUNG:
        assert diag["finished_at"] is None


@pytest.mark.asyncio
async def test_diagnose_host_process_and_missing_container():
    gone = FakeBox(inspect=None, pgrep_running=False)
    with patch("app.services.runtime_manager._ssh_run", new=gone.run):
        assert (await engine_death.diagnose(_row(), BOX))["reason_code"] == engine_death.PROCESS_GONE
    docker_gone = FakeBox(inspect=None)
    with patch("app.services.runtime_manager._ssh_run", new=docker_gone.run):
        diag = await engine_death.diagnose(_row("vllm_docker", process=None, container="c"), BOX)
    assert diag["reason_code"] == engine_death.CONTAINER_GONE
    assert not any(c.startswith("pgrep") for c in docker_gone.commands)


@pytest.mark.asyncio
async def test_diagnose_without_anchor_or_ssh_is_unknown():
    assert (await engine_death.diagnose(_row(process=None), BOX))["reason_code"] == engine_death.UNKNOWN
    assert (await engine_death.diagnose(_row(), None))["reason_code"] == engine_death.UNKNOWN


# ── stop_runtime marks the stop as planned ───────────────────────────────

@pytest.mark.asyncio
async def test_stop_runtime_marks_stop_and_acknowledges_crash(fake_redis):
    from app.services import runtime_manager

    async def _get_redis():
        return fake_redis

    await fake_redis.set(runtime_grace.crash_key("lms-rt"), json.dumps({"reason_code": "killed"}))
    with patch("app.services.runtime_grace.get_redis", _get_redis), \
            patch("app.services.runtime_manager._ssh_run", new=AsyncMock(return_value=("", "", 0))):
        await runtime_manager.stop_runtime(
            {"id": "x", "slug": "lms-rt", "display_name": "lms", "runtime_type": "lmstudio",
             "lms_identifier": "m"},
            host=BOX,
        )
    assert await runtime_grace.was_stopped("lms-rt", fake_redis) is True
    assert await fake_redis.get(runtime_grace.crash_key("lms-rt")) is None


# ── Critic round 1 (19.10.): the live topology and its edge cases ─────────
#
# Live, every local agent is bound to the box's SLOT row; the recipe rows on
# the same port have no agents. Every recipe switch marks the previous recipe
# "evicted" for an hour. These tests pin that exact shape.

async def _live_topology(session):
    slot = await _rt(session, slug="box-slot", model="DeepSeek-V4.1", is_slot=True,
                     runtime_type="openai_compatible")
    glm = await _rt(session, slug="glm", model="GLM-5.3", process="glm-tf")
    dsv = await _rt(session, slug="dsv", model="DeepSeek-V4.1")
    board, agent = await _board_agent(session, slot)
    return slot, glm, dsv, board, agent


async def _running(session, board, agent, title="running") -> Task:
    now = datetime.now(timezone.utc)
    task = Task(board_id=board.id, title=title, status="in_progress",
                assigned_agent_id=agent.id, dispatched_at=now, ack_at=now)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_death_soon_after_a_recipe_switch_still_hands_slot_tasks_back(async_session, fake_redis):
    """Blocker: GLM → DeepSeek switch marks GLM evicted for 1 h. DeepSeek dies
    20 min later. The slot-bound agents' running work must go back, the event
    must say so, and there must be no second "unreachable" alarm."""
    slot, glm, dsv, board, agent = await _live_topology(async_session)
    task = await _running(async_session, board, agent)
    probe = Probe(None)
    with _stack(FakeBox(), fake_redis, probe):
        await runtime_grace.mark_evicted(glm.slug, by=dsv.slug)  # the earlier switch
        watcher = RuntimeWatcher(interval=90)
        await _serve_then_die(watcher, async_session, probe, "DeepSeek-V4.1")
        await watcher.tick(session=async_session)  # one more silent tick

    died = await _events(async_session, "runtime.engine_died")
    assert len(died) == 1
    assert died[0].detail["slug"] == dsv.slug
    assert died[0].detail["requeued_task_ids"] == [str(task.id)]
    assert "1 laufende Task(s) zurück" in died[0].title
    assert await _events(async_session, "runtime.unreachable") == []
    await async_session.refresh(task)
    assert task.status == "inbox"
    # The recipe card (the one the stage shows) and the slot line both say it.
    dsv_live = json.loads(await fake_redis.get(RedisKeys.runtime_live(dsv.slug)))
    assert dsv_live["crash"]["requeued_tasks"] == 1
    slot_live = json.loads(await fake_redis.get(RedisKeys.runtime_live(slot.slug)))
    assert slot_live["status"] == "crashed"
    assert slot_live["crash"]["requeued_tasks"] == 1


@pytest.mark.asyncio
async def test_a_lost_race_in_the_hand_back_keeps_the_alarm(async_session, fake_redis):
    """Major: one 409 (the agent PATCHed the card in the same instant) must not
    expire the watcher's objects and swallow the whole alarm."""
    from fastapi import HTTPException

    from app.services import task_state

    _, _, dsv, board, agent = await _live_topology(async_session)
    first = await _running(async_session, board, agent, "first")
    second = await _running(async_session, board, agent, "second")
    real = task_state.lock_and_set
    calls = {"n": 0}

    async def flaky(session, task_id, to, *, actor):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HTTPException(status_code=409, detail="raced")
        return await real(session, task_id, to, actor=actor)

    probe = Probe(None)
    with _stack(FakeBox(), fake_redis, probe), patch.object(task_state, "lock_and_set", flaky):
        await _serve_then_die(RuntimeWatcher(interval=90), async_session, probe, "DeepSeek-V4.1")

    died = await _events(async_session, "runtime.engine_died")
    assert len(died) == 1
    assert len(died[0].detail["requeued_task_ids"]) == 1
    statuses = set()
    for t in (first, second):
        await async_session.refresh(t)
        statuses.add(t.status)
    assert statuses == {"inbox", "in_progress"}
    await async_session.refresh(dsv)
    assert dsv.serving_since is None
    assert json.loads(await fake_redis.get(RedisKeys.runtime_live(dsv.slug)))["status"] == "crashed"


@pytest.mark.asyncio
async def test_an_unexpected_error_in_the_hand_back_keeps_the_alarm(async_session, fake_redis):
    """Any other error mid-hand-back rolls back — the alarm and the rest of the
    tick must survive it (no MissingGreenlet on expired objects)."""
    _, _, dsv, board, agent = await _live_topology(async_session)
    await _running(async_session, board, agent, "first")
    await _running(async_session, board, agent, "second")
    calls = {"n": 0}

    async def boom(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup")

    probe = Probe(None)
    with _stack(FakeBox(), fake_redis, probe), \
            patch("app.services.dispatch_attempt_audit.clear_dispatch_attempt_id", new=boom):
        await _serve_then_die(RuntimeWatcher(interval=90), async_session, probe, "DeepSeek-V4.1")

    died = await _events(async_session, "runtime.engine_died")
    assert len(died) == 1
    assert len(died[0].detail["requeued_task_ids"]) == 1
    await async_session.refresh(dsv)
    assert dsv.serving_since is None


@pytest.mark.asyncio
async def test_a_box_that_cannot_be_asked_reports_but_keeps_the_tasks(async_session, fake_redis):
    """Major: SSH timeout / network flap is no proof of a death. Report it
    (reason unknown), but do not pull running work."""
    _, _, dsv, board, agent = await _live_topology(async_session)
    task = await _running(async_session, board, agent)

    class Unreachable(FakeBox):
        async def run(self, command, *, host=None, timeout=None):
            raise TimeoutError("ssh timed out")

    probe = Probe(None)
    with _stack(Unreachable(), fake_redis, probe):
        await _serve_then_die(RuntimeWatcher(interval=90), async_session, probe, "DeepSeek-V4.1")

    died = await _events(async_session, "runtime.engine_died")
    assert len(died) == 1
    assert died[0].detail["reason_code"] == engine_death.UNKNOWN
    assert died[0].detail["requeued_task_ids"] == []
    assert "bleiben" in died[0].title
    await async_session.refresh(task)
    assert task.status == "in_progress"
    live = json.loads(await fake_redis.get(RedisKeys.runtime_live(dsv.slug)))
    assert live["crash"]["tasks_kept"] is True


@pytest.mark.asyncio
async def test_stop_on_the_crashed_recipe_also_clears_the_slot_crash(async_session, fake_redis):
    slot, _, dsv, _, _ = await _live_topology(async_session)
    probe = Probe(None)
    watcher = RuntimeWatcher(interval=90)
    with _stack(FakeBox(), fake_redis, probe):
        await _serve_then_die(watcher, async_session, probe, "DeepSeek-V4.1")
        assert await fake_redis.get(runtime_grace.crash_key(slot.slug)) is not None
        await runtime_grace.mark_stopped(dsv.slug)
        await watcher.tick(session=async_session)
    assert await fake_redis.get(runtime_grace.crash_key(slot.slug)) is None
    slot_live = json.loads(await fake_redis.get(RedisKeys.runtime_live(slot.slug)))
    assert slot_live.get("status") != "crashed"


@pytest.mark.asyncio
async def test_duo_head_death_says_the_worker_may_still_run(async_session, fake_redis):
    from app.models.runtime_host import RuntimeHost

    _, _, dsv, _, _ = await _live_topology(async_session)
    async_session.add(RuntimeHost(runtime_id=dsv.id, host_id=uuid.uuid4(), role="worker", node_rank=1))
    await async_session.commit()
    probe = Probe(None)
    with _stack(FakeBox(), fake_redis, probe):
        await _serve_then_die(RuntimeWatcher(interval=90), async_session, probe, "DeepSeek-V4.1")
    died = (await _events(async_session, "runtime.engine_died"))[0]
    assert "zweiten Box" in died.title and "Stoppen" in died.title
    live = json.loads(await fake_redis.get(RedisKeys.runtime_live(dsv.slug)))
    assert live["crash"]["multi_box"] is True


@pytest.mark.asyncio
async def test_autostart_on_promises_no_more_than_the_gate_allows(async_session, fake_redis):
    await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(None)
    with _stack(FakeBox(), fake_redis, probe), \
            patch("app.services.runtime_watcher.RuntimeWatcher._autostart_target",
                  new=AsyncMock(return_value=(object(), object()))):
        await _serve_then_die(RuntimeWatcher(interval=90), async_session, probe, "DeepSeek-V4.1")
    died = (await _events(async_session, "runtime.engine_died"))[0]
    assert "darf" in died.title and "versucht einen Neustart" not in died.title


@pytest.mark.asyncio
@pytest.mark.parametrize("inspect,logs,expected", [
    # A start banner mentioning memguard does not turn a Python crash into a memguard kill.
    ("exited|1|false|2026-10-18T17:20:17Z|x", "memguard: armed, floor 2.5 GiB\nValueError: shape mismatch",
     engine_death.EXITED_ERROR),
    ("exited|137|false|2026-10-18T17:20:17Z|x", "memguard: armed, floor 2.5 GiB", engine_death.KILLED),
    ("exited|137|false|2026-10-18T17:20:17Z|x",
     "memguard: MemAvailable 2551 MiB was under the 2.5 GiB floor: killed container", engine_death.MEMGUARD),
])
async def test_memguard_needs_a_kill_line_and_exit_137(inspect, logs, expected):
    box = FakeBox(inspect=inspect, logs=logs)
    with patch("app.services.runtime_manager._ssh_run", new=box.run):
        diag = await engine_death.diagnose(_row(), BOX)
    assert diag["reason_code"] == expected
