"""A slot row takes the window of the instance it serves (live 23.10.2026).

What happened: DeepSeek-1017 was switched to a 262144 window. Only the
INSTANCE row was patched; the box's slot row stayed at 1048576. Every agent
bound to the slot (omp + Hermes) kept sizing its turns against 1M and would
have run until the memory guard killed the engine. Fixed by hand.

Rule (ADR-096 "one truth, every path through one function"): when the window
of an instance row changes — PATCH, restart, recipe start, watcher drift — it
is mirrored onto the slot row that currently serves that instance. As long as
the engine reports no window of its own, the instance row is the truth.

Every assertion has a counter-case (a slot serving another instance, an
ambiguous box, a deliberately smaller preferred window). Test data: box-m,
documentation IPs, made-up model names.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.services import slot_runtimes
from app.services.agent_runtime_switch import ProbedModel

MODEL = "org/model-big"
OTHER = "org/model-other"
OLD = 1_048_576
NEW = 262_144


async def _host(session: AsyncSession, slug: str = "box-m", **kw) -> Host:
    host = Host(slug=slug, display_name=slug.upper(), kind="ssh", ssh_host="192.0.2.20", **kw)
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


async def _instance(
    session: AsyncSession, host: Host, slug: str, model: str = MODEL, ctx: int = OLD, **kw
) -> Runtime:
    rt = Runtime(
        slug=slug,
        display_name=slug,
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.20:8000/v1",
        container_name=f"mc-{slug}",
        launch_command="docker run -d img",
        model_identifier=model,
        max_context_len=ctx,
        preferred_context_len=ctx,
        exclusive_memory=True,
        enabled=True,
        host_id=host.id,
        **kw,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _slot(
    session: AsyncSession, host: Host, model: str = MODEL, ctx: int = OLD, **kw
) -> Runtime:
    fields = dict(
        display_name=f"{host.display_name} :8000",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.20:8000/v1",
        model_identifier=model,
        max_context_len=ctx,
        preferred_context_len=ctx,
        exclusive_memory=False,
        is_slot=True,
        enabled=True,
    )
    fields.update(kw)
    rt = Runtime(slug=f"{host.slug}-slot", host_id=host.id, **fields)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _agent(session: AsyncSession, rt: Runtime, slug: str = "agent-m", **kw) -> Agent:
    fields = dict(
        name=slug, slug=slug, role="developer", agent_runtime="cli-bridge",
        harness="omp", model=rt.model_identifier, runtime_id=rt.id,
    )
    fields.update(kw)
    agent = Agent(**fields)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


# ── 1. PATCH: the live failure, replayed ─────────────────────────────────────


@pytest.mark.asyncio
async def test_patching_the_instance_window_reaches_the_slot(async_session, auth_client):
    host = await _host(async_session)
    inst = await _instance(async_session, host, "inst-big")
    slot = await _slot(async_session, host)
    agent = await _agent(async_session, slot)

    resp = await auth_client.patch(
        f"/api/v1/runtimes/db/{inst.slug}",
        json={"max_context_len": NEW, "preferred_context_len": NEW},
    )
    assert resp.status_code == 200, resp.text

    await async_session.refresh(slot)
    assert slot.max_context_len == NEW
    assert slot.preferred_context_len == NEW
    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is True, (
        "the slot's window changed — its agents must get the new window rendered"
    )


@pytest.mark.asyncio
async def test_patch_also_flags_agents_bound_to_the_instance_itself(
    async_session, auth_client
):
    host = await _host(async_session)
    inst = await _instance(async_session, host, "inst-big")
    agent = await _agent(async_session, inst)

    resp = await auth_client.patch(
        f"/api/v1/runtimes/db/{inst.slug}", json={"max_context_len": NEW}
    )
    assert resp.status_code == 200, resp.text
    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is True


@pytest.mark.asyncio
async def test_slot_serving_another_instance_is_untouched(async_session, auth_client):
    """Counter-case: the patched instance is stopped; the box serves OTHER."""
    host = await _host(async_session)
    inst = await _instance(async_session, host, "inst-big")
    await _instance(async_session, host, "inst-other", model=OTHER, ctx=OLD)
    slot = await _slot(async_session, host, model=OTHER)
    agent = await _agent(async_session, slot)

    resp = await auth_client.patch(
        f"/api/v1/runtimes/db/{inst.slug}", json={"max_context_len": NEW}
    )
    assert resp.status_code == 200, resp.text

    await async_session.refresh(slot)
    assert slot.max_context_len == OLD
    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is False


@pytest.mark.asyncio
async def test_slot_serving_a_model_without_instance_row_is_untouched(
    async_session, auth_client
):
    """Counter-case: the box's ONLY instance row is not what the port serves."""
    host = await _host(async_session)
    inst = await _instance(async_session, host, "inst-big")
    slot = await _slot(async_session, host, model=OTHER)

    resp = await auth_client.patch(
        f"/api/v1/runtimes/db/{inst.slug}", json={"max_context_len": NEW}
    )
    assert resp.status_code == 200, resp.text
    await async_session.refresh(slot)
    assert slot.max_context_len == OLD


