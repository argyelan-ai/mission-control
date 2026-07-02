"""ADR-045 — Tests for the `omp` runtime type.

Covers the three routing branch-points + the switch readiness gate:
  1. build_runtime_env(omp)  → OPENAI_BASE_URL + OPENAI_MODEL, NO anthropic auth
  2. pick_image_for_runtime(omp) → mc-omp-agent:latest (image selection)
  3. docker_agent_sync .env writer treats omp as OpenAI-compatible (slug-based
     is_anthropic → False), not anthropic
  4. validate_compatibility: omp runtime (enabled, supports_tools) is switchable
     for a cli-bridge agent — no hard incompatibility, no soft tool warning
  5. switch_agent_runtime openclaude→omp is a CROSS-image switch that passes
     ready_signals=("OMP_BRIDGE_READY",) to wait_for_agent_healthy
  6. wait_for_agent_healthy: ready_signals routes to the pane scrape even when
     respawn_mode is False (the cross-image path), and the sentinel REPLACES the
     default glyph tuple in _wait_for_window_ready
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime


OMP_ENDPOINT = "http://192.0.2.100:8000/v1"
OMP_MODEL = "nvidia/Qwen3.6-35B-A3B-NVFP4"
OMP_IMAGE = "mc-omp-agent:latest"


def _omp_runtime(**over) -> Runtime:
    defaults = dict(
        slug="omp-qwen",
        display_name="omp headless (Qwen)",
        runtime_type="omp",
        endpoint=OMP_ENDPOINT,
        model_identifier=OMP_MODEL,
        enabled=True,
        supports_tools=True,
    )
    defaults.update(over)
    return Runtime(**defaults)


# ── 1. build_runtime_env — OpenAI env, no anthropic ─────────────────────────


@pytest.mark.asyncio
async def test_build_runtime_env_omp_sets_openai_not_anthropic(async_session):
    from app.routers.internal import build_runtime_env

    rt = _omp_runtime()
    with patch(
        "app.routers.internal.get_secret_plaintext_by_key",
        new=AsyncMock(return_value="should-not-leak"),
    ) as mocked:
        env = await build_runtime_env(rt, async_session)

    assert env["OPENAI_BASE_URL"] == OMP_ENDPOINT
    assert env["OPENAI_MODEL"] == OMP_MODEL
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    # The omp branch must not even hit the vault for anthropic keys.
    mocked.assert_not_called()


@pytest.mark.asyncio
async def test_build_runtime_env_omp_disabled_returns_empty(async_session):
    from app.routers.internal import build_runtime_env

    assert await build_runtime_env(_omp_runtime(enabled=False), async_session) == {}


# ── 2. Image selection ──────────────────────────────────────────────────────


def test_pick_image_for_runtime_omp():
    from app.services.compose_renderer import pick_image_for_runtime, OMP_IMAGE as CONST

    assert CONST == OMP_IMAGE
    assert pick_image_for_runtime(_omp_runtime()) == OMP_IMAGE


def test_pick_image_omp_disabled_returns_none():
    from app.services.compose_renderer import pick_image_for_runtime

    assert pick_image_for_runtime(_omp_runtime(enabled=False)) is None


def test_detect_image_change_openclaude_to_omp_is_true():
    from app.services.compose_renderer import detect_image_change

    old = Runtime(
        slug="qwen-coder-lms", display_name="Qwen", runtime_type="lmstudio",
        endpoint="http://x/v1", model_identifier="q", enabled=True,
    )
    assert detect_image_change(old, _omp_runtime()) is True


def test_new_agent_block_uses_omp_anchor():
    from app.services.compose_renderer import _build_new_agent_block

    block = _build_new_agent_block("sparky", OMP_IMAGE, is_vault_writer=False)
    assert "<<: *omp-agent-base" in block
    # Anchor default image == OMP_IMAGE, so NO explicit `image:` line is emitted.
    assert "image: mc-omp-agent:latest" not in block


# ── 3. docker_agent_sync treats omp as OpenAI-compatible (not anthropic) ─────


def test_omp_slug_is_not_anthropic():
    # The .env writer's is_anthropic gate is slug-prefix based; omp-qwen is
    # non-anthropic → takes the OPENAI_BASE_URL/MODEL/KEY branch.
    rt = _omp_runtime()
    is_anthropic = rt.enabled and rt.slug.startswith("anthropic-claude-")
    assert is_anthropic is False


# ── 4. Compatibility / switchability ────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_compatibility_omp_switchable_for_cli_bridge(async_session):
    from app.services.agent_runtime_switch import validate_compatibility

    rt = _omp_runtime()
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    agent = Agent(name=f"sparky-{uuid.uuid4().hex[:6]}", agent_runtime="cli-bridge")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    # No hard incompatibility (enabled) and no soft tool warning (supports_tools).
    warnings = await validate_compatibility(async_session, agent, rt)
    assert warnings == []


@pytest.mark.asyncio
async def test_validate_compatibility_omp_disabled_raises(async_session):
    from app.services.agent_runtime_switch import (
        RuntimeIncompatibleError,
        validate_compatibility,
    )

    rt = _omp_runtime(slug="omp-qwen-off", enabled=False)
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    agent = Agent(name=f"a-{uuid.uuid4().hex[:6]}", agent_runtime="cli-bridge")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    with pytest.raises(RuntimeIncompatibleError):
        await validate_compatibility(async_session, agent, rt)


# ── 5. Switch openclaude→omp passes the OMP_BRIDGE_READY sentinel ───────────


@pytest.fixture
def _patched_redis(fake_redis):
    async def _get():
        return fake_redis
    with patch("app.services.agent_runtime_switch.get_redis", _get), \
         patch("app.services.sse.get_redis", _get), \
         patch("app.redis_client.get_redis", _get):
        yield fake_redis


@pytest.mark.asyncio
async def test_switch_to_omp_is_cross_image_and_passes_ready_signals(
    async_session, _patched_redis
):
    from app.services.agent_runtime_switch import switch_agent_runtime

    old = Runtime(
        slug="qwen-coder-lms", display_name="Qwen LMS", runtime_type="lmstudio",
        endpoint="http://x/v1", model_identifier="q", enabled=True, supports_tools=True,
    )
    new = _omp_runtime()
    async_session.add(old)
    async_session.add(new)
    await async_session.commit()
    await async_session.refresh(old)
    await async_session.refresh(new)

    agent = Agent(
        name=f"sparky-{uuid.uuid4().hex[:6]}", agent_runtime="cli-bridge",
        runtime_id=old.id, cli_plugins=[],
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    health_mock = AsyncMock(return_value={"healthy": True, "reason": "sentinel ok"})
    restart = lambda a, *, force_recreate=False, respawn_window_only=False: {
        "status": "recreated", "container": "x",
        "mode": "recreate" if force_recreate else "restart",
    }
    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=restart), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", health_mock), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "true"})):
        result = await switch_agent_runtime(async_session, agent, new.id)

    assert result.image_switched is True  # openclaude -> omp = cross-image
    assert result.new_runtime["slug"] == "omp-qwen"
    # The health check for an omp target MUST anchor on the sentinel.
    _, kwargs = health_mock.await_args
    assert kwargs.get("ready_signals") == ("OMP_BRIDGE_READY",)
    assert kwargs.get("respawn_mode") is False  # cross-image path


# ── 6. wait_for_agent_healthy readiness routing ─────────────────────────────


@pytest.mark.asyncio
async def test_wait_for_agent_healthy_ready_signals_routes_to_pane_scrape():
    from app.services import docker_agent_sync as das

    agent = Agent(name="omp-agent", agent_runtime="cli-bridge")
    captured: dict = {}

    async def _fake_window_ready(a, *, timeout, poll_interval, ready_signals=None):
        captured["ready_signals"] = ready_signals
        return {"healthy": True, "reason": "sentinel"}

    with patch.object(das, "_wait_for_window_ready", _fake_window_ready):
        # respawn_mode False but ready_signals provided → still pane scrape.
        res = await das.wait_for_agent_healthy(
            agent, timeout=1, respawn_mode=False, ready_signals=("OMP_BRIDGE_READY",),
        )

    assert res["healthy"] is True
    assert captured["ready_signals"] == ("OMP_BRIDGE_READY",)


@pytest.mark.asyncio
async def test_wait_for_window_ready_sentinel_replaces_default_glyphs():
    """The sentinel must REPLACE the default glyph tuple — a pane showing only a
    bash `$ ` prompt (bridge logs) must NOT be considered ready for omp."""
    from app.services import docker_agent_sync as das

    agent = Agent(name="omp-agent", agent_runtime="cli-bridge")

    class _Proc:
        def __init__(self, out):
            self.stdout = out

    # Pane shows a shell prompt + bridge log noise, but NOT the sentinel.
    def _fake_run(cmd, capture_output, text, timeout):
        return _Proc("agent@omp:~$ [serve] poll error: TimeoutError\n")

    with patch("subprocess.run", _fake_run):
        res = await das._wait_for_window_ready(
            agent, timeout=1, poll_interval=0.01, ready_signals=("OMP_BRIDGE_READY",),
        )
    # Times out — sentinel never appeared, default '$ ' glyph is NOT accepted.
    assert res["healthy"] is False
