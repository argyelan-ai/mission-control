"""Regression tests for compose_renderer.py — Phase 24-09.

Goal: Lock in that `write_compose_agents()` / `render_compose_agents()` ONLY
operate on `agent_runtime == "cli-bridge"` agents. Host-side agents (Boss,
Hermes, Sparky-if-host) must never leak into the rendered compose YAML.

Threat covered: T-24-60 (Hermes leaks into compose → unwanted container
spawn → port conflict + workspace corruption).

Architectural note: compose_renderer.py works as an *overlay* — it reads the
existing static compose file as a baseline and only rewrites `image:` lines
for agents that are present in the DB AND have `agent_runtime='cli-bridge'`
AND a non-null runtime_id. Service blocks NOT present in the DB query result
(e.g. host-runtime agents) are never touched / never injected by the renderer.
That means: as long as the static `docker-compose.agents.yml` does not contain
a hermes service block (it doesn't — host-side agents are spawned via
launchd / host-side scripts), Hermes cannot reach the compose YAML by way of
the renderer. These tests assert that DB-state alone (i.e. `agent_runtime`
filter) is the gate.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.compose_renderer import (
    OPENCLAUDE_IMAGE,
    render_compose_agents,
    write_compose_agents,
)


# ── Redis patch for write_compose_agents ──────────────────────────────────────
@pytest.fixture(autouse=True)
def _patch_compose_redis(fake_redis):
    async def _get_redis():
        return fake_redis
    with patch("app.services.compose_renderer.get_redis", _get_redis):
        yield


COMPOSE_FIXTURE = """\
# docker/docker-compose.agents.yml — test fixture (mirror of real layout)

x-claude-agent-base: &claude-agent-base
  image: mc-claude-agent:latest
  restart: unless-stopped

x-openclaude-agent-base: &openclaude-agent-base
  image: mc-agent-base:latest
  restart: unless-stopped

services:
  mc-agent-davinci:
    <<: *claude-agent-base
    container_name: mc-agent-davinci
    environment:
      - AGENT_NAME=davinci

  mc-agent-rex:
    <<: *claude-agent-base
    container_name: mc-agent-rex
    environment:
      - AGENT_NAME=rex

  mc-agent-sparky:
    <<: *openclaude-agent-base
    container_name: mc-agent-sparky
    environment:
      - AGENT_NAME=sparky

networks:
  mission-control_default:
    external: true
