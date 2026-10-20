"""Engine death — ADR-096 §11 (runtime_watcher + engine_death + runtime_grace).

The live case (19.10.2026): a recipe's memory guard killed the DeepSeek engine
(container Exited 137). ``runtime.unreachable`` came 3 min 53 s later, without
a cause, three times — once per row sharing the Spark's ``:8000``.

Contract pinned here:
- §11.2 speed: connection refused + own anchor exited → death at THAT probe;
  any other failure + anchor exited → at the 2nd probe; anchor running or not
  askable → no death, the threshold of 3 stays.
- §11.1 planned vs. unplanned: a durable stop intent without TTL; a manual
  stop is never a death and never undone by auto-recovery; only the
  runtime's own start lifts it.
- §11.3 explain: existing readers, existing live fields (status "failed" +
  reason) plus reason_code / box / at.
- §11.4 one notification per death: runtime.unreachable with the cause, for
  the dead row only.
- §11.5 work stays: tasks in progress keep their status, get one comment.
"""
from __future__ import annotations

import asyncio
import json
import socket
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlmodel import select

from app.models.activity import ActivityEvent
from app.models.agent import Agent
from app.models.board import Board
from app.models.host import Host
from app.models.runtime import Runtime
from app.models.task import Task, TaskComment
from app.redis_client import RedisKeys
from app.services import engine_death, runtime_grace, runtime_manager
from app.services import runtime_watcher as rw
from app.services import sse as sse_mod
from app.services.agent_runtime_switch import (
    FAIL_ERROR,
    FAIL_REFUSED,
    FAIL_TIMEOUT,
    ProbedModel,
    classify_probe_error,
    probe_runtime_model_info,
)
from app.services.host_resolver import ResolvedHost
from app.services.runtime_watcher import UNREACHABLE_EVENT_THRESHOLD, RuntimeWatcher

BOX = ResolvedHost(ssh_host="192.0.2.10", ssh_user="mc", kind="ssh", source="registry",
                   display_name="Box A")
ENDPOINT = "http://192.0.2.10:8000/v1"
INTERVAL = 90
EXITED_137 = "exited|137|false|2026-10-19T18:44:25Z|2026-10-19T09:00:00Z"
RUNNING = "running|0|false|0001-01-01T00:00:00Z|2026-10-19T09:00:00Z"


class FakeBox:
    """``docker inspect`` / ``docker logs`` / handle check of one box."""

    def __init__(self, *, inspect: str | None = EXITED_137, logs: str = "",
                 pgrep_running: bool = False, ssh_fails: bool = False, hang: float = 0.0):
        self.inspect = inspect
        self.logs = logs
        self.pgrep_running = pgrep_running
        self.ssh_fails = ssh_fails
        self.hang = hang
        self.commands: list[str] = []

    async def run(self, command, *, host=None, timeout=None):
        self.commands.append(command)
        if self.hang:
            await asyncio.sleep(self.hang)
        if self.ssh_fails:
            raise OSError("ssh: connect to host: Connection timed out")
        if command.startswith('docker inspect --format "{{.State.Status}}'):
            if self.inspect is None:
                return ("", "Error: No such object: x", 1)
            return (self.inspect, "", 0)
        if command.startswith("docker logs"):
            return (self.logs, "", 0)
        if command.startswith("pgrep -x"):
            return ("", "", 0 if self.pgrep_running else 1)
        return ("", "", 0)

    @property
    def inspects(self) -> int:
        return sum(1 for c in self.commands if c.startswith('docker inspect --format "{{.State.Status}}'))


class Probe:
    """What the port answers this tick: a model id, or None + failure kind."""

    def __init__(self, served=None, failure=FAIL_REFUSED):
        self.served = served
        self.failure = failure

    async def __call__(self, runtime):
        if self.served is not None:
            return ProbedModel(self.served, None)
        return ProbedModel(None, None, self.failure)


