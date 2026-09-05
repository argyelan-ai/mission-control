"""Live-Befund 05.09.2026 — der Runtime-Wechsel eines omp-Agenten scheiterte
zweimal am Health-Check („timeout after 30s — window not ready"), obwohl der
Container gesund war.

Grund: der omp-TUI braucht nach einem Neustart laenger als 30 s, bis die
Prompt-Glyphen in Fenster 0 stehen. Die kurze Neustart-Frist galt aber fuer
alle Harnesses gleich. Fuer omp gilt darum jetzt dieselbe lange Frist wie beim
Neuerstellen (90 s). Die Pruefung endet ohnehin, sobald die Glyphen da sind —
die Frist ist nur die Obergrenze, kein Warten auf Vorrat.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime


@pytest.fixture(autouse=True)
def _patched_redis(fake_redis):
    async def _get():
        return fake_redis
    with patch("app.services.agent_runtime_switch.get_redis", _get), \
         patch("app.services.sse.get_redis", _get), \
         patch("app.redis_client.get_redis", _get):
        yield fake_redis


def _restart_ok(agent, *, force_recreate=False, respawn_window_only=False):
    return {
        "status": "recreated" if force_recreate else "restarted",
        "container": "x",
        "mode": "recreate" if force_recreate else "restart",
    }


async def _mk_pair(session, runtime_type: str, slug_a: str, slug_b: str):
    rows = []
    for slug in (slug_a, slug_b):
        rt = Runtime(
            slug=slug, display_name=f"RT {slug}", runtime_type=runtime_type,
            endpoint="http://192.0.2.20:8000/v1", model_identifier=f"model-{slug}",
            enabled=True, supports_tools=True,
        )
        session.add(rt)
        rows.append(rt)
    await session.commit()
    for rt in rows:
        await session.refresh(rt)

    agent = Agent(
        name=f"a-{uuid.uuid4().hex[:6]}", agent_runtime="cli-bridge",
        runtime_id=rows[0].id, cli_plugins=[],
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return rows[0], rows[1], agent


async def _switch_and_capture(session, agent, new_id):
    from app.services.agent_runtime_switch import switch_agent_runtime

    health = AsyncMock(return_value={"healthy": True, "reason": "ok"})
    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=_restart_ok), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", health), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "false"})):
        result = await switch_agent_runtime(session, agent, new_id)
    return result, health.await_args.kwargs


@pytest.mark.asyncio
async def test_omp_same_image_switch_gets_long_health_timeout(async_session):
    """omp -> omp ist ein Neustart im gleichen Image — trotzdem 90 s Frist."""
    _old, new, agent = await _mk_pair(async_session, "omp", "omp-a", "omp-b")

    result, kwargs = await _switch_and_capture(async_session, agent, new.id)

    assert result.image_switched is False  # gleiches Image => Neustart-Pfad
    assert kwargs.get("ready_signals") == ("╭─", "❯")
    assert kwargs.get("timeout") == 90  # HEALTH_TIMEOUT_RESTART_OMP


@pytest.mark.asyncio
async def test_non_omp_same_image_switch_keeps_short_timeout(async_session):
    """Gegenprobe: alle anderen Harnesses behalten die kurze Neustart-Frist."""
    _old, new, agent = await _mk_pair(async_session, "lmstudio", "lms-a", "lms-b")

    result, kwargs = await _switch_and_capture(async_session, agent, new.id)

    assert result.image_switched is False
    assert kwargs.get("ready_signals") is None
    assert kwargs.get("timeout") == 30  # HEALTH_TIMEOUT_RESTART
