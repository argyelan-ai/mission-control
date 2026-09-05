"""Slot-Runtime, Kern (ADR-078) — „der Agent hängt an der Box, nicht am Rezept".

Was hier abgesichert wird — und jede Prüfung MIT Sabotage-Probe, also einem
Gegenfall, der ohne die neue Regel durchgeht:

  * eine Slot-Zeile ist nie die Instanz eines Rezepts (``recipe_matches_runtime``),
  * eine Slot-Zeile belegt nie eine Box (``load_fleet_state.occupied``),
  * eine Slot-Zeile ist nie Verdrängungsopfer (``ensure_exclusive_host``),
  * eine Slot-Zeile wird nie wiederbelebt (``_autostart_target`` /
    ``_maybe_auto_recover``),
  * der Drift-Wächter folgt ihr IMMER, auch ohne Anker
    (``_served_answer_is_own``),
  * der Umschalter setzt den Übergangs-Marker auf die Slot-Zeile und schreibt
    Modell + Fenster sofort hinein,
  * das Bereitschafts-Tor verschiebt eine Zustellung, solange der Marker steht,
  * ``ensure_slot_runtimes`` ist idempotent, hängt cli-bridge-Agenten um (auch
    von ``enabled = false``-Zeilen) und lässt Cloud-/Kimi-Agenten in Ruhe,
  * Migration 0194 (Quelltext-Ebene): eine Spalte, Server-Default, Downgrade
    nimmt sie zurück, keine Datenzeilen.

Testdaten heissen box-a / box-b / recipe-x / agent-a. Kein Netz: Health-Probe
und ``start_runtime`` werden ersetzt.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.models.task import Task
from app.services import recipe_switcher, runtime_manager, slot_runtimes

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"

TEMPLATE = (
    "docker run -d --name {container_name} --label mc.runtime.slug={slug} "
    "-p {port}:8000 img"
)


# ── Aufbau ───────────────────────────────────────────────────────────────────


async def _host(session: AsyncSession, slug: str = "box-a", **kw) -> Host:
    fields = dict(display_name=slug.upper(), kind="ssh", ssh_host="192.0.2.10")
    fields.update(kw)
    host = Host(slug=slug, **fields)
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


async def _recipe(session: AsyncSession, slug: str = "recipe-x", **kw) -> LocalRecipe:
    fields = dict(
        display_name=slug.replace("-", " ").title(),
        engine="vllm_docker",
        model_identifier=f"org/{slug}",
        launch_template=TEMPLATE,
        port=8000,
        context_len=131072,
    )
    fields.update(kw)
    recipe = LocalRecipe(slug=slug, **fields)
    session.add(recipe)
    await session.commit()
    await session.refresh(recipe)
    return recipe


async def _recipe_runtime(
    session: AsyncSession, slug: str, host: Host | None, **kw
) -> Runtime:
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
    rt = Runtime(slug=slug, host_id=host.id if host else None, **fields)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _slot_runtime(session: AsyncSession, host: Host, **kw) -> Runtime:
    """Eine Slot-Zeile nach Vertrag (ADR-078) — ohne Anker, ohne Startbefehl."""
    fields = dict(
        display_name=f"{host.display_name} :8000",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="org/recipe-x",
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


async def _agent(session: AsyncSession, rt: Runtime | None, **kw) -> Agent:
    fields = dict(name="agent-a", role="developer", agent_runtime="cli-bridge")
    fields.update(kw)
    agent = Agent(runtime_id=rt.id if rt else None, **fields)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


# ── 1. Die Slot-Zeile matcht nie ein Rezept ──────────────────────────────────


@pytest.mark.asyncio
async def test_slot_row_never_matches_a_recipe(session):
    """Gleicher ``model_identifier`` — und trotzdem kein Match.

    Ohne diesen Riegel hielte der Umschalter die Slot-Zeile für die Instanz des
    laufenden Rezepts und lehnte den nächsten Start mit „Startbefehl fehlt" ab.
    """
    host = await _host(session)
    recipe = await _recipe(session)
    slot = await _slot_runtime(session, host, model_identifier=recipe.model_identifier)

    assert recipe_switcher.recipe_matches_runtime(recipe, slot) is False

    # Sabotage: dieselbe Zeile ohne das Kennzeichen WÜRDE matchen — die Prüfung
    # hängt also wirklich an is_slot und nicht an einer Nebenbedingung.
    slot.is_slot = False
    assert recipe_switcher.recipe_matches_runtime(recipe, slot) is True


# ── 2. Die Slot-Zeile ist nie eine Belegung ──────────────────────────────────


@pytest.mark.asyncio
async def test_slot_row_is_never_an_occupancy(session):
    """Eine Box darf sich nicht durch ihre eigene Slot-Zeile belegt halten."""
    host = await _host(session)
    await _recipe(session)
    slot = await _slot_runtime(session, host)

    with patch.object(recipe_switcher, "probe_running", AsyncMock(return_value=True)):
        state = await recipe_switcher.load_fleet_state(session)
    assert state.occupied.get(host.id) is None
    assert [rt.slug for rt in state.runtimes] == []

    # Sabotage: ohne das Kennzeichen taucht genau dieselbe Zeile als Belegung auf.
    slot.is_slot = False
    session.add(slot)
    await session.commit()
    with patch.object(recipe_switcher, "probe_running", AsyncMock(return_value=True)):
        state = await recipe_switcher.load_fleet_state(session)
    assert [rt.slug for rt in state.occupied.get(host.id, [])] == [slot.slug]


# ── 3. Die Slot-Zeile ist nie Verdrängungsopfer ──────────────────────────────


@pytest.mark.asyncio
async def test_slot_row_is_never_evicted(session):
    """``ensure_exclusive_host`` fasst eine Slot-Zeile nicht an.

    Selbst wenn jemand ihr versehentlich ``exclusive_memory`` gesetzt hat: es
    gäbe nichts zu stoppen, und ein fehlgeschlagener Stopp bräche den Start ab.
    """
    host = await _host(session)
    slot = await _slot_runtime(session, host, exclusive_memory=True)
    starter = await _recipe_runtime(session, "recipe-x-box-a", host)

    stop_calls: list[str] = []

    async def _fake_evict(slug, **_kw):
        stop_calls.append(slug)
        return {"ok": True}

    async def _fake_stop(runtime_dict, **_kw):
        # Die Slot-Zeile ist ``openai_compatible`` — ihr Stopp-Pfad wäre der
        # allgemeine ``stop_runtime``, nicht die Docker-Räumung. Beide Wege
        # werden mitgeschrieben, sonst wäre die Sabotage-Probe blind.
        stop_calls.append(runtime_dict.get("id") or runtime_dict.get("slug"))
        return {"ok": True}

    def _patches():
        return (
            patch.object(runtime_manager, "evict_spark_runtime_containers", _fake_evict),
            patch.object(runtime_manager, "stop_runtime", _fake_stop),
            patch.object(runtime_manager, "get_runtime_state",
                         AsyncMock(return_value={"state": "ready"})),
            patch.object(runtime_manager, "resolve_host_for_runtime",
                         AsyncMock(return_value=None), create=True),
        )

    p1, p2, p3, p4 = _patches()
    with p1, p2, p3, p4:
        result = await runtime_manager.ensure_exclusive_host(
            starter.model_dump(), session=session, host_id=host.id
        )
    assert result["ok"] is True
    assert slot.slug not in stop_calls

    # Sabotage: ohne das Kennzeichen wäre genau diese Zeile gestoppt worden.
    slot.is_slot = False
    session.add(slot)
    await session.commit()
    stop_calls.clear()
    p1, p2, p3, p4 = _patches()
    with p1, p2, p3, p4:
        await runtime_manager.ensure_exclusive_host(
            starter.model_dump(), session=session, host_id=host.id
        )
    assert slot.slug in stop_calls


# ── 4. Die Slot-Zeile wird nie wiederbelebt ──────────────────────────────────


@pytest.mark.asyncio
async def test_slot_row_is_never_auto_recovered(session, isolate_redis_singleton):
    """Weder Autostart-Ziel noch Auto-Recovery-Start."""
    from app.services.runtime_watcher import RuntimeWatcher

    host = await _host(
        session, autostart_enabled=True, autostart_recipe_slug="recipe-x"
    )
    recipe = await _recipe(session)
    slot = await _slot_runtime(session, host, model_identifier=recipe.model_identifier)
    watcher = RuntimeWatcher()

    assert await watcher._autostart_target(session, slot) == (None, None)

    started = AsyncMock(return_value={"ok": True})
    with patch.object(watcher, "_autostart_start", started):
        await watcher._maybe_auto_recover(session, isolate_redis_singleton, slot, fails=3)
    assert started.await_count == 0

    # Sabotage: eine gewöhnliche Rezept-Zeile derselben Box IST ein Ziel.
    normal = await _recipe_runtime(
        session, "recipe-x-box-a", host, model_identifier=recipe.model_identifier
    )
    host_row, matched = await watcher._autostart_target(session, normal)
    assert host_row is not None and matched is not None


# ── 5. Drift folgt ohne Anker ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slot_row_follows_drift_without_anchor(session):
    """``_served_answer_is_own`` sagt für eine Slot-Zeile immer ja.

    Der Gegenfall ist der schärfste: eine ankerlose ``ssh_process``-Zeile gilt
    sonst ausdrücklich als NICHT eigen (sonst schriebe der Wächter ihr das
    Modell des Nachbarn auf demselben Port zu).
    """
    from app.services.runtime_watcher import RuntimeWatcher

    host = await _host(session)
    slot = await _slot_runtime(
        session, host, runtime_type="ssh_process", process_name=None, container_name=None
    )
    watcher = RuntimeWatcher()

    assert await watcher._served_answer_is_own(session, slot) is True

    # Sabotage: ohne das Kennzeichen ist dieselbe Zeile nicht „eigen".
    slot.is_slot = False
    assert await watcher._served_answer_is_own(session, slot) is False


# ── 6. Der Umschalter setzt den Marker und schreibt sofort ───────────────────


@pytest.mark.asyncio
async def test_recipe_start_marks_and_writes_slot_row(session):
    """Marker auf der Slot-Zeile + Modell/Fenster sofort in der Zeile."""
    from app.services import runtime_grace

    host = await _host(session)
    recipe = await _recipe(session, model_identifier="org/neu", context_len=262144)
    slot = await _slot_runtime(session, host, model_identifier="org/alt", max_context_len=8192)

    marks: list[tuple[str, str, str]] = []

    async def _mark(slug, phase, source):
        marks.append((slug, phase, source))

    with patch.object(recipe_switcher, "probe_running", AsyncMock(return_value=False)), \
         patch.object(runtime_manager, "start_runtime",
                      AsyncMock(return_value={"ok": True, "message": "gestartet"})), \
         patch.object(runtime_grace, "mark_switching", _mark):
        result = await recipe_switcher.start_recipe_on_host(session, host, recipe)

    assert result["ok"] is True
    assert marks and marks[0][0] == slot.slug
    assert marks[0][1] == runtime_grace.PHASE_LOADING
    assert marks[0][2] == runtime_grace.SOURCE_SWITCH

    await session.refresh(slot)
    assert slot.model_identifier == "org/neu"
    assert slot.max_context_len == 262144
    # Der Name trägt das laufende Modell — sonst zeigt die Oberfläche nach dem
    # ersten Wechsel etwas Falsches.
    assert "org/neu" in slot.display_name


@pytest.mark.asyncio
async def test_failed_recipe_start_clears_the_slot_marker(session):
    """Sabotage-Probe: kein Start = kein „wechselt gerade" für 20 Minuten."""
    from app.services import runtime_grace

    host = await _host(session)
    recipe = await _recipe(session)
    slot = await _slot_runtime(session, host)

    cleared: list[str] = []

    async def _clear(slug):
        cleared.append(slug)

    with patch.object(recipe_switcher, "probe_running", AsyncMock(return_value=False)), \
         patch.object(runtime_manager, "start_runtime",
                      AsyncMock(return_value={"ok": False, "message": "kaputt"})), \
         patch.object(runtime_grace, "mark_switching", AsyncMock()), \
         patch.object(runtime_grace, "clear_switching", _clear):
        with pytest.raises(recipe_switcher.RecipeStartError):
            await recipe_switcher.start_recipe_on_host(session, host, recipe)

    assert cleared == [slot.slug]