@pytest.mark.asyncio
async def test_slot_of_another_box_is_untouched(async_session, auth_client):
    host_a = await _host(async_session, "box-m")
    host_b = await _host(async_session, "box-n")
    inst = await _instance(async_session, host_a, "inst-big")
    await _slot(async_session, host_a)
    slot_b = await _slot(async_session, host_b)

    await auth_client.patch(f"/api/v1/runtimes/db/{inst.slug}", json={"max_context_len": NEW})

    await async_session.refresh(slot_b)
    assert slot_b.max_context_len == OLD


# ── 2. The mirror itself: who counts as "serving", the preferred rule ────────


@pytest.mark.asyncio
async def test_ambiguous_box_without_confirmed_recipe_is_left_alone(async_session):
    """Two instances of the same model, nothing says which one runs → no guess."""
    host = await _host(async_session)
    inst = await _instance(async_session, host, "inst-vllm", ctx=250_000)
    await _instance(async_session, host, "inst-tf", ctx=OLD)
    slot = await _slot(async_session, host)

    inst.max_context_len = NEW
    async_session.add(inst)
    await async_session.commit()
    assert await slot_runtimes.mirror_instance_window_to_slot(async_session, inst) is None
    await async_session.refresh(slot)
    assert slot.max_context_len == OLD


@pytest.mark.asyncio
async def test_confirmed_recipe_breaks_the_tie(async_session):
    host = await _host(async_session, autostart_recipe_slug="recipe-tf")
    async_session.add(LocalRecipe(
        slug="recipe-tf", display_name="TF", engine="vllm_docker",
        model_identifier=MODEL, port=8000, context_len=OLD,
    ))
    await async_session.commit()
    await _instance(async_session, host, "inst-vllm", ctx=250_000)
    tf = await _instance(
        async_session, host, "inst-tf", ctx=NEW, topology={"recipe_slug": "recipe-tf"}
    )
    slot = await _slot(async_session, host)

    updated = await slot_runtimes.mirror_instance_window_to_slot(async_session, tf)
    assert updated is not None
    await async_session.refresh(slot)
    assert slot.max_context_len == NEW


@pytest.mark.asyncio
async def test_a_deliberately_smaller_preferred_window_stays(async_session):
    """Same preferred rule as runtime_watcher._handle_context_drift."""
    host = await _host(async_session)
    inst = await _instance(async_session, host, "inst-big", ctx=OLD)
    slot = await _slot(async_session, host, preferred_context_len=131_072)

    inst.max_context_len = NEW
    async_session.add(inst)
    await async_session.commit()
    await slot_runtimes.mirror_instance_window_to_slot(async_session, inst)

    await async_session.refresh(slot)
    assert slot.max_context_len == NEW
    assert slot.preferred_context_len == 131_072


@pytest.mark.asyncio
async def test_unchanged_window_flags_nobody(async_session):
    host = await _host(async_session)
    inst = await _instance(async_session, host, "inst-big", ctx=OLD)
    slot = await _slot(async_session, host)
    agent = await _agent(async_session, slot)

    await slot_runtimes.mirror_instance_window_to_slot(async_session, inst)
    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is False


