"""Agent-Bootstrap helpers for host-side agents (Phase 24, HERM-01).

Phase 24 (Hermes Worker Foundation) — host-side provisioning helpers.
Currently exposes ``bootstrap_hermes_agent`` which renders agent.env
(chmod 600), bootstraps the launchd plist, and transitions the
agent's provision_status local → provisioning → provisioned.

Pattern source: ``app/routers/cli_terminal.py::provision_cli_agent``
(token gen) and ``scripts/hermes-bridge.py`` (env-file parsing).

ADR-029: Hermes is single_instance host-side worker. Provisioning
runs from inside the backend Docker container but writes to
``$HOME/.mc/agents/hermes`` which is bind-mounted on the
host with identical absolute paths. ``launchctl bootstrap`` is
attempted from inside the container — on failure (e.g. container
cannot reach host's launchd domain) we surface the error in the
response so the operator can run the bootstrap manually on the host.

Tests: ``backend/tests/test_hermes_provisioning.py``.
"""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.config import settings, effective_host_ssh_user
from app.models.agent import Agent
from app.models.runtime import Runtime
from app.routers.internal import build_runtime_env
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger("mc.agent_bootstrap")


# ── Constants ─────────────────────────────────────────────────────────────────

HERMES_TMUX_SESSION = "hermes-worker"
HERMES_PLIST_PATH_REL = "Library/LaunchAgents/com.mc.hermes-bridge.plist"

# Grok Build CLI host harness (ADR-066). Headless per-dispatch — no tmux session.
GROK_PLIST_PATH_REL = "Library/LaunchAgents/com.mc.grok-bridge.plist"
# ADR-068: grok now runs as a persistent TUI in a tmux session (paste model),
# not a headless per-dispatch subprocess. Session name = slug convention.
GROK_TMUX_SESSION = "grok"


def _home_host() -> Path:
    """Resolve host-side HOME using HOME_HOST override (per project memory).

    See feedback_home_host_pattern: code that touches ~/.openclaw inside
    the backend container must respect HOME_HOST so tests + container
    runs both land at the right path.
    """
    home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
    return Path(home)


def _format_env_file(env: dict[str, str]) -> str:
    """Render a KEY=VALUE env file. Values are single-quoted for safety."""
    lines = []
    for key in sorted(env.keys()):
        val = env[key]
        # Escape single quotes by closing+escaping+reopening
        safe = val.replace("'", "'\"'\"'")
        lines.append(f"{key}='{safe}'")
    return "\n".join(lines) + "\n"


def _unquote_env_value(raw: str) -> str:
    """Exact inverse of `_format_env_file`'s single-quote escaping.

    `_format_env_file` wraps every value in single quotes and rewrites each
    embedded ``'`` as ``'"'"'``. A reader that only does ``.strip("'")`` peels
    the outer quotes but leaves the ``'"'"'`` sequences intact — so on the next
    write they get re-escaped, and any value carrying a literal quote grows ~3×
    per round-trip (this is how a 64-char token ballooned to 13 KB). Reversing
    the escaping makes the read/write round-trip idempotent, so growth can never
    start regardless of how a stray quote got seeded.
    """
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1].replace("'\"'\"'", "'")
    # Unquoted / partially-quoted fallback (legacy hand-edited files).
    return raw.strip("'")


# ── Hermes Bootstrap ───────────────────────────────────────────────────────────


async def _default_host_agent_board_id(session: AsyncSession):
    """Resolve the canonical MC Development board for host-runtime agents.

    Returns None if not found (caller logs + skips assignment, no crash).
    Phase 25 / ADR-030: host-side autonomous workers (Hermes) need a board
    binding so board-scoped APIs (PATCH /agent/boards/{id}/tasks/{id}) work
    without manual DB UPDATE.
    """
    import uuid as _uuid  # noqa: F401  (typing hint only)
    from sqlmodel import select
    from app.models.board import Board
    result = await session.exec(select(Board).where(Board.name == "MC Development"))
    board = result.first()
    return board.id if board else None


