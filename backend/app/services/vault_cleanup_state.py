"""Checkpoint + log management for the autonomous Vault Cleanup orchestrator.

State lives in ~/.mc/vault.cleanup.state/ by default. All state files are
human-readable (JSON, plain text) so the operator can inspect or edit them
between phases. The whitelist.txt file is the manual intervention point —
operators can add note-paths that must NOT be archived even if heuristics match.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from pathlib import Path


def _default_root() -> Path:
    """Vault cleanup state lives on the host-mounted ~/.mc (HOME_HOST), not the
    container home. Backend runs as mcuser (HOME=/home/mcuser) but the data is
    bind-mounted at HOME_HOST (e.g. /Users/<login>) — a bare expanduser('~')
    would resolve to the non-existent, ephemeral /home/mcuser/.mc.
    See feedback_home_host_pattern."""
    home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
    return Path(home) / ".mc" / "vault.cleanup.state"


DEFAULT_ROOT = _default_root()


class VaultCleanupState:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _default_root()
        self._run_id: str | None = None

    def ensure(self) -> None:
        """Create the state directory and seed run.log + run_id (idempotent)."""
        self.root.mkdir(parents=True, exist_ok=True)
        log = self.root / "run.log"
        if not log.exists():
            log.write_text("")
        if self._run_id is None:
            id_file = self.root / "current_run.id"
            if id_file.exists():
                self._run_id = id_file.read_text().strip()
            else:
                self._run_id = (
                    dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
                    + "-"
                    + uuid.uuid4().hex[:6]
                )
                id_file.write_text(self._run_id)

    @property
    def run_id(self) -> str:
        if self._run_id is None:
            self.ensure()
        assert self._run_id is not None
        return self._run_id

    def set_checkpoint(self, phase: str, value: str) -> None:
        """Record the last-completed item for a resumable phase."""
        (self.root / f"{phase}.checkpoint").write_text(value)

    def get_checkpoint(self, phase: str) -> str | None:
        """Read the last checkpoint for a phase, or None if no prior run."""
        f = self.root / f"{phase}.checkpoint"
        return f.read_text().strip() if f.exists() else None

    def whitelist(self) -> set[str]:
        """Return the manually-curated keep-list (strips comments + blanks)."""
        f = self.root / "whitelist.txt"
        if not f.exists():
            return set()
        out: set[str] = set()
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.add(line)
        return out

    def log(self, level: str, message: str) -> None:
        """Append a timestamped line to run.log."""
        ts = dt.datetime.utcnow().isoformat(timespec="seconds")
        with (self.root / "run.log").open("a") as f:
            f.write(f"{ts}  {level:5s} {message}\n")

    def write_manifest(self, name: str, data: dict) -> None:
        """Persist a named manifest as JSON."""
        (self.root / f"{name}.json").write_text(json.dumps(data, indent=2, default=str))

    def read_manifest(self, name: str) -> dict | None:
        """Read a named manifest or return None if absent."""
        f = self.root / f"{name}.json"
        return json.loads(f.read_text()) if f.exists() else None