"""


@pytest.fixture
def compose_path(tmp_path: Path) -> Path:
    p = tmp_path / "docker-compose.agents.yml"
    p.write_text(COMPOSE_FIXTURE, encoding="utf-8")
    return p


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hermes_excluded_from_compose(async_session, compose_path):
    """Hermes (agent_runtime='host') must NOT cause any change to the rendered
    compose YAML. Even though Hermes is in the DB, the renderer's WHERE clause
    filters it out, so no override is generated for a `hermes` service block.

    We assert:
      - The rendered output contains no 'hermes' string.
      - The cli-bridge agent (Davinci with vllm runtime) IS rendered (regression).
    """
    rt = Runtime(
        slug="qwen-general",
        display_name="Qwen 3.6",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    # Hermes — host-side agent. Has agent_runtime='host'.
    hermes = Agent(
        name="Hermes",
        agent_runtime="host",
        runtime_id=None,
    )
    # Davinci — cli-bridge, switched to vllm runtime.
    davinci = Agent(
        name="Davinci",
        agent_runtime="cli-bridge",
        runtime_id=rt.id,
    )
    async_session.add_all([hermes, davinci])
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    # Hermes must not appear ANYWHERE in the rendered output.
    assert "hermes" not in rendered.lower(), (
        "Hermes (agent_runtime='host') leaked into rendered compose YAML — "
        "the agent_runtime filter is broken or has been removed."
    )

    # Sanity: Davinci's vllm override IS applied (regression for cli-bridge path).
    parsed = yaml.safe_load(rendered)
    services = parsed["services"]
    assert services["mc-agent-davinci"].get("image") == OPENCLAUDE_IMAGE


@pytest.mark.asyncio
async def test_boss_excluded_from_compose(async_session, compose_path):
    """Boss (host-runtime) must also be excluded. Regression for any pre-existing
    host-agent exclusion behavior — guards against future refactors that might
    convert the agent_runtime filter into a slug-blocklist (which would miss
    new host-side agents like Hermes)."""
    boss = Agent(
        name="Boss",
        agent_runtime="host",
        runtime_id=None,
        is_board_lead=True,
    )
    async_session.add(boss)
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    # Boss is not in the static fixture either — it must not be injected.
    parsed = yaml.safe_load(rendered)
    services = parsed.get("services", {})
    assert "mc-agent-boss" not in services
    # env_file hardening (ADR-051) is applied even with no cli-bridge overrides,
    # so the output is no longer byte-identical to the static fixture — that is
    # intentional and correct.  The primary assertion is that Boss is absent.


@pytest.mark.asyncio
async def test_only_cli_bridge_agents_rendered(async_session, compose_path):
    """Mix of cli-bridge + host + openclaw agents → only cli-bridge agents
    contribute image overrides. Asserts the agent_runtime filter is exhaustive
    against all non-cli-bridge runtime values."""
    rt = Runtime(
        slug="qwen-general",
        display_name="Qwen 3.6",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    # Two host-runtime agents (Hermes, Boss).
    hermes = Agent(name="Hermes", agent_runtime="host", runtime_id=None)
    boss = Agent(name="Boss", agent_runtime="host", runtime_id=None, is_board_lead=True)
    # One openclaw-runtime agent (Henry).
    henry = Agent(name="Henry", agent_runtime="openclaw", runtime_id=None)
    # One cli-bridge agent with cross-image switch (Davinci → vllm).
    davinci = Agent(name="Davinci", agent_runtime="cli-bridge", runtime_id=rt.id)

    async_session.add_all([hermes, boss, henry, davinci])
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    parsed = yaml.safe_load(rendered)
    services = parsed["services"]

    # Only the static-file services exist — no host/openclaw injection.
    expected_services = {"mc-agent-davinci", "mc-agent-rex", "mc-agent-sparky"}
    assert set(services.keys()) == expected_services

    # Only Davinci got an explicit image override.
    assert services["mc-agent-davinci"].get("image") == OPENCLAUDE_IMAGE

    # Names / runtimes of host-side agents must not appear in the YAML.
    rendered_lower = rendered.lower()
    assert "hermes" not in rendered_lower
    assert "boss" not in rendered_lower
    assert "henry" not in rendered_lower


@pytest.mark.asyncio
async def test_empty_db_renders_minimal_compose(async_session, compose_path):
    """No agents in DB → renderer returns the static compose file unchanged.
    Covers T-24-61: empty compose YAML must not break docker startup."""
    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    # Output is valid YAML.
    parsed = yaml.safe_load(rendered)
    assert parsed is not None
    assert "services" in parsed
    # The static service blocks survive (renderer doesn't strip them).
    assert "mc-agent-davinci" in parsed["services"]
    # env_file hardening (ADR-051) is applied even with an empty DB, so output
    # is not byte-identical to source — that is expected and correct.


@pytest.mark.asyncio
async def test_write_compose_with_hermes_in_db_does_not_leak(
    async_session, compose_path
):
    """End-to-end via write_compose_agents (atomic write path): Hermes in DB
    must not appear in the file written to disk. This is the production code
    path — the smoke test in the plan's verification section."""
    rt = Runtime(
        slug="qwen-general",
        display_name="Qwen 3.6",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    hermes = Agent(name="Hermes", agent_runtime="host", runtime_id=None)
    davinci = Agent(name="Davinci", agent_runtime="cli-bridge", runtime_id=rt.id)
    async_session.add_all([hermes, davinci])
    await async_session.commit()

    result = await write_compose_agents(async_session, compose_path=compose_path)
    assert result["changed"] == "true"

    written = compose_path.read_text(encoding="utf-8")
    assert "hermes" not in written.lower(), (
        "Hermes leaked into written docker-compose.agents.yml — check "
        "compose_renderer.render_compose_agents() agent_runtime filter."
    )
    # Sanity: Davinci's override landed on disk.
    assert OPENCLAUDE_IMAGE in written
