"""Crash-loop detection in the runtime watcher (PR 8).

The gap, live on the Spark: the sparkinfer compose stack ships
``restart: unless-stopped``. When the engine could not allocate its KV cache it
exited, docker restarted it, it exited again — for hours. From MC's side that
looks EXACTLY like a slow cold load: the endpoint is down, and the switch-grace
marker deliberately suppresses the alarm. Docker knows the difference
(``RestartCount``), so the watcher asks it.

The two tests that matter are the pair: a real crash loop is stopped and
reported, and a legitimately long load — the thing this could most easily
break — is left completely alone.
"""
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.models.activity import ActivityEvent
from app.models.runtime import Runtime
from app.redis_client import RedisKeys
from app.services import sse as sse_mod
from app.services.host_resolver import ResolvedHost
from app.services.runtime_grace import mark_switching
from app.services.runtime_watcher import CRASH_LOOP_RESTART_THRESHOLD, RuntimeWatcher

SPARK = ResolvedHost(ssh_host="192.0.2.10", ssh_user="mc", kind="ssh", source="registry")

VLLM_OOM_LOG = """
INFO 08-08 00:11:02 [core.py:71] Initializing a V1 LLM engine
INFO 08-08 00:12:40 [gpu_worker.py:298] Available KV cache memory: -3.21 GiB
ERROR 08-08 00:12:41 [core.py:588] EngineCore failed to start.
ValueError: To serve at least one request with the model's max seq len (262144), (12.4 GiB KV cache is needed, which is larger than the available KV cache memory
ERROR 08-08 00:12:41 [core_client.py:512] Engine core initialization failed. See root cause above.
""".strip()


class FakeDocker:
    """A box whose `docker inspect` answers with a configurable RestartCount."""

    def __init__(self, *, restart_count=0, logs=""):
        self.restart_count = restart_count
        self.logs = logs
        self.commands: list[str] = []
        self.restart_policy = "unless-stopped"
        self.running = True

    async def run(self, command, *, host=None, timeout=None):
        self.commands.append(command)
        if "docker inspect" in command:
            if not self.running and "RestartCount" in command:
                return ("", "No such container", 1)
            return (f"{self.restart_count} 2026-08-08T00:12:41Z", "", 0)
        if "docker logs" in command:
            return (self.logs, "", 0)
        if command.startswith("docker update --restart=no"):
            self.restart_policy = "no"
            return ("", "", 0)
        if command.startswith("docker stop"):
            self.running = False
            return ("", "", 0)
        return ("", "", 0)

    def ran(self, needle: str) -> bool:
        return any(needle in c for c in self.commands)


