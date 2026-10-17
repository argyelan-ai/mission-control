"""setup.sh locks .env to the owner (chmod 600).

.env carries DB/JWT/encryption secrets in plaintext. Before this change
setup.sh left it at whatever umask `cp`/`>>` produced (0644 on most
systems) — readable by every other local account. These tests run the
REAL setup.sh in a throw-away copy (same harness idea as
test_setup_sh_remote_access.py: only what the script touches is copied,
HOME points into the scratch dir) and check the permission bits, both for
a fresh install and for an existing .env created before this fix.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from app.services.compose_renderer import COMPOSE_TEMPLATE_FILENAME

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _mode(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))[2:]


def _run_setup_sh(workdir: Path, **extra_env: str) -> subprocess.CompletedProcess:
    (workdir / "docker").mkdir(parents=True, exist_ok=True)
    (workdir / ".env.example").write_text(
        (_REPO_ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (workdir / "docker" / COMPOSE_TEMPLATE_FILENAME).write_text(
        (_REPO_ROOT / "docker" / COMPOSE_TEMPLATE_FILENAME).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (workdir / "scripts").mkdir(exist_ok=True)
    (workdir / "scripts" / "ensure-agents-yml.sh").write_text(
        (_REPO_ROOT / "scripts" / "ensure-agents-yml.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (workdir / "scripts" / "lib").mkdir(exist_ok=True)
    (workdir / "scripts" / "lib" / "remote-access.sh").write_text(
        (_REPO_ROOT / "scripts" / "lib" / "remote-access.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "MC_REMOTE", "MC_PUBLIC_HOST",
            "MC_SETUP_VIA_INSTALLER", "MC_INTERACTIVE",
        }
    }
    env.update({"HOME": str(workdir), **extra_env})
    return subprocess.run(
        ["bash", str(_REPO_ROOT / "setup.sh")],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def test_fresh_env_is_owner_only(tmp_path: Path):
    proc = _run_setup_sh(tmp_path)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    env_file = tmp_path / ".env"
    assert env_file.exists()
    assert _mode(env_file) == "600"


def test_existing_env_from_an_older_install_gets_tightened(tmp_path: Path):
    """An .env created before this fix (or hand-copied from .env.example)
    sat at the default umask. setup.sh must not skip it just because the
    file already exists."""
    env_file = tmp_path / ".env"
    env_file.write_text((_REPO_ROOT / ".env.example").read_text(encoding="utf-8"))
    env_file.chmod(0o644)
    before_ino = env_file.stat().st_ino

    proc = _run_setup_sh(tmp_path)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert _mode(env_file) == "600"
    # chmod only flips permission bits — the file (and its bind-mounted
    # inode) must stay the same object, never replaced via sed -i/mv.
    assert env_file.stat().st_ino == before_ino