# ── 7. Das Bereitschafts-Tor ─────────────────────────────────────────────────


async def _task(session: AsyncSession) -> Task:
    from app.models.board import Board

    board = Board(name="board-a", slug="board-a")
    session.add(board)
    await session.commit()
    await session.refresh(board)
    task = Task(board_id=board.id, title="task-a")
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_readiness_gate_defers_while_the_marker_stands(session):
    from app.services import runtime_grace
    from app.services.dispatch_delivery import _check_runtime_readiness

    host = await _host(session)
    slot = await _slot_runtime(session, host)
    agent = await _agent(session, slot)
    task = await _task(session)

    await runtime_grace.mark_switching(
        slot.slug, runtime_grace.PHASE_LOADING, runtime_grace.SOURCE_SWITCH
    )
    allowed = await _check_runtime_readiness(
        task, agent, session, task.board_id, str(agent.id)
    )
    assert allowed is False

    # Marker weg, keine Probe bekannt → wieder frei (kein Warten nach Neustart).
    await runtime_grace.clear_switching(slot.slug)
    allowed = await _check_runtime_readiness(
        task, agent, session, task.board_id, str(agent.id)
    )
    assert allowed is True


@pytest.mark.asyncio
async def test_readiness_gate_defers_when_the_last_probe_saw_nothing(
    session, isolate_redis_singleton
):
    from app.redis_client import RedisKeys
    from app.services.dispatch_delivery import _check_runtime_readiness

    host = await _host(session)
    slot = await _slot_runtime(session, host)
    agent = await _agent(session, slot)
    task = await _task(session)

    await isolate_redis_singleton.set(
        RedisKeys.runtime_live(slot.slug), json.dumps({"reachable": False})
    )
    assert await _check_runtime_readiness(
        task, agent, session, task.board_id, str(agent.id)
    ) is False

    await isolate_redis_singleton.set(
        RedisKeys.runtime_live(slot.slug), json.dumps({"reachable": True})
    )
    assert await _check_runtime_readiness(
        task, agent, session, task.board_id, str(agent.id)
    ) is True


