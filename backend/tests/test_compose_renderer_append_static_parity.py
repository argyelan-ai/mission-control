"""Parity test: append-path blocks vs. hand-maintained static blocks.

Incident 2026-09-12: `_build_new_agent_block` (the append path, used when a
cli-bridge agent's ``mc-agent-<slug>:`` service is not yet in the static
compose template) emitted a volumes list that was missing three mounts every
hand-maintained claude-harness block in the file carries — most painfully
``/workspace-ref``, the mount a developer/reviewer agent's own SOUL tells it
to `git clone` from. The gap only showed up once the recreated container
failed at its first real task.

Rather than pinning the three specific line strings (which would go stale the
next time a mount is added to the static convention but not to the renderer),
this test compares the *set* of container-side mount targets between a
representative static claude block and a freshly appended one. Any future
drift between the two code paths fails this test, not just today's three.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.compose_renderer import render_compose_agents


@pytest.fixture(autouse=True)
def _patch_compose_redis(fake_redis):
    async def _get_redis():
        return fake_redis
    with patch("app.services.compose_renderer.get_redis", _get_redis):
        yield


# A representative, fully hand-maintained claude block — this is the shape
# the append path must match. Neutral slug ("worker-a"), not a real fleet name.
COMPOSE_FIXTURE = """\
# docker/docker-compose.agents.yml — test fixture for append/static parity

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
  mc-agent-worker-a:
    <<: *claude-agent-base
    container_name: mc-agent-worker-a
    environment:
      - AGENT_NAME=worker-a
      - MC_API_URL=${MC_API_URL:-http://backend:8000}
      - MC_TOKEN=${MC_TOKEN_WORKER_A}
      - AGENT_RECYCLER_ENABLED=${AGENT_RECYCLER_ENABLED:-true}
      - AGENT_VAULT_PATH=/vault/agents/worker-a
      - AGENT_VAULT_INBOX=/vault/_inbox
      - AGENT_SLUG=worker-a
    volumes:
      - ${HOME}/.mc/agents/worker-a/claude-config:/home/agent/.claude
      - ${HOME}/.mc/mcp-servers:/mc-servers:ro
      - ${HOME}/.mc/workspaces/worker-a:/workspace
      - ${HOME}/Workspace/Projects:/workspace-ref:ro
      - ${HOME}/.mc/deliverables/worker-a:/deliverables
      - mc_shared_deliverables:/shared-deliverables:ro
      - ${HOME}/.mc/mcp-screenshots:/shared-mcp:ro
      - ${HOME}/.mc/vault:/vault:rw
      - ${HOME}/.mc/references:${HOME}/.mc/references:ro

networks:
  mission-control_default:
    external: true

volumes:
  mc_shared_deliverables:
    external: true
    name: mission-control_mc_shared_deliverables
"""


@pytest.fixture
def compose_path(tmp_path: Path) -> Path:
    p = tmp_path / "docker-compose.agents.yml"
    p.write_text(COMPOSE_FIXTURE, encoding="utf-8")
    return p


def _mount_targets(block: str) -> set[str]:
    """Container-side mount target (+ mode) per volume line, slug-independent.

    ``- <source>:<target>[:<mode>]`` → ``<target>[:<mode>]``. The one
    self-referencing exception (``${HOME}/.mc/references:${HOME}/.mc/references:ro``,
    ADR-053 — source and target are the same host path on purpose) is kept
    as its full spec since it has no distinct container-side target.
    """
    targets: set[str] = set()
    for line in block.splitlines():
        m = re.match(r"^\s*-\s*(.+)$", line)
        if not m:
            continue
        spec = m.group(1).strip()
        if spec.startswith("${HOME}/.mc/references:"):
            targets.add(spec)
            continue
        parts = spec.split(":")
        if len(parts) < 2:
            continue
        targets.add(":".join(parts[1:]))
    return targets


def _extract_service_block(rendered: str, slug: str) -> str:
    marker = f"mc-agent-{slug}:"
    start = rendered.index(marker)
    rest = rendered[start + len(marker):]
    end = len(rest)
    for other in ["\n  mc-agent-", "\nnetworks:", "\nvolumes:"]:
        idx = rest.find(other)
        if idx != -1:
            end = min(end, idx)
    return rest[:end]


@pytest.mark.asyncio
async def test_appended_claude_block_has_same_mount_targets_as_static_block(
    async_session, compose_path
):
    """A newly appended claude-harness agent must expose the same set of
    container-side mount targets as an existing, hand-maintained one —
    ``/workspace-ref``, ``/shared-deliverables`` and ``/shared-mcp`` included."""
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

    # scopes=None → vault:write, same backward-compat rule as the static
    # fixture agent (which also has a /vault:rw mount).
    new_agent = Agent(
        name="Worker-B",
        agent_runtime="cli-bridge",
        runtime_id=rt.id,
        scopes=None,
    )
    async_session.add(new_agent)
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)

    assert "mc-agent-worker-b:" in rendered, "new agent block was not appended"

    static_block = _extract_service_block(rendered, "worker-a")
    appended_block = _extract_service_block(rendered, "worker-b")

    static_targets = _mount_targets(static_block)
    appended_targets = _mount_targets(appended_block)

    missing = static_targets - appended_targets
    extra = appended_targets - static_targets
    assert not missing, (
        f"appended block is missing mounts present in the static block: {missing}"
    )
    assert not extra, (
        f"appended block has mounts absent from the static block: {extra}"
    )


@pytest.mark.asyncio
async def test_appended_openclaude_block_does_not_get_claude_only_mounts(
    async_session, compose_path
):
    """The three claude-only mounts are scoped to the claude anchor — an
    appended openclaude-harness agent must NOT get them (matches the one real
    openclaude instance today, which predates this fix and has no
    shared-mcp/shared-deliverables mount)."""
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

    new_agent = Agent(
        name="Local-Coder",
        agent_runtime="cli-bridge",
        runtime_id=rt.id,
        scopes=["tasks:read"],
    )
    async_session.add(new_agent)
    await async_session.commit()

    rendered = await render_compose_agents(async_session, compose_path=compose_path)
    block = _extract_service_block(rendered, "local-coder")

    assert "<<: *openclaude-agent-base" in block
    assert "/shared-deliverables:ro" not in block
    assert "/shared-mcp:ro" not in block
