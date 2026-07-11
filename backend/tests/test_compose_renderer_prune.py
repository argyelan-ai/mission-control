"""Tests for compose-block pruning on agent deletion (2026-07-11).

render_compose_agents is additive — it overlays image overrides and appends
new service blocks, but never removes one. So a deleted cli-bridge agent's
``mc-agent-<slug>:`` block lingered in docker-compose.agents.yml forever and
``docker compose up`` kept recreating its container. ``prune_compose_agent``
removes exactly the named block.

Contract:
  1. Pure prune removes the targeted block and leaves siblings intact.
  2. Pure prune is a no-op (removed=False) for an absent slug.
  3. Remaining YAML is still valid and keeps the untouched services.
  4. File wrapper backs up, rewrites atomically, and reports changed=true.
  5. File wrapper on an absent slug writes nothing (changed=false).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from app.services.compose_renderer import (
    prune_compose_agent,
    prune_compose_agent_block,
)


@pytest.fixture(autouse=True)
def _patch_compose_redis(fake_redis):
    async def _get_redis():
        return fake_redis
    with patch("app.services.compose_renderer.get_redis", _get_redis):
        yield


COMPOSE_FIXTURE = """\
x-claude-agent-base: &claude-agent-base
  image: mc-claude-agent:latest
  restart: unless-stopped

services:
  mc-agent-rex:
    <<: *claude-agent-base
    container_name: mc-agent-rex
    environment:
      - AGENT_NAME=rex
      - MC_TOKEN=${MC_TOKEN_REX}
    volumes:
      - ${HOME}/.mc/workspaces/rex:/workspace

  mc-agent-deleteme:
    <<: *claude-agent-base
    container_name: mc-agent-deleteme
    environment:
      - AGENT_NAME=deleteme
      - MC_TOKEN=${MC_TOKEN_DELETEME}
    volumes:
      - ${HOME}/.mc/workspaces/deleteme:/workspace

  mc-agent-sparky:
    <<: *claude-agent-base
    container_name: mc-agent-sparky
    environment:
      - AGENT_NAME=sparky
      - MC_TOKEN=${MC_TOKEN_SPARKY}
    volumes:
      - ${HOME}/.mc/workspaces/sparky:/workspace

networks:
  mission-control_default:
    external: true
"""


def test_prune_block_removes_targeted_service():
    out, removed = prune_compose_agent_block(COMPOSE_FIXTURE, "deleteme")
    assert removed is True
    assert "mc-agent-deleteme" not in out
    # Siblings and their contents survive.
    assert "mc-agent-rex" in out
    assert "mc-agent-sparky" in out
    assert "AGENT_NAME=sparky" in out


def test_prune_block_absent_slug_is_noop():
    out, removed = prune_compose_agent_block(COMPOSE_FIXTURE, "ghost")
    assert removed is False
    assert out == COMPOSE_FIXTURE


def test_prune_block_does_not_match_prefix_slug():
    """Pruning 'rex' must NOT touch a 'rex2' block whose name it prefixes —
    the exact header match (incl. the trailing colon) is what guards this."""
    fixture = COMPOSE_FIXTURE.replace(
        "  mc-agent-sparky:", "  mc-agent-rex2:"
    ).replace("AGENT_NAME=sparky", "AGENT_NAME=rex2").replace(
        "AGENT_SLUG=sparky", "AGENT_SLUG=rex2"
    )
    out, removed = prune_compose_agent_block(fixture, "rex")
    assert removed is True
    assert "mc-agent-rex2" in out          # sibling with shared prefix survives
    assert "AGENT_NAME=rex2" in out
    # The exact 'rex' block is gone (its env line used AGENT_NAME=rex).
    assert "AGENT_NAME=rex\n" not in out


def test_prune_block_handles_crlf():
    crlf = COMPOSE_FIXTURE.replace("\n", "\r\n")
    out, removed = prune_compose_agent_block(crlf, "deleteme")
    assert removed is True
    assert "mc-agent-deleteme" not in out
    assert "mc-agent-rex" in out


def test_prune_block_leaves_valid_yaml_with_two_services():
    out, _ = prune_compose_agent_block(COMPOSE_FIXTURE, "deleteme")
    doc = yaml.safe_load(out)
    services = doc["services"]
    assert set(services) == {"mc-agent-rex", "mc-agent-sparky"}
    # networks (a sibling top-level key after the last service) survives.
    assert "networks" in doc


def test_prune_block_removes_last_service_before_top_level_key():
    """The block immediately before a top-level key (networks:) must stop at
    that key, not swallow it."""
    out, removed = prune_compose_agent_block(COMPOSE_FIXTURE, "sparky")
    assert removed is True
    doc = yaml.safe_load(out)
    assert "mc-agent-sparky" not in doc["services"]
    assert "networks" in doc


@pytest.fixture
def compose_path(tmp_path: Path) -> Path:
    p = tmp_path / "docker-compose.agents.yml"
    p.write_text(COMPOSE_FIXTURE, encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_prune_file_rewrites_and_backs_up(compose_path: Path):
    result = await prune_compose_agent("deleteme", compose_path=compose_path)
    assert result["changed"] == "true"
    assert result["removed"] == "true"

    written = compose_path.read_text(encoding="utf-8")
    assert "mc-agent-deleteme" not in written
    assert "mc-agent-rex" in written
    # Backup holds the pre-prune content.
    bak = compose_path.with_suffix(compose_path.suffix + ".bak")
    assert "mc-agent-deleteme" in bak.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_prune_file_absent_slug_writes_nothing(compose_path: Path):
    before = compose_path.read_text(encoding="utf-8")
    result = await prune_compose_agent("ghost", compose_path=compose_path)
    assert result["changed"] == "false"
    assert compose_path.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_prune_file_missing_path_is_noop(tmp_path: Path):
    missing = tmp_path / "does-not-exist.yml"
    result = await prune_compose_agent("deleteme", compose_path=missing)
    assert result["changed"] == "false"
    assert not missing.exists()
