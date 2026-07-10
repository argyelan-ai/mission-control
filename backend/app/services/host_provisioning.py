"""Host-agent file provisioning for the onboarding wizard (2026-07-10).

Renders a launchd plist + launcher script + agent.env into
``~/.mc/agents/<slug>/`` so the operator can review and load a new host
(launchd) agent. No such generalized renderer existed before — Boss was
provisioned by hand (docker/boss-host/*.plist with YOUR_USER placeholders)
and Hermes has a bespoke bootstrap that assumes a pre-existing plist.

SAFETY (owner directive): ``launchctl`` is only invoked when
``settings.host_agent_autoload_enabled`` is True. Tests never enable it.
Staging (file writes) is always safe and always tested.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.agent import Agent
from app.models.runtime import Runtime
from app.routers.internal import build_runtime_env
from app.services.mcp_sync import render_agent_mcp_json
from app.services.template_renderer import render_agent_file

logger = logging.getLogger("mc.host_provisioning")

# Harness → native binary name expected on the host PATH. openclaude/omp are
# OpenAI-protocol harnesses; claude is the Anthropic CLI.
_HARNESS_BINARY: dict[str, str] = {
    "claude": "claude",
    "openclaude": "openclaude",
    "omp": "omp",
}


def _home_host() -> Path:
    home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
    return Path(home)


_SLUG_UNSAFE = re.compile(r"[^a-z0-9-]+")
_SLUG_DASHES = re.compile(r"-{2,}")


def _slugify(name: str) -> str:
    """Derive a filesystem/plist/shell-safe slug.

    Strips everything outside [a-z0-9-] (this also removes '/', '..'
    sequences, '&', '<', '>', newlines, tabs — anything that could break
    out of the workspace path, inject a shell line, or break XML), then
    collapses repeated hyphens and trims leading/trailing ones. Falls back
    to "agent" if nothing safe remains.
    """
    lowered = (name or "").lower().strip().replace(" ", "-")
    safe = _SLUG_UNSAFE.sub("", lowered)
    collapsed = _SLUG_DASHES.sub("-", safe).strip("-")
    return collapsed or "agent"


def _resolve_slug(agent: Agent) -> str:
    """Prefer the DB's already-derived Agent.slug, normalized through the
    hardened sanitizer; fall back to re-deriving from the display name."""
    candidate = agent.slug or agent.name
    return _slugify(candidate)


def _format_env_file(env: dict[str, str]) -> str:
    lines = []
    for key in sorted(env.keys()):
        safe = env[key].replace("'", "'\"'\"'")
        lines.append(f"{key}='{safe}'")
    return "\n".join(lines) + "\n"


@dataclass
class HostStageResult:
    slug: str
    workspace_path: str
    plist_label: str
    plist_staged_path: str
    run_script_path: str
    env_path: str
    launchctl_command: str
    # Only set for harness=="claude" — see stage_host_agent_files().
    mcp_config_path: str | None = None


async def stage_host_agent_files(
    agent: Agent,
    runtime: Runtime,
    raw_token: str,
    *,
    session: AsyncSession,
) -> HostStageResult:
    """Render plist + run.sh + agent.env into ~/.mc/agents/<slug>/.

    Idempotent — overwrites existing files. Does NOT touch launchd.
    """
    home = _home_host()
    slug = _resolve_slug(agent)
    agents_root = (home / ".mc" / "agents").resolve()
    workspace = home / ".mc" / "agents" / slug
    resolved_workspace = workspace.resolve()
    if resolved_workspace != agents_root and agents_root not in resolved_workspace.parents:
        raise ValueError(
            f"refusing to stage host agent files outside {agents_root}: {resolved_workspace}"
        )
    logs_dir = workspace / "logs"
    label = f"com.mc.agent.{slug}"
    plist_path = workspace / f"{label}.plist"
    run_script_path = workspace / "run.sh"
    env_path = workspace / "agent.env"

    workspace.mkdir(parents=True, exist_ok=True, mode=0o755)
    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o755)

    harness = agent.harness or "openclaude"
    if harness not in _HARNESS_BINARY:
        raise ValueError(f"unknown harness {harness!r}; expected one of {sorted(_HARNESS_BINARY)}")
    binary = _HARNESS_BINARY[harness]

    # 1. agent.env (OPENAI_*/MC_* from the runtime + token), mode 600.
    runtime_env = await build_runtime_env(runtime, session)
    env: dict[str, str] = {
        "MC_AGENT_TOKEN": raw_token,
        "MC_BASE_URL": settings.mc_base_url.rstrip("/"),
        "HOME": str(home),
    }
    env.update(runtime_env)
    env_path.write_text(_format_env_file(env))
    os.chmod(env_path, 0o600)

    # 2a. Isolated MCP config, native-claude harness only. The 'claude' CLI
    # defaults to the operator's own $HOME/.claude config (needed here so it
    # finds the Pro/Max OAuth keychain — see agent.plist.j2's HOME var), which
    # means a freshly staged agent otherwise inherits the operator's personal
    # user-scope MCP servers. Since this new workspace directory was never
    # seen before, claude treats every inherited server as "found in this
    # project" and blocks on an interactive trust prompt — no heartbeat ever
    # arrives (2026-07-10 host E2E test, Befund 2). Boss (docker/boss-host/
    # start-claude.sh) already solves this for its one native-claude host
    # agent via --strict-mcp-config pointed at an explicit, isolated file;
    # this generalizes that mechanism (reusing render_agent_mcp_json — SSoT
    # with the cli-bridge/openclaude MCP allowlist rendering) to any host
    # agent staged through the wizard.
    mcp_config_path: str | None = None
    if harness == "claude":
        mcp_config_path = str(workspace / ".mcp.json")
        mcp_config = render_agent_mcp_json(agent)
        Path(mcp_config_path).write_text(json.dumps(mcp_config, indent=2) + "\n")

    # 2b. run.sh launcher.
    run_sh = render_agent_file(
        "host_agent_run.sh.j2",
        {
            "slug": slug,
            "harness": harness,
            "binary": binary,
            "workspace_path": str(workspace),
            "mcp_config_path": mcp_config_path,
        },
    )
    run_script_path.write_text(run_sh)
    os.chmod(run_script_path, 0o755)

    # 3. plist.
    plist = render_agent_file(
        "agent.plist.j2",
        {
            "label": label,
            "run_script_path": str(run_script_path),
            "workspace_path": str(workspace),
            "home": str(home),
        },
    )
    plist_path.write_text(plist)

    dest = home / "Library" / "LaunchAgents" / f"{label}.plist"
    launchctl_command = (
        f"cp '{plist_path}' '{dest}' && "
        f"launchctl bootstrap gui/$(id -u) '{dest}'"
    )

    logger.info("staged host agent files for %s at %s", agent.name, workspace)
    return HostStageResult(
        slug=slug,
        workspace_path=str(workspace),
        plist_label=label,
        plist_staged_path=str(plist_path),
        run_script_path=str(run_script_path),
        env_path=str(env_path),
        launchctl_command=launchctl_command,
        mcp_config_path=mcp_config_path,
    )


def maybe_load_plist(result: HostStageResult) -> dict:
    """Copy the staged plist to ~/Library/LaunchAgents and launchctl-load it.

    Only runs when settings.host_agent_autoload_enabled is True — loading a
    launchd job is an irreversible host action. Otherwise a no-op that tells
    the caller the operator must run result.launchctl_command themselves.
    """
    if not settings.host_agent_autoload_enabled:
        return {"loaded": False, "reason": "autoload disabled", "command": result.launchctl_command}

    home = _home_host()
    dest_dir = home / "Library" / "LaunchAgents"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{result.plist_label}.plist"
    dest.write_bytes(Path(result.plist_staged_path).read_bytes())

    uid = os.getuid()
    cmd = ["launchctl", "bootstrap", f"gui/{uid}", str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
    already = "already" in combined or proc.returncode == 37
    loaded = proc.returncode == 0 or already
    return {
        "loaded": loaded,
        "already": already,
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "").strip(),
        "command": result.launchctl_command,
    }
