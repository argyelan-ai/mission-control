"""Where head runs live. Always derived from settings, never from the
process home: inside the container $HOME is not the operator's (spec §6.1)."""
from __future__ import annotations

import re
from pathlib import Path

from app.config import settings

RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def heads_root() -> Path:
    return Path(settings.heads_root)


def run_dir(run_id: str) -> Path:
    if not RUN_ID_RE.match(run_id or ""):
        raise ValueError("invalid run id")
    return heads_root() / run_id


def spool_dir() -> Path:
    return heads_root() / "spool"


def locks_dir() -> Path:
    return heads_root() / "locks"


def vault_jobs_dir() -> Path:
    return Path(settings.vault_path) / "jobs"
