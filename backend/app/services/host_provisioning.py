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
import shutil
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


def _write_owner_only_file(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` with 0600 perms from the moment the file
    exists — never write-then-chmod, which leaves a window (governed by the
    process umask, not the intended mode) where secret content sits on disk
    at whatever permissions umask produces before the later chmod() call
    narrows them. os.open()'s mode arg is itself umask-masked too, so we
    fchmod() immediately after open()/before any content is written, closing
    that gap as well. Used for both agent.env and the claude-harness
    .mcp.json (2026-07-10 E2E rerun, Befund 4 — .mcp.json can carry inline
    MCP-server secrets, e.g. API keys in `env`, same shape as agent.env)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "w") as f:
        f.write(content)


def _format_env_file(env: dict[str, str]) -> str:
    lines = []
    for key in sorted(env.keys()):
        safe = env[key].replace("'", "'\"'\"'")
        lines.append(f"{key}='{safe}'")
    return "\n".join(lines) + "\n"


def omp_host_profile(slug: str) -> str:
    """omp profile name for a host agent. One profile per agent, never the default.

    The operator's own ``~/.omp`` config must stay untouched, and two host omp
    agents must not share a models.yml — so the profile is namespaced by slug.
    """
    return f"mc-{slug}"


def render_omp_host_models_yml(slug: str, env: dict[str, str], *, home: Path | None = None) -> str | None:
    """Render omp's models.yml for a HOST omp agent from its runtime env.

    WHY THIS EXISTS
    ---------------
    omp is the one generic-staged host harness that does NOT pick its model up
    from OPENAI_BASE_URL/OPENAI_MODEL: it resolves models through its own
    models.yml, which docker/omp-bridge/entrypoint.sh (§2) calls mandatory for
    a self-served endpoint. The container renders that file at boot; nothing
    rendered it on the host path, so a staged omp host agent would have booted
    against whatever ``~/.omp`` config the operator happens to have — silently
    ignoring the runtime the operator bound in MC.

    Resolution rule (verified in-container, entrypoint.sh §2): with
    ``OMP_PROFILE=<p>`` omp reads ``$HOME/.omp/profiles/<p>/agent/models.yml``
    — a file at ``$PI_CODING_AGENT_DIR`` is ignored once a profile is set. So
    the caller MUST also put ``OMP_PROFILE`` into agent.env (run.sh exports it).

    Returns the written path, or None when the env has no usable binding
    (mirrors the container's "no baked-in defaults" rule from ADR-054 — a
    silent fallback to a stale model is the exact drift bug this prevents;
    here we skip the file instead of hard-failing the whole staging run).
    """
    base_url = env.get("OPENAI_BASE_URL")
    model = env.get("OPENAI_MODEL")
    if not base_url or not model:
        logger.warning(
            "omp host agent %s: no OPENAI_BASE_URL/OPENAI_MODEL in its runtime env "
            "— skipping models.yml (omp will fall back to its own config)",
            slug,
        )
        return None

    home = home or _home_host()
    profiles_root = (home / ".omp" / "profiles").resolve()
    models_dir = home / ".omp" / "profiles" / omp_host_profile(slug) / "agent"
    resolved = models_dir.resolve()
    # Same containment property the workspace staging enforces: a hostile slug
    # must not be able to steer this write outside the omp profiles tree.
    if profiles_root not in resolved.parents:
        raise ValueError(
            f"refusing to render omp models.yml outside {profiles_root}: {resolved}"
        )
    models_dir.mkdir(parents=True, exist_ok=True)

    # Keyed endpoints (e.g. Ollama Cloud) get the api key; keyless local vLLM
    # keeps `auth: none`. Same two shapes as the container entrypoint.
    api_key = env.get("OPENAI_API_KEY")
    auth_line = f"    apiKey: {api_key}" if api_key else "    auth: none"
    context_window = env.get("OMP_CONTEXT_WINDOW", "262144")
    max_tokens = env.get("OMP_MAX_TOKENS", "32768")

    models_yml = (
        "providers:\n"
        "  mc-openai:\n"
        "    name: MC OpenAI-compatible endpoint\n"
        f"    baseUrl: {base_url}\n"
        "    api: openai-completions\n"
        f"{auth_line}\n"
        "    models:\n"
        f"      - id: {model}\n"
        "        name: MC model\n"
        "        reasoning: true\n"
        f"        contextWindow: {context_window}\n"
        f"        maxTokens: {max_tokens}\n"
    )
    models_path = models_dir / "models.yml"
    # 0600 like agent.env — apiKey may be rendered inline above.
    _write_owner_only_file(models_path, models_yml)
    return str(models_path)


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
    poll_script_path: str | None = None
    # Only set for harness=="omp" — see render_omp_host_models_yml().
    omp_models_path: str | None = None


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
    #
    # MC_API_URL (not MC_BASE_URL) — this is a genuine pre-existing bug found
    # while wiring up poll.sh (Fix C, 2026-07-10): every curl/mc instruction
    # in SOUL.md, and the generic poll.sh template below, reads $MC_API_URL
    # (the convention templates/cli_agent.env.j2 uses for cli-bridge agents,
    # which — like this generic host template — drive the agent via SOUL.md
    # + curl). MC_BASE_URL is a *different*, unrelated convention used only
    # by agent_bootstrap.py/hermes-bridge.py's own internal Python poll loop
    # (Hermes doesn't run SOUL.md-driven curl calls at all). Before this fix
    # every staged host agent's own tool calls (and now poll.sh) would have
    # failed with an unset variable — this simply never surfaced in earlier
    # E2E runs because the agent never got far enough to try.
    runtime_env = await build_runtime_env(runtime, session)
    env: dict[str, str] = {
        "MC_AGENT_TOKEN": raw_token,
        "MC_API_URL": settings.mc_base_url.rstrip("/"),
        "HOME": str(home),
    }
    env.update(runtime_env)
    # omp needs its own models.yml to resolve the bound model at all (see
    # render_omp_host_models_yml). OMP_PROFILE must land in agent.env BEFORE it
    # is written — run.sh sources this file and exports everything in it, and
    # without the profile omp would read a different models.yml than the one
    # rendered here.
    omp_models_path: str | None = None
    if harness == "omp":
        env["OMP_PROFILE"] = omp_host_profile(slug)
        omp_models_path = render_omp_host_models_yml(slug, env, home=home)
    _write_owner_only_file(env_path, _format_env_file(env))

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
        # 0600, not the write_text() default (umask-dependent, typically
        # 644) — MCP server manifests can carry inline secrets (e.g. API
        # keys in `env`, same shape as the shared registry in
        # mcp_registry.py) and this file sits right next to the 0600
        # agent.env. render_agent_mcp_json() itself only reuses whatever the
        # registry already stores inline; if cli-bridge/openclaude agents
        # ever start referencing env-var placeholders instead of inline
        # values there, this should follow — but that's a separate,
        # pre-existing registry-wide decision, not something to invent here.
        _write_owner_only_file(Path(mcp_config_path), json.dumps(mcp_config, indent=2) + "\n")

    # 2b. poll.sh — without this, the staged agent never picks up work or
    # heartbeats: it boots to an idle interactive prompt and nothing nudges
    # it (2026-07-10 E2E rerun, Befund 3 — last_seen_at stays null forever,
    # provision_status stuck on "provisioning"). Generalizes
    # docker/boss-host/poll.sh, the only working native-claude host worker
    # at the time this was written (see the template's own docstring for
    # what was deliberately changed vs. Boss's orchestrator-specific copy).
    poll_script_path = str(workspace / "poll.sh")
    poll_sh = render_agent_file(
        "host_agent_poll.sh.j2",
        {"slug": slug, "workspace_path": str(workspace)},
    )
    Path(poll_script_path).write_text(poll_sh)
    os.chmod(poll_script_path, 0o755)

    # 2c. run.sh launcher — Window 0 runs the harness, Window 1 runs poll.sh.
    run_sh = render_agent_file(
        "host_agent_run.sh.j2",
        {
            "slug": slug,
            "harness": harness,
            "binary": binary,
            "workspace_path": str(workspace),
            "mcp_config_path": mcp_config_path,
            "poll_script_path": poll_script_path,
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
        poll_script_path=poll_script_path,
        omp_models_path=omp_models_path,
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


def teardown_host_agent_files(agent: Agent) -> dict:
    """Remove ``~/.mc/agents/<slug>/`` for a deleted host agent.

    Counterpart to stage_host_agent_files: agent deletion used to leave the
    staged plist + launcher + agent.env (with a live token!) on disk forever
    (found 2026-07-11). Reuses the exact path-confinement guard from staging
    so a hostile/degenerate slug can never make this rmtree escape the agents
    root — and it refuses to delete the agents root itself.

    Does NOT touch launchd (unloading a job is the operator's manual step,
    symmetric with maybe_load_plist being gated). Returns
    ``{"removed": bool, "path": str}``.
    """
    home = _home_host()
    slug = _resolve_slug(agent)
    agents_root = (home / ".mc" / "agents").resolve()
    workspace = home / ".mc" / "agents" / slug
    resolved_workspace = workspace.resolve()
    if resolved_workspace == agents_root or agents_root not in resolved_workspace.parents:
        raise ValueError(
            f"refusing to remove host agent files outside {agents_root}: {resolved_workspace}"
        )
    existed = resolved_workspace.is_dir()
    if existed:
        shutil.rmtree(resolved_workspace)
    return {"removed": existed, "path": str(resolved_workspace)}
