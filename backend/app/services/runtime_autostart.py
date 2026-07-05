"""Engine Control v0: Autostart-Flag via SSH (ADR-057).

First building block of Cockpit v2 ("MC follows the engine" → "MC steers the
engine"). Some inference engines (e.g. a vLLM/sparkrun systemd unit on the
DGX Spark) decide whether to start on boot based on the *presence* of a flag
file on disk. This service touches/removes that file over SSH on the
runtime's bound host (host_id → hosts, ADR-048 host_resolver chain) and
verifies the resulting state — never trusts the write blindly.

Reuses runtime_manager._ssh_run (asyncssh, same connect/timeout semantics as
every other lifecycle op) instead of a second SSH implementation.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass

import asyncssh

from app.services.host_resolver import ResolvedHost
from app.services.runtime_manager import _ssh_run

_STATUS_TIMEOUT = 10
_TOGGLE_TIMEOUT = 10


class AutostartHostUnreachable(Exception):
    """Raised when the SSH host cannot be reached — never surfaces a stack
    trace to the UI, callers turn this into a friendly 'unknown' state."""


@dataclass(frozen=True)
class AutostartStatus:
    enabled: bool | None  # None = unknown (host unreachable)
    reachable: bool


def _quoted_test_command(flag_path: str) -> str:
    """`test -f <path>` with the path shell-quoted — flag_path is operator-set
    (via PATCH /runtimes/db/{slug}), never interpolated raw into the remote
    shell command."""
    return f"test -f {shlex.quote(flag_path)}"


async def _run_or_unreachable(command: str, *, host: ResolvedHost | None, timeout: float) -> tuple[str, str, int]:
    try:
        return await _ssh_run(command, host=host, timeout=timeout)
    except (asyncssh.Error, OSError, TimeoutError, RuntimeError) as exc:
        raise AutostartHostUnreachable(str(exc)) from exc


async def get_autostart_status(
    flag_path: str, *, host: ResolvedHost | None
) -> AutostartStatus:
    """Checks whether the autostart flag file currently exists on the host.

    Returns enabled=None (reachable=False) instead of raising when the host
    can't be reached — a status probe must never 500 the /runtimes page.
    """
    try:
        _, _, exit_code = await _run_or_unreachable(
            _quoted_test_command(flag_path), host=host, timeout=_STATUS_TIMEOUT
        )
    except AutostartHostUnreachable:
        return AutostartStatus(enabled=None, reachable=False)
    return AutostartStatus(enabled=(exit_code == 0), reachable=True)


async def set_autostart(
    flag_path: str, enabled: bool, *, host: ResolvedHost | None
) -> AutostartStatus:
    """Touches (enabled=True) or removes (enabled=False) the flag file, then
    reads the state back to confirm the write actually landed.

    Raises AutostartHostUnreachable if the host can't be reached at all —
    callers turn that into a clear operator-facing error (not a stack trace).
    """
    quoted = shlex.quote(flag_path)
    command = f"touch {quoted}" if enabled else f"rm -f {quoted}"
    await _run_or_unreachable(command, host=host, timeout=_TOGGLE_TIMEOUT)
    return await get_autostart_status(flag_path, host=host)
