"""Context-window drift (PR9).

"Engine leads, MC follows" for the WINDOW, not just the model id.

The live failure this replays (08.08.): the Spark was switched to an engine
serving ``deepseek-v4-flash-0731-spark`` with ``max_model_len: 262144``. Model
drift detection worked — ``runtime.model_identifier`` followed. But
``max_context_len`` / ``preferred_context_len`` stayed at the 98304 of a
previous profile, and routers/internal.py:122 renders exactly those fields as
omp's ``OMP_CONTEXT_WINDOW`` / ``OMP_MAX_TOKENS``. So every agent on that
runtime sized its turns against a window the engine no longer had, until Mark
fixed both columns by hand.

These tests are written against the SAME two-probe contract as model drift, and
the last one is the "agent must actually be re-rendered" half — a context change
that updates the row but never reaches the container would be the same bug in a
new place.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.redis_client import RedisKeys
from app.services import sse as sse_mod
from app.services.agent_runtime_switch import ProbedModel
from app.services.runtime_watcher import RuntimeWatcher


async def _mk_rt(
    session,
    *,
    slug="ctx-rt",
    model="deepseek-v4-flash-0731-spark",
    preferred=98304,
    maximum=98304,
    runtime_type="vllm_docker",
):
    rt = Runtime(
        slug=slug, display_name=slug, runtime_type=runtime_type,
        endpoint="http://192.0.2.10:8000/v1", model_identifier=model, enabled=True,
        preferred_context_len=preferred, max_context_len=maximum,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis
    return _get


def _patches(fake_redis, probed):
    return (
        patch(
            "app.services.runtime_watcher.probe_runtime_model_info",
            new=AsyncMock(return_value=probed),
        ),
        patch("app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
    )


def test_context_drift_key_is_separate_from_the_model_drift_key():
    """An engine restarted with a new --max-model-len keeps its model id, so
    the two candidates must be able to be pending independently."""
    assert RedisKeys.runtime_context_drift_candidate("x") == "mc:runtime-ctx-drift:x"
    assert RedisKeys.runtime_context_drift_candidate(
        "x"
    ) != RedisKeys.runtime_drift_candidate("x")


@pytest.mark.asyncio
async def test_context_drift_requires_two_consecutive_probes(async_session, fake_redis):
    """The live failure, replayed: 98304 → 262144 on an unchanged model id."""
    rt = await _mk_rt(async_session)
    watcher = RuntimeWatcher(interval=90)
    p1, p2, p3 = _patches(
        fake_redis, ProbedModel("deepseek-v4-flash-0731-spark", 262144)
    )

    with p1, p2, p3, patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=1),
    ) as mock_mark:
        await watcher.tick(session=async_session)          # probe 1: candidate
        await async_session.refresh(rt)
        assert rt.max_context_len == 98304, "one probe must not be enough"
        mock_mark.assert_not_awaited()

        await watcher.tick(session=async_session)          # probe 2: confirmed
        await async_session.refresh(rt)
        assert rt.max_context_len == 262144
        assert rt.preferred_context_len == 262144
        mock_mark.assert_awaited_once()

    # And the candidate key is cleaned up, so a third tick is a no-op.
    assert await fake_redis.get(
        RedisKeys.runtime_context_drift_candidate(rt.slug)
    ) is None


@pytest.mark.asyncio
async def test_context_change_emits_its_own_event(async_session, fake_redis):
    await _mk_rt(async_session, slug="ctx-event-rt")
    watcher = RuntimeWatcher(interval=90)
    p1, p2, p3 = _patches(
        fake_redis, ProbedModel("deepseek-v4-flash-0731-spark", 262144)
    )

    with p1, p2, p3, patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=1),
    ), patch(
        "app.services.runtime_watcher.emit_event", new=AsyncMock()
    ) as mock_emit:
        await watcher.tick(session=async_session)
        await watcher.tick(session=async_session)

    kinds = [call.args[1] for call in mock_emit.await_args_list]
    assert "runtime.context_changed" in kinds
    detail = next(
        call.kwargs["detail"]
        for call in mock_emit.await_args_list
        if call.args[1] == "runtime.context_changed"
    )
    assert detail["old_max_context_len"] == 98304
    assert detail["new_max_context_len"] == 262144


@pytest.mark.asyncio
async def test_flapping_window_is_never_persisted(async_session, fake_redis):
    """Two DIFFERENT windows in a row are not a confirmation — same guard the
    model path has against an engine flapping during warm-up."""
    rt = await _mk_rt(async_session, slug="ctx-flap-rt")
    watcher = RuntimeWatcher(interval=90)

    for served_ctx in (262144, 131072, 262144):
        p1, p2, p3 = _patches(fake_redis, ProbedModel(rt.model_identifier, served_ctx))
        with p1, p2, p3, patch(
            "app.services.runtime_watcher.mark_agents_for_sync",
            new=AsyncMock(return_value=0),
        ):
            await watcher.tick(session=async_session)

    await async_session.refresh(rt)
    assert rt.max_context_len == 98304


@pytest.mark.asyncio
async def test_engine_that_reports_no_window_leaves_the_row_alone(
    async_session, fake_redis
):
    """``None`` means "the endpoint did not say", never "no window". Writing a
    guess here would be worse than the stale value it replaced."""
    rt = await _mk_rt(async_session, slug="ctx-silent-rt")
    watcher = RuntimeWatcher(interval=90)
    p1, p2, p3 = _patches(fake_redis, ProbedModel(rt.model_identifier, None))

    with p1, p2, p3, patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=0),
    ) as mock_mark:
        await watcher.tick(session=async_session)
        await watcher.tick(session=async_session)

    await async_session.refresh(rt)
    assert rt.max_context_len == 98304
    assert rt.preferred_context_len == 98304
    mock_mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_matching_window_is_not_drift(async_session, fake_redis):
    """No churn when the engine confirms what the row already says."""
    rt = await _mk_rt(async_session, slug="ctx-same-rt", preferred=262144, maximum=262144)
    watcher = RuntimeWatcher(interval=90)
    p1, p2, p3 = _patches(fake_redis, ProbedModel(rt.model_identifier, 262144))

    with p1, p2, p3, patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=0),
    ) as mock_mark:
        await watcher.tick(session=async_session)
        await watcher.tick(session=async_session)

    mock_mark.assert_not_awaited()
    assert await fake_redis.get(
        RedisKeys.runtime_context_drift_candidate(rt.slug)
    ) is None


@pytest.mark.asyncio
async def test_shrinking_window_clamps_a_larger_preferred(async_session, fake_redis):
    """The dangerous direction. A preferred window ABOVE the engine's real cap
    makes omp ask for more than the model can take (HTTP 400 mid-turn), which
    is exactly the failure the OMP_CONTEXT_WINDOW comment in
    routers/internal.py describes."""
    rt = await _mk_rt(async_session, slug="ctx-shrink-rt", preferred=131072, maximum=262144)
    watcher = RuntimeWatcher(interval=90)
    p1, p2, p3 = _patches(fake_redis, ProbedModel(rt.model_identifier, 65536))

    with p1, p2, p3, patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=1),
    ):
        await watcher.tick(session=async_session)
        await watcher.tick(session=async_session)

    await async_session.refresh(rt)
    assert rt.max_context_len == 65536
    assert rt.preferred_context_len == 65536


@pytest.mark.asyncio
async def test_deliberately_smaller_preferred_survives_a_growing_window(
    async_session, fake_redis
):
    """The engine owns the ceiling; the operator owns the working size below
    it. A preferred value that was never "the whole window" stays put."""
    rt = await _mk_rt(async_session, slug="ctx-grow-rt", preferred=32768, maximum=98304)
    watcher = RuntimeWatcher(interval=90)
    p1, p2, p3 = _patches(fake_redis, ProbedModel(rt.model_identifier, 262144))

    with p1, p2, p3, patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=1),
    ):
        await watcher.tick(session=async_session)
        await watcher.tick(session=async_session)

    await async_session.refresh(rt)
    assert rt.max_context_len == 262144
    assert rt.preferred_context_len == 32768


@pytest.mark.asyncio
async def test_model_and_context_change_together_flag_agents_once_and_sync(
    async_session, fake_redis
):
    """The full 08.08. switch: new model AND new window in one probe.

    Both paths converge on mark_agents_for_sync, and the tick's single
    sync_pending_agents pass must then actually reach the flagged agent — a
    row that is right while the container is stale is the whole bug.
    """
    rt = await _mk_rt(
        async_session, slug="ctx-both-rt", model="laguna-s21", runtime_type="omp"
    )
    agent = Agent(
        name="Ctx Sparky", slug="ctx-sparky", agent_runtime="cli-bridge",
        harness="omp", runtime_id=rt.id, status="idle",
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    p1, p2, p3 = _patches(
        fake_redis, ProbedModel("deepseek-v4-flash-0731-spark", 262144)
    )
    with p1, p2, p3, patch(
        "app.services.runtime_propagation.sync_docker_agent_files",
        new=AsyncMock(return_value={}),
    ) as mock_render, patch(
        "app.services.runtime_propagation.restart_docker_agent_container",
        return_value={"status": "restarted"},
    ) as mock_restart, patch(
        "app.services.runtime_propagation.wait_for_agent_healthy",
        new=AsyncMock(return_value={"healthy": True}),
    ), patch(
        "app.services.runtime_propagation.get_redis", _fake_get_redis(fake_redis)
    ):
        await watcher_tick(async_session)
        await async_session.refresh(rt)
        assert rt.max_context_len == 98304  # one probe is still not enough

        await watcher_tick(async_session)

    await async_session.refresh(rt)
    await async_session.refresh(agent)
    assert rt.model_identifier == "deepseek-v4-flash-0731-spark"
    assert rt.max_context_len == 262144
    # Render THEN recreate, through MC's own path — never a second renderer.
    mock_render.assert_awaited_once()
    mock_restart.assert_called_once()
    assert agent.pending_runtime_sync is False


async def watcher_tick(session):
    await RuntimeWatcher(interval=90).tick(session=session)


@pytest.mark.asyncio
async def test_confirmed_drift_reaches_the_rendered_agent_env(
    async_session, fake_redis
):
    """The last link, asserted end to end rather than assumed.

    Updating the row is only worth anything because build_runtime_env reads
    that column into OMP_CONTEXT_WINDOW / OMP_MAX_TOKENS. This test walks the
    whole chain in one place — probe → two-probe confirmation → row → rendered
    env — so a future change that moves the window to a different column, or
    renders it from somewhere else, fails here instead of silently shipping
    the stale value the fleet ran on before 08.08.
    """
    from app.routers.internal import build_runtime_env

    rt = await _mk_rt(async_session, slug="ctx-render-rt", runtime_type="omp")
    agent = Agent(
        name="Ctx Render", slug="ctx-render", agent_runtime="cli-bridge",
        harness="omp", runtime_id=rt.id, status="idle",
    )
    async_session.add(agent)
    await async_session.commit()

    before = await build_runtime_env(rt, async_session, agent=agent)
    assert before["OMP_CONTEXT_WINDOW"] == "98304"

    p1, p2, p3 = _patches(
        fake_redis, ProbedModel("deepseek-v4-flash-0731-spark", 262144)
    )
    with p1, p2, p3, patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=1),
    ):
        await watcher_tick(async_session)
        await watcher_tick(async_session)

    await async_session.refresh(rt)
    after = await build_runtime_env(rt, async_session, agent=agent)
    assert after["OMP_CONTEXT_WINDOW"] == "262144"
    assert after["OPENAI_MODEL"] == "deepseek-v4-flash-0731-spark"


@pytest.mark.asyncio
async def test_live_status_carries_the_served_window(async_session, fake_redis):
    """The cockpit feed must be able to show what the engine actually serves —
    a UI that can only show the DB row cannot reveal a drift it hasn't
    confirmed yet."""
    rt = await _mk_rt(async_session, slug="ctx-live-rt")
    watcher = RuntimeWatcher(interval=90)
    p1, p2, p3 = _patches(fake_redis, ProbedModel(rt.model_identifier, 262144))

    with p1, p2, p3, patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=0),
    ):
        await watcher.tick(session=async_session)

    status = json.loads(await fake_redis.get(RedisKeys.runtime_live(rt.slug)))
    assert status["served_context_len"] == 262144