@pytest.mark.asyncio
async def test_readiness_gate_leaves_every_other_binding_alone(session):
    """Sabotage: derselbe Marker, aber der Agent hängt NICHT an einer Slot-Zeile.

    Ohne diese Probe könnte das Tor unbemerkt die ganze Flotte anhalten.
    """
    from app.services import runtime_grace
    from app.services.dispatch_delivery import _check_runtime_readiness

    host = await _host(session)
    normal = await _recipe_runtime(session, "recipe-x-box-a", host)
    agent = await _agent(session, normal)
    task = await _task(session)

    await runtime_grace.mark_switching(
        normal.slug, runtime_grace.PHASE_LOADING, runtime_grace.SOURCE_SWITCH
    )
    assert await _check_runtime_readiness(
        task, agent, session, task.board_id, str(agent.id)
    ) is True

    # Und ein Agent ganz ohne Runtime ebenfalls.
    free = await _agent(session, None, name="agent-b")
    assert await _check_runtime_readiness(
        task, free, session, task.board_id, str(agent.id)
    ) is True


# ── 8. ensure_slot_runtimes ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_slot_runtimes_creates_and_rebinds_idempotently(session):
    host = await _host(session)
    active = await _recipe_runtime(
        session, "recipe-x-box-a", host, model_identifier="org/recipe-x"
    )
    # Eine STILLGELEGTE Rezept-Zeile — genau der Fall, der Agenten mit in den
    # Ruhestand nahm.
    retired = await _recipe_runtime(
        session, "recipe-y-box-a", host, enabled=False, model_identifier="org/recipe-y"
    )
    on_active = await _agent(session, active, name="agent-a", harness="omp")
    on_retired = await _agent(session, retired, name="agent-b", harness="omp")

    summary = await slot_runtimes.ensure_slot_runtimes(session)
    assert summary["created"] == ["box-a-slot"]
    assert sorted(summary["rebound"]) == ["agent-a", "agent-b"]

    slot = await slot_runtimes.find_slot_runtime(session, host.id)
    assert slot is not None
    assert slot.is_slot is True
    assert slot.runtime_type == "openai_compatible"
    assert slot.launch_command is None and slot.container_name is None
    assert slot.exclusive_memory is False and slot.autostart_supported is False
    assert slot.host_id == host.id  # bleibt gesetzt: die langen omp-Zeitgeber
    assert slot.endpoint == active.endpoint

    for agent in (on_active, on_retired):
        await session.refresh(agent)
        assert agent.runtime_id == slot.id
        assert agent.pending_runtime_sync is True

    # Zweiter Lauf: nichts Neues, nichts umgehängt.
    again = await slot_runtimes.ensure_slot_runtimes(session)
    assert again == {"created": [], "rebound": [], "skipped_no_endpoint": []}
    rows = (await session.exec(select(Runtime).where(Runtime.is_slot == True))).all()  # noqa: E712
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_ensure_slot_runtimes_leaves_cloud_and_kimi_agents_alone(session):
    """Nur cli-bridge-Agenten an Rezept-Zeilen DIESER Box werden umgehängt."""
    host = await _host(session)
    active = await _recipe_runtime(session, "recipe-x-box-a", host)
    cloud_rt = Runtime(
        slug="anthropic-claude-cloud",
        display_name="cloud",
        runtime_type="anthropic_cloud",
        endpoint="https://example.invalid/v1",
        model_identifier="cloud-model",
    )
    kimi_rt = Runtime(
        slug="kimi-cloud",
        display_name="kimi",
        runtime_type="kimi",
        endpoint="https://example.invalid/coding/v1",
    )
    session.add(cloud_rt)
    session.add(kimi_rt)
    await session.commit()
    await session.refresh(cloud_rt)
    await session.refresh(kimi_rt)

    cloud_agent = await _agent(session, cloud_rt, name="agent-cloud", harness="claude")
    kimi_agent = await _agent(session, kimi_rt, name="agent-kimi", harness="kimi")
    host_agent = await _agent(
        session, active, name="agent-host", agent_runtime="host", harness="omp"
    )
    # Ein claude-Agent, der (fälschlich) an der Rezept-Zeile der Box hängt,
    # darf trotzdem nicht auf eine OpenAI-Zeile wandern.
    wrong_harness = await _agent(
        session, active, name="agent-claude", harness="claude"
    )

    summary = await slot_runtimes.ensure_slot_runtimes(session)
    assert summary["rebound"] == []

    slot = await slot_runtimes.find_slot_runtime(session, host.id)
    for agent, expected in (
        (cloud_agent, cloud_rt.id),
        (kimi_agent, kimi_rt.id),
        (host_agent, active.id),
        (wrong_harness, active.id),
    ):
        await session.refresh(agent)
        assert agent.runtime_id == expected
        assert agent.runtime_id != slot.id


