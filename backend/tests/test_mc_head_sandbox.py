"""A6 — the sandbox profile (spec §9 layer 3) denies what it must.

Runs a plain /bin/sh under ``sandbox-exec`` with the real profile and fake
paths. Whether omp/claude themselves work inside the sandbox (network, gh,
caches) is open point 2 of the spec — this only proves the deny rules.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.heads_host_helpers import REPO_ROOT

PROFILE = REPO_ROOT / "scripts" / "head" / "head.sb"
SANDBOX = Path("/usr/bin/sandbox-exec")

pytestmark = pytest.mark.skipif(not SANDBOX.exists(), reason="sandbox-exec only on macOS")


@pytest.fixture
def layout(tmp_path: Path) -> dict:
    tmp_path = tmp_path.resolve()  # sandbox rules match real paths (/private/var/…)
    real_home = tmp_path / "home"
    (real_home / ".ssh").mkdir(parents=True)
    (real_home / ".ssh" / "id_test").write_text("PRIVATE KEY\n")
    mc_home = real_home / ".mc"
    run = mc_home / "heads" / "run1"
    for sub in ("wt", ".wrapper", "home", ".backend"):
        (run / sub).mkdir(parents=True)
    (run / "wt" / ".env").write_text("SECRET=1\n")
    clone_git = mc_home / "heads" / "clones" / "o--r" / ".git"
    clone_git.mkdir(parents=True)
    jobs = mc_home / "vault" / "jobs"
    jobs.mkdir(parents=True)
    return {"real_home": real_home, "mc_home": mc_home, "run": run, "clone_git": clone_git, "jobs": jobs}


def _sh(layout: dict, script: str) -> subprocess.CompletedProcess:
    params = {
        "RUN": layout["run"],
        "WT": layout["run"] / "wt",
        "CLONE_GIT": layout["clone_git"],
        "VAULT_JOBS": layout["jobs"],
        "REAL_HOME": layout["real_home"],
        "MC_HOME": layout["mc_home"],
        "USER_TMP": layout["real_home"] / "tmp",
    }
    argv = [str(SANDBOX), "-f", str(PROFILE)]
    for k, v in params.items():
        argv += ["-D", f"{k}={v}"]
    return subprocess.run([*argv, "/bin/sh", "-c", script], capture_output=True, text=True, cwd=layout["run"] / "wt")


def test_worktree_and_status_files_are_writable(layout):
    run = layout["run"]
    res = _sh(
        layout,
        f"echo ok > {run}/wt/file.txt && echo 'step 1/7' > {run}/step.txt"
        f" && echo q > {run}/question.md && echo r > {layout['jobs']}/rr.md && echo g > {layout['clone_git']}/x",
    )
    assert res.returncode == 0, res.stderr
    assert (run / "wt" / "file.txt").read_text() == "ok\n"


def test_wrapper_status_is_not_writable(layout):
    res = _sh(layout, f"echo '{{\"phase\":\"exited\"}}' > {layout['run']}/.wrapper/status.json")
    assert res.returncode != 0
    assert not (layout["run"] / ".wrapper" / "status.json").exists()


def test_backend_bookkeeping_is_not_writable(layout):
    res = _sh(layout, f"echo x > {layout['run']}/.backend/mirror.json")
    assert res.returncode != 0


def test_ssh_key_is_not_readable(layout):
    res = _sh(layout, f"cat {layout['real_home']}/.ssh/id_test")
    assert res.returncode != 0
    assert "PRIVATE KEY" not in res.stdout


def test_env_file_is_not_readable_even_in_worktree(layout):
    res = _sh(layout, f"cat {layout['run']}/wt/.env")
    assert res.returncode != 0
    assert "SECRET" not in res.stdout


def test_writes_outside_the_allowed_places_are_denied(layout):
    res = _sh(layout, f"echo x > {layout['real_home']}/outside.txt")
    assert res.returncode != 0
    assert not (layout["real_home"] / "outside.txt").exists()
