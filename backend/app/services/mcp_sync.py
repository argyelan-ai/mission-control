"""Render per-agent MCP allowlist into user-scope .claude.json.

We used to write project-scoped `.mcp.json` files — but project-scope MCPs
are only discovered when openclaude runs from the directory that holds the
file, and they require explicit user approval (`State: skipped` in
`mcp doctor`). Our agents start openclaude from `$HOME` and never approve.

User-scope (`$CLAUDE_CONFIG_DIR/.claude.json` → `mcpServers` field) is always
loaded, cwd-independent, and pre-approved — the right primitive for agents.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from app.models.agent import Agent
from app.services.mcp_registry import MCPRegistry, MCPRegistryError

logger = logging.getLogger(__name__)


def _agent_slug(agent: Agent) -> str:
    """Derive agent slug from name (MC convention: lowercase + dashes)."""
    return agent.name.lower().replace(" ", "-")


def _agent_claude_config_dir(agent: Agent) -> Path:
    override = os.environ.get("MC_AGENTS_ROOT")
    if override:
        base = Path(override)
    else:
        base = Path(os.path.expanduser("~/.mc/agents"))
    return base / _agent_slug(agent) / "claude-config"


def render_agent_mcp_servers(
    agent: Agent, registry: MCPRegistry | None = None
) -> dict[str, Any]:
    """Return the `mcpServers` mapping for an agent (user-scope format).

    Each entry is wrapped with explicit `"type": "stdio"|"http"|"sse"` — that
    is how openclaude persists user-scope entries via `mcp add -s user`, and
    the shape it accepts back unambiguously.

    Allowlist semantics:
      - None    → all available MCP servers
      - []      → none
      - [...]   → only these (intersected with available)
    """
    registry = registry or MCPRegistry()
    available = {m.name for m in registry.list_installed()}

    if agent.mcp_servers is None:
        allowed = available
    elif not agent.mcp_servers:
        allowed = set()
    else:
        allowed = {name for name in agent.mcp_servers if name in available}

    servers: dict[str, Any] = {}
    for name in sorted(allowed):
        try:
            raw = registry.render_mcp_json_entry(name)
        except MCPRegistryError as e:
            logger.warning("Skipping MCP %s for agent %s: %s", name, agent.name, e)
            continue

        # Normalize to user-scope shape (explicit type tag).
        if "command" in raw:
            entry: dict[str, Any] = {
                "type": "stdio",
                "command": raw["command"],
                "args": list(raw.get("args") or []),
                "env": dict(raw.get("env") or {}),
            }
        elif "url" in raw:
            entry = {
                "type": raw.get("type", "http"),
                "url": raw["url"],
            }
            if raw.get("headers"):
                entry["headers"] = dict(raw["headers"])
        else:
            logger.warning("Skipping MCP %s: malformed registry entry %r", name, raw)
            continue

        servers[name] = entry

    return servers


# Backward-compatible alias — returns the full {"mcpServers": {...}} wrapper
# that the old project-scope `.mcp.json` used. Kept because call sites and
# tests still expect this shape.
def render_agent_mcp_json(
    agent: Agent, registry: MCPRegistry | None = None
) -> dict[str, Any]:
    return {"mcpServers": render_agent_mcp_servers(agent, registry)}


def sync_agent_mcp_to_disk(
    agent: Agent, registry: MCPRegistry | None = None
) -> Path:
    """Merge the agent's MCP allowlist into user-scope .claude.json.

    Read-modify-write: preserves whatever openclaude itself has written to
    .claude.json (numStartups, tipsHistory, oauth tokens, etc.) and only
    rewrites the `mcpServers` field.

    Also removes any stale project-scope `.mcp.json` to avoid confusion.
    """
    servers = render_agent_mcp_servers(agent, registry)
    target_dir = _agent_claude_config_dir(agent)
    target_dir.mkdir(parents=True, exist_ok=True)
    claude_json = target_dir / ".claude.json"

    existing: dict[str, Any] = {}
    if claude_json.exists():
        try:
            existing = json.loads(claude_json.read_text())
            if not isinstance(existing, dict):
                logger.warning(
                    "%s is not a JSON object (got %s) — recreating",
                    claude_json, type(existing).__name__,
                )
                existing = {}
        except json.JSONDecodeError as e:
            logger.warning("Invalid JSON in %s (%s) — recreating", claude_json, e)
            existing = {}

    existing["mcpServers"] = servers
    claude_json.write_text(json.dumps(existing, indent=2))
    logger.info(
        "Wrote %s mcpServers (%d servers: %s)",
        claude_json, len(servers), sorted(servers.keys()),
    )

    stale_mcp_json = target_dir / ".mcp.json"
    if stale_mcp_json.exists():
        try:
            stale_mcp_json.unlink()
            logger.info(
                "Removed stale project-scope %s (replaced with user-scope)",
                stale_mcp_json,
            )
        except OSError as e:
            logger.warning("Could not remove stale %s: %s", stale_mcp_json, e)

    return claude_json
