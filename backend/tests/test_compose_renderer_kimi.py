"""Kimi-Harness im Compose-Renderer (vierte Harness, 2026-07-24).

Der per-Agent kimi-config-Mount (KIMI_CODE_HOME) ist überlebenswichtig:
Kimi hat keinen langlebigen Token — der OAuth-Grant lebt als credentials/-
Dateien im Mount. Ohne den Mount verliert der Agent bei jedem Recreate seinen
Login (und Refresh-Rotation macht Kopien unbrauchbar, Spike 2026-07-24).
"""
from __future__ import annotations

import re
import textwrap

from app.services.compose_renderer import (
    KIMI_IMAGE,
    _build_new_agent_block,
    _ensure_kimi_config_volume,
    _rewrite_compose,
)


def _extract_service_block(content: str, slug: str) -> str:
    pattern = re.compile(
        rf"(^  mc-agent-{re.escape(slug)}:\s*$.*?)(?=^  mc-agent-|^[a-zA-Z]|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(content)
    if not m:
        raise AssertionError(f"service block mc-agent-{slug} not found")
    return m.group(1)


_MINIMAL_COMPOSE = textwrap.dedent(
    """\
    x-claude-agent-base: &claude-agent-base
      image: mc-claude-agent:latest
      restart: unless-stopped

    x-kimi-agent-base: &kimi-agent-base
      image: mc-kimi-agent:latest
      restart: unless-stopped

    services:
      mc-agent-kimi:
        <<: *kimi-agent-base
        container_name: mc-agent-kimi
        environment:
          - AGENT_NAME=kimi
        volumes:
          - ${HOME}/.mc/workspaces/kimi:/workspace
      mc-agent-rex:
        <<: *claude-agent-base
        container_name: mc-agent-rex
        environment:
          - AGENT_NAME=rex
        volumes:
          - ${HOME}/.mc/workspaces/rex:/workspace
    """
)


def test_kimi_service_gets_kimi_config_mount():
    result = _rewrite_compose(_MINIMAL_COMPOSE, image_overrides={})
    block = _extract_service_block(result, "kimi")
    assert "${HOME}/.mc/agents/kimi/kimi-config:/home/agent/.kimi-code" in block


def test_kimi_config_mount_is_idempotent():
    once = _rewrite_compose(_MINIMAL_COMPOSE, image_overrides={})
    twice = _rewrite_compose(once, image_overrides={})
    assert once == twice
    assert twice.count("kimi-config:/home/agent/.kimi-code") == 1


def test_non_kimi_agents_get_no_kimi_config_mount():
    result = _rewrite_compose(_MINIMAL_COMPOSE, image_overrides={})
    block = _extract_service_block(result, "rex")
    assert "kimi-config" not in block


def test_override_to_kimi_image_adds_mount():
    """DB-sourced override auf KIMI_IMAGE zieht den Mount auch ohne Anchor."""
    result = _rewrite_compose(
        _MINIMAL_COMPOSE, image_overrides={"rex": KIMI_IMAGE}
    )
    block = _extract_service_block(result, "rex")
    assert f"image: {KIMI_IMAGE}" in block
    assert "${HOME}/.mc/agents/rex/kimi-config:/home/agent/.kimi-code" in block


def test_build_new_agent_block_kimi():
    block = _build_new_agent_block("kimitest", KIMI_IMAGE, is_vault_writer=False)
    assert "<<: *kimi-agent-base" in block
    # Anchor-Default == Image → keine explizite image:-Zeile nötig.
    assert "image:" not in block
    assert "${HOME}/.mc/agents/kimitest/kimi-config:/home/agent/.kimi-code" in block
    assert "MSG_DELIVERY_MODE=${MSG_DELIVERY_MODE:-nudge}" in block


def test_ensure_kimi_config_volume_creates_volumes_block():
    body = ["    environment:", "      - AGENT_NAME=x"]
    out = _ensure_kimi_config_volume(body, "x")
    assert "    volumes:" in out
    assert any("kimi-config:/home/agent/.kimi-code" in line for line in out)
