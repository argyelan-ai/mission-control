"""Reload statt Neustart (ADR-078, PR 2).

Ein Rezeptwechsel darf keine Container-Neustarts mehr auslösen. Für omp reicht
es, ``models.yml`` + ``omp.env`` IM laufenden Container neu zu schreiben
(``docker exec <container> render-omp-config.sh``) — ``launch-omp.sh`` liest
``omp.env`` bei jedem Window-Respawn neu ein, und die Bridge respawnt Window 0
für jede Aufgabe.

Abgesichert wird:
  * omp-Agent → Exec-Pfad, KEIN Neustart, keine Health-Frist,
  * Exec schlägt fehl → Neustart wie vorher (Rückfall),
  * anderer Harness → unverändert Neustart (kein Exec),
  * das Ereignis sagt, welcher Weg genommen wurde (``mode``),
  * die Skripte selbst: der Entrypoint bricht nicht mehr sofort ab und ruft den
    Renderer, der Renderer liegt in PATH.

Testdaten heissen box-a / recipe-x / agent-a. Kein Docker: ``reload_omp_config``
und ``restart_docker_agent_container`` werden ersetzt.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services import runtime_propagation as rp

DOCKER = Path(__file__).resolve().parents[2] / "docker"


async def _runtime(session, **kw) -> Runtime:
    fields = dict(
        slug="box-a-slot",
        display_name="BOX-A :8000",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="org/recipe-x",
        is_slot=True,
        enabled=True,
    )
    fields.update(kw)
    rt = Runtime(**fields)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _agent(session, rt, *, harness="omp") -> Agent:
    agent = Agent(
        name="agent-a",
        role="developer",
        agent_runtime="cli-bridge",
        runtime_id=rt.id,
        harness=harness,
        pending_runtime_sync=True,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


def _lock_patches():
    return (
        patch.object(rp, "_acquire_lock", AsyncMock(return_value=True)),
        patch.object(rp, "_release_lock", AsyncMock()),
        patch.object(rp, "sync_docker_agent_files", AsyncMock()),
    )


@pytest.mark.asyncio
async def test_omp_agent_is_reloaded_without_a_restart(async_session):
    rt = await _runtime(async_session)
    agent = await _agent(async_session, rt)

    reload_mock = MagicMock(return_value={"status": "reloaded"})
    restart_mock = MagicMock(return_value={"status": "restarted"})
    health_mock = AsyncMock(return_value={"healthy": True})
    p1, p2, p3 = _lock_patches()
    with p1, p2, p3, \
         patch.object(rp, "reload_omp_config", reload_mock), \
         patch.object(rp, "restart_docker_agent_container", restart_mock), \
         patch.object(rp, "wait_for_agent_healthy", health_mock):
        await rp._sync_one(async_session, agent)

    assert reload_mock.call_count == 1
    assert restart_mock.call_count == 0
    # Keine Health-Frist: der Container wurde gar nicht angefasst.
    assert health_mock.await_count == 0
    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is False
    assert agent.model == "org/recipe-x"


@pytest.mark.asyncio
async def test_reload_failure_falls_back_to_restart(async_session):
    """Sabotage: altes Image ohne das Skript → der alte Weg trägt weiter."""
    rt = await _runtime(async_session)
    agent = await _agent(async_session, rt)

    reload_mock = MagicMock(return_value={"status": "error: no such file"})
    restart_mock = MagicMock(return_value={"status": "restarted"})
    health_mock = AsyncMock(return_value={"healthy": True})
    p1, p2, p3 = _lock_patches()
    with p1, p2, p3, \
         patch.object(rp, "reload_omp_config", reload_mock), \
         patch.object(rp, "restart_docker_agent_container", restart_mock), \
         patch.object(rp, "wait_for_agent_healthy", health_mock):
        await rp._sync_one(async_session, agent)

    assert reload_mock.call_count == 1
    assert restart_mock.call_count == 1
    assert health_mock.await_count == 1
    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is False


@pytest.mark.asyncio
async def test_other_harnesses_keep_the_restart_path(async_session):
    """openclaude & Co. haben kein render-omp-config.sh — nichts ändert sich."""
    rt = await _runtime(async_session)
    agent = await _agent(async_session, rt, harness="openclaude")

    reload_mock = MagicMock(return_value={"status": "reloaded"})
    restart_mock = MagicMock(return_value={"status": "restarted"})
    p1, p2, p3 = _lock_patches()
    with p1, p2, p3, \
         patch.object(rp, "reload_omp_config", reload_mock), \
         patch.object(rp, "restart_docker_agent_container", restart_mock), \
         patch.object(rp, "wait_for_agent_healthy",
                      AsyncMock(return_value={"healthy": True})):
        await rp._sync_one(async_session, agent)

    assert reload_mock.call_count == 0
    assert restart_mock.call_count == 1


@pytest.mark.asyncio
async def test_event_says_which_way_was_taken(async_session):
    rt = await _runtime(async_session)
    agent = await _agent(async_session, rt)

    events: list[tuple[str, dict]] = []

    async def _emit(_session, event_type, _title, **kw):
        events.append((event_type, kw.get("detail") or {}))

    p1, p2, p3 = _lock_patches()
    with p1, p2, p3, \
         patch.object(rp, "reload_omp_config",
                      MagicMock(return_value={"status": "reloaded"})), \
         patch.object(rp, "restart_docker_agent_container",
                      MagicMock(return_value={"status": "restarted"})), \
         patch.object(rp, "wait_for_agent_healthy",
                      AsyncMock(return_value={"healthy": True})), \
         patch.object(rp, "emit_event", _emit):
        await rp._sync_one(async_session, agent)

    synced = [d for t, d in events if t == "agent.model_synced"]
    assert synced and synced[0]["mode"] == "reload"


def test_reload_never_targets_a_non_agent_container(async_session):
    """Stolperdraht: der Containername kommt NUR aus dem Agenten-Slug.

    Ein ``docker exec`` in einen Modell-Container wäre der Vorfall, den
    ADR-059 abgeschafft hat.
    """
    import subprocess

    agent = Agent(name="agent-a", role="developer", agent_runtime="cli-bridge")
    captured: list[list[str]] = []

    def _fake_run(cmd, **_kw):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", _fake_run):
        result = rp.reload_omp_config(agent)

    assert result["status"] == "reloaded"
    assert captured[0][0:3] == ["docker", "exec", "mc-agent-agent-a"]
    assert captured[0][3] == "render-omp-config.sh"


# ── Die Skripte selbst ───────────────────────────────────────────────────────


def test_render_script_exists_and_is_wired_into_the_image():
    script = DOCKER / "omp-bridge" / "render-omp-config.sh"
    assert script.is_file()
    source = script.read_text(encoding="utf-8")
    # Es rendert BEIDE Dateien — omp.env allein reicht nicht (models.yml ist
    # der Provider, ohne den omp das Modell gar nicht auflöst).
    assert "models.yml" in source and "omp.env" in source
    assert "--no-bootstrap" in source and "--wait" in source

    dockerfile = (DOCKER / "omp-bridge" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY render-omp-config.sh /opt/omp-bridge/render-omp-config.sh" in dockerfile
    # Symlink in PATH: `docker exec` startet keine Login-Shell.
    assert "/usr/local/bin/render-omp-config.sh" in dockerfile


def test_omp_entrypoint_calls_the_renderer_and_no_longer_exits_at_once():
    source = (DOCKER / "omp-bridge" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "/opt/omp-bridge/render-omp-config.sh --no-bootstrap" in source
    assert "render-omp-config.sh --wait 1800" in source
    # Die Vorlage darf es nur noch EINMAL geben (im Renderer), sonst driften
    # Start und Reload auseinander.
    assert "providers:" not in source
    assert "mc-openai:" not in source


def test_base_entrypoint_waits_for_a_model_before_giving_up():
    source = (DOCKER / "mc-agent-base" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "_wait_for_bootstrap_model" in source
    # Der Riegel bleibt: ohne Modell wird am Ende doch nicht gestartet.
    assert "FATAL: OPENAI_MODEL not set" in source
    # Und er kommt NACH dem Warten, nicht davor.
    assert source.index("_wait_for_bootstrap_model()") < source.index(
        "FATAL: OPENAI_MODEL not set"
    )
