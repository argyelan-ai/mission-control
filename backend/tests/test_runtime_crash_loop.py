"""Crash-loop detection in the runtime watcher (PR 8, hardened by Task #24).

The gap, live on the Spark: the sparkinfer compose stack ships
``restart: unless-stopped``. When the engine could not allocate its KV cache it
exited, docker restarted it, it exited again — for hours. From MC's side that
looks EXACTLY like a slow cold load: the endpoint is down, and the switch-grace
marker deliberately suppresses the alarm. Docker knows the difference
(``RestartCount``), so the watcher asks it.

Task #24 (09.08.26): the ORIGINAL version of this feature let a log-pattern
match stop the container on its own, and read raw ``docker logs --tail 200``
without scoping it to the current boot. Both were live bugs — a container
whose first attempt failed and then loaded cleanly on retry was killed mid-
load (restarts 0→1, one log line was "enough"). This file's tests were
extended for the fix: the decision is delta-only now (log content is
reason-text, never a trigger), and logs are read scoped to the CURRENT boot
via ``--since {{.State.StartedAt}}``, so a resolved failure from an earlier
restart cannot be reported as today's cause.

The tests that matter are the pairs: a real crash loop is stopped and
reported, a legitimately long load is left completely alone, a log pattern
below the restart threshold changes nothing, and a restart from outside the
crash-loop window does not silently count towards today's delta.
"""
import asyncio
import json
from contextlib import ExitStack

from app.services.agent_runtime_switch import ProbedModel


def _probed(model_id):
    """served-model → ProbedModel, wie probe_runtime_model_info es liefert."""
    return ProbedModel(model_id=model_id, context_len=None)


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
    """A box whose `docker inspect` answers with a configurable RestartCount.

    ``stale_logs`` simulates the content of a PREVIOUS boot of the SAME
    container (the in-place-restart concatenation problem, Task #24): it is
    only visible in the reply when the command has no ``--since`` — exactly
    how real ``docker logs`` behaves. ``logs`` is the CURRENT boot's content
    and is always visible.
    """

    def __init__(self, *, restart_count=0, logs="", stale_logs="",
                 started_at="2026-08-08T00:12:41Z"):
        self.restart_count = restart_count
        self.logs = logs
        self.stale_logs = stale_logs
        self.started_at = started_at
        self.commands: list[str] = []
        self.restart_policy = "unless-stopped"
        self.running = True

    async def run(self, command, *, host=None, timeout=None):
        self.commands.append(command)
        if "docker inspect" in command:
            if not self.running and "RestartCount" in command:
                return ("", "No such container", 1)
            return (f"{self.restart_count} {self.started_at}", "", 0)
        if "docker logs" in command:
            if "--since" in command:
                return (self.logs, "", 0)
            return (self.stale_logs + self.logs, "", 0)
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
        # PR9 renamed the watcher's probe to probe_runtime_model_info (it now
        # carries the context window as well) — same served-model semantics.
        patch("app.services.runtime_watcher.probe_runtime_model_info",
              new=AsyncMock(return_value=_probed(served))),
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
async def test_a_pattern_match_below_the_restart_threshold_does_not_stop(async_session, fake_redis):
    """This is the exact live incident (09.08.26, Task #24): restarts 0→1, one
    failed first attempt, then a clean retry loading 92 GiB of weights. The
    OLD code let the log pattern alone stop it. The decision must be
    delta-only now — a single restart is normal, and logs must not even be
    read for it (they are read only once the delta already says "loop")."""
    rt = await _mk_runtime(async_session, slug="one-shot", container="mc-one-shot")
    docker = FakeDocker(restart_count=0, logs=VLLM_OOM_LOG)
    watcher = RuntimeWatcher(interval=90)

    with _watcher_stack(docker, fake_redis):
        await mark_switching(rt.slug, "loading", "manual_start")
        await watcher.tick(session=async_session)   # baseline 0
        docker.restart_count = 1                    # one restart, below threshold
        await watcher.tick(session=async_session)

    assert not docker.ran("docker stop")
    assert not docker.ran("docker logs")  # never even read below threshold
    assert await _events(async_session, "runtime.crash_loop_stopped") == []


@pytest.mark.asyncio
async def test_since_scopes_the_reason_to_the_current_boot(async_session, fake_redis):
    """A real, threshold-crossing loop (Docker's RestartCount does not lie) —
    but the container was restarted IN PLACE, so its cumulative log carries a
    stale, already-irrelevant error from a much earlier, unrelated boot
    (before `.State.StartedAt` of the run being inspected now). Without
    `--since` that stale line would be reported as "the" reason for today's
    stop, which is misleading for whoever reads the event. With `--since` the
    reason reflects only the current boot (falls back to the generic restart
    count, since the current boot's own log has nothing alarming yet)."""
    rt = await _mk_runtime(async_session, slug="restarted-in-place", container="mc-rip")
    docker = FakeDocker(
        restart_count=0,
        logs="INFO 08-09 loading weights 12%",  # current boot: nothing alarming yet
        stale_logs=VLLM_OOM_LOG + "\n",           # an old, resolved failure
    )
    watcher = RuntimeWatcher(interval=90)

    with _watcher_stack(docker, fake_redis):
        await mark_switching(rt.slug, "loading", "manual_start")
        await watcher.tick(session=async_session)   # baseline 0
        docker.restart_count = CRASH_LOOP_RESTART_THRESHOLD
        await watcher.tick(session=async_session)

    # The delta alone made this a real loop — it IS stopped, correctly.
    assert docker.ran("docker stop mc-rip")
    events = await _events(async_session, "runtime.crash_loop_stopped")
    assert len(events) == 1
    # But the reason must not be the stale, unrelated error from before
    # StartedAt — --since kept it out of the read entirely.
    assert events[0].detail["log_pattern"] is None
    assert "KV cache" not in events[0].detail["reason"]
    assert events[0].detail["reason"] == f"{CRASH_LOOP_RESTART_THRESHOLD} Neustarts im Startfenster"
    # Command-string proof that --since was actually used, keyed off the
    # StartedAt this check itself read via `docker inspect`.
    assert docker.ran(f"--since {docker.started_at}")


@pytest.mark.asyncio
async def test_restarts_outside_the_crash_loop_window_do_not_accumulate(async_session, fake_redis):
    """A restart baseline that has aged out of CRASH_LOOP_WINDOW_SECONDS must
    reset rather than keep contributing to today's delta — otherwise a box
    that restarted once, weeks ago, for an unrelated and long-resolved
    reason would need only ONE more restart today to look like a loop."""
    rt = await _mk_runtime(async_session, slug="old-restart", container="mc-old-restart")
    docker = FakeDocker(restart_count=1)
    watcher = RuntimeWatcher(interval=90)

    with _watcher_stack(
        docker, fake_redis,
        extra=[patch("app.services.runtime_watcher.CRASH_LOOP_WINDOW_SECONDS", 1)],
    ):
        await mark_switching(rt.slug, "loading", "manual_start")
        await watcher.tick(session=async_session)  # baseline=1, TTL=1s

        await asyncio.sleep(1.2)  # let the window expire

        # If the old baseline (1) still applied, this delta would already be
        # at threshold. Because the window expired, a fresh baseline (4) is
        # captured on this tick instead, so the delta is 0.
        docker.restart_count = 1 + CRASH_LOOP_RESTART_THRESHOLD
        await watcher.tick(session=async_session)

    assert not docker.ran("docker stop")
    assert await _events(async_session, "runtime.crash_loop_stopped") == []


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
