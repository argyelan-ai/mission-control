"""Der Reload muss das Modell MITBRINGEN (Nachlese zu ADR-078, 06.09.2026).

Live-Befund (Slot-Runtime, PR #430/#432): nach einem Rezeptwechsel meldete der
Sync für jeden omp-Agenten

    omp reload for <Agent> not possible (error: [render-omp-config] kein Modell
    bekannt (OPENAI_BASE_URL/OPENAI_MODEL leer)) — falling back to restart

und danach ``model sync for <Agent> failed (8/3): health check failed: timeout
after 60s``. Ursache: ``docker exec <container> render-omp-config.sh`` OHNE
Werte liess das Skript den Bootstrap rufen — und dessen ``curl`` schickte den
``Authorization: Bearer <INTERNAL_BOOTSTRAP_SECRET>`` nicht mit, den
``internal.agent_bootstrap`` verlangt (HTTP 401). Also blieben die Werte leer.

Abgesichert wird — jede Prüfung mit Sabotage-Probe (Gegenfall, der ohne die
neue Regel durchginge):

  * der Exec bringt die Werte aus ``build_runtime_env`` als ``-e`` mit und
    ruft ``--no-bootstrap`` · Gegenfall: fehlt das Modell, wird gar nicht
    execed,
  * die Werte kommen aus DERSELBEN Quelle wie beim Container-Start (keine
    zweite Wahrheit) · Gegenfall: ein anderes Modell in der Zeile ⇒ anderer
    ``-e``-Wert,
  * der Rückfall-Neustart wartet für omp 90 s wie der Umschalter · Gegenfall:
    ein anderer Harness behält 60 s,
  * ein aufgegebener Agent wird nicht in jeder Runde neu gestartet ·
    Gegenfall: ohne Sperrfrist startet dieselbe Runde neu,
  * das Skript selbst schickt den Bootstrap-Schlüssel mit, nennt den
    HTTP-Code und behält einen vorhandenen API-Schlüssel.

Testdaten heissen box-a / recipe-x / agent-a. Kein Docker, kein Netz.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis
from app.services import runtime_propagation as rp

DOCKER = Path(__file__).resolve().parents[2] / "docker"


async def _runtime(session, **kw) -> Runtime:
    fields = dict(
        slug="box-a-slot",
        display_name="BOX-A :8000",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="org/recipe-x",
        max_context_len=131072,
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
        model="org/altes-rezept",
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


# ── 1. Der Exec bringt die Werte mit ─────────────────────────────────────────


def test_exec_carries_the_values_and_skips_the_bootstrap():
    agent = Agent(name="agent-a", role="developer", agent_runtime="cli-bridge")
    captured: list[list[str]] = []

    def _fake_run(cmd, **_kw):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    env = {
        "OPENAI_BASE_URL": "http://192.0.2.10:8000/v1",
        "OPENAI_MODEL": "org/recipe-x",
        "OMP_CONTEXT_WINDOW": "131072",
    }
    with patch("subprocess.run", _fake_run):
        result = rp.reload_omp_config(agent, env)

    assert result["status"] == "reloaded"
    cmd = captured[0]
    assert cmd[0:2] == ["docker", "exec"]
    # Der Containername kommt weiterhin NUR aus dem Agenten-Slug (ADR-059).
    assert cmd[cmd.index(rp._OMP_RENDER_SCRIPT) - 1] == "mc-agent-agent-a"
    assert cmd[-1] == "--no-bootstrap"
    for key, value in env.items():
        assert "-e" in cmd and f"{key}={value}" in cmd


def test_without_a_model_nothing_is_execed():
    """Sabotage: leere Zeile ⇒ ehrlicher Fehler statt Exec ins Blaue."""
    agent = Agent(name="agent-a", role="developer", agent_runtime="cli-bridge")
    run_mock = MagicMock()
    with patch("subprocess.run", run_mock):
        result = rp.reload_omp_config(agent, {"OPENAI_BASE_URL": "http://x/v1"})
    assert result["status"].startswith("error")
    assert "OPENAI_MODEL" in result["status"]
    assert run_mock.call_count == 0


# ── 2. Eine Quelle: build_runtime_env ────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_passes_the_bootstrap_values_to_the_reload(async_session):
    rt = await _runtime(async_session)
    agent = await _agent(async_session, rt)

    reload_mock = MagicMock(return_value={"status": "reloaded"})
    restart_mock = MagicMock(return_value={"status": "restarted"})
    p1, p2, p3 = _lock_patches()
    with p1, p2, p3, \
         patch.object(rp, "reload_omp_config", reload_mock), \
         patch.object(rp, "restart_docker_agent_container", restart_mock):
        await rp._sync_one(async_session, agent)

    assert restart_mock.call_count == 0
    passed_env = reload_mock.call_args[0][1]
    from app.routers.internal import build_runtime_env

    assert passed_env == await build_runtime_env(rt, async_session, agent=agent)
    assert passed_env["OPENAI_BASE_URL"] == "http://192.0.2.10:8000/v1"
    assert passed_env["OPENAI_MODEL"] == "org/recipe-x"
    assert passed_env["OMP_CONTEXT_WINDOW"] == "131072"


@pytest.mark.asyncio
async def test_a_different_row_yields_different_values(async_session):
    """Gegenfall: das Modell kommt aus der Zeile, nicht aus einer Kopie."""
    rt = await _runtime(
        async_session,
        model_identifier="org/recipe-y",
        endpoint="http://192.0.2.11:9000/v1",
    )
    agent = await _agent(async_session, rt)

    reload_mock = MagicMock(return_value={"status": "reloaded"})
    p1, p2, p3 = _lock_patches()
    with p1, p2, p3, patch.object(rp, "reload_omp_config", reload_mock):
        await rp._sync_one(async_session, agent)

    passed_env = reload_mock.call_args[0][1]
    assert passed_env["OPENAI_MODEL"] == "org/recipe-y"
    assert passed_env["OPENAI_BASE_URL"] == "http://192.0.2.11:9000/v1"


# ── 3. Rückfall-Neustart: Frist wie beim Umschalter ──────────────────────────


@pytest.mark.asyncio
async def test_fallback_restart_waits_as_long_as_the_switch(async_session):
    from app.services.agent_runtime_switch import HEALTH_TIMEOUT_RESTART_OMP

    rt = await _runtime(async_session)
    agent = await _agent(async_session, rt)

    health_mock = AsyncMock(return_value={"healthy": True})
    p1, p2, p3 = _lock_patches()
    with p1, p2, p3, \
         patch.object(rp, "reload_omp_config",
                      MagicMock(return_value={"status": "error: kaputt"})), \
         patch.object(rp, "restart_docker_agent_container",
                      MagicMock(return_value={"status": "restarted"})), \
         patch.object(rp, "wait_for_agent_healthy", health_mock):
        await rp._sync_one(async_session, agent)

    assert health_mock.await_args.kwargs["timeout"] == HEALTH_TIMEOUT_RESTART_OMP
    assert HEALTH_TIMEOUT_RESTART_OMP == 90


@pytest.mark.asyncio
async def test_other_harnesses_keep_the_short_deadline(async_session):
    """Gegenfall: nur omp braucht die längere Frist (TUI-Glyphen)."""
    rt = await _runtime(async_session)
    agent = await _agent(async_session, rt, harness="openclaude")

    health_mock = AsyncMock(return_value={"healthy": True})
    p1, p2, p3 = _lock_patches()
    with p1, p2, p3, \
         patch.object(rp, "restart_docker_agent_container",
                      MagicMock(return_value={"status": "restarted"})), \
         patch.object(rp, "wait_for_agent_healthy", health_mock):
        await rp._sync_one(async_session, agent)

    assert health_mock.await_args.kwargs["timeout"] == 60


# ── 4. Kein Neustart-Karussell nach dem Aufgeben ─────────────────────────────


@pytest.mark.asyncio
async def test_giving_up_starts_a_cooldown(async_session):
    rt = await _runtime(async_session)
    agent = await _agent(async_session, rt)
    redis = await get_redis()
    await redis.set(
        RedisKeys.agent_model_sync_fails(str(agent.id)), rp.MAX_SYNC_ATTEMPTS - 1
    )

    p1, p2, p3 = _lock_patches()
    with p1, p2, p3, \
         patch.object(rp, "reload_omp_config",
                      MagicMock(return_value={"status": "error: kaputt"})), \
         patch.object(rp, "restart_docker_agent_container",
                      MagicMock(return_value={"status": "restarted"})), \
         patch.object(rp, "wait_for_agent_healthy",
                      AsyncMock(return_value={"healthy": False, "reason": "timeout"})):
        await rp._sync_one(async_session, agent)

    assert await redis.exists(RedisKeys.agent_model_sync_giveup(str(agent.id)))


@pytest.mark.asyncio
async def test_cooldown_blocks_the_next_round(async_session):
    """Der Live-Befund „8/3": jede Runde flaggte neu, jede Runde ein Neustart."""
    rt = await _runtime(async_session)
    agent = await _agent(async_session, rt)
    redis = await get_redis()
    await redis.set(RedisKeys.agent_model_sync_giveup(str(agent.id)), "1")

    reload_mock = MagicMock(return_value={"status": "error: kaputt"})
    restart_mock = MagicMock(return_value={"status": "restarted"})
    p1, p2, p3 = _lock_patches()
    with p1, p2, p3, \
         patch.object(rp, "reload_omp_config", reload_mock), \
         patch.object(rp, "restart_docker_agent_container", restart_mock), \
         patch.object(rp, "wait_for_agent_healthy",
                      AsyncMock(return_value={"healthy": True})):
        await rp._sync_one(async_session, agent)

    assert restart_mock.call_count == 0
    assert reload_mock.call_count == 0
    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is False


