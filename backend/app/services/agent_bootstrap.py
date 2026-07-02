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
import subprocess
from pathlib import Path
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.config import settings
from app.models.agent import Agent
from app.models.runtime import Runtime
from app.routers.internal import build_runtime_env
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger("mc.agent_bootstrap")


# ── Constants ─────────────────────────────────────────────────────────────────

HERMES_TMUX_SESSION = "hermes-worker"
HERMES_PLIST_PATH_REL = "Library/LaunchAgents/com.mc.hermes-bridge.plist"


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


def _run_launchctl_bootstrap(plist_path: Path) -> dict[str, Any]:
    """Run ``launchctl bootstrap gui/$(id -u) <plist>``.

    Tolerates "already loaded" / "already bootstrapped" — these are
    benign for idempotent re-provision. Returns dict with returncode,
    stderr, ``loaded`` bool, and ``already`` bool.

    Raises ``RuntimeError`` on hard failures (non-zero exit that is
    not the already-loaded case).
    """
    uid = os.getuid()
    cmd = ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)]
    proc = subprocess.run(  # noqa: S603 — fixed cmd, no shell
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
      3. mkdir -p ``$HOME_HOST/.mc/agents/hermes`` (mode 755).
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
    home = _home_host()
    workspace = home / ".mc" / "agents" / "hermes"
    env_path = workspace / "agent.env"
    logs_dir = workspace / "logs"
    plist_path = home / HERMES_PLIST_PATH_REL

    # 1. Token
    raw_token, token_hash = generate_agent_token()

    # 2. Env
    env = await build_hermes_agent_env(runtime, raw_token, session=session)

    # 3. + 4. Directories
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

    # Vault-Rotation mc_token_{slug}: haelt /internal/bootstrap konsistent zum
    # frisch geschriebenen agent.env (sonst liefert der Vault einen stale Token).
    from app.services.secrets_helper import upsert_agent_token_secret
    await upsert_agent_token_secret(session, agent.name, raw_token)

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
