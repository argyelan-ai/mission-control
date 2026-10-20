"""Write an agent's env file only when its content really changes.

Host agents run under launchd, and a launchd job may watch its own
``agent.env`` (``WatchPaths``) so that a real change — a new model, a new
token — restarts the bridge and is picked up. launchd cannot tell a real
change from a rewrite with the same bytes: every write is a restart, and a
restart cuts off whatever the agent is doing (chat turn, task step).

Live incident 2026-10-20: a periodic sync rewrote a host agent's unchanged
``agent.env`` about every 10.5 minutes, and its WatchPaths bridge restarted
each time, all day. Writers that may run more than once for the same state
(syncs, healers, re-renders) therefore go through
:func:`write_env_file_if_changed`.

How the file is written, and why:

* **Compare first.** Same bytes → no write at all: mtime, inode and mode stay
  untouched, so no watcher fires.
* **In place, never tmp + rename.** A rename gives the path a NEW inode. A
  single-file bind mount into a container keeps pointing at the old one (the
  container silently reads the stale file — the same trap as ``sed -i`` on a
  mounted ``.env``). Writing into the existing inode keeps every reader on
  the same file, exactly like the writers this replaces.
* **Owner-only.** ``agent.env`` carries the agent token: the file is created
  0600 and ``fchmod``-ed to 0600 before any content lands (same rule as
  ``host_provisioning._write_owner_only_file``).
* The content is written right after the truncate in one ``os.write`` loop
  (a few hundred bytes), the same window the previous ``write_text`` had.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_OWNER_ONLY = 0o600


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
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _OWNER_ONLY)
    try:
        os.fchmod(fd, _OWNER_ONLY)
        view = memoryview(new)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)
    logger.info("agent env: wrote %s (%d bytes, content changed)", path, len(new))
    return True