@pytest.mark.asyncio
async def test_without_the_cooldown_the_round_runs(async_session):
    """Sabotage-Gegenprobe: ohne Sperrfrist wird sehr wohl neu gestartet."""
    rt = await _runtime(async_session)
    agent = await _agent(async_session, rt)

    restart_mock = MagicMock(return_value={"status": "restarted"})
    p1, p2, p3 = _lock_patches()
    with p1, p2, p3, \
         patch.object(rp, "reload_omp_config",
                      MagicMock(return_value={"status": "error: kaputt"})), \
         patch.object(rp, "restart_docker_agent_container", restart_mock), \
         patch.object(rp, "wait_for_agent_healthy",
                      AsyncMock(return_value={"healthy": True})):
        await rp._sync_one(async_session, agent)

    assert restart_mock.call_count == 1


@pytest.mark.asyncio
async def test_a_forced_sync_lifts_the_cooldown(async_session):
    """Marks Knopf „Sync erzwingen" muss immer durchkommen."""
    rt = await _runtime(async_session)
    agent = await _agent(async_session, rt)
    redis = await get_redis()
    await redis.set(RedisKeys.agent_model_sync_giveup(str(agent.id)), "1")

    reload_mock = MagicMock(return_value={"status": "reloaded"})
    p1, p2, p3 = _lock_patches()
    with p1, p2, p3, patch.object(rp, "reload_omp_config", reload_mock):
        await rp.sync_pending_agents(async_session, force=True, runtime_id=rt.id)

    assert reload_mock.call_count == 1
    assert not await redis.exists(RedisKeys.agent_model_sync_giveup(str(agent.id)))


