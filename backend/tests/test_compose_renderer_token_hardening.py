"""Tests for MC_TOKEN hardening in compose_renderer.py (ADR-051).

Verifies:
1. Every agent service in the rendered output has ``env_file`` containing
   ``docker/.env.agents`` so MC_TOKEN_<NAME> vars reach the container even
   without explicit ``--env-file`` at compose-up time.
2. The backward-compatible ``MC_TOKEN=${MC_TOKEN_<NAME>}`` env var is still
   emitted (parse-time interpolation path still works when --env-file IS passed).
3. New agents appended by the renderer (not in static template) also carry both.
4. The injection is idempotent — running the renderer twice does not duplicate
   the env_file entry.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.compose_renderer import render_compose_agents


# ── Redis patch (same pattern as test_compose_renderer.py) ────────────────────
@pytest.fixture(autouse=True)
def _patch_compose_redis(fake_redis):
    async def _get_redis():
        return fake_redis
    with patch("app.services.compose_renderer.get_redis", _get_redis):
        yield


# ── Compose fixture with representative services ──────────────────────────────
# Mirrors the real file layout: anchor blocks + two existing services + networks.
COMPOSE_FIXTURE = """\
x-claude-agent-base: &claude-agent-base
  image: mc-claude-agent:latest
  restart: unless-stopped
  env_file:
    - docker/.env.shared

x-openclaude-agent-base: &openclaude-agent-base
  image: mc-agent-base:latest
  restart: unless-stopped
  env_file:
    - docker/.env.shared

