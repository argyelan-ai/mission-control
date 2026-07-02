"""Tests for render_compose_agents — new-agent service block emission.

Covers the feature added in feature/compose-render-new-agents:
  When a cli-bridge agent's ``mc-agent-<slug>:`` service is NOT already
  present in the static compose template, ``render_compose_agents`` must
  APPEND a full service block at the end of the file.

Four contract tests:
  1. Existing services preserved — rendering does not alter blocks that
     already exist in the template.
  2. New agent appended — an agent not in the template gets a full block.
  3. No duplicate — an agent already in the template is NOT appended again.
  4. YAML validity — the combined output parses via yaml.safe_load and
     contains both the existing and the new service under ``services:``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.compose_renderer import (
    CLAUDE_IMAGE,
    OPENCLAUDE_IMAGE,
    render_compose_agents,
)


# ── Redis patch (write_compose_agents acquires a Redis lock; render does not,
# but autouse keeps the fixture consistent with the other compose test modules).
@pytest.fixture(autouse=True)
def _patch_compose_redis(fake_redis):
    async def _get_redis():
        return fake_redis
    with patch("app.services.compose_renderer.get_redis", _get_redis):
        yield


# ── Fixture: a representative compose template with 3 pre-existing services ──

COMPOSE_FIXTURE = """\
# docker/docker-compose.agents.yml — test fixture for new-agent block tests

x-claude-agent-base: &claude-agent-base
  image: mc-claude-agent:latest
  restart: unless-stopped
  networks:
    - mission-control_default

x-openclaude-agent-base: &openclaude-agent-base
  image: mc-agent-base:latest
  restart: unless-stopped
  networks:
    - mission-control_default

