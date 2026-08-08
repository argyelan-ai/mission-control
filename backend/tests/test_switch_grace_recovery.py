"""PR5 — switch grace + watcher auto-recovery.

Two operational problems, one shared Redis marker:
  (a) planned downtime (recipe switch, cold load) must not look like an outage,
  (b) a box that came back without its container must be started ONCE, with a
      cooldown and a hard stop after two failed attempts.
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.activity import ActivityEvent
from app.models.runtime import Runtime
from app.redis_client import RedisKeys
from app.services import runtime_grace, sparkrun_manager
from app.services.agent_runtime_switch import ProbedModel
from app.services.host_resolver import ResolvedHost
from app.services.runtime_watcher import (
    AUTO_RECOVERY_COOLDOWN,
    AUTO_RECOVERY_MAX_ATTEMPTS,
    UNREACHABLE_EVENT_THRESHOLD,
    RuntimeWatcher,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis
    return _get


async def _mk_runtime(session: AsyncSession, **overrides) -> Runtime:
    fields = dict(
        slug="grace-rt",
        display_name="Grace RT",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="some-model",
        launch_command="uvx sparkrun run @official/x --solo",
        enabled=True,
    )
    fields.update(overrides)
    rt = Runtime(**fields)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _event_types(session: AsyncSession) -> list[str]:
    result = await session.exec(select(ActivityEvent))
    return [e.event_type for e in result.all()]


async def _events(session: AsyncSession) -> list[ActivityEvent]:
    result = await session.exec(select(ActivityEvent))
    return list(result.all())


@pytest.fixture
def grace_redis(fake_redis):
    """Route runtime_grace at the same fake Redis the watcher tests inspect."""
    with patch(
        "app.services.runtime_grace.get_redis", _fake_get_redis(fake_redis)
    ):
        yield fake_redis


SSH_HOST = ResolvedHost(ssh_host="box.internal", ssh_user="op", kind="ssh")


# ── keys + settings ──────────────────────────────────────────────────────


def test_new_redis_keys_and_kill_switch_exist():
    from app.config import settings

    assert RedisKeys.runtime_switching("x") == "mc:runtime-switching:x"
    assert RedisKeys.runtime_recovery_cooldown("x") == "mc:runtime-recovery:cooldown:x"
    assert RedisKeys.runtime_recovery_failures("x") == "mc:runtime-recovery:failures:x"
    assert settings.runtime_auto_recovery_enabled is True
    assert AUTO_RECOVERY_COOLDOWN == 900
    assert AUTO_RECOVERY_MAX_ATTEMPTS == 2


@pytest.mark.asyncio
async def test_marker_carries_phase_source_and_ttl(grace_redis):
    await runtime_grace.mark_switching("rt-a", runtime_grace.PHASE_EVICTING, "switch_recipe")

    doc = json.loads(await grace_redis.get(RedisKeys.runtime_switching("rt-a")))
    assert doc["phase"] == "evicting"
    assert doc["source"] == "switch_recipe"
    assert doc["started_at"]
    ttl = await grace_redis.ttl(RedisKeys.runtime_switching("rt-a"))
    assert 0 < ttl <= runtime_grace.SWITCHING_TTL


@pytest.mark.asyncio
async def test_grace_helpers_survive_redis_down():
    """Redis unavailable → behave exactly as before PR5: no grace, no crash."""
    boom = AsyncMock(side_effect=RuntimeError("redis down"))
    with patch("app.services.runtime_grace.get_redis", boom):
        await runtime_grace.mark_switching("rt-a", "loading", "manual_start")
        await runtime_grace.clear_switching("rt-a")
        assert await runtime_grace.get_switching("rt-a") is None


# ── (a) watcher respects grace ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_grace_suppresses_failure_count_and_unreachable_event(
    async_session: AsyncSession, fake_redis, grace_redis
):
    rt = await _mk_runtime(async_session, slug="switching-rt")
    await runtime_grace.mark_switching(rt.slug, "loading", "switch_recipe")
    watcher = RuntimeWatcher(interval=90)

    with (
        patch("app.services.runtime_watcher.probe_runtime_model_info",
              new=AsyncMock(return_value=ProbedModel(None, None))),
        patch("app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)),
    ):
        for _ in range(UNREACHABLE_EVENT_THRESHOLD + 2):
            await watcher.tick(session=async_session)

    assert await fake_redis.get(f"{RedisKeys.runtime_live(rt.slug)}:fails") is None
    assert "runtime.unreachable" not in await _event_types(async_session)
    snapshot = json.loads(await fake_redis.get(RedisKeys.runtime_live(rt.slug)))
    assert snapshot["status"] == "switching"
    assert snapshot["phase"] == "loading"
    assert snapshot["reachable"] is False


@pytest.mark.asyncio
async def test_probe_success_during_grace_clears_the_marker(
    async_session: AsyncSession, fake_redis, grace_redis
):
    rt = await _mk_runtime(async_session, slug="done-rt", model_identifier="some-model")
    await runtime_grace.mark_switching(rt.slug, "loading", "switch_recipe")
    watcher = RuntimeWatcher(interval=90)

    with (
        patch("app.services.runtime_watcher.probe_runtime_model_info",
              new=AsyncMock(return_value=ProbedModel("some-model", None))),
        patch("app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)),
    ):
        await watcher.tick(session=async_session)

    assert await runtime_grace.get_switching(rt.slug) is None
    snapshot = json.loads(await fake_redis.get(RedisKeys.runtime_live(rt.slug)))
    assert snapshot["reachable"] is True
    assert "status" not in snapshot


@pytest.mark.asyncio
async def test_expired_marker_restores_normal_alerting(
    async_session: AsyncSession, fake_redis, grace_redis
):
    """TTL is the safety net: once the key is gone (backend died mid-switch)
    the watcher must count failures and alert exactly as before."""
    rt = await _mk_runtime(async_session, slug="ttl-rt")
    await runtime_grace.mark_switching(rt.slug, "loading", "switch_recipe")
    await fake_redis.delete(RedisKeys.runtime_switching(rt.slug))  # simulate expiry
    watcher = RuntimeWatcher(interval=90)

    with (
        patch("app.services.runtime_watcher.probe_runtime_model_info",
              new=AsyncMock(return_value=ProbedModel(None, None))),
        patch("app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)),
        patch.object(RuntimeWatcher, "_maybe_auto_recover", new=AsyncMock()),
    ):
        for _ in range(UNREACHABLE_EVENT_THRESHOLD):
            await watcher.tick(session=async_session)

    assert "runtime.unreachable" in await _event_types(async_session)


# ── (a) switch_recipe sets/clears the marker on every path ───────────────


@pytest.fixture
async def switch_runtime(async_session: AsyncSession) -> Runtime:
    return await _mk_runtime(
        async_session,
        slug="switch-rt",
        launch_command=(
            "uvx sparkrun run @official/old-recipe --solo --no-rm --ensure "
            "--no-follow --label mc.runtime.slug=switch-rt"
        ),
        container_name="sparkrun_old_solo",
    )


@pytest.mark.asyncio
async def test_switch_recipe_marks_evicting_before_the_evict(
    async_session: AsyncSession, switch_runtime: Runtime, grace_redis
):
    """The marker must exist BEFORE the model is killed — the window between
    eviction and marking is exactly where the false alarms came from."""
    seen: dict = {}

    async def _evict(*args, **kwargs):
        seen["at_evict"] = await runtime_grace.get_switching(switch_runtime.slug)
        return {"ok": True, "message": "evicted", "stopped": []}

    with (
        patch("app.services.runtime_manager.evict_spark_runtime_containers", _evict),
        patch("app.services.runtime_manager.start_runtime",
              AsyncMock(return_value={"ok": True, "message": "starting"})),
        patch("app.services.agent_runtime_switch.probe_runtime_model",
              AsyncMock(return_value=None)),
        patch("app.services.runtime_model_resolver.get_redis",
              _fake_get_redis(grace_redis)),
    ):
        result = await sparkrun_manager.switch_recipe(
            async_session, switch_runtime, "@official/new-recipe"
        )

    assert result["ok"] is True
    assert seen["at_evict"]["phase"] == runtime_grace.PHASE_EVICTING
    assert seen["at_evict"]["source"] == runtime_grace.SOURCE_SWITCH
    # Still in flight after a successful switch — only a serving probe ends it.
    after = await runtime_grace.get_switching(switch_runtime.slug)
    assert after["phase"] == runtime_grace.PHASE_LOADING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "abort_path",
    ["same_recipe", "multi_node", "evict_failed", "start_failed"],
)
async def test_switch_recipe_leaves_no_marker_on_any_abort_path(
    async_session: AsyncSession, switch_runtime: Runtime, grace_redis, abort_path
):
    """Every early return must leave the runtime un-graced — a stuck marker
    would blind the watcher for the full 20-minute TTL."""
    target = "@official/new-recipe"
    patches = [
        patch("app.services.agent_runtime_switch.probe_runtime_model",
              AsyncMock(return_value=None)),
        patch("app.services.runtime_model_resolver.get_redis",
              _fake_get_redis(grace_redis)),
    ]
    if abort_path == "same_recipe":
        target = "@official/old-recipe"
    elif abort_path == "multi_node":
        patches += [
            patch("app.services.sparkrun_manager.get_host_gpu_count",
                  AsyncMock(return_value=1)),
            patch("app.services.sparkrun_manager.list_recipes",
                  AsyncMock(return_value=[{"name": target, "model": "m",
                                           "registry": "official", "tp": 4,
                                           "nodes": 2, "solo_capable": False}])),
        ]
    elif abort_path == "evict_failed":
        patches.append(patch(
            "app.services.runtime_manager.evict_spark_runtime_containers",
            AsyncMock(return_value={"ok": False, "message": "still running"}),
        ))
    else:  # start_failed
        patches += [
            patch("app.services.runtime_manager.evict_spark_runtime_containers",
                  AsyncMock(return_value={"ok": True, "message": "evicted",
                                          "stopped": []})),
            patch("app.services.runtime_manager.start_runtime",
                  AsyncMock(return_value={"ok": False, "message": "no container"})),
        ]

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = await sparkrun_manager.switch_recipe(
            async_session, switch_runtime, target
        )

    if abort_path == "same_recipe":
        assert result["ok"] is True and "no-op" in result["message"].lower()
    else:
        assert result["ok"] is False
    assert await runtime_grace.get_switching(switch_runtime.slug) is None


@pytest.mark.asyncio
async def test_failed_manual_start_clears_its_own_marker(grace_redis):
    """start_runtime marks the runtime in-flight; a start that reports failure
    must undo that itself, or a broken manual start blinds the watcher."""
    from app.services import runtime_manager

    rt = {"id": "x", "slug": "manual-rt", "display_name": "M",
          "runtime_type": "vllm_docker", "endpoint": "http://192.0.2.10:8000/v1",
          "container_name": None, "launch_command": None}
    result = await runtime_manager.start_runtime(rt)

    assert result["ok"] is False  # no container, no launch_command
    assert await runtime_grace.get_switching("manual-rt") is None


@pytest.mark.asyncio
async def test_successful_manual_start_marks_launching(grace_redis):
    from app.services import runtime_manager

    rt = {"id": "x", "slug": "manual-ok-rt", "display_name": "M",
          "runtime_type": "vllm_docker", "endpoint": "http://192.0.2.10:8000/v1",
          "container_name": "c1", "launch_command": None}
    with patch("app.services.runtime_manager._ssh_run",
               AsyncMock(return_value=("running", "", 0))):
        result = await runtime_manager.start_runtime(rt)

    assert result["ok"] is True
    doc = await runtime_grace.get_switching("manual-ok-rt")
    assert doc["phase"] == runtime_grace.PHASE_LAUNCHING
    assert doc["source"] == runtime_grace.SOURCE_MANUAL


# ── (b) auto-recovery ────────────────────────────────────────────────────


async def _run_until_recovery(
    session: AsyncSession, fake_redis, *, start_result: dict, ticks: int = 3,
    host: ResolvedHost | None = SSH_HOST, ssh_ok: bool = True,
) -> AsyncMock:
    """Drive the watcher through `ticks` unreachable probes with the recovery
    dependencies mocked. Returns the start_runtime mock."""
    start_mock = AsyncMock(return_value=start_result)
    ssh = AsyncMock(return_value=("", "", 0 if ssh_ok else 1))
    watcher = RuntimeWatcher(interval=90)
    with (
        patch("app.services.runtime_watcher.probe_runtime_model_info",
              new=AsyncMock(return_value=ProbedModel(None, None))),
        patch("app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)),
        patch("app.services.runtime_watcher.resolve_host_for_runtime",
              new=AsyncMock(return_value=host)),
        patch("app.services.runtime_manager._ssh_run", ssh),
        patch("app.services.runtime_manager.start_runtime", start_mock),
    ):
        for _ in range(ticks):
            await watcher.tick(session=session)
    return start_mock


@pytest.mark.asyncio
async def test_auto_recovery_starts_the_engine_and_emits_events(
    async_session: AsyncSession, fake_redis, grace_redis
):
    rt = await _mk_runtime(async_session, slug="crashed-rt")

    start_mock = await _run_until_recovery(
        async_session, fake_redis, start_result={"ok": True, "message": "starting"}
    )

    start_mock.assert_awaited_once()
    kwargs = start_mock.await_args.kwargs
    assert kwargs["host"] is SSH_HOST
    assert kwargs["grace_source"] == runtime_grace.SOURCE_AUTO_RECOVERY
    assert start_mock.await_args.args[0]["slug"] == rt.slug

    types = await _event_types(async_session)
    assert "runtime.auto_recovery_started" in types
    assert "runtime.auto_recovery_succeeded" in types
    assert "runtime.auto_recovery_failed" not in types
    severities = {e.event_type: e.severity for e in await _events(async_session)}
    assert severities["runtime.auto_recovery_started"] == "info"
    assert severities["runtime.auto_recovery_succeeded"] == "info"


@pytest.mark.asyncio
async def test_auto_recovery_needs_the_confirmed_outage_threshold(
    async_session: AsyncSession, fake_redis, grace_redis
):
    """A single blip must never restart an engine."""
    await _mk_runtime(async_session, slug="blip-rt")

    start_mock = await _run_until_recovery(
        async_session, fake_redis,
        start_result={"ok": True, "message": "starting"},
        ticks=UNREACHABLE_EVENT_THRESHOLD - 1,
    )
    start_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_blocks_a_second_attempt(
    async_session: AsyncSession, fake_redis, grace_redis
):
    await _mk_runtime(async_session, slug="cooldown-rt")

    start_mock = await _run_until_recovery(
        async_session, fake_redis,
        start_result={"ok": False, "message": "boom"},
        ticks=UNREACHABLE_EVENT_THRESHOLD + 4,
    )

    start_mock.assert_awaited_once()
    ttl = await fake_redis.ttl(RedisKeys.runtime_recovery_cooldown("cooldown-rt"))
    assert 0 < ttl <= AUTO_RECOVERY_COOLDOWN


@pytest.mark.asyncio
async def test_gives_up_after_two_failed_attempts(
    async_session: AsyncSession, fake_redis, grace_redis
):
    """Mark's rule: after 2 failures, stop and hand over to the operator."""
    await _mk_runtime(async_session, slug="giveup-rt")

    for _ in range(AUTO_RECOVERY_MAX_ATTEMPTS + 2):
        await fake_redis.delete(RedisKeys.runtime_recovery_cooldown("giveup-rt"))
        await _run_until_recovery(
            async_session, fake_redis,
            start_result={"ok": False, "message": "boom"},
            ticks=UNREACHABLE_EVENT_THRESHOLD,
        )

    events = await _events(async_session)
    failed = [e for e in events if e.event_type == "runtime.auto_recovery_failed"]
    given_up = [e for e in events if e.event_type == "runtime.auto_recovery_given_up"]
    assert len(failed) == AUTO_RECOVERY_MAX_ATTEMPTS  # no third attempt
    assert len(given_up) == 1
    assert failed[0].severity == "warning"
    assert given_up[0].severity == "warning"


