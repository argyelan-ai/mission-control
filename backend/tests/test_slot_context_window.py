"""Slot row must carry the window the box really serves (live 01.10.2026).

What happened: the Spark switched from a GLM vLLM recipe (250000 window) to
the GLM TensorFold recipe (1048576). Same model name ``GLM-5.3-Flash-EXL3``.
Agents bound to the box's slot row kept seeing 250000:

  * the recipe starts at 17:15 / 17:36 / 17:49 UTC all came back not-ok
    (``memory_prep_finished start_ok=false`` / ``memory_prep_timeout``), so the
    immediate slot write in ``start_recipe_on_host`` never ran — yet the engine
    came up anyway (``host.autostart_recipe_confirmed`` 17:39),
  * the watcher could not notice: TensorFold's ``/v1/models`` has no window
    field (it is only on ``/health``), and the model name did not change,
  * and even a successful start would have left the agents stale: the switch
    only flagged agents when the MODEL changed, not the window.

Every assertion below has a counter-case (sabotage probe) that would pass
without the rule. Test data: box-a / recipe-tf / agent-a, documentation IPs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.services import agent_runtime_switch as ars
from app.services import recipe_switcher, runtime_manager
from app.services import sse as sse_mod
from app.services.agent_runtime_switch import ProbedModel
from app.services.runtime_watcher import RuntimeWatcher

MODEL = "GLM-5.3-Flash-EXL3"
# Live shapes, 01.10.2026 (curl against the box, trimmed to what matters).
TF_MODELS = {"object": "list", "data": [
    {"id": MODEL, "object": "model", "owned_by": "tensorfold"},
]}
TF_HEALTH = {
    "ok": True, "backend": "tensorfold", "busy": False,
    "streams": {"decoding": 0, "prefilling": 0, "max": 4},
    "pool_tokens": 1251328, "pool_free_tokens": 1232896,
    "context_length": 1048576,
}


# ── 1. Probe: /health fallback ───────────────────────────────────────────────


@pytest.fixture
def mock_engine(monkeypatch):
    """Serve ``/v1/models`` and ``/health`` separately; return seen URLs."""
    seen: list[str] = []

    def _install(models, health, *, health_status=200):
        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.path.endswith("/health"):
                return httpx.Response(health_status, json=health)
            return httpx.Response(200, json=models)

        transport = httpx.MockTransport(_handler)
        original = httpx.AsyncClient

        def fake_async_client(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)
        return seen

    return _install


def _probe_rt(endpoint="http://192.0.2.10:8000/v1") -> Runtime:
    return Runtime(
        slug="probe-tf", display_name="probe-tf", runtime_type="openai_compatible",
        endpoint=endpoint, model_identifier=MODEL, enabled=True,
    )


@pytest.mark.asyncio
async def test_probe_reads_the_window_from_health_when_models_has_none(mock_engine):
    seen = mock_engine(TF_MODELS, TF_HEALTH)
    probed = await ars.probe_runtime_model_info(_probe_rt())
    assert probed == ProbedModel(MODEL, 1048576)
    # /health sits next to /v1, not under it.
    assert "http://192.0.2.10:8000/health" in seen


@pytest.mark.asyncio
async def test_probe_health_on_a_bare_endpoint(mock_engine):
    seen = mock_engine(TF_MODELS, TF_HEALTH)
    probed = await ars.probe_runtime_model_info(_probe_rt("http://192.0.2.10:8000"))
    assert probed == ProbedModel(MODEL, 1048576)
    assert "http://192.0.2.10:8000/health" in seen


@pytest.mark.asyncio
async def test_window_on_models_wins_and_health_is_not_asked(mock_engine):
    """Counter-case: an engine that reports on /v1/models (vLLM) is unchanged."""
    seen = mock_engine(
        {"data": [{"id": MODEL, "max_model_len": 250000}]}, TF_HEALTH
    )
    assert (await ars.probe_runtime_model_info(_probe_rt())).context_len == 250000
    assert not any(url.endswith("/health") for url in seen)


@pytest.mark.asyncio
async def test_pool_tokens_alone_is_not_a_window(mock_engine):
    """The KV pool is shared by up to 4 streams — never a per-request window."""
    health = {k: v for k, v in TF_HEALTH.items() if k != "context_length"}
    mock_engine(TF_MODELS, health)
    assert (await ars.probe_runtime_model_info(_probe_rt())) == ProbedModel(MODEL, None)


@pytest.mark.asyncio
async def test_health_naming_another_model_is_ignored(mock_engine):
    mock_engine(TF_MODELS, {**TF_HEALTH, "model": "some-other-model"})
    assert (await ars.probe_runtime_model_info(_probe_rt())) == ProbedModel(MODEL, None)


@pytest.mark.asyncio
async def test_missing_health_keeps_the_model_id(mock_engine):
    mock_engine(TF_MODELS, {"detail": "Not Found"}, health_status=404)
    assert (await ars.probe_runtime_model_info(_probe_rt())) == ProbedModel(MODEL, None)


# ── Shared DB setup ──────────────────────────────────────────────────────────


async def _host(session: AsyncSession, **kw) -> Host:
    host = Host(
        slug="box-a", display_name="BOX-A", kind="ssh", ssh_host="192.0.2.10", **kw
    )
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


async def _recipe(session: AsyncSession, **kw) -> LocalRecipe:
    fields = dict(
        slug="recipe-tf",
        display_name="Recipe TF",
        engine="vllm_docker",
        model_identifier=MODEL,
        launch_template=(
            "docker run -d --name {container_name} --label mc.runtime.slug={slug} "
            "-p {port}:8000 img"
        ),
        port=8000,
        context_len=1048576,
    )
    fields.update(kw)
    recipe = LocalRecipe(**fields)
    session.add(recipe)
    await session.commit()
    await session.refresh(recipe)
    return recipe


async def _slot(session: AsyncSession, host: Host, **kw) -> Runtime:
    fields = dict(
        display_name="BOX-A :8000",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier=MODEL,
        max_context_len=250000,
        preferred_context_len=250000,
        exclusive_memory=False,
        is_slot=True,
        enabled=True,
    )
    fields.update(kw)
    rt = Runtime(slug="box-a-slot", host_id=host.id, **fields)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _agent(session: AsyncSession, rt: Runtime) -> Agent:
    agent = Agent(
        name="agent-a", slug="agent-a", role="developer", agent_runtime="cli-bridge",
        harness="omp", model=MODEL, runtime_id=rt.id,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


# ── 2. Recipe start: a new window alone must reach the agents ────────────────


def _start_patches():
    from app.services import runtime_grace

    return (
        patch.object(recipe_switcher, "probe_running", AsyncMock(return_value=False)),
        patch.object(
            runtime_manager, "start_runtime",
            AsyncMock(return_value={"ok": True, "message": "gestartet"}),
        ),
        patch.object(runtime_grace, "mark_switching", AsyncMock()),
        patch.object(runtime_grace, "clear_switching", AsyncMock()),
    )


@pytest.mark.asyncio
async def test_recipe_start_with_same_model_but_new_window_flags_agents(session):
    host = await _host(session)
    recipe = await _recipe(session)
    slot = await _slot(session, host)
    agent = await _agent(session, slot)

    p1, p2, p3, p4 = _start_patches()
    with p1, p2, p3, p4:
        result = await recipe_switcher.start_recipe_on_host(session, host, recipe)

    assert result["ok"] is True
    await session.refresh(slot)
    assert slot.max_context_len == 1048576
    assert slot.preferred_context_len == 1048576
    await session.refresh(agent)
    assert agent.pending_runtime_sync is True, (
        "window changed 250000 → 1048576 — the agent's rendered "
        "OMP_CONTEXT_WINDOW must be re-rendered"
    )


@pytest.mark.asyncio
async def test_restart_of_the_same_recipe_does_not_flag(session):
    """Counter-case: same model, same window = nothing to re-render."""
    host = await _host(session)
    recipe = await _recipe(session)
    slot = await _slot(session, host, max_context_len=1048576, preferred_context_len=1048576)
    agent = await _agent(session, slot)

    p1, p2, p3, p4 = _start_patches()
    with p1, p2, p3, p4:
        await recipe_switcher.start_recipe_on_host(session, host, recipe)

    await session.refresh(agent)
    assert agent.pending_runtime_sync is False


# ── 3. Watcher: engine silent about its window → confirmed recipe's window ───


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis
    return _get


def _watch_patches(fake_redis, probed):
    return (
        patch(
            "app.services.runtime_watcher.probe_runtime_model_info",
            new=AsyncMock(return_value=probed),
        ),
        patch("app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
    )


async def _two_ticks(session, fake_redis, probed):
    p1, p2, p3 = _watch_patches(fake_redis, probed)
    with p1, p2, p3, patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=1),
    ) as mock_mark:
        watcher = RuntimeWatcher(interval=90)
        await watcher.tick(session=session)
        await watcher.tick(session=session)
    return mock_mark


@pytest.mark.asyncio
async def test_slot_takes_the_confirmed_recipe_window_when_the_engine_is_silent(
    async_session, fake_redis
):
    """The live failure, replayed: a failed-but-running start left 250000."""
    from app.routers.internal import build_runtime_env

    host = await _host(async_session, autostart_recipe_slug="recipe-tf")
    await _recipe(async_session)
    slot = await _slot(async_session, host)
    agent = await _agent(async_session, slot)
    before = await build_runtime_env(slot, async_session, agent=agent)
    assert before["OMP_CONTEXT_WINDOW"] == "250000"

    mock_mark = await _two_ticks(async_session, fake_redis, ProbedModel(MODEL, None))

    await async_session.refresh(slot)
    assert slot.max_context_len == 1048576
    assert slot.preferred_context_len == 1048576
    mock_mark.assert_awaited_once()
    after = await build_runtime_env(slot, async_session, agent=agent)
    assert after["OMP_CONTEXT_WINDOW"] == "1048576"


@pytest.mark.asyncio
async def test_recipe_fallback_needs_the_served_model(async_session, fake_redis):
    """Counter-case: the confirmed recipe is not what the port serves."""
    host = await _host(async_session, autostart_recipe_slug="recipe-tf")
    await _recipe(async_session, model_identifier="org/other-model")
    slot = await _slot(async_session, host)

    mock_mark = await _two_ticks(async_session, fake_redis, ProbedModel(MODEL, None))

    await async_session.refresh(slot)
    assert slot.max_context_len == 250000
    mock_mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_recipe_fallback_is_for_slot_rows_only(async_session, fake_redis):
    """Counter-case: a non-slot row on the same box is left alone."""
    host = await _host(async_session, autostart_recipe_slug="recipe-tf")
    await _recipe(async_session)
    rt = await _slot(async_session, host, is_slot=False)

    mock_mark = await _two_ticks(async_session, fake_redis, ProbedModel(MODEL, None))

    await async_session.refresh(rt)
    assert rt.max_context_len == 250000
    mock_mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_reported_window_beats_the_recipe(async_session, fake_redis):
    """Engine leads, MC follows: a reported window wins over the recipe's."""
    host = await _host(async_session, autostart_recipe_slug="recipe-tf")
    await _recipe(async_session, context_len=1048576)
    slot = await _slot(async_session, host)

    await _two_ticks(async_session, fake_redis, ProbedModel(MODEL, 524288))

    await async_session.refresh(slot)
    assert slot.max_context_len == 524288