# ── 5. Das Skript selbst ─────────────────────────────────────────────────────


def test_render_script_sends_the_bootstrap_secret_and_names_the_http_code():
    source = (DOCKER / "omp-bridge" / "render-omp-config.sh").read_text(
        encoding="utf-8"
    )
    assert "Authorization: Bearer ${INTERNAL_BOOTSTRAP_SECRET:-}" in source
    # Ohne HTTP-Code im Fehlertext hiess ein 401 nur „kein Modell bekannt".
    assert "http_code" in source
    assert "bootstrap HTTP" in source


def test_the_test_suite_cannot_touch_real_containers():
    """Der Vorfall selbst als Prüfung (06.09.2026).

    Ein Test, der ``reload_omp_config`` NICHT ersetzt, lief auf dem Dev-Rechner
    in einen echten, gleichnamigen Agenten-Container und schrieb dessen
    ``omp.env`` mit Testwerten um. Seitdem blockt ``conftest.block_real_docker``
    jeden handelnden Docker-Aufruf.
    """
    with pytest.raises(AssertionError, match="Docker"):
        subprocess.run(["docker", "exec", "mc-agent-x", "true"], capture_output=True)
    # Gegenprobe: reines Auflösen von YAML (kein Daemon) bleibt erlaubt, und
    # Nicht-Docker-Aufrufe sowieso.
    assert subprocess.run(["true"]).returncode == 0


def test_render_script_keeps_an_existing_api_key():
    """``docker exec`` sieht den zur Laufzeit gesetzten Schlüssel nicht —
    ein Reload darf die laufende Anmeldung trotzdem nicht wegwerfen."""
    source = (DOCKER / "omp-bridge" / "render-omp-config.sh").read_text(
        encoding="utf-8"
    )
    assert "OPENAI_API_KEY=" in source
    assert "sed -n" in source and "$OMP_ENV_FILE" in source
