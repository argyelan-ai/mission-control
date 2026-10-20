"""How MC writes an agent's secret files on the host — the one place for it.

Two functions:

* :func:`write_owner_only_file` — the write itself: owner-only from the first
  byte, in place. Used for ``agent.env``, a host omp agent's ``models.yml``
  and the claude harness' ``.mcp.json`` (all three can carry secrets).
* :func:`write_env_file_if_changed` — compare first, then the same write.

Why compare first: host agents run under launchd, and a launchd job may
watch its own ``agent.env`` (``WatchPaths``) so that a real change — a new
model, a new token — restarts the bridge and is picked up. launchd cannot
tell a real change from a rewrite with the same bytes: every write is a
restart, and a restart cuts off whatever the agent is doing (chat turn, task
step). Live incident 2026-10-20: a periodic sync rewrote a host agent's
unchanged ``agent.env`` about every 10.5 minutes, and its WatchPaths bridge
restarted each time, all day. Writers that may run more than once for the
same state (syncs, healers, re-renders) therefore go through
:func:`write_env_file_if_changed`. One-shot writers that always carry a
freshly minted token (provisioning, bootstrap) would never compare equal
anyway.

How the file is written, and why:

* **Same bytes → no write at all**: mtime, inode and mode stay untouched, so
  no watcher fires.
* **In place, never tmp + rename.** A rename gives the path a NEW inode. A
  single-file bind mount into a container keeps pointing at the old one (the
  container silently reads the stale file — the same trap as ``sed -i`` on a
  mounted ``.env``). Writing into the existing inode keeps every reader on
  the same file.
* **Owner-only from the moment the file exists** — never write-then-chmod,
  which leaves a window (governed by the process umask) where secret content
  sits on disk at whatever mode umask produces. ``os.open``'s mode argument
  is umask-masked too, so the descriptor is ``fchmod``-ed to 0600 before any
  content lands (2026-07-10 E2E rerun, Befund 4).
* **Exactly the compared bytes**: the content is encoded as UTF-8 once and
  written in binary mode — a locale-default text encoding could make every
  comparison differ and every sync write again.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_OWNER_ONLY = 0o600


def write_owner_only_file(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` in place, mode 0600 from the first byte."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _OWNER_ONLY)
    try:
        os.fchmod(fd, _OWNER_ONLY)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "wb") as f:
        f.write(content.encode("utf-8"))


def write_env_file_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` only if the file's bytes differ.

    Returns True when the file was written (created or changed), False when
    it already held exactly ``content`` and was left untouched.
    """
    new = content.encode("utf-8")
    try:
        if path.read_bytes() == new:
            return False
    except FileNotFoundError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    write_owner_only_file(path, content)
    logger.info("agent env: wrote %s (%d bytes, content changed)", path, len(new))
    return True