def _stack(box: FakeBox, fake_redis, probe: Probe, *, recover=None):
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
        patch("app.services.runtime_watcher.RuntimeWatcher._maybe_auto_recover",
              new=recover or AsyncMock(return_value=None)),
        patch("app.services.runtime_watcher.host_memory_prep.finish_for_host", new=AsyncMock(return_value=False)),
        patch("app.services.runtime_watcher.host_memory_prep.recover_orphaned_preps", new=AsyncMock(return_value=[])),
        patch("app.services.runtime_watcher.sync_pending_agents", new=AsyncMock(return_value=None)),
        patch("app.services.runtime_watcher.recreate_pending_agents", new=AsyncMock(return_value=None)),
        patch("app.services.runtime_watcher.mark_agents_for_sync", new=AsyncMock(return_value=None)),
    ):
        stack.enter_context(ctx)
    return stack


async def _rt(session, *, slug, model, is_slot=False, runtime_type="ssh_process",
              process="dsv-tf", container=None, endpoint=ENDPOINT) -> Runtime:
    rt = Runtime(
        slug=slug, display_name=slug, runtime_type=runtime_type, endpoint=endpoint,
        model_identifier=model, enabled=True, is_slot=is_slot,
        process_name=None if is_slot else process, container_name=container,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _events(session, event_type: str) -> list[ActivityEvent]:
    return list((await session.exec(
        select(ActivityEvent).where(ActivityEvent.event_type == event_type)
    )).all())


async def _deaths(session) -> list[ActivityEvent]:
    return [e for e in await _events(session, "runtime.unreachable")
            if (e.detail or {}).get("engine_death")]


async def _live(fake_redis, slug) -> dict:
    return json.loads(await fake_redis.get(RedisKeys.runtime_live(slug)))


class FakeClock:
    """Virtual time for the watcher's schedule: one tick every INTERVAL s.
    The engine dies right after a healthy probe — the worst case for any
    probe-based detector (a whole interval passes before anyone looks)."""

    def __init__(self):
        self.now = 0.0
        self.died_at: float | None = None
        self.decided_at: float | None = None

    async def tick(self, watcher, session):
        self.now += INTERVAL
        await watcher.tick(session=session)


async def _measure(session, probe: Probe, *, model, max_ticks=5) -> FakeClock:
    """Serve once, die, then tick until a death is declared (or max_ticks)."""
    clock = FakeClock()
    watcher = RuntimeWatcher(interval=INTERVAL)
    probe.served = model
    await watcher.tick(session=session)          # t=0: healthy, own answer
    clock.died_at = clock.now                    # dies right after it
    probe.served = None
    for _ in range(max_ticks):
        await clock.tick(watcher, session)
        if await _deaths(session):
            clock.decided_at = clock.now
            break
    return clock


# ── §11.2 speed ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refused_with_exited_anchor_is_a_death_at_the_first_probe(async_session, fake_redis):
    """Before (#895 as opened, and main): decision at probe 3 = 270 s after the
    death in this schedule (live 3 min 53 s). Now: probe 1 = 90 s."""
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    box = FakeBox(logs="INFO serving\nKilled\n")
    probe = Probe(failure=FAIL_REFUSED)
    with _stack(box, fake_redis, probe):
        clock = await _measure(async_session, probe, model="DeepSeek-V4.1")

    assert clock.decided_at is not None
    latency = clock.decided_at - clock.died_at
    assert latency == 1 * INTERVAL                     # ≤ 2 intervals (ADR target)
    assert latency < UNREACHABLE_EVENT_THRESHOLD * INTERVAL
    deaths = await _deaths(async_session)
    assert len(deaths) == 1
    d = deaths[0]
    assert d.severity == "error"
    assert d.detail["reason_code"] == engine_death.EXIT
    assert d.detail["exit_code"] == 137
    assert d.detail["consecutive_failures"] == 1
    assert d.detail["failure"] == FAIL_REFUSED
    assert d.detail["box"] == "Box A"
    assert d.title.startswith("dsv gestoppt auf Box A: Engine hart beendet (Exit 137")
    assert d.detail["reason_en"].startswith("dsv stopped on Box A: engine killed (exit 137")
    assert d.detail["log_line"] == "Killed"            # built-in marker, existing reader
    # Explanation used the crash-loop reader, scoped to the current boot.
    assert any(c.startswith("docker logs --since 2026-10-19T09:00:00Z --tail 200 dsv-tf")
               for c in box.commands)

    live = await _live(fake_redis, rt.slug)
    assert live["status"] == "failed"                  # existing status value
    assert live["reason"] == d.title                   # existing reason field
    assert live["reason_code"] == engine_death.EXIT
    assert live["at"] == "2026-10-19T18:44:25Z"        # Docker's own finish time
    assert live["box"] == "Box A"
    await async_session.refresh(rt)
    assert rt.serving_since is None


@pytest.mark.asyncio
async def test_timeout_with_exited_anchor_is_a_death_at_the_second_probe(async_session, fake_redis):
    await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    box = FakeBox()
    probe = Probe(failure=FAIL_TIMEOUT)
    with _stack(box, fake_redis, probe):
        clock = await _measure(async_session, probe, model="DeepSeek-V4.1")
    assert clock.decided_at - clock.died_at == 2 * INTERVAL
    assert (await _deaths(async_session))[0].detail["consecutive_failures"] == 2
    # The first timeout did not even ask the box (a stalling box looks like that).
    assert box.inspects == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [FAIL_REFUSED, FAIL_TIMEOUT])