async def build_hermes_agent_env(
    runtime: Runtime,
    mc_agent_token: str,
    *,
    session: AsyncSession,
) -> dict[str, str]:
    """Compose env vars for the Hermes agent.env file.

    Combines runtime-derived OPENAI_BASE_URL/OPENAI_MODEL (via
    ``build_runtime_env``) with MC_AGENT_TOKEN, MC_BASE_URL, HOME, PATH.

    Plan 24-02 only set OPENAI_*; this plan owns MC_BASE_URL injection
    in agent.env so hermes-worker can call back into MC's API.
    """
    runtime_env = await build_runtime_env(runtime, session)
    home = str(_home_host())
    env = {
        "MC_AGENT_TOKEN": mc_agent_token,
        "MC_BASE_URL": settings.mc_base_url.rstrip("/"),
        "HOME": home,
        "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    }
    # OPENAI_BASE_URL / OPENAI_MODEL win over any defaults above
    env.update(runtime_env)
    return env


def _assert_singleton_slug(agent: Agent, expected: str) -> None:
    """Refuse to bootstrap a singleton host bridge onto the wrong agent.

    The hermes/grok host harnesses are SINGLETONS: their whole provisioning
    path (config dir ``~/.mc/agents/<expected>``, the one
    ``com.mc.<expected>-bridge.plist``) is hardcoded to a single slug, because
    there is exactly one hermes-bridge and one grok-bridge on the host. If a
    *different* agent (e.g. a wizard-created "Dev" that picked harness=hermes)
    reaches this bootstrap, the env-write step would silently overwrite the real
    singleton's ``agent.env`` with a foreign token — corrupting a live agent
    (found 2026-07-12: creating "Dev" clobbered Hermes's agent.env). Guard here
    as defense-in-depth; the provision router (routers/agents.py) rejects it
    earlier with a 422, but the service layer must never be able to clobber even
    when called directly.
    """
    slug = (agent.slug or agent.name or "").lower().replace(" ", "-")
    if slug != expected:
        raise ValueError(
            f"harness {expected!r} is a singleton host bridge bound to the "
            f"pre-seeded {expected!r} agent and cannot be provisioned onto "
            f"agent {agent.name!r} (slug {slug!r}). Use the openclaude or omp "
            f"harness for a generic host agent."
        )


def _launchctl_bootstrap_argv(plist_path: Path) -> list[str]:
    """Build the argv that loads ``plist_path`` into the host's launchd GUI domain.

    launchd only exists on macOS, but the backend runs inside a Linux Docker
    container where ``launchctl`` is absent — a plain ``subprocess.run(["launchctl",
    …])`` there dies with ``[Errno 2] No such file or directory: 'launchctl'``
    (found 2026-07-12: every hermes/grok re-provision failed at this exact line,
    and the failure fired *after* the agent.env write, so a mis-targeted bootstrap
    could clobber a live agent's env yet never finish). When launchctl is present
    (a Mac-host backend or the CI/local test box) we call it directly; otherwise we
    SSH to the host and run it there — the same host-SSH path
    ``cli_terminal._ssh_host`` already uses for start/stop/restart. ``$(id -u)`` is
    evaluated ON the host so the GUI domain targets the host login's uid, never the
    container user's.
    """
    if shutil.which("launchctl"):
        return ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)]
    remote = f"launchctl bootstrap gui/$(id -u) {shlex.quote(str(plist_path))}"
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=5",
        "-i", "/home/mcuser/.ssh/id_rsa",
        f"{effective_host_ssh_user()}@host.docker.internal",
        remote,
    ]


def _run_launchctl_bootstrap(plist_path: Path) -> dict[str, Any]:
    """Run ``launchctl bootstrap gui/$(id -u) <plist>`` (locally or via host SSH).

    Tolerates "already loaded" / "already bootstrapped" — these are
    benign for idempotent re-provision. Returns dict with returncode,
    stderr, ``loaded`` bool, and ``already`` bool.

    Raises ``RuntimeError`` on hard failures (non-zero exit that is
    not the already-loaded case). ssh passes the remote launchctl's exit
    code straight through, so rc==37 (already loaded) still round-trips.
    """
    cmd = _launchctl_bootstrap_argv(plist_path)
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        cmd, capture_output=True, text=True, check=False
    )
    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    combined = f"{stdout}\n{stderr}".lower()
    already = (
        "already loaded" in combined
        or "service already" in combined
        or "already bootstrapped" in combined
        or proc.returncode == 37  # macOS launchctl: service already loaded
    )
    result: dict[str, Any] = {
        "returncode": proc.returncode,
        "stderr": stderr,
        "stdout": stdout,
        "loaded": proc.returncode == 0 or already,
        "already": already,
    }
    if proc.returncode == 0:
        logger.info("launchctl bootstrap %s: ok", plist_path.name)
    elif already:
        logger.info("launchctl bootstrap %s: already loaded (idempotent)", plist_path.name)
    else:
        # Hard failure — surface to caller for rollback decision.
        logger.error(
            "launchctl bootstrap %s failed: rc=%s stderr=%s",
            plist_path.name, proc.returncode, stderr,
        )
        raise RuntimeError(
            f"launchctl bootstrap failed (rc={proc.returncode}): {stderr or stdout}"
        )
    return result


