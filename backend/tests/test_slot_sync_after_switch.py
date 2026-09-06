"""Nach dem Rezeptwechsel muss die ``omp.env`` mitziehen (ADR-078, Nachlese).

Live-Befund 06.09.2026: der Umschalter schrieb das neue Modell sofort in die
Slot-Zeile — und genau deshalb sah der Drift-Wächter danach KEINE Drift mehr.
Der einzige Weg, der bisher ``mark_agents_for_sync`` auslöste, war aber der
Drift-Pfad. Ergebnis: die Agenten an der Slot-Zeile behielten ihr altes
``OPENAI_MODEL`` und fragten das verschwundene Modell an (404).

Abgesichert wird hier — jede Prüfung mit Sabotage-Probe (Gegenfall, der ohne
die neue Regel durchginge):

  * der Umschalter flaggt die Agenten der Slot-Zeile SELBST, sobald er das
    neue Modell hineinschreibt · Gegenfall: gleiches Modell = kein Flaggen,
  * Sicherheitsnetz im Wächter: antwortet die Runtime und trägt ein gebundener
    cli-bridge-Agent noch den alten Modellnamen, wird einmal (gedrosselt)
    geflaggt · Gegenfälle: passender Modellname = nichts, und ein zweiter Tick
    flaggt nicht erneut,
  * ``ensure_slot_runtimes`` zieht beim Umhängen auch ``agents.model`` nach ·
    Gegenfall: ein Nicht-cli-bridge-Agent wird nicht angefasst.

Testdaten heissen box-a / recipe-x / agent-a. Kein Netz, kein Docker: Probe,
Start und der Sync-Lauf werden ersetzt.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.redis_client import RedisKeys
from app.services import recipe_switcher, runtime_manager, slot_runtimes
from app.services.agent_runtime_switch import ProbedModel
from app.services.runtime_watcher import RuntimeWatcher

TEMPLATE = (
    "docker run -d --name {container_name} --label mc.runtime.slug={slug} "
    "-p {port}:8000 img"
)


# ── Aufbau ───────────────────────────────────────────────────────────────────


async def _host(session: AsyncSession, slug: str = "box-a") -> Host:
    host = Host(slug=slug, display_name=slug.upper(), kind="ssh", ssh_host="192.0.2.10")
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


async def _recipe(session: AsyncSession, **kw) -> LocalRecipe:
    fields = dict(
        display_name="Recipe X",
        engine="vllm_docker",
        model_identifier="org/neu",
        launch_template=TEMPLATE,
        port=8000,
        context_len=131072,
    )
    fields.update(kw)
    recipe = LocalRecipe(slug=kw.pop("slug", "recipe-x"), **fields)
    session.add(recipe)
    await session.commit()
    await session.refresh(recipe)
    return recipe


async def _slot(session: AsyncSession, host: Host, **kw) -> Runtime:
    fields = dict(
        display_name=f"{host.display_name} :8000",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="org/alt",
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


async def _recipe_runtime(session: AsyncSession, slug: str, host: Host, **kw) -> Runtime:
    fields = dict(
        display_name=slug,
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        launch_command="docker run --label mc.runtime.slug=x img",
        model_identifier="org/recipe-x",
        exclusive_memory=True,
        enabled=True,
    )
    fields.update(kw)
    rt = Runtime(slug=slug, host_id=host.id, **fields)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _agent(session: AsyncSession, rt: Runtime, **kw) -> Agent:
    fields = dict(
        name="agent-a",
        role="developer",
        agent_runtime="cli-bridge",
        harness="omp",
        model="org/alt",
    )
    fields.update(kw)
    agent = Agent(runtime_id=rt.id, **fields)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


def _start_patches():
    from app.services import runtime_grace

    return (
        patch.object(recipe_switcher, "probe_running", AsyncMock(return_value=False)),
        patch.object(
            runtime_manager,
            "start_runtime",
            AsyncMock(return_value={"ok": True, "message": "gestartet"}),
        ),
        patch.object(runtime_grace, "mark_switching", AsyncMock()),
        patch.object(runtime_grace, "clear_switching", AsyncMock()),
    )


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis

    return _get


# ── 1. Der Umschalter flaggt selbst ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_recipe_start_flags_slot_agents_for_sync(session):
    """Neues Modell in der Slot-Zeile ⇒ die Agenten daran werden geflaggt."""
    host = await _host(session)
    recipe = await _recipe(session, model_identifier="org/neu", context_len=262144)
    slot = await _slot(session, host, model_identifier="org/alt")
    agent = await _agent(session, slot)

    p1, p2, p3, p4 = _start_patches()
    with p1, p2, p3, p4:
        result = await recipe_switcher.start_recipe_on_host(session, host, recipe)

    assert result["ok"] is True
    await session.refresh(slot)
    assert slot.model_identifier == "org/neu"
    await session.refresh(agent)
    assert agent.pending_runtime_sync is True


@pytest.mark.asyncio
async def test_recipe_start_does_not_flag_when_the_model_is_unchanged(session):
    """Sabotage-Probe: gleiches Modell = kein Flaggen der ganzen Flotte.

    Ohne diesen Gegenfall würde jeder Neustart desselben Rezepts jeden Agenten
    der Box unnötig durch den Reload-Pfad schicken.
    """
    host = await _host(session)
    recipe = await _recipe(session, model_identifier="org/alt", context_len=131072)
    slot = await _slot(session, host, model_identifier="org/alt", max_context_len=131072)
    agent = await _agent(session, slot, model="org/alt")

    p1, p2, p3, p4 = _start_patches()
    with p1, p2, p3, p4:
        await recipe_switcher.start_recipe_on_host(session, host, recipe)

    await session.refresh(agent)
    assert agent.pending_runtime_sync is False


# ── 2. Sicherheitsnetz im Wächter ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watcher_syncs_agents_whose_model_is_stale(async_session, fake_redis):
    """Runtime antwortet, Agent trägt noch den alten Namen ⇒ einmal flaggen."""
    host = await _host(async_session)
    slot = await _slot(async_session, host, model_identifier="org/neu")
    await _agent(async_session, slot, model="org/alt")
    watcher = RuntimeWatcher(interval=90)

    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel("org/neu", None)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)
    ), patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=1),
    ) as mock_mark:
        await watcher.tick(session=async_session)
        assert mock_mark.await_count == 1
        assert mock_mark.await_args.args[1].id == slot.id

        # Zweiter Tick: die Drosselung hält — sonst flaggte der Wächter die
        # Flotte in jeder Runde neu, solange ein Sync nicht durchkommt.
        await watcher.tick(session=async_session)
        assert mock_mark.await_count == 1

    assert await fake_redis.get(RedisKeys.runtime_agent_model_guard(slot.slug))


@pytest.mark.asyncio
async def test_watcher_leaves_matching_agents_alone(async_session, fake_redis):
    """Sabotage-Probe: passender Modellname ⇒ kein Flaggen, keine Drosselung."""
    host = await _host(async_session)
    slot = await _slot(async_session, host, model_identifier="org/neu")
    await _agent(async_session, slot, model="org/neu")
    watcher = RuntimeWatcher(interval=90)

    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel("org/neu", None)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)
    ), patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=0),
    ) as mock_mark:
        await watcher.tick(session=async_session)

    mock_mark.assert_not_awaited()
    assert await fake_redis.get(RedisKeys.runtime_agent_model_guard(slot.slug)) is None


@pytest.mark.asyncio
async def test_watcher_leaves_agents_without_a_recorded_model_alone(
    async_session, fake_redis
):
    """Sabotage-Probe: leeres ``agents.model`` ist kein Beleg für „veraltet".

    Sonst würde jeder frisch angelegte Agent — und jede Zeile, deren Drift der
    Wächter erst noch bestätigen muss — grundlos durch den Reload-Pfad
    geschickt.
    """
    host = await _host(async_session)
    slot = await _slot(async_session, host, model_identifier="org/neu")
    await _agent(async_session, slot, model=None)
    watcher = RuntimeWatcher(interval=90)

    with patch(
        "app.services.runtime_watcher.probe_runtime_model_info",
        new=AsyncMock(return_value=ProbedModel("org/neu", None)),
    ), patch(
        "app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)
    ), patch(
        "app.services.runtime_watcher.mark_agents_for_sync",
        new=AsyncMock(return_value=0),
    ) as mock_mark:
        await watcher.tick(session=async_session)

    mock_mark.assert_not_awaited()


# ── 3. Umhängen zieht agents.model nach ──────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_slot_runtimes_pulls_agent_model_along(session):
    host = await _host(session)
    active = await _recipe_runtime(
        session, "recipe-x-box-a", host, model_identifier="org/recipe-x"
    )
    agent = await _agent(session, active, model="org/alt")
    cloud = Runtime(
        slug="anthropic-cloud",
        display_name="cloud",
        runtime_type="anthropic",
        endpoint="https://api.example.invalid",
        model_identifier="cloud-model",
        enabled=True,
    )
    session.add(cloud)
    await session.commit()
    await session.refresh(cloud)
    # Sabotage-Probe: ein Agent, der nicht cli-bridge ist, wird nie angefasst.
    untouched = await _agent(
        session, cloud, name="agent-b", agent_runtime="claude_code", model="cloud-model"
    )

    summary = await slot_runtimes.ensure_slot_runtimes(session)
    assert summary["rebound"] == ["agent-a"]

    slot = await slot_runtimes.find_slot_runtime(session, host.id)
    assert slot is not None
    await session.refresh(agent)
    assert agent.runtime_id == slot.id
    assert agent.model == slot.model_identifier == "org/recipe-x"
    await session.refresh(untouched)
    assert untouched.runtime_id == cloud.id
    assert untouched.model == "cloud-model"