async def test_anchor_still_running_is_no_death_and_keeps_the_threshold(async_session, fake_redis, failure):
    await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(failure=failure)
    with _stack(FakeBox(inspect=RUNNING), fake_redis, probe):
        clock = await _measure(async_session, probe, model="DeepSeek-V4.1", max_ticks=4)
    assert clock.decided_at is None
    plain = await _events(async_session, "runtime.unreachable")
    assert len(plain) == 1 and not plain[0].detail.get("engine_death")
    assert plain[0].detail["failure"] == failure


@pytest.mark.asyncio
async def test_box_not_askable_is_no_death(async_session, fake_redis):
    await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(failure=FAIL_REFUSED)
    with _stack(FakeBox(ssh_fails=True), fake_redis, probe):
        clock = await _measure(async_session, probe, model="DeepSeek-V4.1", max_ticks=4)
    assert clock.decided_at is None
    assert len(await _events(async_session, "runtime.unreachable")) == 1  # plain, at 3


@pytest.mark.asyncio
async def test_the_decision_is_bounded_under_30_seconds(async_session, fake_redis):
    """A box that hangs on SSH cannot hold the tick: anchor check and log read
    each have a hard bound, together under 30 s from the failed probe."""
    assert rw._ANCHOR_CHECK_TIMEOUT + rw._EXPLAIN_TIMEOUT < 30
    assert rw._ANCHOR_SSH_TIMEOUT < rw._ANCHOR_CHECK_TIMEOUT
    await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(failure=FAIL_REFUSED)
    loop = asyncio.get_running_loop()
    with _stack(FakeBox(hang=5), fake_redis, probe), \
            patch.object(rw, "_ANCHOR_CHECK_TIMEOUT", 0.2):
        watcher = RuntimeWatcher(interval=INTERVAL)
        probe.served = "DeepSeek-V4.1"
        await watcher.tick(session=async_session)
        probe.served = None
        started = loop.time()
        await watcher.tick(session=async_session)
        elapsed = loop.time() - started
    assert elapsed < 1.5
    assert await _deaths(async_session) == []


@pytest.mark.asyncio
async def test_host_process_gone_is_a_death_with_reason_exit(async_session, fake_redis):
    await _rt(async_session, slug="dsv", model="DeepSeek-V4.1", process="dsv-engine")
    probe = Probe(failure=FAIL_REFUSED)
    with _stack(FakeBox(inspect=None, pgrep_running=False), fake_redis, probe):
        clock = await _measure(async_session, probe, model="DeepSeek-V4.1")
    assert clock.decided_at - clock.died_at == INTERVAL
    d = (await _deaths(async_session))[0]
    assert d.detail["reason_code"] == engine_death.EXIT
    assert d.detail["exit_code"] is None
    assert "Engine-Prozess läuft nicht mehr" in d.title


