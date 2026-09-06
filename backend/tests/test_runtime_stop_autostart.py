"""Buehne v2 (docs/specs/runtimes-buehne-v2.md §5/§7) PR 2 — Stop schaltet
Box-Autostart aus + Dispatch-Gate.

Zwei Dinge werden hier abgesichert, beide reine DB-Operationen (kein SSH,
``runtime_manager.stop_runtime`` wird gemockt):

1. Ein erfolgreicher Stop einer host-gebundenen Runtime schaltet
   ``hosts.autostart_enabled`` aus, wenn es an war — sonst holt der Waechter
   (``runtime_watcher._maybe_auto_recover``) das Modell binnen 15 Minuten
   zurueck. War es schon aus, bleibt es aus (kein Event, keine Schreibung).
   Ein FEHLGESCHLAGENER Stop fasst Autostart gar nicht an.
2. Ein Agent, der gerade auf dieser Runtime (oder ihrer Slot-Zeile auf
   derselben Box, Duo) dispatcht (``current_task_id`` gesetzt), blockt den
   Stop mit 409 ``{code: "agent_busy", agents: [...]}`` — ausser
   ``force=true``, was den Stop durchlaufen laesst und ein
   ``runtime.stop_forced``-Event schreibt.

Der Waechter-Riegel selbst (nie starten wenn ``autostart_enabled=false``) ist
bereits durch ``tests/test_recipe_switcher_p3.py::test_watcher_does_not_revive_when_the_switch_is_off``
abgedeckt — hier nicht erneut, um Ueberlappung zu vermeiden.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.models.activity import ActivityEvent
from app.models.agent import Agent
from app.models.host import Host
from app.models.runtime import Runtime


async def _host(session, slug: str = "box-a", *, autostart_enabled: bool = False, **kw) -> Host:
    host = Host(
        slug=slug,
        display_name=slug.upper(),
        kind="ssh",
        ssh_host="192.0.2.10",
        autostart_enabled=autostart_enabled,
        **kw,
    )
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


async def _runtime(session, slug: str, host: Host | None, *, is_slot: bool = False, **kw) -> Runtime:
    fields = dict(
        display_name=slug,
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        container_name="mc-test",
        is_slot=is_slot,
    )
    fields.update(kw)
    rt = Runtime(slug=slug, host_id=host.id if host else None, **fields)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _agent(session, name: str, runtime: Runtime, *, current_task_id) -> Agent:
    agent = Agent(name=name, runtime_id=runtime.id, current_task_id=current_task_id)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


def _stop_ok():
    return patch(
        "app.services.runtime_manager.stop_runtime",
        AsyncMock(return_value={"ok": True, "message": "gestoppt"}),
    )


def _stop_fail():
    return patch(
        "app.services.runtime_manager.stop_runtime",
        AsyncMock(return_value={"ok": False, "message": "SSH-Fehler"}),
    )


# ── 1. Autostart-Kopplung ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_turns_off_autostart_when_it_was_on(async_session, auth_client):
    host = await _host(async_session, autostart_enabled=True)
    rt = await _runtime(async_session, "recipe-a", host)

    with _stop_ok():
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/stop")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["autostart_disabled"] is True
    assert body["host_slug"] == host.slug

    await async_session.refresh(host)
    assert host.autostart_enabled is False

    events = (
        await async_session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "host.autostart_disabled_by_stop")
        )
    ).all()
    assert len(events) == 1
    assert events[0].detail["slug"] == host.slug


@pytest.mark.asyncio
async def test_stop_leaves_autostart_untouched_when_already_off(async_session, auth_client):
    host = await _host(async_session, autostart_enabled=False)
    rt = await _runtime(async_session, "recipe-b", host)

    with _stop_ok():
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/stop")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["autostart_disabled"] is False
    assert body["host_slug"] == host.slug

    await async_session.refresh(host)
    assert host.autostart_enabled is False

    events = (
        await async_session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "host.autostart_disabled_by_stop")
        )
    ).all()
    assert events == []


@pytest.mark.asyncio
async def test_a_failing_stop_never_touches_autostart(async_session, auth_client):
    host = await _host(async_session, autostart_enabled=True)
    rt = await _runtime(async_session, "recipe-c", host)

    with _stop_fail():
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/stop")

    assert resp.status_code == 400

    await async_session.refresh(host)
    assert host.autostart_enabled is True

    events = (
        await async_session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "host.autostart_disabled_by_stop")
        )
    ).all()
    assert events == []


# ── 2. Dispatch-Gate ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_busy_agent_blocks_the_stop_with_409(async_session, auth_client):
    host = await _host(async_session, autostart_enabled=True)
    rt = await _runtime(async_session, "recipe-d", host)
    task_id = uuid.uuid4()
    await _agent(async_session, "Agent-Alpha", rt, current_task_id=task_id)

    stop_mock = AsyncMock(return_value={"ok": True, "message": "gestoppt"})
    with patch("app.services.runtime_manager.stop_runtime", stop_mock):
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/stop")

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "agent_busy"
    assert detail["agents"][0]["name"] == "Agent-Alpha"
    assert detail["agents"][0]["task_id"] == str(task_id)
    stop_mock.assert_not_awaited()

    # Autostart untouched — the stop never ran.
    await async_session.refresh(host)
    assert host.autostart_enabled is True


@pytest.mark.asyncio
async def test_force_overrides_the_gate_and_logs_it(async_session, auth_client):
    host = await _host(async_session, autostart_enabled=True)
    rt = await _runtime(async_session, "recipe-e", host)
    task_id = uuid.uuid4()
    await _agent(async_session, "Agent-Alpha", rt, current_task_id=task_id)

    with _stop_ok():
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/stop?force=true")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["autostart_disabled"] is True

    events = (
        await async_session.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "runtime.stop_forced")
        )
    ).all()
    assert len(events) == 1
    assert "Agent-Alpha" in events[0].title
    assert events[0].detail["agents"][0]["name"] == "Agent-Alpha"


@pytest.mark.asyncio
async def test_an_agent_without_a_task_does_not_block(async_session, auth_client):
    host = await _host(async_session, autostart_enabled=True)
    rt = await _runtime(async_session, "recipe-f", host)
    await _agent(async_session, "Idle-Bot", rt, current_task_id=None)

    with _stop_ok():
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/stop")

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_a_busy_agent_on_the_slot_row_of_the_same_host_also_blocks(async_session, auth_client):
    """Duo: Agenten binden auf die Slot-Zeile der Box (ADR-078), nicht auf
    die Rezept-Instanz selbst — das Gate muss ueber den Host-Umweg finden."""
    host = await _host(async_session, autostart_enabled=True)
    recipe_rt = await _runtime(async_session, "recipe-duo-head", host)
    slot_rt = await _runtime(async_session, "recipe-duo-head-slot", host, is_slot=True, runtime_type="openai_compatible")
    task_id = uuid.uuid4()
    await _agent(async_session, "Duo-Worker", slot_rt, current_task_id=task_id)

    stop_mock = AsyncMock(return_value={"ok": True, "message": "gestoppt"})
    with patch("app.services.runtime_manager.stop_runtime", stop_mock):
        resp = await auth_client.post(f"/api/v1/runtimes/{recipe_rt.slug}/stop")

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["agents"][0]["name"] == "Duo-Worker"
    stop_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_busy_agent_on_an_unrelated_host_does_not_block(async_session, auth_client):
    host_a = await _host(async_session, "box-a", autostart_enabled=True)
    host_b = await _host(async_session, "box-b", autostart_enabled=True)
    rt_a = await _runtime(async_session, "recipe-g", host_a)
    rt_b = await _runtime(async_session, "recipe-h", host_b)
    await _agent(async_session, "Busy-Elsewhere", rt_b, current_task_id=uuid.uuid4())

    with _stop_ok():
        resp = await auth_client.post(f"/api/v1/runtimes/{rt_a.slug}/stop")

    assert resp.status_code == 200, resp.text


# ── 3. Riegel + Rand: Slot-Zeile, Cloud-Runtime ohne Host ───────────────────


@pytest.mark.asyncio
async def test_the_slot_row_itself_cannot_be_stopped(async_session, auth_client):
    """ADR-078: die Slot-Zeile ist nur eine Adresse (kein Startbefehl, kein
    Anker) — ein Stop-Klick darauf waere ein No-Op, der aber faelschlich
    Autostart der Box mit ausschalten wuerde. Muss vor jeder anderen Pruefung
    (Dispatch-Gate, Autostart) mit 400 abgewiesen werden."""
    host = await _host(async_session, autostart_enabled=True)
    slot_rt = await _runtime(async_session, "recipe-slot-only", host, is_slot=True, runtime_type="openai_compatible")

    stop_mock = AsyncMock(return_value={"ok": True, "message": "gestoppt"})
    with patch("app.services.runtime_manager.stop_runtime", stop_mock):
        resp = await auth_client.post(f"/api/v1/runtimes/{slot_rt.slug}/stop")

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "slot_not_stoppable"
    stop_mock.assert_not_awaited()

    # Autostart bleibt unangetastet — der Riegel greift vor der Kopplung.
    await async_session.refresh(host)
    assert host.autostart_enabled is True


@pytest.mark.asyncio
async def test_a_cloud_runtime_without_a_host_stops_cleanly_with_no_coupling(async_session, auth_client):
    """Cloud-/HTTP-only-Runtimes haben keinen host_id (ADR-048) — die
    Autostart-Kopplung darf dafuer weder krachen noch faelschlich eine Box
    anfassen. Antwort bleibt explizit false/null, nicht einfach fehlend."""
    rt = await _runtime(async_session, "recipe-cloud", host=None, runtime_type="cloud")

    with _stop_ok():
        resp = await auth_client.post(f"/api/v1/runtimes/{rt.slug}/stop")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["autostart_disabled"] is False
    assert body["host_slug"] is None