services:
  mc-agent-rex:
    <<: *claude-agent-base
    container_name: mc-agent-rex
    environment:
      - AGENT_NAME=rex
      - MC_API_URL=${MC_API_URL:-http://backend:8000}
      - MC_TOKEN=${MC_TOKEN_REX}
      - AGENT_RECYCLER_ENABLED=${AGENT_RECYCLER_ENABLED:-true}
      - AGENT_VAULT_PATH=/vault/agents/rex
      - AGENT_VAULT_INBOX=/vault/_inbox
      - AGENT_SLUG=rex
    volumes:
      - ${HOME}/.mc/agents/rex/claude-config:/home/agent/.claude
      - ${HOME}/.mc/mcp-servers:/mc-servers:ro
      - ${HOME}/.mc/workspaces/rex:/workspace
      - ${HOME}/.mc/deliverables/rex:/deliverables
      - ${HOME}/.mc/vault:/vault:rw

  mc-agent-sparky:
    <<: *openclaude-agent-base
    container_name: mc-agent-sparky
    environment:
      - AGENT_NAME=sparky
      - MC_API_URL=${MC_API_URL:-http://backend:8000}
      - MC_TOKEN=${MC_TOKEN_SPARKY}
      - AGENT_RECYCLER_ENABLED=${AGENT_RECYCLER_ENABLED:-true}
      - AGENT_VAULT_PATH=/vault/agents/sparky
      - AGENT_VAULT_INBOX=/vault/_inbox
      - AGENT_SLUG=sparky
    volumes:
      - ${HOME}/.mc/agents/sparky/claude-config:/home/agent/.claude
      - ${HOME}/.mc/mcp-servers:/mc-servers:ro
      - ${HOME}/.mc/workspaces/sparky:/workspace
      - ${HOME}/.mc/deliverables/sparky:/deliverables
      - ${HOME}/.mc/vault:/vault:rw

networks:
  mission-control_default:
    external: true
"""


@pytest.fixture
def compose_path(tmp_path: Path) -> Path:
    p = tmp_path / "docker-compose.agents.yml"
    p.write_text(COMPOSE_FIXTURE, encoding="utf-8")
    return p


# ── Test 1: Existing services preserved ──────────────────────────────────────


@pytest.mark.asyncio
async def test_existing_services_preserved_when_new_agent_appended(
    async_session, compose_path
):
    """Rendering with a new agent present in DB must NOT alter existing
    service blocks (rex, sparky).  Every key line that was present before
    must still be present — and each existing service slug must still appear
    exactly once as ``mc-agent-<slug>:``."""
    # New agent NOT in the template.
    rt = Runtime(
        slug="anthropic-claude-sonnet",
        display_name="Claude Sonnet",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    estrich = Agent(
        name="Estrich-Vision",
        agent_runtime="cli-bridge",
        runtime_id=rt.id,
        scopes=["tasks:read"],  # explicit non-vault scope
    )
    async_session.add(estrich)
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    # Both pre-existing services are still present exactly once.
    assert rendered.count("mc-agent-rex:") == 1, "rex service was removed or duplicated"
    assert rendered.count("mc-agent-sparky:") == 1, "sparky service was removed or duplicated"

    # Spot-check key lines that must survive untouched in rex's block.
    assert "MC_TOKEN=${MC_TOKEN_REX}" in rendered
    assert "AGENT_SLUG=rex" in rendered
    assert "${HOME}/.mc/vault:/vault:rw" in rendered  # rex keeps its vault mount

    # sparky's block survives.
    assert "MC_TOKEN=${MC_TOKEN_SPARKY}" in rendered
    assert "AGENT_SLUG=sparky" in rendered


# ── Test 2: New agent block appended ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_agent_block_appended(async_session, compose_path):
    """A cli-bridge agent whose slug is not in the template gets a full service
    block appended.  Verifies anchor, env vars, volumes, and vault mount."""
    rt = Runtime(
        slug="anthropic-claude-sonnet",
        display_name="Claude Sonnet",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    # scopes=None → vault:write (backward-compat all-scopes rule).
    estrich = Agent(
        name="Estrich-Vision",
        agent_runtime="cli-bridge",
        runtime_id=rt.id,
        scopes=None,
    )
    async_session.add(estrich)
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    # Service header is present.
    assert "mc-agent-estrich-vision:" in rendered

    # Anchor selection: claude runtime → claude-agent-base.
    assert "<<: *claude-agent-base" in rendered

    # Required env vars for estrich-vision.
    assert "AGENT_SLUG=estrich-vision" in rendered
    assert "MC_TOKEN=${MC_TOKEN_ESTRICH_VISION}" in rendered
    assert "AGENT_NAME=estrich-vision" in rendered
    assert "MC_API_URL=${MC_API_URL:-http://backend:8000}" in rendered
    assert "AGENT_VAULT_PATH=/vault/agents/estrich-vision" in rendered
    assert "AGENT_VAULT_INBOX=/vault/_inbox" in rendered

    # Standard volumes.
    assert "${HOME}/.mc/agents/estrich-vision/claude-config:/home/agent/.claude" in rendered
    assert "${HOME}/.mc/mcp-servers:/mc-servers:ro" in rendered
    assert "${HOME}/.mc/workspaces/estrich-vision:/workspace" in rendered
    assert "${HOME}/.mc/deliverables/estrich-vision:/deliverables" in rendered

    # Vault mount because scopes=None → vault:write.
    assert "${HOME}/.mc/vault:/vault:rw" in rendered


@pytest.mark.asyncio
async def test_new_agent_no_vault_when_non_vault_scope(async_session, compose_path):
    """A new agent with explicit non-vault scopes must NOT get the vault mount."""
    rt = Runtime(
        slug="anthropic-claude-sonnet",
        display_name="Claude Sonnet",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    scout = Agent(
        name="Scout",
        agent_runtime="cli-bridge",
        runtime_id=rt.id,
        scopes=["tasks:read", "knowledge:read"],
    )
    async_session.add(scout)
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    # Service block is present.
    assert "mc-agent-scout:" in rendered

    # Extract scout-specific block to avoid asserting on rex/sparky vault mounts.
    scout_start = rendered.index("mc-agent-scout:")
    # Find the start of the next top-level entry (another mc-agent-* or network/volume section).
    rest = rendered[scout_start + len("mc-agent-scout:"):]
    next_top = len(rendered)  # default: end of file
    for marker in ["mc-agent-", "\nnetworks:", "\nvolumes:"]:
        idx = rest.find(marker)
        if idx != -1:
            candidate = scout_start + len("mc-agent-scout:") + idx
            if candidate < next_top:
                next_top = candidate
    scout_block = rendered[scout_start:next_top]

    # Vault mount must NOT appear inside scout's own block.
    assert "/vault:rw" not in scout_block, (
        "Vault mount injected for non-vault agent Scout"
    )


@pytest.mark.asyncio
async def test_new_agent_openclaude_anchor_for_openclaude_image(
    async_session, compose_path
):
    """When the resolved image is OPENCLAUDE_IMAGE, the new block must use
    ``*openclaude-agent-base`` as the anchor, not ``*claude-agent-base``."""
    rt = Runtime(
        slug="qwen-local",
        display_name="Qwen Local",
        runtime_type="vllm_docker",
        endpoint="http://localhost:8000/v1",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    analyst = Agent(
        name="Analyst",
        agent_runtime="cli-bridge",
        runtime_id=rt.id,
        scopes=["tasks:read"],
    )
    async_session.add(analyst)
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    assert "mc-agent-analyst:" in rendered
    # Find the analyst block and confirm the openclaude anchor is used.
    analyst_idx = rendered.index("mc-agent-analyst:")
    analyst_block = rendered[analyst_idx : analyst_idx + 600]
    assert "<<: *openclaude-agent-base" in analyst_block, (
        "openclaude anchor not used for vllm_docker runtime"
    )


# ── Test 3: No duplicate for existing service ─────────────────────────────────


@pytest.mark.asyncio
async def test_no_duplicate_for_existing_service(async_session, compose_path):
    """An agent whose service already exists in the template must NOT result in
    a second ``mc-agent-<slug>:`` block being appended."""
    rt = Runtime(
        slug="anthropic-claude-sonnet",
        display_name="Claude Sonnet",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    # Rex IS already in the template — should not be appended.
    rex = Agent(
        name="Rex",
        agent_runtime="cli-bridge",
        runtime_id=rt.id,
        scopes=None,
    )
    async_session.add(rex)
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    # mc-agent-rex: must appear exactly once (no duplicate).
    occurrences = rendered.count("mc-agent-rex:")
    assert occurrences == 1, (
        f"mc-agent-rex: appears {occurrences} times — service was duplicated"
    )


# ── Test 4: YAML validity ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_yaml_valid_with_existing_and_new_agent(async_session, compose_path):
    """The combined rendered output (existing template + new agent block) must
    parse as valid YAML and contain both an existing service and the new one
    under the ``services:`` key."""
    rt = Runtime(
        slug="anthropic-claude-sonnet",
        display_name="Claude Sonnet",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)

    estrich = Agent(
        name="Estrich-Vision",
        agent_runtime="cli-bridge",
        runtime_id=rt.id,
        scopes=None,
    )
    async_session.add(estrich)
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    # Substitute shell-variable syntax that confuses yaml.safe_load so we can
    # verify structural validity without needing a real shell environment.
    safe_yaml = (
        rendered
        .replace("${HOME}", "/FAKE_HOME")
        .replace("${MC_API_URL:-http://backend:8000}", "http://backend:8000")
        .replace("${AGENT_RECYCLER_ENABLED:-true}", "true")
        .replace("${", "__ENV_")
        .replace("}", "__")
    )

    parsed = yaml.safe_load(safe_yaml)
    assert parsed is not None, "yaml.safe_load returned None — invalid YAML structure"
    assert "services" in parsed, "rendered YAML has no 'services' key"

    services = parsed["services"]
    # Existing service survives.
    assert "mc-agent-rex" in services, "Pre-existing mc-agent-rex was removed"
    # New service was appended.
    assert "mc-agent-estrich-vision" in services, (
        "New agent mc-agent-estrich-vision is missing from parsed services"
    )
