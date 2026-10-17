"""Shared-secret auth for the cli-bridge host helper (scripts/cli-bridge.py).

The helper starts agents and attaches terminals on the host, so it only
answers requests that carry ``X-MC-Bridge-Token``. Every backend caller of
``settings.free_code_bridge_url`` — HTTP and the WebSocket PTY proxy — takes
its headers from :func:`bridge_headers` here; nothing else builds them.

Token source, first match wins:

1. ``CLI_BRIDGE_TOKEN`` (settings.cli_bridge_token) — explicit, e.g. Linux
   hosts where the container cannot read the host file.
2. The helper's own token file, ``CLI_BRIDGE_TOKEN_FILE`` or
   ``<HOME_HOST>/.mc/secrets/cli-bridge.token``. The backend and worker
   containers bind-mount ``~/.mc`` 1:1, so on a standard install they read
   the very file the helper created — no copy in ``.env`` needed. Read on
   every call (tiny file) so a rotated token is picked up without a restart.

No token found → no header. The old helper ignores headers, the new one
answers 401; either way the token value never reaches a log line.
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.config import settings

logger = logging.getLogger("mc.cli_bridge_auth")

TOKEN_HEADER = "X-MC-Bridge-Token"

# Log "no token" once per process, not on every poll.
_warned_missing = False


def token_file_path() -> Path:
    explicit = (settings.cli_bridge_token_file or "").strip()
    if explicit:
        return Path(explicit)
    return Path(settings.home_host) / ".mc" / "secrets" / "cli-bridge.token"


def bridge_token() -> str | None:
    global _warned_missing
    explicit = (settings.cli_bridge_token or "").strip()
    if explicit:
        return explicit
    path = token_file_path()
    try:
        token = path.read_text().strip()
    except OSError:
        token = ""
    if token:
        return token
    if not _warned_missing:
        _warned_missing = True
        logger.warning(
            "cli-bridge token not configured (CLI_BRIDGE_TOKEN empty, %s not readable) — "
            "requests go out without %s; an updated helper will answer 401",
            path, TOKEN_HEADER,
        )
    return None


def bridge_headers() -> dict[str, str]:
    """Headers every request to the helper must carry (empty if no token)."""
    token = bridge_token()
    return {TOKEN_HEADER: token} if token else {}


def bridge_ws_base_url() -> str:
    """ws:// base of the helper's PTY server (HTTP port 18792 → WS 18793)."""
    url = settings.free_code_bridge_url.replace("http://", "ws://").replace("https://", "wss://")
    return url.replace(":18792", ":18793")