@pytest.mark.asyncio
async def test_oom_killed_reads_as_oom(async_session, fake_redis):
    await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(failure=FAIL_REFUSED)
    box = FakeBox(inspect="exited|137|true|2026-10-19T18:44:25Z|2026-10-19T09:00:00Z")
    with _stack(box, fake_redis, probe):
        await _measure(async_session, probe, model="DeepSeek-V4.1")
    assert (await _deaths(async_session))[0].detail["reason_code"] == engine_death.OOM


@pytest.mark.asyncio
async def test_death_status_holds_and_ends_with_the_next_answer(async_session, fake_redis):
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(failure=FAIL_REFUSED)
    watcher = RuntimeWatcher(interval=INTERVAL)
    with _stack(FakeBox(), fake_redis, probe):
        await _measure(async_session, probe, model="DeepSeek-V4.1")
        for _ in range(4):
            await watcher.tick(session=async_session)
        assert (await _live(fake_redis, rt.slug))["status"] == "failed"
        assert len(await _deaths(async_session)) == 1
        assert await _events(async_session, "runtime.unreachable") == await _deaths(async_session)
        probe.served = "DeepSeek-V4.1"
        await watcher.tick(session=async_session)
    live = await _live(fake_redis, rt.slug)
    assert live.get("status") != "failed" and "reason_code" not in live
    assert await engine_death.get_death(fake_redis, rt.slug) is None


@pytest.mark.asyncio
async def test_kill_switch_restores_the_plain_alarm(async_session, fake_redis):
    await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(failure=FAIL_REFUSED)
    with _stack(FakeBox(), fake_redis, probe), \
            patch("app.services.runtime_watcher.settings.runtime_engine_death_enabled", False):
        clock = await _measure(async_session, probe, model="DeepSeek-V4.1", max_ticks=3)
    assert clock.decided_at is None
    assert len(await _events(async_session, "runtime.unreachable")) == 1


# ── §11.2 one engine death = one death (shared :8000) ────────────────────

@pytest.mark.asyncio
async def test_shared_port_one_death_one_notification_no_autostart_of_glm(async_session, fake_redis):
    """ADR-096 PR 3 test (a): slot + DeepSeek + GLM on one port; DeepSeek dies
    while GLM's eviction marker has long expired → exactly one death, one
    notification, for DeepSeek; GLM gets no alarm and no autostart."""
    host = Host(slug="box-a", display_name="Box A", kind="ssh", ssh_host="192.0.2.10",
                autostart_enabled=True, autostart_recipe_slug="recipe-glm")
    async_session.add(host)
    await async_session.commit()
    slot = await _rt(async_session, slug="box-slot", model="DeepSeek-V4.1", is_slot=True,
                     runtime_type="openai_compatible")
    dsv = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    glm = await _rt(async_session, slug="glm", model="GLM-5.3", process="glm-tf")
    for r in (slot, dsv, glm):
        r.host_id = host.id
        async_session.add(r)
    await async_session.commit()
    # GLM was switched away when DeepSeek started — the 1 h marker is gone,
    # the durable intent is not.
    async def _get_redis():
        return fake_redis
    with patch("app.services.runtime_grace.get_redis", _get_redis):
        await runtime_grace.mark_stop_intent(glm.slug, runtime_grace.INTENT_SWITCHED_AWAY, by=dsv.slug)
    assert await runtime_grace.get_evicted(glm.slug, fake_redis) is None

    start = AsyncMock(return_value={"ok": True, "message": "x"})
    probe = Probe(failure=FAIL_REFUSED)
    own = {dsv.slug: True, glm.slug: False}

    async def _own(self, session, runtime):
        return own.get(runtime.slug, True)

    with _stack(FakeBox(), fake_redis, probe, recover=None), \
            patch.object(RuntimeWatcher, "_maybe_auto_recover", RuntimeWatcher._maybe_auto_recover), \
            patch.object(RuntimeWatcher, "_served_answer_is_own", _own), \
            patch.object(RuntimeWatcher, "_autostart_start", start), \
            patch.object(RuntimeWatcher, "_autostart_target",
                         AsyncMock(return_value=(host, object()))), \
            patch.object(RuntimeWatcher, "_record_autostart_attempt", AsyncMock()), \
            patch.object(RuntimeWatcher, "_host_answers", AsyncMock(return_value=True)), \
            patch.object(RuntimeWatcher, "_active_exclusive_sibling", AsyncMock(return_value=None)):
        await _measure(async_session, probe, model="DeepSeek-V4.1", max_ticks=1)
        watcher = RuntimeWatcher(interval=INTERVAL)
        for _ in range(UNREACHABLE_EVENT_THRESHOLD + 1):
            await watcher.tick(session=async_session)

    alarms = await _events(async_session, "runtime.unreachable")
    assert len(alarms) == 1
    assert alarms[0].detail["engine_death"] is True
    assert alarms[0].detail["slug"] == dsv.slug
    started = [c.args[1].slug for c in start.await_args_list]
    assert glm.slug not in started                     # GLM never autostarted
    glm_live = await _live(fake_redis, glm.slug)
    assert glm_live.get("status") != "failed"