@pytest.mark.asyncio
async def test_box_without_command_driven_runtime_gets_no_slot(session):
    """Sabotage: eine Box, auf der keine Rezepte wechseln, bekommt nichts."""
    host = await _host(session, slug="box-b")
    summary = await slot_runtimes.ensure_slot_runtimes(session)
    assert summary["created"] == []
    assert await slot_runtimes.find_slot_runtime(session, host.id) is None

    # …aber eine Box mit ausdrücklicher Head-Rolle schon.
    host.role = "head"
    session.add(host)
    await session.commit()
    summary = await slot_runtimes.ensure_slot_runtimes(session)
    assert summary["created"] == ["box-b-slot"]


# ── 9. Der Nachzügler-Wächter umgeht das Tor nicht ───────────────────────────


@pytest.mark.asyncio
async def test_undispatched_watchdog_respects_the_gate(session):
    """``_check_undispatched_tasks`` ist der ZWEITE Weg zum Agenten.

    Ohne den Riegel dort würde die Aufgabe, die der Dispatch gerade
    zurückgehalten hat, 30 Sekunden später doch zugestellt — in ein Modell,
    das noch lädt.
    """
    from datetime import timedelta

    from app.services import runtime_grace
    from app.services.watchdog.task_monitor import TaskMonitorMixin
    from app.utils import utcnow

    host = await _host(session)
    slot = await _slot_runtime(session, host)
    agent = await _agent(session, slot)
    task = await _task(session)
    task.assigned_agent_id = agent.id
    task.updated_at = utcnow() - timedelta(minutes=5)
    session.add(task)
    await session.commit()

    await runtime_grace.mark_switching(
        slot.slug, runtime_grace.PHASE_LOADING, runtime_grace.SOURCE_SWITCH
    )

    monitor = TaskMonitorMixin()
    delivered = AsyncMock(return_value=True)
    with patch("app.services.cli_bridge_runner.dispatch_to_cli_bridge", delivered), \
         patch("app.services.dispatch._build_dispatch_message",
               AsyncMock(return_value="brief")):
        await monitor._check_undispatched_tasks(session)
    assert delivered.await_count == 0
    await session.refresh(task)
    assert task.dispatched_at is None

    # Sabotage: Marker weg → derselbe Lauf stellt zu.
    await runtime_grace.clear_switching(slot.slug)
    with patch("app.services.cli_bridge_runner.dispatch_to_cli_bridge", delivered), \
         patch("app.services.dispatch._build_dispatch_message",
               AsyncMock(return_value="brief")):
        await monitor._check_undispatched_tasks(session)
    assert delivered.await_count == 1


