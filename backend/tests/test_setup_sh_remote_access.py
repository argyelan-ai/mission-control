"""setup.sh — the remote-access step on a fresh .env (scripts/lib/remote-access.sh).

Finding 17.10.2026: a fresh install on a headless board listened on
127.0.0.1 with an empty PUBLIC_HOST and told the user to open
http://localhost — impossible on a machine without a browser. setup.sh now
settles the question before the first start. These tests run the REAL
setup.sh in a throw-away copy (same harness idea as
test_compose_renderer_bootstrap.py: only what the script touches is copied,
HOME points into the scratch dir).

The library's own behaviour (prompts, detection, bad addresses) is covered
by scripts/tests/test_remote_access.sh; this file pins the wiring.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app.services.compose_renderer import COMPOSE_TEMPLATE_FILENAME

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_value(env_file: Path, key: str) -> str:
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return ""


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
        stdin=subprocess.DEVNULL,  # no TTY: the unattended path
        env=env,
    )


def test_unattended_setup_stays_local_only(tmp_path: Path):
    proc = _run_setup_sh(tmp_path)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    env_file = tmp_path / ".env"
    assert _env_value(env_file, "MC_BIND_ADDRESS") == "127.0.0.1"
    assert _env_value(env_file, "PUBLIC_HOST") == ""
    assert "Open http://localhost and register" in proc.stdout


def test_setup_never_opens_the_network_before_an_admin_exists(tmp_path: Path):
    """Review finding: setup.sh runs before the first start — no admin exists
    yet, so opening the LAN would let anyone register as admin. Even an
    explicit address leaves the binding alone; open-network.sh does it later."""
    proc = _run_setup_sh(tmp_path, MC_REMOTE="yes", MC_PUBLIC_HOST="192.0.2.20")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    env_file = tmp_path / ".env"
    assert _env_value(env_file, "MC_BIND_ADDRESS") == "127.0.0.1"
    assert _env_value(env_file, "PUBLIC_HOST") == ""


def test_headless_setup_prints_tunnel_and_open_command(tmp_path: Path):
    proc = _run_setup_sh(tmp_path, SSH_CONNECTION="198.51.100.7 51515 192.0.2.30 22")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "ssh -L 8080:localhost:80" in proc.stdout
    assert "@192.0.2.30" in proc.stdout
    assert "bash scripts/open-network.sh" in proc.stdout


def test_installer_run_leaves_the_hints_to_install_sh(tmp_path: Path):
    proc = _run_setup_sh(
        tmp_path, MC_SETUP_VIA_INSTALLER="1", SSH_CONNECTION="198.51.100.7 1 192.0.2.30 22"
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "open-network.sh" not in proc.stdout


def test_open_network_refuses_without_an_admin(tmp_path: Path):
    """scripts/open-network.sh is the only later way to open the LAN; with no
    admin (here: no backend at all) and no terminal it changes nothing."""
    (tmp_path / "scripts" / "lib").mkdir(parents=True)
    for rel in ("scripts/open-network.sh", "scripts/lib/remote-access.sh"):
        (tmp_path / rel).write_text((_REPO_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("MC_BIND_ADDRESS=127.0.0.1\nPUBLIC_HOST=\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "open-network.sh"), "192.0.2.20"],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # no controlling terminal → no prompts
        # Port 9 (discard) is closed: "no backend" must read as "no admin".
        env={**os.environ, "HOME": str(tmp_path), "MC_API_URL": "http://127.0.0.1:9"},
    )
    assert proc.returncode != 0
    assert "no admin" in proc.stdout + proc.stderr
    assert _env_value(env_file, "MC_BIND_ADDRESS") == "127.0.0.1"
    assert _env_value(env_file, "PUBLIC_HOST") == ""