@pytest.mark.asyncio
async def test_a_row_that_never_ran_raises_nothing_even_without_any_marker(async_session, fake_redis):
    await _rt(async_session, slug="box-slot", model="DeepSeek-V4.1", is_slot=True,
              runtime_type="openai_compatible")
    dsv = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    await _rt(async_session, slug="glm", model="GLM-5.3", process="glm-tf")
    own = {"glm": False}

    async def _own(self, session, runtime):
        return own.get(runtime.slug, True)

    probe = Probe(failure=FAIL_TIMEOUT)
    with _stack(FakeBox(inspect=RUNNING), fake_redis, probe), \
            patch.object(RuntimeWatcher, "_served_answer_is_own", _own):
        await _measure(async_session, probe, model="DeepSeek-V4.1", max_ticks=4)
    # No death (engine still running), so ONE plain alarm for the endpoint —
    # not three, and never from the GLM row.
    alarms = await _events(async_session, "runtime.unreachable")
    assert len(alarms) == 1
    assert alarms[0].detail["slug"] != "glm"
    assert alarms[0].detail["slug"] in {dsv.slug, "box-slot"}


# ── §11.1 planned vs. unplanned ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_manual_stop_is_never_a_death_and_never_expires(async_session, fake_redis):
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(failure=FAIL_REFUSED)
    watcher = RuntimeWatcher(interval=INTERVAL)
    with _stack(FakeBox(), fake_redis, probe), \
            patch.object(runtime_manager, "stop_ssh_process", AsyncMock(return_value={"ok": True, "message": "s"})):
        probe.served = "DeepSeek-V4.1"
        await watcher.tick(session=async_session)
        await runtime_manager.stop_runtime(rt.to_registry_dict(), host=BOX)
        probe.served = None
        for _ in range(UNREACHABLE_EVENT_THRESHOLD + 3):
            await watcher.tick(session=async_session)
        # A neighbour answering on the shared port does not lift it either.
        probe.served = "GLM-5.3"
        await watcher.tick(session=async_session)
        probe.served = None
        for _ in range(UNREACHABLE_EVENT_THRESHOLD + 1):
            await watcher.tick(session=async_session)

    assert await _events(async_session, "runtime.unreachable") == []
    assert (await runtime_grace.get_stop_intent(rt.slug, fake_redis))["why"] == runtime_grace.INTENT_OPERATOR_STOP
    assert await fake_redis.ttl(RedisKeys.runtime_stop_intent(rt.slug)) == -1   # no TTL


@pytest.mark.asyncio
async def test_stop_intent_is_set_before_the_stop_itself(fake_redis):
    seen = {}

    async def _stop(runtime, *, host=None):
        seen["intent"] = await runtime_grace.get_stop_intent("dsv", fake_redis)
        return {"ok": True, "message": "s"}

    async def _get_redis():
        return fake_redis
    with patch("app.services.runtime_grace.get_redis", _get_redis), \
            patch.object(runtime_manager, "stop_ssh_process", _stop):
        await runtime_manager.stop_runtime(
            {"slug": "dsv", "runtime_type": "ssh_process", "display_name": "dsv",
             "process_name": "dsv-tf"}, host=BOX,
        )
    assert seen["intent"]["why"] == runtime_grace.INTENT_OPERATOR_STOP


