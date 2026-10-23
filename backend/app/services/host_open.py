"""Opening a file on the MC host ("Open on the Mac", "Show in Finder").

Both endpoints that do it — a task deliverable (`routers/tasks.py`) and the
Files page (`routers/files.py`) — go through this module:

* ``require_host_viewer`` — the action only makes sense for a viewer who sits
  at the MC host: the file opens on THAT screen. From a laptop over Tailscale
  or an iPad it would open unseen on the Mac (round-2 user tests t3 S1, r1 S4,
  t4, r2 N6). The client IP cannot tell the two apart — Tailscale Serve,
  Caddy and the Next dev proxy all connect to the backend from a local
  address. What can: the address the browser itself used, sent as ``Origin``
  on every POST. Without an Origin (a script) the forwarded or own ``Host``
  stands in. This is a guard against a misleading click, not a security
  boundary: any logged-in client can set these headers.
  The frontend applies the same rule to ``location.hostname`` and hides the
  buttons (``frontend-v2/src/lib/hostOnly.ts``).

* ``open_on_host`` — runs macOS ``open`` (``-R`` = reveal in Finder), directly
  on a Mac, through the host helper (``tools/mc-open-helper.py``) from
  Docker. A helper that is down or answers with an error raises
  ``HostOpenFailed``; the routers turn it into 502, so the UI never says
  "opened" when nothing happened (it used to answer ``{"ok": true}``).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import subprocess
import sys
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

logger = logging.getLogger("mc.host_open")

HELPER_URL = "http://host.docker.internal:8765/open"
HELPER_TIMEOUT_S = 3.0

HOST_ONLY_DETAIL = (
    "Opens on the computer MC runs on only - open MC there (via localhost) "
    "to use it, or download the file."
)
HELPER_FAILED_DETAIL = "The file could not be opened on the MC host (the open helper did not answer)."


class HostOpenFailed(Exception):
    """The host could not open the path (helper down, helper error, no `open`)."""


def in_docker() -> bool:
    """Does this backend run inside Docker (so ``open`` must go via the helper)?"""
    return os.path.exists("/.dockerenv") or sys.platform.startswith("linux")


# ── who is asking ──────────────────────────────────────────────────────────


_PORT_RE = re.compile(r":\d{1,5}$")


def _hostname(value: str | None) -> str:
    """The bare host name of a ``Host``-style value (``name[:port]``, ``[v6]:port``)."""
    v = (value or "").strip().lower()
    if v.startswith("["):
        end = v.find("]")
        return v[1:end] if end > 0 else ""
    if v.count(":") == 1:
        v = _PORT_RE.sub("", v)
    return v


def is_loopback_host(host: str | None) -> bool:
    """True for a host name that only ever means "this computer".
    Same rule as ``isLoopbackHostname`` in frontend-v2/src/lib/hostOnly.ts."""
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    h = h.rstrip(".")
    if not h:
        return False
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback and (ip.version == 6 or h.count(".") == 3)


def request_is_from_host(request: Request) -> bool:
    """Did the caller open MC on the MC host itself (see the module comment)?

    Order: the browser's ``Origin``; without one the first ``X-Forwarded-Host``
    (a proxy's record of the original host); then ``Host``. ``Origin: null``
    (a sandboxed page) is never the operator.
    """
    origin = request.headers.get("origin")
    if origin is not None:
        origin = origin.strip()
        if not origin or origin == "null":
            return False
        try:
            return is_loopback_host(urlsplit(origin).hostname)
        except ValueError:
            return False
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:
        return is_loopback_host(_hostname(forwarded.split(",")[0]))
    return is_loopback_host(_hostname(request.headers.get("host")))


# rule: R-host-open-at-host-only - a click from a laptop opened files unseen on the Mac
def require_host_viewer(request: Request) -> None:
    """403 unless the caller sits at the MC host (see the module comment)."""
    if not request_is_from_host(request):
        raise HTTPException(status_code=403, detail=HOST_ONLY_DETAIL)


# ── opening ────────────────────────────────────────────────────────────────


async def open_on_host(host_path: str, *, reveal: bool) -> None:
    """Open (or, with ``reveal``, show in Finder) ``host_path`` on the Mac.

    Raises ``HostOpenFailed`` when it did not happen as far as MC can see.
    """
    if in_docker():
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    HELPER_URL,
                    json={"path": host_path, "reveal": reveal},
                    timeout=HELPER_TIMEOUT_S,
                )
        except Exception as exc:  # noqa: BLE001 — any transport error: the helper is not there
            logger.warning("open helper unreachable: %s", type(exc).__name__)
            raise HostOpenFailed("helper unreachable") from exc
        if resp.status_code >= 400:
            logger.warning("open helper answered %s", resp.status_code)
            raise HostOpenFailed(f"helper answered {resp.status_code}")
        return
    try:
        subprocess.Popen(["open", "-R", host_path] if reveal else ["open", host_path])
    except OSError as exc:
        logger.warning("open failed: %s", type(exc).__name__)
        raise HostOpenFailed("open failed") from exc


async def open_on_host_or_502(host_path: str, *, reveal: bool) -> None:
    """``open_on_host`` for a router: a failure is a 502 with a plain reason."""
    try:
        await open_on_host(host_path, reveal=reveal)
    except HostOpenFailed as exc:
        raise HTTPException(status_code=502, detail=HELPER_FAILED_DETAIL) from exc
