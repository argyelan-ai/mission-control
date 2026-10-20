"""The Claude account behind every Claude agent (Welle 2, PR 2-4, ADR-093).

One vault token (``claude_code_oauth_token``) signs in all Docker agents on
Claude: each container fetches it at its bootstrap (``routers/internal.py``
→ ``harness_compat.resolve_provider_credentials``). This module answers the
three questions the settings card asks and adds no second way to store it:

- **Is it working?** — the registry check ``secret`` / ``claude_code_oauth_token``
  (``services/config_checks``). Reading the status never calls Claude; the
  real one-token request runs only on save and on a click (decision E8a).
- **Who still has the old one?** — a running Claude container that started
  before the token last changed got the old value at its bootstrap. A restart
  (``POST /agents/{id}/restart``) bootstraps it again.
- **Reconnect without a terminal** — ``claude setup-token`` runs in a
  throwaway container from the Claude agent image. MC shows the sign-in link
  it prints, types the code the user pastes back, reads the printed token,
  stores it through ``secrets_helper.upsert_secret_by_key`` (the same helper
  the other vault writers use) and runs the check once. The token never
  leaves this module: no response, no log line carries it, and the container
  — with its terminal scrollback — is removed right after.

Host agents (on the machine itself, not in Docker) read the token from
``.env`` instead; the status counts them apart so the card can say so.

**The .env fallback.** Installs from before the vault keep
``CLAUDE_CODE_OAUTH_TOKEN`` in the root ``.env``; ``scripts/start-all.sh``
copies it into ``docker/.env.shared``, which every Claude container loads. The
entrypoint only replaces it when the bootstrap returns a vault token, so with
no vault token the containers still sign in with the .env one — and removing
the vault token does not stop them. The status therefore reports whether such
a fallback exists (``env_fallback``, a yes/no — the value is never read out).

**Errors are codes** (``login.error``: ``image_unavailable``,
``setup_token_failed``, ``timeout``, ``code_rejected``, ``store_failed``,
``saved_not_accepted``); the card words them in the user's language.

**One worker only.** The running login lives in this process (``_LOGIN``).
That is right while the backend runs a single uvicorn worker (the backend
Dockerfile starts ``--workers 1``); with several workers a poll could land
on a worker that knows nothing about the login.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets as _secrets
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from app.services import docker_gateway

logger = logging.getLogger("mc.claude_account")

TOKEN_KEY = "claude_code_oauth_token"
CHECK_KIND = "secret"

#: Prefix of the throwaway login containers; also their docker label value.
LOGIN_CONTAINER_PREFIX = "mc-claude-login-"
LOGIN_LABEL = "mc.role=claude-login"
_TMUX_SESSION = "mclogin"
#: ``claude setup-token`` quits ~0.5 s after printing the token, and a tmux
#: session ends with its command — taking the pane (and the token) with it.
#: A shell that keeps running after claude keeps the pane readable until MC
#: has read it and removed the container (checked by hand with tmux:
#: ``'echo …'`` alone → "no server running"; ``'echo …; sleep 900'`` → readable).
_LOGIN_COMMAND = "claude setup-token; sleep 900"
#: The container ends itself after this long even if MC forgets it.
_CONTAINER_LIFETIME_S = 900
#: A login nobody finishes is given up after this long.
LOGIN_TIMEOUT_S = 600
#: How long submitting the code waits for Claude to print the token.
_CODE_WAIT_S = 30.0
_CODE_POLL_S = 1.0

_URL_RE = re.compile(r"https://\S*(?:oauth|authorize)\S*")
_TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_\-]{20,}")
_FAILURE_RE = re.compile(r"(?i)\b(invalid code|oauth error|authentication failed|login failed)\b")
#: What a pasted code may contain: the OAuth code plus its ``#state`` part.
CODE_RE = re.compile(r"^[A-Za-z0-9_\-#.~]{1,512}$")


# ── docker (one seam, patched in tests) ───────────────────────────────────


def _docker(args: list[str], timeout: float, input: str | None = None) -> tuple[int, str]:
    """Run ``docker <args>`` (``input`` goes to stdin) and return (exit code, stdout+stderr)."""
    try:
        proc = docker_gateway.run(
            args, capture_output=True, text=True, timeout=timeout, input=input
        )
    except docker_gateway.DockerDeferred as exc:
        # Update drain: the login container is not started; say why.
        return 1, str(exc)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


async def _run_docker(args: list[str], timeout: float = 15, input: str | None = None) -> tuple[int, str]:
    return await asyncio.to_thread(_docker, args, timeout, input)


# ── the .env fallback (yes/no only) ───────────────────────────────────────

_ENV_FILES = (".env", "docker/.env.shared")
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?CLAUDE_CODE_OAUTH_TOKEN\s*=\s*(.*)$")


def env_fallback_set() -> bool:
    """Whether ``.env`` or ``docker/.env.shared`` holds a non-empty token.

    Only the presence is reported, never the value. Both files are visible to
    the backend container (``.env`` read-only, ``docker/`` as the renderer's
    mount); the containers load ``docker/.env.shared``, which ``start-all.sh``
    regenerates from ``.env`` — so either one counts.
    """
    from app.config import settings

    root = Path(settings.mc_repo_path)
    for rel in _ENV_FILES:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _ENV_LINE_RE.match(line)
            if m and m.group(1).strip().strip("\"'").strip():
                return True
    return False


# ── status ────────────────────────────────────────────────────────────────


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_docker_time(value: str) -> datetime | None:
    """Docker prints RFC 3339 with nanoseconds; Python takes microseconds."""
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$", value.strip())
    if not m:
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    try:
        return datetime.fromisoformat(f"{m.group(1)}.{frac}{tz}")
    except ValueError:
        return None


async def _claude_agents(session: AsyncSession):
    """Agents whose provider is the Claude account, split by where they run."""
    from app.models.agent import Agent
    from app.models.runtime import Runtime
    from app.services.docker_agent_sync import agent_is_archived, is_host_agent
    from app.services.harness_compat import uses_claude_account

    runtimes = {rt.id: rt for rt in (await session.exec(select(Runtime))).all()}
    docker_agents, host_agents = [], []
    for agent in (await session.exec(select(Agent).order_by(Agent.name))).all():
        if agent_is_archived(agent) or not uses_claude_account(runtimes.get(agent.runtime_id)):
            continue
        (host_agents if is_host_agent(agent) else docker_agents).append(agent)
    return docker_agents, host_agents


async def _container_starts(names: list[str]) -> dict[str, tuple[str, datetime | None]]:
    """``{container: (status, started_at)}`` for the containers that exist."""
    if not names:
        return {}
    _code, out = await _run_docker(
        ["inspect", "--format", "{{.Name}}|{{.State.Status}}|{{.State.StartedAt}}", *names]
    )
    # Exit code 1 only means "one of them does not exist" — the others still print.
    found: dict[str, tuple[str, datetime | None]] = {}
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        found[parts[0].lstrip("/")] = (parts[1], _parse_docker_time(parts[2]))
    return found


def _container_name(agent) -> str:
    # Same name the restart endpoint (routers/cli_terminal.py) acts on.
    from app.services.docker_agent_sync import agent_container_name

    return agent_container_name(agent)


async def stale_agents(session: AsyncSession, token_changed_at: datetime | None) -> list[dict]:
    """Running Claude containers that still hold the token from before
    ``token_changed_at`` (they fetched it at their last start)."""
    if token_changed_at is None:
        return []
    docker_agents, _host = await _claude_agents(session)
    starts = await _container_starts([_container_name(a) for a in docker_agents])
    stale = []
    for agent in docker_agents:
        status, started = starts.get(_container_name(agent), ("missing", None))
        if status != "running" or started is None or started >= token_changed_at:
            continue
        stale.append(
            {
                "id": str(agent.id),
                "name": agent.name,
                "busy": agent.current_task_id is not None,
                "started_at": started.isoformat(),
            }
        )
    return stale


async def account_status(session: AsyncSession) -> dict:
    """Everything the card shows. Never calls Claude (E8a)."""
    from app.models.secret import Secret
    from app.services import config_checks

    row = (await session.exec(select(Secret).where(Secret.key == TOKEN_KEY))).first()
    changed_at = _aware(row.updated_at or row.created_at) if row else None
    check = await config_checks.status_item(session, CHECK_KIND, TOKEN_KEY) if row else None
    docker_agents, host_agents = await _claude_agents(session)
    return {
        "configured": row is not None,
        "env_fallback": env_fallback_set(),
        "token_updated_at": changed_at.isoformat() if changed_at else None,
        "check": check,
        "claude_agents": len(docker_agents),
        "host_agents": len(host_agents),
        "stale_agents": await stale_agents(session, changed_at) if row else [],
        "login": login_view(),
    }


# ── reconnect: ``claude setup-token`` in a throwaway container ────────────


@dataclass
class _Login:
    container: str
    started: float = field(default_factory=time.monotonic)
    state: str = "starting"  # starting | waiting_for_code | verifying | connected | failed
    url: str | None = None
    error: str | None = None
    code_sent: bool = False


_LOGIN: _Login | None = None
_LOCK = asyncio.Lock()


def _reset_login_for_tests() -> None:
    global _LOGIN
    _LOGIN = None


def login_view() -> dict:
    """The login as the UI may see it — link and state, never the token."""
    if _LOGIN is None:
        return {"state": "idle", "url": None, "error": None}
    return {"state": _LOGIN.state, "url": _LOGIN.url, "error": _LOGIN.error}


def _login_image() -> str:
    from app.services.compose_renderer import CLAUDE_IMAGE

    return CLAUDE_IMAGE


async def _remove(container: str) -> None:
    await _run_docker(["rm", "-f", container], timeout=20)


async def _finish(state: str, error: str | None = None) -> None:
    """End the login: remove the container (and its scrollback) for good."""
    if _LOGIN is None:
        return
    _LOGIN.state, _LOGIN.error = state, error
    await _remove(_LOGIN.container)


async def start_login() -> dict:
    """Start ``claude setup-token``; a login still open is replaced."""
    global _LOGIN
    async with _LOCK:
        if _LOGIN is not None and _LOGIN.state not in ("connected", "failed"):
            await _remove(_LOGIN.container)
        name = f"{LOGIN_CONTAINER_PREFIX}{_secrets.token_hex(4)}"
        _LOGIN = _Login(container=name)
        code, out = await _run_docker(
            [
                "run", "-d", "--rm", "--name", name, "--label", LOGIN_LABEL,
                "--entrypoint", "sleep", _login_image(), str(_CONTAINER_LIFETIME_S),
            ],
            timeout=180,  # a first run may pull the image
        )
        if code != 0:
            logger.warning("claude login: could not start the login container (%s)", out.strip()[:200])
            _LOGIN.state = "failed"
            _LOGIN.error = "image_unavailable"
            return login_view()
        code, _ = await _run_docker(
            [
                "exec", name, "tmux", "new-session", "-d", "-s", _TMUX_SESSION,
                "-x", "400", "-y", "80", _LOGIN_COMMAND,
            ]
        )
        if code != 0:
            await _finish("failed", "setup_token_failed")
        logger.info("claude login: started in %s", name)
        return login_view()


async def _pane() -> str:
    assert _LOGIN is not None
    code, out = await _run_docker(
        ["exec", _LOGIN.container, "tmux", "capture-pane", "-p", "-J", "-S", "-400", "-t", _TMUX_SESSION]
    )
    return out if code == 0 else ""


async def _store_token(session: AsyncSession, token: str) -> bool:
    """Save through the shared vault helper, then run the real check once."""
    from app.routers.secrets import PROVIDER_TEMPLATES
    from app.services import config_checks
    from app.services.secrets_helper import upsert_secret_by_key

    template = next((t for t in PROVIDER_TEMPLATES if t["key"] == TOKEN_KEY), {})
    await upsert_secret_by_key(
        session,
        key=TOKEN_KEY,
        value=token,
        provider=template.get("provider"),
        label=template.get("label"),
        description=template.get("description"),
    )
    run = await config_checks.run_check(session, CHECK_KIND, TOKEN_KEY)
    return run.outcome.ok


async def _advance(session: AsyncSession) -> None:
    """Read the login terminal once and move the state on."""
    if _LOGIN is None or _LOGIN.state in ("connected", "failed"):
        return
    if time.monotonic() - _LOGIN.started > LOGIN_TIMEOUT_S:
        await _finish("failed", "timeout")
        return
    text = await _pane()
    token_match = _TOKEN_RE.search(text)
    if token_match:
        token = token_match.group(0)
        del text  # the scrollback held the token; drop our copy before anything else
        # The container goes first: its scrollback is the only other copy.
        await _remove(_LOGIN.container)
        try:
            ok = await _store_token(session, token)
        except Exception as exc:  # noqa: BLE001 — the login must end, not hang in "verifying"
            del token
            # Only the type: the message of a DB error could quote the value.
            logger.error("claude login: storing the new token failed (%s)", type(exc).__name__)
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            _LOGIN.state, _LOGIN.error = "failed", "store_failed"
            return
        del token
        _LOGIN.state = "connected"
        _LOGIN.error = None if ok else "saved_not_accepted"
        logger.info("claude login: new token stored (check %s)", "ok" if ok else "failed")
        return
    if _LOGIN.code_sent and _FAILURE_RE.search(text):
        await _finish("failed", "code_rejected")
        return
    if _LOGIN.url is None:
        url_match = _URL_RE.search(text)
        if url_match:
            _LOGIN.url = url_match.group(0).rstrip("│|.,)")
            _LOGIN.state = "waiting_for_code"


async def login_status(session: AsyncSession) -> dict:
    async with _LOCK:
        await _advance(session)
        return login_view()


class NoLogin(Exception):
    """A code arrived but no login is waiting for one."""


async def submit_code(session: AsyncSession, code: str) -> dict:
    """Type the pasted code into ``claude setup-token`` and wait for the token."""
    async with _LOCK:
        if _LOGIN is None or _LOGIN.state not in ("starting", "waiting_for_code", "verifying"):
            raise NoLogin()
        # The code goes in on stdin (tmux buffer), not as an argument: arguments
        # show up in the host's process list while docker runs.
        await _run_docker(
            ["exec", "-i", _LOGIN.container, "tmux", "load-buffer", "-b", "mccode", "-"], input=code
        )
        await _run_docker(
            ["exec", _LOGIN.container, "tmux", "paste-buffer", "-d", "-b", "mccode", "-t", _TMUX_SESSION]
        )
        await _run_docker(["exec", _LOGIN.container, "tmux", "send-keys", "-t", _TMUX_SESSION, "Enter"])
        _LOGIN.code_sent = True
        _LOGIN.state = "verifying"
        deadline = time.monotonic() + _CODE_WAIT_S
        while True:
            await _advance(session)
            if _LOGIN.state in ("connected", "failed") or time.monotonic() >= deadline:
                break
            await asyncio.sleep(_CODE_POLL_S)
        return login_view()


async def cancel_login() -> dict:
    global _LOGIN
    async with _LOCK:
        if _LOGIN is not None:
            await _remove(_LOGIN.container)
        _LOGIN = None
        return login_view()