@pytest.mark.asyncio
async def test_auto_recovery_never_overrides_a_stop_intent(async_session, fake_redis):
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    async def _get_redis():
        return fake_redis
    with patch("app.services.runtime_grace.get_redis", _get_redis):
        await runtime_grace.mark_stop_intent(rt.slug, runtime_grace.INTENT_OPERATOR_STOP)
    target = AsyncMock(return_value=(object(), object()))
    with patch("app.services.runtime_watcher.settings.runtime_auto_recovery_enabled", True), \
            patch.object(RuntimeWatcher, "_autostart_target", target):
        await RuntimeWatcher(interval=INTERVAL)._maybe_auto_recover(
            async_session, fake_redis, rt, UNREACHABLE_EVENT_THRESHOLD + 5,
        )
    target.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_the_runtimes_own_start_lifts_the_intent(fake_redis):
    async def _get_redis():
        return fake_redis
    with patch("app.services.runtime_grace.get_redis", _get_redis), \
            patch.object(runtime_manager, "already_running",
                         AsyncMock(return_value={"ok": True, "message": "läuft schon"})):
        await runtime_grace.mark_stop_intent("dsv", runtime_grace.INTENT_OPERATOR_STOP)
        await runtime_grace.mark_stop_intent("glm", runtime_grace.INTENT_SWITCHED_AWAY, by="dsv")
        # A start of ANOTHER runtime does not touch it.
        await runtime_manager.start_runtime(
            {"slug": "glm", "runtime_type": "vllm_docker", "container_name": "glm"}, host=BOX)
        assert await runtime_grace.get_stop_intent("dsv", fake_redis) is not None
        assert await runtime_grace.get_stop_intent("glm", fake_redis) is None
        # Auto-recovery is not "the operator chose it again".
        await runtime_manager.start_runtime(
            {"slug": "dsv", "runtime_type": "vllm_docker", "container_name": "dsv"}, host=BOX,
            grace_source=runtime_grace.SOURCE_AUTO_RECOVERY)
        assert await runtime_grace.get_stop_intent("dsv", fake_redis) is not None
        await runtime_manager.start_runtime(
            {"slug": "dsv", "runtime_type": "vllm_docker", "container_name": "dsv"}, host=BOX)
        assert await runtime_grace.get_stop_intent("dsv", fake_redis) is None


@pytest.mark.asyncio
async def test_eviction_marks_switched_away_before_it_stops(session, fake_redis):
    from tests.test_recipe_switcher_p3 import _host, _runtime

    box = await _host(session, "box-a")
    await _runtime(session, "tf-box-a", box, container_name="tf-engine")
    new = await _runtime(session, "vllm-box-a", box, container_name="vllm-head")
    seen = {}

    async def fake_evict(slug, **_kw):
        seen["intent"] = await runtime_grace.get_stop_intent(slug, fake_redis)
        return {"ok": True}

    with (
        patch("app.services.runtime_grace.get_redis", AsyncMock(return_value=fake_redis)),
        patch.object(runtime_manager, "evict_spark_runtime_containers", fake_evict),
        patch.object(runtime_manager, "get_runtime_state", AsyncMock(return_value={"state": "ready"})),
        patch("app.services.heads.box_guard.switch_lock", AsyncMock(return_value=(None, []))),
    ):
        await runtime_manager.ensure_exclusive_host(new.model_dump(), session=session)
    assert seen["intent"]["why"] == runtime_grace.INTENT_SWITCHED_AWAY
    assert seen["intent"]["by"] == "vllm-box-a"
    assert await fake_redis.ttl(RedisKeys.runtime_stop_intent("tf-box-a")) == -1