@pytest.mark.asyncio
async def test_engine_returning_on_its_own_resets_the_give_up_counter(
    async_session: AsyncSession, fake_redis, grace_redis
):
    rt = await _mk_runtime(async_session, slug="reset-rt", model_identifier="m")
    await fake_redis.set(RedisKeys.runtime_recovery_failures(rt.slug), "2")
    watcher = RuntimeWatcher(interval=90)

    with (
        patch("app.services.runtime_watcher.probe_runtime_model_info",
              new=AsyncMock(return_value=ProbedModel("m", None))),
        patch("app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)),
    ):
        await watcher.tick(session=async_session)

    assert await fake_redis.get(RedisKeys.runtime_recovery_failures(rt.slug)) is None


@pytest.mark.asyncio
async def test_kill_switch_disables_recovery_but_not_grace(
    async_session: AsyncSession, fake_redis, grace_redis
):
    from app.config import settings

    rt = await _mk_runtime(async_session, slug="killswitch-rt")
    await runtime_grace.mark_switching(rt.slug, "loading", "switch_recipe")

    with patch.object(settings, "runtime_auto_recovery_enabled", False):
        start_mock = await _run_until_recovery(
            async_session, fake_redis,
            start_result={"ok": True, "message": "starting"},
            ticks=UNREACHABLE_EVENT_THRESHOLD + 1,
        )
        start_mock.assert_not_awaited()
        # Grace is untouched by the kill switch.
        snapshot = json.loads(await fake_redis.get(RedisKeys.runtime_live(rt.slug)))
        assert snapshot["status"] == "switching"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["switching", "disabled", "not_docker", "host_not_ssh", "box_unreachable"],
)
async def test_no_recovery_when_preconditions_are_not_met(
    async_session: AsyncSession, fake_redis, grace_redis, case
):
    overrides: dict = {"slug": f"pre-{case}-rt"}
    host: ResolvedHost | None = SSH_HOST
    ssh_ok = True
    if case == "disabled":
        overrides["enabled"] = False
    elif case == "not_docker":
        overrides["runtime_type"] = "openai_compatible"
    elif case == "host_not_ssh":
        host = ResolvedHost(kind="flask_wol", control_url="http://192.0.2.20:5555")
    elif case == "box_unreachable":
        ssh_ok = False

    rt = await _mk_runtime(async_session, **overrides)
    if case == "switching":
        await runtime_grace.mark_switching(rt.slug, "loading", "switch_recipe")

    start_mock = await _run_until_recovery(
        async_session, fake_redis,
        start_result={"ok": True, "message": "starting"},
        ticks=UNREACHABLE_EVENT_THRESHOLD + 1,
        host=host, ssh_ok=ssh_ok,
    )
    start_mock.assert_not_awaited()
    assert "runtime.auto_recovery_started" not in await _event_types(async_session)


@pytest.mark.asyncio
async def test_watcher_survives_redis_without_the_grace_keys(
    async_session: AsyncSession, fake_redis
):
    """runtime_grace failing (Redis down) must degrade to pre-PR5 behaviour,
    not raise inside the tick."""
    await _mk_runtime(async_session, slug="degraded-rt")
    watcher = RuntimeWatcher(interval=90)

    with (
        patch("app.services.runtime_watcher.probe_runtime_model_info",
              new=AsyncMock(return_value=ProbedModel(None, None))),
        patch("app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)),
        patch("app.services.runtime_grace.get_redis",
              AsyncMock(side_effect=RuntimeError("redis down"))),
        patch("app.services.runtime_watcher.resolve_host_for_runtime",
              new=AsyncMock(return_value=None)),
    ):
        for _ in range(UNREACHABLE_EVENT_THRESHOLD):
            await watcher.tick(session=async_session)

    assert "runtime.unreachable" in await _event_types(async_session)
