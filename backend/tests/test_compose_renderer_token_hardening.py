"""Tests for MC_TOKEN handling in compose_renderer.py.

Security invariant (fix/agent-token-env-file-leak): ``docker/.env.agents``
holds the tokens of EVERY agent.  Mounting it as ``env_file`` handed each
container all 15 ``MC_TOKEN_<NAME>`` secrets in plain text — a compromised
agent could impersonate any other.  The renderer must therefore

1. never emit ``docker/.env.agents`` as ``env_file`` for any agent service,
2. strip a lingering entry from files rendered by older versions,
3. keep ``docker/.env.shared`` when a service-level ``env_file`` remains
   (YAML merge: a service-level list replaces the anchor list),
4. keep the ``MC_TOKEN=${MC_TOKEN_<NAME>}`` interpolation line — the only
   token path left; every compose caller passes ``--env-file docker/.env.agents``
   (cli_terminal.py, docker_agent_sync.py, start-all.sh).
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

  mc-agent-legacy:
    <<: *claude-agent-base
    container_name: mc-agent-legacy
    env_file:
      - docker/.env.shared
      - docker/.env.agents
    environment:
      - AGENT_NAME=legacy
      - MC_API_URL=${MC_API_URL:-http://backend:8000}
      - MC_TOKEN=${MC_TOKEN_LEGACY}
      - AGENT_SLUG=legacy

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
async def test_no_service_mounts_env_agents(async_session, compose_path):
    """No agent service may carry docker/.env.agents in env_file — that file
    contains every agent's token."""
    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    parsed = yaml.safe_load(rendered)
    services = parsed["services"]

    for svc_name, svc_def in services.items():
        ef = _env_file_list(svc_def)
        assert not any(".env.agents" in entry for entry in ef), (
            f"Service {svc_name} leaks docker/.env.agents via env_file: {ef}"
        )


@pytest.mark.asyncio
async def test_legacy_env_agents_entry_is_stripped_but_shared_kept(async_session, compose_path):
    """A file rendered by an older backend already lists docker/.env.agents at
    service level.  The renderer must remove it and keep docker/.env.shared
    (dropping the whole block would fall back to the anchor, but an explicit
    block that lost .env.shared would silently strip CLAUDE_CODE_OAUTH_TOKEN)."""
    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    parsed = yaml.safe_load(rendered)
    ef = _env_file_list(parsed["services"]["mc-agent-legacy"])

    assert not any(".env.agents" in e for e in ef), ef
    assert any(".env.shared" in e for e in ef), ef


@pytest.mark.asyncio
async def test_existing_services_retain_env_shared(async_session, compose_path):
    """Every service still resolves docker/.env.shared (via anchor or explicit
    block) so CLAUDE_CODE_OAUTH_TOKEN / GH_TOKEN keep flowing."""
    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    parsed = yaml.safe_load(rendered)
    services = parsed["services"]

    for svc_name, svc_def in services.items():
        ef = _env_file_list(svc_def)
        assert any(".env.shared" in entry for entry in ef), (
            f"Service {svc_name} lost docker/.env.shared: {ef}"
        )


@pytest.mark.asyncio
async def test_existing_services_retain_mc_token_env_var(async_session, compose_path):
    """MC_TOKEN=${MC_TOKEN_<NAME>} interpolation is now the ONLY token path and
    must survive rendering."""
    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    parsed = yaml.safe_load(rendered)
    services = parsed["services"]

    for svc_name, svc_def in services.items():
        env = _env_list(svc_def)
        mc_token_entries = [e for e in env if str(e).startswith("MC_TOKEN=")]
        assert mc_token_entries, (
            f"Service {svc_name} lost its MC_TOKEN env entry. env={env}"
        )


@pytest.mark.asyncio
async def test_new_agent_block_has_no_env_agents(async_session, compose_path):
    """A freshly appended agent block must not mount docker/.env.agents either."""
    newbie = Agent(name="Newbie", agent_runtime="cli-bridge")
    async_session.add(newbie)
    await async_session.commit()
    await async_session.refresh(newbie)

    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    parsed = yaml.safe_load(rendered)
    services = parsed["services"]

    assert "mc-agent-newbie" in services, "New agent block not appended"
    ef = _env_file_list(services["mc-agent-newbie"])
    assert not any(".env.agents" in entry for entry in ef), ef
    assert any(".env.shared" in entry for entry in ef), ef


@pytest.mark.asyncio
async def test_new_agent_block_has_mc_token_env_var(async_session, compose_path):
    """New agent blocks emit MC_TOKEN=${MC_TOKEN_<NAME>} for the --env-file path."""
    newbie = Agent(name="Newbie", agent_runtime="cli-bridge")
    async_session.add(newbie)
    await async_session.commit()
    await async_session.refresh(newbie)

    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    parsed = yaml.safe_load(rendered)
    env = _env_list(parsed["services"]["mc-agent-newbie"])
    assert "MC_TOKEN=${MC_TOKEN_NEWBIE}" in env, env


@pytest.mark.asyncio
async def test_strip_is_idempotent(async_session, compose_path):
    """Rendering the rendered output again yields the same env_file blocks."""
    first = await render_compose_agents(async_session, compose_path=compose_path)
    compose_path.write_text(first, encoding="utf-8")
    second = await render_compose_agents(async_session, compose_path=compose_path)

    a = {k: _env_file_list(v) for k, v in yaml.safe_load(first)["services"].items()}
    b = {k: _env_file_list(v) for k, v in yaml.safe_load(second)["services"].items()}
    assert a == b