async def _mk_runtime(session, *, slug="ds4-sparkinfer", container="mc-ds4-sparkinfer"):
    rt = Runtime(
        slug=slug, display_name=slug, runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1", model_identifier="deepseek-v4-flash",
        container_name=container, enabled=True, exclusive_memory=True,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


def _watcher_stack(docker, fake_redis, *, served=None, extra=()):
    """Everything the watcher reaches outside its own module, in one stack.

    ``runtime_grace.get_redis`` is in here for a reason worth remembering: it is
    the ONE client the watcher does not pass explicitly, so leaving it on the
    module singleton put the grace marker in a different Redis than the probe
    read it from — and every "in grace" test silently exercised the no-grace
    path instead.
    """
    async def _get_redis():
        return fake_redis

    stack = ExitStack()
    for ctx in (
        patch("app.services.runtime_watcher.probe_runtime_model",
              new=AsyncMock(return_value=served)),
        patch("app.services.runtime_watcher.get_redis", _get_redis),
        patch("app.services.runtime_grace.get_redis", _get_redis),
        patch.object(sse_mod, "get_redis", _get_redis),
        patch("app.services.runtime_watcher.resolve_host_for_runtime",
              new=AsyncMock(return_value=SPARK)),
        patch("app.services.runtime_manager._ssh_run", new=docker.run),
        # Auto-recovery would otherwise try to start the engine in the same tick.
        patch("app.services.runtime_watcher.RuntimeWatcher._maybe_auto_recover",
              new=AsyncMock(return_value=None)),
        patch("app.services.runtime_watcher.host_memory_prep.finish_for_host",
              new=AsyncMock(return_value=False)),
        patch("app.services.runtime_watcher.host_memory_prep.recover_orphaned_preps",
              new=AsyncMock(return_value=[])),
        *extra,
    ):
        stack.enter_context(ctx)
    return stack


async def _events(session, event_type: str) -> list[ActivityEvent]:
    result = await session.exec(
        select(ActivityEvent).where(ActivityEvent.event_type == event_type)
    )
    return list(result.all())


@pytest.mark.asyncio
async def test_three_restarts_in_the_grace_window_stop_the_loop(async_session, fake_redis):
    """The live failure: restarting forever behind a grace marker."""
    rt = await _mk_runtime(async_session)
    docker = FakeDocker(restart_count=0, logs=VLLM_OOM_LOG)
    watcher = RuntimeWatcher(interval=90)

    with _watcher_stack(docker, fake_redis):
        await mark_switching(rt.slug, "loading", "manual_start")
        # Tick 1 records the baseline while the container is merely loading.
        await watcher.tick(session=async_session)
        assert docker.restart_policy == "unless-stopped"

        docker.restart_count = CRASH_LOOP_RESTART_THRESHOLD
        await watcher.tick(session=async_session)

    # The loop is BROKEN, not just reported: leaving it spinning would burn an
    # engine boot every few seconds until somebody reads the feed.
    assert docker.restart_policy == "no"
    assert docker.ran("docker stop mc-ds4-sparkinfer")

    events = await _events(async_session, "runtime.crash_loop_stopped")
    assert len(events) == 1
    assert events[0].severity == "warning"  # → operator notification
    # The reason is the line an operator can act on, not "container unhealthy".
    assert "KV cache" in events[0].detail["reason"]
    assert events[0].detail["restarts_observed"] == CRASH_LOOP_RESTART_THRESHOLD

    live = json.loads(await fake_redis.get(RedisKeys.runtime_live(rt.slug)))
    assert live["status"] == "failed"
    assert live["container_stopped"] is True

    # Grace is cleared too — the runtime is not "switching" any more, it failed.
    assert await fake_redis.get(RedisKeys.runtime_switching(rt.slug)) is None


@pytest.mark.asyncio
async def test_a_long_cold_load_is_never_touched(async_session, fake_redis):
    """The regression this feature could most easily cause.

    A 107 GB first start spends 10+ minutes with the endpoint down and the
    container quietly running. Zero restarts, so: no logs read, no stop, no
    event, and the grace marker survives.
    """
    rt = await _mk_runtime(async_session, slug="slow-loader", container="mc-slow-loader")
    docker = FakeDocker(restart_count=0, logs=VLLM_OOM_LOG)
    watcher = RuntimeWatcher(interval=90)

    with _watcher_stack(docker, fake_redis):
        await mark_switching(rt.slug, "loading", "manual_start")
        for _ in range(6):
            await watcher.tick(session=async_session)

    assert docker.restart_policy == "unless-stopped"
    assert docker.running is True
    assert not docker.ran("docker stop")
    # Not even the logs are read — a healthy load must not pay for 200 log
    # lines on every tick.
    assert not docker.ran("docker logs")
    assert await _events(async_session, "runtime.crash_loop_stopped") == []
    assert await fake_redis.get(RedisKeys.runtime_switching(rt.slug)) is not None

    live = json.loads(await fake_redis.get(RedisKeys.runtime_live(rt.slug)))
    assert live["status"] == "switching"


@pytest.mark.asyncio
async def test_the_log_pattern_alone_is_enough(async_session, fake_redis):
    """A stack whose restart policy somebody already set to `no` crashes ONCE
    and stays down. RestartCount barely moves, but the engine said in plain
    text that it could not initialise — that is a failure, not a load."""
    rt = await _mk_runtime(async_session, slug="one-shot", container="mc-one-shot")
    docker = FakeDocker(restart_count=0, logs=VLLM_OOM_LOG)
    watcher = RuntimeWatcher(interval=90)

    with _watcher_stack(docker, fake_redis):
        await mark_switching(rt.slug, "loading", "manual_start")
        await watcher.tick(session=async_session)   # baseline 0
        docker.restart_count = 1                    # one restart → look at logs
        await watcher.tick(session=async_session)

    events = await _events(async_session, "runtime.crash_loop_stopped")
    assert len(events) == 1
    assert events[0].detail["log_pattern"] == "Engine core initialization failed"


@pytest.mark.asyncio
async def test_a_restarting_container_with_clean_logs_is_left_alone(async_session, fake_redis):
    """Sabotage: one restart and nothing alarming in the log. A single restart
    has benign causes (a manual `docker restart`, a host reboot) — killing on
    it would make the watcher the outage."""
    rt = await _mk_runtime(async_session, slug="blip", container="mc-blip")
    docker = FakeDocker(restart_count=0, logs="INFO 08-08 loading weights 42%")
    watcher = RuntimeWatcher(interval=90)

    with _watcher_stack(docker, fake_redis):
        await mark_switching(rt.slug, "loading", "manual_start")
        await watcher.tick(session=async_session)
        docker.restart_count = 1
        await watcher.tick(session=async_session)

    assert not docker.ran("docker stop")
    assert await _events(async_session, "runtime.crash_loop_stopped") == []


@pytest.mark.asyncio
async def test_a_missing_container_is_not_a_crash_loop(async_session, fake_redis):
    """`docker inspect` failing means the container is GONE — that is the
    auto-recovery case (start it again), not this one (stop it)."""
    rt = await _mk_runtime(async_session, slug="gone", container="mc-gone")
    docker = FakeDocker(restart_count=0)
    docker.running = False
    watcher = RuntimeWatcher(interval=90)

    with _watcher_stack(docker, fake_redis):
        await watcher.tick(session=async_session)

    assert not docker.ran("docker stop")
    assert await _events(async_session, "runtime.crash_loop_stopped") == []
    assert await fake_redis.get(RedisKeys.runtime_restart_baseline(rt.slug)) is None


@pytest.mark.asyncio
async def test_a_runtime_without_a_container_name_is_skipped(async_session, fake_redis):
    """Recipe-switched runtimes have container_name = NULL until the new
    container appears. Nothing to inspect, and no guessing."""
    await _mk_runtime(async_session, slug="nameless", container=None)
    docker = FakeDocker(restart_count=99, logs=VLLM_OOM_LOG)
    watcher = RuntimeWatcher(interval=90)

    with _watcher_stack(docker, fake_redis):
        await watcher.tick(session=async_session)

    assert not docker.ran("docker inspect")
    assert await _events(async_session, "runtime.crash_loop_stopped") == []


@pytest.mark.asyncio
async def test_a_serving_engine_ends_the_memory_prep(async_session, fake_redis):
    """The probe that first sees the engine answering is the honest end of a
    start — so that is where the cache dropper goes and the watermark returns."""
    rt = await _mk_runtime(async_session, slug="served", container="mc-served")
    docker = FakeDocker()
    watcher = RuntimeWatcher(interval=90)

    finish = AsyncMock(return_value=True)
    with _watcher_stack(
        docker, fake_redis,
        served="deepseek-v4-flash",
        extra=[patch(
            "app.services.runtime_watcher.host_memory_prep.finish_for_host", new=finish
        )],
    ):
        await mark_switching(rt.slug, "loading", "manual_start")
        await watcher.tick(session=async_session)

    finish.assert_awaited_once()
    assert finish.await_args.kwargs["success"] is True
    assert await fake_redis.get(RedisKeys.runtime_switching(rt.slug)) is None


@pytest.mark.asyncio
async def test_the_tick_sweeps_orphaned_memory_preps(async_session, fake_redis):
    await _mk_runtime(async_session, slug="sweeper", container="mc-sweeper")
    docker = FakeDocker()
    watcher = RuntimeWatcher(interval=90)
    sweep = AsyncMock(return_value=[])

    with _watcher_stack(
        docker, fake_redis,
        extra=[patch(
            "app.services.runtime_watcher.host_memory_prep.recover_orphaned_preps", new=sweep
        )],
    ):
        await watcher.tick(session=async_session)

    sweep.assert_awaited_once()