services:
  mc-agent-rex:
    <<: *claude-agent-base
    container_name: mc-agent-rex
    environment:
      - AGENT_NAME=rex
      - MC_API_URL=${MC_API_URL:-http://backend:8000}
      - MC_TOKEN=${MC_TOKEN_REX}
      - AGENT_VAULT_PATH=/vault/agents/rex
      - AGENT_VAULT_INBOX=/vault/_inbox
      - AGENT_SLUG=rex
    volumes:
      - ${HOME}/.mc/agents/rex/claude-config:/home/agent/.claude
      - ${HOME}/.mc/vault:/vault:rw

  mc-agent-sparky:
    <<: *openclaude-agent-base
    container_name: mc-agent-sparky
    environment:
      - AGENT_NAME=sparky
      - MC_API_URL=${MC_API_URL:-http://backend:8000}
      - MC_TOKEN=${MC_TOKEN_SPARKY}
      - AGENT_VAULT_PATH=/vault/agents/sparky
      - AGENT_VAULT_INBOX=/vault/_inbox
      - AGENT_SLUG=sparky
    volumes:
      - ${HOME}/.mc/agents/sparky/claude-config:/home/agent/.claude

networks:
  mission-control_default:
    external: true
"""


@pytest.fixture
def compose_path(tmp_path: Path) -> Path:
    p = tmp_path / "docker-compose.agents.yml"
    p.write_text(COMPOSE_FIXTURE, encoding="utf-8")
    return p


# ── Helpers ───────────────────────────────────────────────────────────────────

def _env_file_list(service_def: dict) -> list[str]:
    """Return the env_file entries for a service as a list of strings."""
    ef = service_def.get("env_file", [])
    if isinstance(ef, str):
        return [ef]
    return list(ef)


def _env_list(service_def: dict) -> list[str]:
    """Return the environment entries for a service as a list of strings."""
    env = service_def.get("environment", [])
    if isinstance(env, list):
        return env
    # dict form: convert to KEY=VALUE strings
    return [f"{k}={v}" for k, v in env.items()]


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_existing_services_get_env_file_agents(async_session, compose_path):
    """Every service already in the static template must have env_file including
    docker/.env.agents after the renderer runs."""
    # No agents in DB → renderer runs purely for env_file injection.
    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    parsed = yaml.safe_load(rendered)
    services = parsed["services"]

    for svc_name, svc_def in services.items():
        ef = _env_file_list(svc_def)
        assert any(".env.agents" in entry for entry in ef), (
            f"Service {svc_name} missing docker/.env.agents in env_file: {ef}"
        )


@pytest.mark.asyncio
async def test_existing_services_retain_env_shared(async_session, compose_path):
    """Regression guard: injecting a service-level env_file REPLACES the anchor's
    env_file in YAML merge semantics.  The renderer must therefore repeat
    docker/.env.shared alongside docker/.env.agents — otherwise every agent
    silently loses CLAUDE_CODE_OAUTH_TOKEN / GH_TOKEN / TAVILY_API_KEY (a worse
    incident than the blank MC_TOKEN this PR fixes)."""
    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    parsed = yaml.safe_load(rendered)
    services = parsed["services"]

    for svc_name, svc_def in services.items():
        ef = _env_file_list(svc_def)
        assert any(".env.shared" in entry for entry in ef), (
            f"Service {svc_name} lost docker/.env.shared after env_file injection "
            f"(YAML merge override bug): {ef}"
        )
        assert any(".env.agents" in entry for entry in ef), (
            f"Service {svc_name} missing docker/.env.agents in env_file: {ef}"
        )


@pytest.mark.asyncio
async def test_existing_services_retain_mc_token_env_var(async_session, compose_path):
    """Backward-compat: MC_TOKEN=${MC_TOKEN_<NAME>} interpolation line must be
    preserved in existing services (canonical --env-file path still works)."""
    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    parsed = yaml.safe_load(rendered)
    services = parsed["services"]

    for svc_name, svc_def in services.items():
        env = _env_list(svc_def)
        # The compose-interpolation line ${MC_TOKEN_REX} etc. is present.
        mc_token_entries = [e for e in env if "MC_TOKEN" in str(e) and "AGENT" not in str(e) and "PATH" not in str(e) and "INBOX" not in str(e)]
        assert mc_token_entries, (
            f"Service {svc_name} lost its MC_TOKEN env entry. env={env}"
        )


@pytest.mark.asyncio
async def test_new_agent_block_includes_env_file_agents(async_session, compose_path):
    """A new cli-bridge agent not in the static template should get env_file
    including docker/.env.agents in its generated service block."""
    newbie = Agent(
        name="Newbie",
        agent_runtime="cli-bridge",
    )
    async_session.add(newbie)
    await async_session.commit()
    await async_session.refresh(newbie)

    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    parsed = yaml.safe_load(rendered)
    services = parsed["services"]

    assert "mc-agent-newbie" in services, "New agent block not appended"
    ef = _env_file_list(services["mc-agent-newbie"])
    assert any(".env.agents" in entry for entry in ef), (
        f"New agent block missing docker/.env.agents in env_file: {ef}"
    )


@pytest.mark.asyncio
async def test_new_agent_block_has_mc_token_env_var(async_session, compose_path):
    """New agent blocks must still emit MC_TOKEN=${MC_TOKEN_<NAME>} for the
    canonical --env-file path."""
    newbie = Agent(
        name="Newbie",
        agent_runtime="cli-bridge",
    )
    async_session.add(newbie)
    await async_session.commit()
    await async_session.refresh(newbie)

    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    parsed = yaml.safe_load(rendered)
    services = parsed["services"]

    env = _env_list(services["mc-agent-newbie"])
    assert any("MC_TOKEN" in str(e) for e in env), (
        f"New agent block missing MC_TOKEN env entry. env={env}"
    )


@pytest.mark.asyncio
async def test_env_file_injection_is_idempotent(async_session, compose_path):
    """Running the renderer twice must not duplicate the .env.agents entry."""
    # First pass
    first = await render_compose_agents(async_session, compose_path=compose_path)
    # Write the first output as the new "static" file and re-render.
    compose_path.write_text(first, encoding="utf-8")
    second = await render_compose_agents(async_session, compose_path=compose_path)

    parsed = yaml.safe_load(second)
    services = parsed["services"]

    for svc_name, svc_def in services.items():
        ef = _env_file_list(svc_def)
        agents_entries = [e for e in ef if ".env.agents" in e]
        assert len(agents_entries) == 1, (
            f"Service {svc_name} has {len(agents_entries)} .env.agents entries "
            f"after double-render (expected 1): {ef}"
        )