# ── 10. Die Liste zeigt das Kennzeichen, aber keinen Autostart-Knopf ─────────


@pytest.mark.asyncio
async def test_runtime_list_exposes_is_slot_without_autostart(async_session, auth_client):
    """Das Frontend braucht ``is_slot`` — und darf nie einen Autostart anbieten."""
    host = await _host(async_session)
    slot = await _slot_runtime(async_session, host, autostart_supported=True)

    async def _stub_state(*_a, **_kw):
        return {"state": "ready", "http_reachable": True, "container_status": None}

    with patch("app.services.runtime_manager.get_runtime_state", side_effect=_stub_state):
        resp = await auth_client.get("/api/v1/runtimes")
    assert resp.status_code == 200, resp.text
    rows = {r["slug"]: r for r in resp.json()["runtimes"]}
    assert rows[slot.slug]["is_slot"] is True
    assert rows[slot.slug]["autostart_supported"] is False


# ── 11. Migration (Quelltext-Ebene, wie 0191/0193) ───────────────────────────


def test_migration_0194_adds_one_column_and_takes_it_back():
    source = (MIGRATIONS / "0194_runtime_is_slot.py").read_text(encoding="utf-8")

    assert 'op.add_column(\n        "runtimes",' in source
    assert '"is_slot"' in source
    assert "sa.Boolean()" in source
    assert "nullable=False" in source
    assert 'server_default=sa.text("false")' in source
    assert 'op.drop_column("runtimes", "is_slot")' in source
    assert 'down_revision = "0193_p3_duo_autostart"' in source
    assert 'revision = "0194_runtime_is_slot"' in source
    # Keine Datenzeilen: welche Boxen es gibt, ist Instanz-Sache.
    for verb in ("INSERT", "UPDATE", "op.execute", "op.bulk_insert"):
        assert verb not in source, verb