@pytest.mark.asyncio
async def test_a_dead_row_with_an_expired_eviction_but_durable_intent_is_no_death(async_session, fake_redis):
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(failure=FAIL_REFUSED)
    watcher = RuntimeWatcher(interval=INTERVAL)
    with _stack(FakeBox(), fake_redis, probe):
        probe.served = "DeepSeek-V4.1"
        await watcher.tick(session=async_session)
        await runtime_grace.mark_stop_intent(rt.slug, runtime_grace.INTENT_SWITCHED_AWAY, by="glm")
        probe.served = None
        for _ in range(UNREACHABLE_EVENT_THRESHOLD + 1):
            await watcher.tick(session=async_session)
    assert await _events(async_session, "runtime.unreachable") == []


# ── crash-loop stop is reported once (test (c)) ──────────────────────────

@pytest.mark.asyncio
async def test_crash_loop_stop_is_reported_once_and_never_again_as_death(async_session, fake_redis):
    rt = await _rt(async_session, slug="vllm", model="org/m", runtime_type="vllm_docker",
                   process=None, container="vllm-c")
    restarts = {"n": 0, "looping": True}

    class LoopBox(FakeBox):
        async def run(self, command, *, host=None, timeout=None):
            if command.startswith('docker inspect --format "{{.RestartCount}}'):
                if restarts["looping"]:
                    restarts["n"] += 3
                return (f"{restarts['n']} 2026-10-19T09:00:00Z", "", 0)
            if command.startswith('docker inspect --format "{{.State.Status}}'):
                # Docker restarts it on its own: "restarting", not a death.
                # After MC's `docker stop` it is plainly exited.
                state = "restarting|1" if restarts["looping"] else "exited|137"
                return (f"{state}|false|2026-10-19T18:44:25Z|2026-10-19T09:00:00Z", "", 0)
            return await super().run(command, host=host, timeout=timeout)

    probe = Probe(failure=FAIL_REFUSED)
    watcher = RuntimeWatcher(interval=INTERVAL)
    with _stack(LoopBox(logs="ValueError: KV cache too small"), fake_redis, probe):
        probe.served = "org/m"
        await watcher.tick(session=async_session)
        probe.served = None
        await watcher.tick(session=async_session)     # baseline
        await watcher.tick(session=async_session)     # delta 3 → loop stopped
        assert len(await _events(async_session, "runtime.crash_loop_stopped")) == 1
        restarts["looping"] = False                   # loop broken: no more restarts
        for _ in range(UNREACHABLE_EVENT_THRESHOLD + 2):
            await watcher.tick(session=async_session)

    assert len(await _events(async_session, "runtime.crash_loop_stopped")) == 1
    assert await _events(async_session, "runtime.unreachable") == []
    live = await _live(fake_redis, rt.slug)
    assert live["status"] == "failed" and live["reason_code"] == engine_death.CRASH_LOOP


# ── §11.5 work stays, one comment ────────────────────────────────────────

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
async def test_running_work_stays_and_gets_one_comment_with_the_reason(async_session, fake_redis):
    slot = await _rt(async_session, slug="box-slot", model="DeepSeek-V4.1", is_slot=True,
                     runtime_type="openai_compatible")
    await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    other = await _rt(async_session, slug="far", model="x", endpoint="http://192.0.2.99:8000/v1")
    board, agent = await _board_agent(async_session, slot)      # bound to the SLOT row
    _, far_agent = await _board_agent(async_session, other)
    now = datetime.now(timezone.utc)
    running = Task(board_id=board.id, title="running", status="in_progress",
                   assigned_agent_id=agent.id, dispatched_at=now, ack_at=now)
    queued = Task(board_id=board.id, title="queued", status="inbox",
                  assigned_agent_id=agent.id, dispatched_at=now)
    elsewhere = Task(board_id=board.id, title="far", status="in_progress",
                     assigned_agent_id=far_agent.id, dispatched_at=now)
    async_session.add_all([running, queued, elsewhere])
    await async_session.commit()
    agent.current_task_id = running.id
    async_session.add(agent)
    await async_session.commit()

    probe = Probe(failure=FAIL_REFUSED)
    watcher = RuntimeWatcher(interval=INTERVAL)
    with _stack(FakeBox(), fake_redis, probe):
        await _measure(async_session, probe, model="DeepSeek-V4.1")
        for _ in range(3):
            await watcher.tick(session=async_session)

    for t in (running, queued, elsewhere):
        await async_session.refresh(t)
    await async_session.refresh(agent)
    assert running.status == "in_progress"                # not moved
    assert running.dispatched_at is not None and running.ack_at is not None
    assert agent.current_task_id == running.id
    assert queued.status == "inbox" and queued.dispatched_at is not None
    assert elsewhere.status == "in_progress"

    notes = list((await async_session.exec(
        select(TaskComment).where(TaskComment.task_id == running.id)
    )).all())
    assert len(notes) == 1                                 # once per death
    assert notes[0].comment_type == "system"
    assert notes[0].content.startswith("Modell ausgefallen: dsv gestoppt auf Box A")
    assert "bleibt in Arbeit" in notes[0].content
    for t in (queued, elsewhere):
        assert (await async_session.exec(
            select(TaskComment).where(TaskComment.task_id == t.id)
        )).all() == []
    d = (await _deaths(async_session))[0]
    assert d.detail["commented_task_ids"] == [str(running.id)]
    assert await _events(async_session, "task.requeued_runtime_down") == []


