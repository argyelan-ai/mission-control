"""Every ``docker compose exec backend|mc-worker`` in the repo names ``-u mcuser``.

Since the host-uid fix the backend image starts as root (the entrypoint
remaps ``mcuser`` to PUID/PGID and drops privileges). ``docker compose exec``
does not go through the entrypoint, so without ``-u mcuser`` a one-off
command runs as root with ``HOME=/root`` — and on Linux leaves root-owned
files in ``~/.mc``. Compose has no separate "default exec user": setting
``user:`` on the service would also start the entrypoint as non-root and
switch the remap off. So the rule lives in the commands, and this test keeps
scripts and docs from drifting back.

ADRs under docs/decisions/ are history and are not rewritten.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SUFFIXES = {".sh", ".py", ".md", ".yml", ".yaml", ".example", ""}
SKIP_DIRS = {"node_modules", ".git", ".venv", "decisions", "archive", "__pycache__", ".next"}
EXEC = re.compile(r"compose(?: -p [\w-]+)?(?: -f \S+)* exec((?: -[A-Za-z]+(?: \S+)?)*) (backend|mc-worker)\b")


def _files():
    for path in REPO.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts) or path.name == Path(__file__).name:
            continue
        if path.name == "Makefile" or path.suffix in SUFFIXES and path.suffix:
            yield path
        elif path.name in {".env.example"}:
            yield path


def offenders() -> list[str]:
    out = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in EXEC.finditer(line):
                if "-u mcuser" not in m.group(1) and "--user mcuser" not in m.group(1):
                    out.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
    return out


def test_every_compose_exec_into_the_backend_runs_as_mcuser():
    assert offenders() == []


def test_the_guard_catches_a_plain_exec(tmp_path, monkeypatch):
    # Sabotage probe built in: a file with the old form must be reported.
    bad = tmp_path / "runbook.md"
    bad.write_text("run `docker compose exec backend alembic upgrade head`\n")
    good = tmp_path / "ok.sh"
    good.write_text("docker compose exec -T -u mcuser backend python3 x.py\n")
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    found = offenders()
    assert len(found) == 1 and "runbook.md" in found[0]