async def bootstrap_hermes_agent(
    session: AsyncSession,
    agent: Agent,
    runtime: Runtime,
) -> dict[str, Any]:
    """Provision the Hermes host-side worker.

    Steps (idempotent — safe to re-run):
      1. Generate fresh PBKDF2 MC_AGENT_TOKEN.
      2. Build env via ``build_hermes_agent_env`` (OPENAI_* + MC_*).
      3. mkdir -p ``$HOME_HOST/.mc/agents/hermes`` (config) +
         ``$HOME_HOST/.mc/workspaces/hermes`` (browsable task workspace).
      4. mkdir -p ``$HOME_HOST/.mc/agents/hermes/logs`` (mode 755).
      5. Write ``agent.env`` (mode 600) — replaces any existing file.
      6. ``launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mc.hermes-bridge.plist``
         — tolerates "already loaded".
      7. agent.provision_status = 'provisioned', set provisioned_at + workspace_path.
      8. emit ``agent.hermes_provisioned`` activity event.

    On failure: caller is expected to rollback ``agent.provision_status``
    to 'local' and emit ``agent.provision_failed`` (see
    ``provision_agent_on_gateway`` orchestration).

    Returns dict with token (one-time visible), env_path, plist_loaded,
    plist_already, tmux_session, workspace_path.
    """
    _assert_singleton_slug(agent, "hermes")
    home = _home_host()
    # Config lives in the sensitive ~/.mc/agents/hermes root (env, logs,
    # entrypoint — never browsable via the Files API). The task WORKSPACE is
    # the browsable ~/.mc/workspaces/hermes root, matching the rest of the
    # fleet (cli-bridge agents + Boss all use ~/.mc/workspaces/<slug>). Before
    # this split Hermes worked inside its own config dir, so its work never
    # showed up under Files → Workspaces.
    config_dir = home / ".mc" / "agents" / "hermes"
    workspace = home / ".mc" / "workspaces" / "hermes"
    env_path = config_dir / "agent.env"
    logs_dir = config_dir / "logs"
    plist_path = home / HERMES_PLIST_PATH_REL

    # 1. Token
    raw_token, token_hash = generate_agent_token()

    # 2. Env
    env = await build_hermes_agent_env(runtime, raw_token, session=session)

    # 3. + 4. Directories (config dir + browsable workspace dir + logs)
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    workspace.mkdir(parents=True, exist_ok=True, mode=0o755)
    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o755)

    # 5. Write env file (mode 600)
    content = _format_env_file(env)
    env_path.write_text(content)
    os.chmod(env_path, 0o600)
    logger.info("hermes bootstrap: wrote %s (mode 600, %d keys)", env_path, len(env))

    # 6. launchctl bootstrap (best-effort, tolerates already-loaded)
    plist_result = _run_launchctl_bootstrap(plist_path)

    # 6.5 Auto-assign default board for host-runtime workers (Phase 25 / ADR-030).
    # Without this, board-scoped APIs reject Hermes with 403 "Agent not assigned
    # to this board" — see Plan 25-07 root cause #1 (smoke task 8d5cce68).
    if agent.board_id is None:
        default_board_id = await _default_host_agent_board_id(session)
        if default_board_id:
            agent.board_id = default_board_id
            logger.info(
                "bootstrap_hermes_agent: auto-assigned %s to MC Development board (%s)",
                agent.name, default_board_id,
            )
        else:
            logger.warning(
                "bootstrap_hermes_agent: 'MC Development' board not found — %s remains board_id=None",
                agent.name,
            )

    # 7. Persist
    agent.agent_token_hash = token_hash
    agent.workspace_path = str(workspace)
    agent.provision_status = "provisioned"
    agent.provisioned_at = utcnow()
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()
    await session.refresh(agent)

    # Vault rotation mc_token_{slug}: keeps /internal/bootstrap consistent with
    # the freshly written agent.env (otherwise the vault would serve a stale token).
    from app.services.secrets_helper import upsert_agent_token_secret
    await upsert_agent_token_secret(session, agent, raw_token)

    # 8. Activity event
    await emit_event(
        session,
        "agent.hermes_provisioned",
        f"{agent.name} (Hermes host worker) provisioniert — tmux session '{HERMES_TMUX_SESSION}'",
        severity="info",
        agent_id=agent.id,
        board_id=agent.board_id,
    )

    return {
        "token": raw_token,  # one-time visible
        "env_path": str(env_path),
        "plist_loaded": plist_result["loaded"],
        "plist_already": plist_result["already"],
        "tmux_session": HERMES_TMUX_SESSION,
        "workspace_path": str(workspace),
    }