@pytest.mark.asyncio
async def test_model_defaults_is_slot_to_false(session):
    """Jede bestehende Zeile bleibt ohne Anpassung gültig."""
    rt = Runtime(
        slug="recipe-x-box-a",
        display_name="X",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    assert rt.is_slot is False
    assert rt.to_registry_dict()["is_slot"] is False


# ── 12. Nachbesserungen aus dem adversarialen Review (05.09.2026) ────────────


@pytest.mark.asyncio
async def test_auto_recovery_riegel_haelt_auch_ohne_den_autostart_riegel(
    session, isolate_redis_singleton
):
    """H/N1: der Riegel in ``_maybe_auto_recover`` war ungetestet.

    Der frühere Test war grün, weil schon ``_autostart_target`` abwies — der
    zweite Riegel hätte fehlen können, ohne dass es jemand merkt. Hier wird
    ``_autostart_target`` bewusst überbrückt (er liefert ein gültiges Ziel),
    sodass NUR noch die ``is_slot``-Prüfung in ``_maybe_auto_recover`` zwischen
    der Slot-Zeile und einem echten Startversuch steht.

    Die Zeile trägt hier absichtlich ``vllm_docker``: mit dem vertragsgemässen
    ``openai_compatible`` würde schon die Engine-Art abweisen, und der Test
    bewiese wieder nichts. Genau dieser Fall — jemand hat der Slot-Zeile eine
    Docker-Art gegeben — ist die Lage, für die der Riegel da ist. (Nachgeprüft:
    ersetzt man ``if runtime.is_slot`` durch ``if False``, wird dieser Test rot.)
    """
    from app.services.runtime_watcher import RuntimeWatcher

    host = await _host(session, autostart_enabled=True, autostart_recipe_slug="recipe-x")
    recipe = await _recipe(session)
    slot = await _slot_runtime(
        session, host, model_identifier=recipe.model_identifier,
        runtime_type="vllm_docker", container_name="mc-box-a",
    )
    watcher = RuntimeWatcher()

    started = AsyncMock(return_value={"ok": True})
    bypass = AsyncMock(return_value=(host, recipe))
    with patch.object(watcher, "_autostart_target", bypass), \
         patch.object(watcher, "_autostart_start", started), \
         patch.object(watcher, "_host_answers", AsyncMock(return_value=True)), \
         patch.object(watcher, "_active_exclusive_sibling", AsyncMock(return_value=None)), \
         patch.object(watcher, "_record_autostart_attempt", AsyncMock()), \
         patch("app.services.runtime_watcher.resolve_host_for_runtime",
               AsyncMock(return_value=SimpleNamespace(ssh_host="192.0.2.10"))), \
         patch("app.services.runtime_watcher.ssh_capable", lambda _h: True):
        await watcher._maybe_auto_recover(session, isolate_redis_singleton, slot, fails=3)
    assert started.await_count == 0

    # Sabotage: NUR das Kennzeichen fällt weg — sonst ändert sich nichts.
    slot.is_slot = False
    with patch.object(watcher, "_autostart_target", bypass), \
         patch.object(watcher, "_autostart_start", started), \
         patch.object(watcher, "_host_answers", AsyncMock(return_value=True)), \
         patch.object(watcher, "_active_exclusive_sibling", AsyncMock(return_value=None)), \
         patch.object(watcher, "_record_autostart_attempt", AsyncMock()), \
         patch("app.services.runtime_watcher.resolve_host_for_runtime",
               AsyncMock(return_value=SimpleNamespace(ssh_host="192.0.2.10"))), \
         patch("app.services.runtime_watcher.ssh_capable", lambda _h: True):
        await watcher._maybe_auto_recover(session, isolate_redis_singleton, slot, fails=3)
    assert started.await_count == 1


@pytest.mark.asyncio
async def test_watcher_erneuert_den_slot_marker_solange_die_instanz_laedt(
    session, isolate_redis_singleton
):
    """M1: ein 30-Minuten-Kaltstart darf keinen Fehlalarm auslösen.

    Der Grace-Marker lebt 20 Minuten. Solange die REZEPT-Zeile in Grace ist,
    bekommt die Slot-Zeile ihren Marker neu — beide sterben damit gleichzeitig,
    statt dass die Slot-Zeile ab Minute 21 ``runtime.unreachable`` feuert.
    """
    from app.services import runtime_grace
    from app.services.runtime_watcher import RuntimeWatcher

    host = await _host(session)
    slot = await _slot_runtime(session, host)
    instance = await _recipe_runtime(session, "recipe-x-box-a", host)
    watcher = RuntimeWatcher()

    marker = {"phase": runtime_grace.PHASE_LOADING, "source": runtime_grace.SOURCE_SWITCH}
    await watcher._refresh_slot_grace(session, instance, marker)
    assert await runtime_grace.get_switching(slot.slug) is not None

    # Sabotage 1: eine Zeile ohne Box hat keine Slot-Zeile, die sie mitziehen
    # könnte — und darf trotzdem nicht stolpern.
    await runtime_grace.clear_switching(slot.slug)
    boxless = await _recipe_runtime(session, "recipe-y", None)
    await watcher._refresh_slot_grace(session, boxless, marker)
    assert await runtime_grace.get_switching(slot.slug) is None

    # Sabotage 2: die Slot-Zeile markiert sich nicht endlos selbst.
    await watcher._refresh_slot_grace(session, slot, marker)
    assert await runtime_grace.get_switching(slot.slug) is None


@pytest.mark.asyncio
async def test_probe_runde_zieht_den_slot_marker_wirklich_mit(
    session, isolate_redis_singleton
):
    """M1, Verdrahtung: die Methode allein nuetzt nichts, wenn sie niemand ruft.

    Hier laeuft eine echte Probe-Runde: die Rezept-Zeile antwortet nicht und
    steht in Grace (= laedt). Danach muss die Slot-Zeile denselben Marker haben.
    """
    from app.services import runtime_grace
    from app.services.runtime_watcher import RuntimeWatcher

    host = await _host(session)
    slot = await _slot_runtime(session, host)
    instance = await _recipe_runtime(session, "recipe-x-box-a", host)
    watcher = RuntimeWatcher()

    await runtime_grace.mark_switching(
        instance.slug, runtime_grace.PHASE_LOADING, runtime_grace.SOURCE_SWITCH
    )
    assert await runtime_grace.get_switching(slot.slug) is None

    silent = SimpleNamespace(model_id=None, context_len=None)
    with patch("app.services.runtime_watcher.probe_runtime_model_info",
               AsyncMock(return_value=silent)), \
         patch.object(watcher, "_check_crash_loop", AsyncMock(return_value=False)):
        await watcher._probe_one(session, instance)

    assert await runtime_grace.get_switching(slot.slug) is not None


@pytest.mark.asyncio
async def test_warnung_feuert_nach_45_minuten_auch_ohne_marker(
    session, isolate_redis_singleton
):
    """H1: die Warnung hing am Grace-Marker — der stirbt nach 20 Minuten.

    Das Alter kommt jetzt aus einem eigenen Zähler, der beim ERSTEN Aufschub
    gesetzt wird und lange genug lebt. Hier wird er auf „vor 50 Minuten"
    zurückdatiert, während gar kein Marker mehr steht (nur eine tote Box) —
    genau die Lage, in der die Warnung früher nie kam.
    """
    from app.redis_client import RedisKeys
    from app.services import dispatch_delivery as dd

    host = await _host(session)
    slot = await _slot_runtime(session, host)
    agent = await _agent(session, slot)
    task = await _task(session)

    await isolate_redis_singleton.set(
        RedisKeys.runtime_live(slot.slug), json.dumps({"reachable": False})
    )
    old = datetime.now(timezone.utc) - timedelta(minutes=50)
    await isolate_redis_singleton.set(
        dd._DEFER_KEY.format(task_id=str(task.id)),
        json.dumps({"first": old.isoformat(), "last_event": None}),
    )

    events: list[tuple[str, str]] = []

    async def _emit(_s, event_type, _title, **kw):
        events.append((event_type, kw.get("severity", "info")))

    with patch.object(dd, "emit_event", _emit):
        allowed = await dd._check_runtime_readiness(
            task, agent, session, task.board_id, str(agent.id)
        )
    assert allowed is False
    assert events == [("dispatch.deferred_runtime_loading", "warning")]

    # Sabotage: frisch aufgeschoben → nur „info", keine Warnung.
    await isolate_redis_singleton.delete(dd._DEFER_KEY.format(task_id=str(task.id)))
    events.clear()
    with patch.object(dd, "emit_event", _emit):
        await dd._check_runtime_readiness(
            task, agent, session, task.board_id, str(agent.id)
        )
    assert events == [("dispatch.deferred_runtime_loading", "info")]


@pytest.mark.asyncio
async def test_wartende_aufgabe_erzeugt_hoechstens_alle_5_minuten_ein_ereignis(
    session, isolate_redis_singleton
):
    """M2: der Nachzügler-Wächter fragt alle 30 s — das darf nicht 60 Ereignisse
    je Wechsel bedeuten."""
    from app.redis_client import RedisKeys
    from app.services import dispatch_delivery as dd

    host = await _host(session)
    slot = await _slot_runtime(session, host)
    agent = await _agent(session, slot)
    task = await _task(session)
    await isolate_redis_singleton.set(
        RedisKeys.runtime_live(slot.slug), json.dumps({"reachable": False})
    )

    events: list[str] = []

    async def _emit(_s, event_type, _title, **_kw):
        events.append(event_type)

    with patch.object(dd, "emit_event", _emit):
        for _ in range(10):  # 10 Runden à 30 s = 5 Minuten
            await dd._check_runtime_readiness(
                task, agent, session, task.board_id, str(agent.id)
            )
    assert len(events) == 1, events

    # Nach Ablauf des Fensters kommt genau EIN weiteres Ereignis.
    raw = await isolate_redis_singleton.get(dd._DEFER_KEY.format(task_id=str(task.id)))
    doc = json.loads(raw)
    doc["last_event"] = (
        datetime.now(timezone.utc) - timedelta(seconds=dd.DEFER_EVENT_INTERVAL_SECONDS + 5)
    ).isoformat()
    await isolate_redis_singleton.set(
        dd._DEFER_KEY.format(task_id=str(task.id)), json.dumps(doc)
    )
    with patch.object(dd, "emit_event", _emit):
        await dd._check_runtime_readiness(
            task, agent, session, task.board_id, str(agent.id)
        )
    assert len(events) == 2


@pytest.mark.asyncio
async def test_wartezaehler_wird_geloescht_sobald_die_box_wieder_antwortet(
    session, isolate_redis_singleton
):
    """Sonst zählte der NÄCHSTE Wechsel ab dem alten Startpunkt und warnte sofort."""
    from app.redis_client import RedisKeys
    from app.services import dispatch_delivery as dd

    host = await _host(session)
    slot = await _slot_runtime(session, host)
    agent = await _agent(session, slot)
    task = await _task(session)
    key = dd._DEFER_KEY.format(task_id=str(task.id))

    await isolate_redis_singleton.set(
        RedisKeys.runtime_live(slot.slug), json.dumps({"reachable": False})
    )
    with patch.object(dd, "emit_event", AsyncMock()):
        await dd._check_runtime_readiness(
            task, agent, session, task.board_id, str(agent.id)
        )
    assert await isolate_redis_singleton.get(key) is not None

    await isolate_redis_singleton.set(
        RedisKeys.runtime_live(slot.slug), json.dumps({"reachable": True})
    )
    assert await dd._check_runtime_readiness(
        task, agent, session, task.board_id, str(agent.id)
    ) is True
    assert await isolate_redis_singleton.get(key) is None


@pytest.mark.asyncio
async def test_schalter_haelt_den_rueckbau_ueber_den_neustart(session):
    """H2: ohne Schalter legte der nächste Backend-Start alles wieder an."""
    from app.config import settings

    host = await _host(session)
    await _recipe_runtime(session, "recipe-x-box-a", host)

    original = settings.slot_runtimes_enabled
    try:
        settings.slot_runtimes_enabled = False
        summary = await slot_runtimes.ensure_slot_runtimes(session)
        assert summary["created"] == [] and summary.get("disabled") is True
        assert await slot_runtimes.find_slot_runtime(session, host.id) is None

        # Sabotage: Schalter an → dieselbe Lage legt sehr wohl eine Zeile an.
        settings.slot_runtimes_enabled = True
        summary = await slot_runtimes.ensure_slot_runtimes(session)
        assert summary["created"] == ["box-a-slot"]
    finally:
        settings.slot_runtimes_enabled = original


@pytest.mark.asyncio
async def test_endpunkt_der_slot_zeile_ist_deterministisch(session):
    """M4: ohne feste Reihenfolge entschied die DB, welchen Endpunkt eine Box bekommt."""
    host = await _host(session)
    # Absichtlich in „falscher" Reihenfolge angelegt.
    await _recipe_runtime(
        session, "recipe-z-box-a", host, endpoint="http://192.0.2.10:9000/v1", ui_order=5
    )
    await _recipe_runtime(
        session, "recipe-a-box-a", host, endpoint="http://192.0.2.10:8000/v1", ui_order=1
    )

    summary = await slot_runtimes.ensure_slot_runtimes(session)
    assert summary["created"] == ["box-a-slot"]
    slot = await slot_runtimes.find_slot_runtime(session, host.id)
    assert slot.endpoint == "http://192.0.2.10:8000/v1"  # ui_order 1 gewinnt, nie der Zufall


@pytest.mark.asyncio
async def test_ohne_instanz_gewinnt_die_tailscale_adresse(session):
    """M4: eine LAN-Adresse kann aus dem Container ins Leere laufen.

    Eine Tailscale-Route auf dem Host kapert genau diese LAN-Adresse — der
    Wächter warnt später selbst davor und schlägt die Tailscale-Adresse vor.
    """
    host = await _host(
        session, slug="box-b", role="head", ssh_host="192.0.2.20",
        tailscale_host="box-b.vpn.invalid",
    )
    summary = await slot_runtimes.ensure_slot_runtimes(session)
    assert summary["created"] == ["box-b-slot"]
    slot = await slot_runtimes.find_slot_runtime(session, host.id)
    assert slot.endpoint == "http://box-b.vpn.invalid:8000/v1"


# ── 13. Der Rückweg (Skript slot_rollback.py) ────────────────────────────────


@pytest.mark.asyncio
async def test_rollback_haengt_zurueck_und_loescht_die_zeile(session):
    from scripts import slot_rollback

    host = await _host(session)
    target = await _recipe_runtime(session, "recipe-x-box-a", host)
    slot = await _slot_runtime(session, host)
    agent = await _agent(session, slot)

    with patch.object(slot_rollback, "AsyncSession", _session_factory(session)):
        moved = await slot_rollback.rollback(dry_run=False, keep_rows=False)
    assert moved == 1
    await session.refresh(agent)
    assert agent.runtime_id == target.id
    assert agent.pending_runtime_sync is True
    assert (await session.exec(select(Runtime).where(Runtime.is_slot == True))).all() == []  # noqa: E712


@pytest.mark.asyncio
async def test_rollback_bricht_ab_und_schreibt_nichts_wenn_ein_ziel_fehlt(session):
    """H3: vorher wurde Agent für Agent committet und die Zeile trotzdem gelöscht
    — eine Fremdschlüssel-Verletzung im halb fertigen Rückbau."""
    from scripts import slot_rollback

    host = await _host(session)
    target = await _recipe_runtime(session, "recipe-x-box-a", host)
    slot_a = await _slot_runtime(session, host)
    ok_agent = await _agent(session, slot_a, name="agent-a")

    # Zweite Box OHNE Rezept-Zeile: für ihren Agenten gibt es kein Ziel.
    host_b = await _host(session, slug="box-b", role="head")
    slot_b = await _slot_runtime(session, host_b)
    slot_b.slug = "box-b-slot"
    session.add(slot_b)
    await session.commit()
    lost_agent = await _agent(session, slot_b, name="agent-b")

    with patch.object(slot_rollback, "AsyncSession", _session_factory(session)):
        result = await slot_rollback.rollback(dry_run=False, keep_rows=False)
    assert result < 0

    # NICHTS wurde geschrieben — auch nicht für den Agenten, der ein Ziel hatte.
    await session.refresh(ok_agent)
    await session.refresh(lost_agent)
    assert ok_agent.runtime_id == slot_a.id
    assert ok_agent.runtime_id != target.id
    assert lost_agent.runtime_id == slot_b.id
    rows = (await session.exec(select(Runtime).where(Runtime.is_slot == True))).all()  # noqa: E712
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_rollback_probelauf_schreibt_nichts(session):
    from scripts import slot_rollback

    host = await _host(session)
    await _recipe_runtime(session, "recipe-x-box-a", host)
    slot = await _slot_runtime(session, host)
    agent = await _agent(session, slot)

    with patch.object(slot_rollback, "AsyncSession", _session_factory(session)):
        moved = await slot_rollback.rollback(dry_run=True, keep_rows=False)
    assert moved == 1
    await session.refresh(agent)
    assert agent.runtime_id == slot.id  # unverändert
    rows = (await session.exec(select(Runtime).where(Runtime.is_slot == True))).all()  # noqa: E712
    assert len(rows) == 1


def _session_factory(existing):
    """Das Skript öffnet seine eigene Sitzung — im Test ist es dieselbe.

    ``AsyncSession(engine, ...)`` als Kontext-Manager nachgebildet, damit das
    Skript unverändert laufen kann und der Test danach denselben Zustand sieht.
    """
    class _Factory:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return existing

        async def __aexit__(self, *_exc):
            return False

    return _Factory
