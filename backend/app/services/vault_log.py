"""Vault Activity Log — append-only chronological ledger.

Writes to ``~/.mc/vault/log.md``. Every vault event (write, promote,
cascade, lint, decay, search) appends one line. File-locked via
``fcntl.flock()`` for concurrency safety across async workers.

Rotation: when log.md exceeds MAX_LINES, content is moved to
``log-YYYY-MM.md`` (monthly archive). The active log is truncated
to the most recent entries.

Fail-soft: all public methods swallow exceptions — a broken log
must never crash the caller (compactor, promoter, decay cron).
"""

from __future__ import annotations

import fcntl
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("mc.vault_log")

MAX_LINES = 10_000


class VaultLog:
    def __init__(self, vault_path: Path):
        self.log_path = vault_path / "log.md"
        self.vault_path = vault_path

    def append(self, action: str, title: str, agent: str) -> None:
        """Append a single line to log.md. Thread/process-safe via flock.

        Format: ``## [YYYY-MM-DD HH:MM] action | title | agent``

        Fails silently on any I/O error.
        """
        try:
            self._append_impl(action, title, agent)
        except Exception as e:
            logger.warning("vault_log append failed (non-fatal): %s", e)

    def _append_impl(self, action: str, title: str, agent: str) -> None:
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%d %H:%M")
        # Sanitise title: replace pipes to preserve the 3-field format
        safe_title = (title.strip() or "(untitled)").replace("|", "-")
        line = f"## [{ts}] {action} | {safe_title} | {agent}\n"

        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        fd = None
        try:
            fd = open(self.log_path, "a")
            fcntl.flock(fd, fcntl.LOCK_EX)
            fd.write(line)
            fd.flush()
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    fd.close()
                except Exception:
                    pass

        # Check rotation after releasing the append lock
        self._maybe_rotate()

    def _maybe_rotate(self) -> None:
        """Rotate log.md when it exceeds MAX_LINES.

        All file reading happens INSIDE the exclusive lock to avoid TOCTOU
        races where concurrent writers between a lockless read and a locked
        truncate would lose entries.
        """
        if not self.log_path.exists():
            return

        fd = None
        try:
            fd = open(self.log_path, "r+")
            fcntl.flock(fd, fcntl.LOCK_EX)

            content = fd.read()
            lines = content.split("\n")
            if len(lines) <= MAX_LINES:
                return  # no rotation needed

            now = datetime.now(timezone.utc)
            archive_name = f"log-{now.strftime('%Y-%m')}.md"
            archive_path = self.vault_path / archive_name

            # Split: older lines go to archive, recent lines stay
            split_at = len(lines) - MAX_LINES
            old_lines = lines[:split_at]
            new_lines = lines[split_at:]

            # Append old lines to archive (may already exist from prior rotation)
            with open(archive_path, "a") as af:
                af.write("\n".join(old_lines))
                if old_lines and not old_lines[-1].endswith("\n"):
                    af.write("\n")

            # Truncate current log to recent lines
            fd.seek(0)
            fd.truncate()
            fd.write("\n".join(new_lines))

            logger.info(
                "vault_log rotated: %d lines -> %s, %d lines remain",
                len(old_lines), archive_name, len(new_lines),
            )
        except Exception as e:
            logger.warning("vault_log rotation failed (non-fatal): %s", e)
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    fd.close()
                except Exception:
                    pass