# ── Grok Bootstrap (ADR-066) ────────────────────────────────────────────────────


async def build_grok_agent_env(
    runtime: Runtime,
    mc_agent_token: str,
    *,
    session: AsyncSession,
) -> dict[str, str]:
    """Compose env vars for the grok agent.env file.

    Unlike Hermes, grok reads NO provider env: the Grok Build CLI talks only to
    xAI cloud over its own OAuth (~/.grok/auth.json), so there is deliberately no
    OPENAI_*/ANTHROPIC_* here — only the MC_* control-plane vars grok-bridge.py
    needs to poll/heartbeat and the copied `mc` CLI needs to call back. `runtime`
    is accepted for interface symmetry (display anchor) but its endpoint/model
    are not injected (ADR-066). `session` is unused — kept for the adapter
    Protocol signature.
    """
    home = str(_home_host())
    return {
        "MC_AGENT_TOKEN": mc_agent_token,
        "MC_BASE_URL": settings.mc_base_url.rstrip("/"),
        "HOME": home,
        "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    }


async def bootstrap_grok_agent(
    session: AsyncSession,
    agent: Agent,
    runtime: Runtime,
) -> dict[str, Any]:
    """Provision the grok host-side worker (headless Grok Build CLI, ADR-066).

    Parallels bootstrap_hermes_agent but for the headless model:
      1. Generate fresh MC_AGENT_TOKEN.
      2. Build env via build_grok_agent_env (MC_* only — NO provider env).
      3. mkdir config (~/.mc/agents/grok) + browsable workspace
         (~/.mc/workspaces/grok) + logs.
      4. Write agent.env (mode 600).
      5. launchctl bootstrap ~/Library/LaunchAgents/com.mc.grok-bridge.plist
         (tolerates already-loaded).
      6. Auto-assign the MC Development board when unset.
      7. provision_status = 'provisioned', persist, rotate vault token.
      8. emit agent.grok_provisioned event.

    Returns the same dict shape as bootstrap_hermes_agent (the provision endpoint
    reads these keys uniformly). `tmux_session` is the persistent grok TUI session
    (ADR-068) — the Sessions page mounts its terminal via
    cli_terminal._HOST_AGENT_TMUX_TARGETS["grok"].
    """
    _assert_singleton_slug(agent, "grok")
    home = _home_host()
    config_dir = home / ".mc" / "agents" / "grok"
    workspace = home / ".mc" / "workspaces" / "grok"
    env_path = config_dir / "agent.env"
    logs_dir = config_dir / "logs"
    plist_path = home / GROK_PLIST_PATH_REL

    raw_token, token_hash = generate_agent_token()
    env = await build_grok_agent_env(runtime, raw_token, session=session)

    config_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    workspace.mkdir(parents=True, exist_ok=True, mode=0o755)
    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o755)

    content = _format_env_file(env)
    env_path.write_text(content)
    os.chmod(env_path, 0o600)
    logger.info("grok bootstrap: wrote %s (mode 600, %d keys)", env_path, len(env))

    plist_result = _run_launchctl_bootstrap(plist_path)

    if agent.board_id is None:
        default_board_id = await _default_host_agent_board_id(session)
        if default_board_id:
            agent.board_id = default_board_id
            logger.info(
                "bootstrap_grok_agent: auto-assigned %s to MC Development board (%s)",
                agent.name, default_board_id,
            )
        else:
            logger.warning(
                "bootstrap_grok_agent: 'MC Development' board not found — %s remains board_id=None",
                agent.name,
            )

    agent.agent_token_hash = token_hash
    agent.workspace_path = str(workspace)
    agent.provision_status = "provisioned"
    agent.provisioned_at = utcnow()
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()
    await session.refresh(agent)

    from app.services.secrets_helper import upsert_agent_token_secret
    await upsert_agent_token_secret(session, agent, raw_token)

    await emit_event(
        session,
        "agent.grok_provisioned",
        f"{agent.name} (Grok host worker) provisioniert — grok-bridge TUI session '{GROK_TMUX_SESSION}'",
        severity="info",
        agent_id=agent.id,
        board_id=agent.board_id,
    )

    return {
        "token": raw_token,  # one-time visible
        "env_path": str(env_path),
        "plist_loaded": plist_result["loaded"],
        "plist_already": plist_result["already"],
        "tmux_session": GROK_TMUX_SESSION,  # ADR-068: persistent TUI (paste model)
        "workspace_path": str(workspace),
    }