# ── 3. Restart: the PATCH-then-restart path ──────────────────────────────────


@pytest.mark.asyncio
async def test_restart_mirrors_the_instance_window(async_session, auth_client):
    host = await _host(async_session)
    inst = await _instance(async_session, host, "inst-big", ctx=NEW)
    slot = await _slot(async_session, host, ctx=OLD)

    with patch(
        "app.services.runtime_manager.restart_runtime",
        new=AsyncMock(return_value={"ok": True, "message": "stub"}),
    ):
        resp = await auth_client.post(f"/api/v1/runtimes/{inst.slug}/restart")
    assert resp.status_code == 200, resp.text
    await async_session.refresh(slot)
    assert slot.max_context_len == NEW


# ── 4. Watcher: a silent engine must not drag the slot back to the recipe ───


@pytest.mark.asyncio
async def test_silent_engine_keeps_the_instance_window_not_the_recipe(
    async_session, fake_redis
):
    """Recipe still says 1M, instance says 262144 → instance wins (no ping-pong)."""
    from app.services import sse as sse_mod
    from app.services.runtime_watcher import RuntimeWatcher

    host = await _host(async_session, autostart_recipe_slug="recipe-big")
    async_session.add(LocalRecipe(
        slug="recipe-big", display_name="Big", engine="vllm_docker",
        model_identifier=MODEL, port=8000, context_len=OLD,
    ))
    await async_session.commit()
    await _instance(
        async_session, host, "inst-big", ctx=NEW, topology={"recipe_slug": "recipe-big"}
    )
    slot = await _slot(async_session, host, ctx=NEW)

    async def _get():
        return fake_redis

    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel(MODEL, None)),
    ), patch("app.services.runtime_watcher.get_redis", _get), patch.object(
        sse_mod, "get_redis", _get
    ), patch(
        "app.services.runtime_watcher.RuntimeWatcher._served_answer_is_own",
        new=AsyncMock(return_value=True),
    ):
        watcher = RuntimeWatcher(interval=90)
        await watcher.tick(session=async_session)
        await watcher.tick(session=async_session)

    await async_session.refresh(slot)
    assert slot.max_context_len == NEW


# ── 5. The agents get it: omp render + Hermes agent.env (token kept) ─────────


@pytest.mark.asyncio
async def test_mirrored_window_reaches_omp_render_and_hermes_agent_env(
    async_session, tmp_path, monkeypatch
):
    from app.routers.internal import build_runtime_env
    from app.services.host_harness_adapter import sync_host_agent_model

    host = await _host(async_session)
    inst = await _instance(async_session, host, "inst-big", ctx=OLD)
    slot = await _slot(async_session, host)
    omp = await _agent(async_session, slot, "omp-m")
    hermes = await _agent(
        async_session, slot, "hermes", agent_runtime="host", harness="hermes"
    )
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    env_path = tmp_path / ".mc" / "agents" / "hermes" / "agent.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        f"MC_AGENT_TOKEN='tok-keep'\nOPENAI_MODEL='{MODEL}'\nMC_CONTEXT_WINDOW='{OLD}'\n"
    )

    inst.max_context_len = NEW
    inst.preferred_context_len = NEW
    async_session.add(inst)
    await async_session.commit()
    await slot_runtimes.mirror_instance_window_to_slot(async_session, inst)

    await async_session.refresh(omp)
    await async_session.refresh(hermes)
    assert omp.pending_runtime_sync is True
    assert hermes.pending_runtime_sync is True
    await async_session.refresh(slot)
    env = await build_runtime_env(slot, async_session, agent=omp)
    assert env["OMP_CONTEXT_WINDOW"] == str(NEW)

    # The sync pass the watcher runs for a flagged host agent:
    await sync_host_agent_model(hermes, slot, session=async_session)
    text = env_path.read_text()
    assert f"MC_CONTEXT_WINDOW='{NEW}'" in text
    assert "MC_AGENT_TOKEN='tok-keep'" in text