# ── stop acknowledges a death on the card ────────────────────────────────

@pytest.mark.asyncio
async def test_stop_of_a_dead_runtime_clears_its_card_reason(async_session, fake_redis):
    rt = await _rt(async_session, slug="dsv", model="DeepSeek-V4.1")
    probe = Probe(failure=FAIL_REFUSED)
    with _stack(FakeBox(), fake_redis, probe), \
            patch.object(runtime_manager, "stop_ssh_process", AsyncMock(return_value={"ok": True, "message": "s"})):
        await _measure(async_session, probe, model="DeepSeek-V4.1")
        assert await engine_death.get_death(fake_redis, rt.slug) is not None
        await runtime_manager.stop_runtime(rt.to_registry_dict(), host=BOX)
        assert await engine_death.get_death(fake_redis, rt.slug) is None
        await RuntimeWatcher(interval=INTERVAL).tick(session=async_session)
    assert (await _live(fake_redis, rt.slug)).get("status") != "failed"


# ── probe failure kinds ──────────────────────────────────────────────────

def test_classify_probe_error():
    req = httpx.Request("GET", "http://x/v1/models")
    refused = httpx.ConnectError("All connection attempts failed", request=req)
    refused.__cause__ = ConnectionRefusedError(111, "Connection refused")
    assert classify_probe_error(refused) == FAIL_REFUSED
    assert classify_probe_error(httpx.ConnectTimeout("timed out", request=req)) == FAIL_TIMEOUT
    assert classify_probe_error(httpx.ReadTimeout("timed out", request=req)) == FAIL_TIMEOUT
    dns = httpx.ConnectError("[Errno -2] Name or service not known", request=req)
    assert classify_probe_error(dns) == FAIL_ERROR


@pytest.mark.asyncio
async def test_a_closed_local_port_probes_as_refused():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    rt = Runtime(slug="p", display_name="p", runtime_type="vllm_docker",
                 endpoint=f"http://127.0.0.1:{port}/v1")
    probed = await probe_runtime_model_info(rt)
    assert probed.model_id is None
    assert probed.failure == FAIL_REFUSED


def test_reason_sentences_name_box_and_time():
    with patch("app.services.engine_death.settings.usage_timezone", "Europe/Zurich"):
        de, en = engine_death.sentences(
            engine_death.EXIT, display="DeepSeek", box="Box A",
            at="2026-10-19T18:44:25Z", exit_code=137, marker=None,
        )
    assert de == "DeepSeek gestoppt auf Box A: Engine hart beendet (Exit 137, SIGKILL — z. B. ein Speicherwächter) um 20:44"
    assert en == "DeepSeek stopped on Box A: engine killed (exit 137, SIGKILL — e.g. a memory guard) at 20:44"
    assert set(engine_death.REASON_CODES) == {
        "guard", "oom", "kernel_oom", "exit", "member_down", "crash_loop", "unknown",
    }
