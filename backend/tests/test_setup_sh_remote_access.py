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


def test_setup_with_an_address_opens_mc_before_the_first_start(tmp_path: Path):
    proc = _run_setup_sh(tmp_path, MC_PUBLIC_HOST="192.0.2.20")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    env_file = tmp_path / ".env"
    assert _env_value(env_file, "MC_BIND_ADDRESS") == "0.0.0.0"
    assert _env_value(env_file, "PUBLIC_HOST") == "192.0.2.20"
    # The printed next step is the URL that works from the other device …
    assert "Open http://192.0.2.20 and register" in proc.stdout
    # … together with what opening the network means.
    assert "ANYONE on this network" in proc.stdout
    assert "tailscale serve" in proc.stdout


def test_installer_run_leaves_the_question_to_install_sh(tmp_path: Path):
    """install.sh asks itself (with its flags); setup.sh must not decide first."""
    proc = _run_setup_sh(tmp_path, MC_SETUP_VIA_INSTALLER="1", MC_PUBLIC_HOST="192.0.2.20")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert _env_value(tmp_path / ".env", "PUBLIC_HOST") == ""


def test_rerun_with_an_existing_env_does_not_ask_again(tmp_path: Path):
    first = _run_setup_sh(tmp_path, MC_PUBLIC_HOST="192.0.2.20")
    assert first.returncode == 0, first.stderr or first.stdout
    second = _run_setup_sh(tmp_path, MC_PUBLIC_HOST="198.51.100.9")
    assert second.returncode == 0, second.stderr or second.stdout
    assert _env_value(tmp_path / ".env", "PUBLIC_HOST") == "192.0.2.20"
    assert "Open http://192.0.2.20 and register" in second.stdout
