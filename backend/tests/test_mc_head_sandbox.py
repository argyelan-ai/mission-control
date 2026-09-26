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
    scratch_origin = mc_home / "heads" / "scratch-origin" / "r.git"
    (scratch_origin / "objects").mkdir(parents=True)
    other = mc_home / "heads" / "scratch-origin" / "other.git"
    other.mkdir(parents=True)
    return {"real_home": real_home, "mc_home": mc_home, "run": run, "clone_git": clone_git, "jobs": jobs,
            "scratch_origin": scratch_origin, "other_origin": other}


def _sh(layout: dict, script: str, **overrides) -> subprocess.CompletedProcess:
    params = {
        "RUN": layout["run"],
        "WT": layout["run"] / "wt",
        "CLONE_GIT": layout["clone_git"],
        "VAULT_JOBS": layout["jobs"],
        "REAL_HOME": layout["real_home"],
        "MC_HOME": layout["mc_home"],
        "USER_TMP": layout["real_home"] / "tmp",
        # empty = real repo: no write outside the run's own places
        "SCRATCH_ORIGIN": "",
    }
    params.update(overrides)
    argv = [str(SANDBOX), "-f", str(PROFILE)]
    for k, v in params.items():
        argv += ["-D", f"{k}={v}"]
    return subprocess.run([*argv, "/bin/sh", "-c", script], capture_output=True, text=True, cwd=layout["run"] / "wt")


def test_worktree_and_status_files_are_writable(layout):
    run = layout["run"]
    res = _sh(
        layout,
        f"echo ok > {run}/wt/file.txt && echo 'step 1/7' > {run}/step.txt"
        f" && echo q > {run}/question.md && echo r > {run}/run-record.md && echo g > {layout['clone_git']}/x"
        f" && echo w | tee -a {run}/work.log",
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


def test_vault_is_not_writable_the_wrapper_files_the_run_record(layout):
    res = _sh(layout, f"echo x > {layout['jobs']}/forged-run-record.md")
    assert res.returncode != 0
    assert not (layout["jobs"] / "forged-run-record.md").exists()


def test_more_credential_files_are_not_readable(layout):
    home = layout["real_home"]
    (home / ".netrc").write_text("machine x password NETRC\n")
    (home / ".git-credentials").write_text("https://u:GITCRED@example.invalid\n")
    (home / ".docker").mkdir()
    (home / ".docker" / "config.json").write_text('{"auths":"DOCKERCFG"}\n')
    res = _sh(layout, f"cat {home}/.netrc {home}/.git-credentials {home}/.docker/config.json")
    assert res.returncode != 0
    for secret in ("NETRC", "GITCRED", "DOCKERCFG"):
        assert secret not in res.stdout


def test_docker_socket_is_not_reachable(layout, tmp_path):
    """A head's own code (pytest, npm test …) must not reach the Docker
    daemon — python's socket module ignores the docker shim."""
    import socket
    import threading

    import shutil
    import tempfile

    # AF_UNIX paths are limited to ~104 bytes on macOS: a short fake home.
    home = Path(tempfile.mkdtemp(prefix="mcsb", dir="/private/tmp"))
    sock_dir = home / ".docker" / "run"
    sock_dir.mkdir(parents=True)
    path = sock_dir / "docker.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    threading.Thread(target=lambda: server.accept(), daemon=True).start()
    probe = (
        "import socket,sys\n"
        "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
        f"s.connect({str(path)!r})\n"
        "print('CONNECTED')\n"
    )
    try:
        res = _sh(layout, f"/usr/bin/python3 -c \"{probe}\"", REAL_HOME=home)
        # control: the same probe WITHOUT the sandbox connects — the socket works
        plain = subprocess.run(["/usr/bin/python3", "-c", probe], capture_output=True, text=True)
    finally:
        server.close()
        shutil.rmtree(home, ignore_errors=True)
    assert "CONNECTED" in plain.stdout, plain.stderr
    assert "CONNECTED" not in res.stdout
    assert res.returncode != 0


# ── scratch repo with a local bare origin ───────────────────────────────


def test_scratch_origin_is_writable_when_passed(layout):
    """A scratch repo whose origin is a local bare repo: `git push` writes
    objects into it (live finding: "remote unpack failed: unable to create
    temporary object directory")."""
    origin = layout["scratch_origin"]
    res = _sh(layout, f"mkdir {origin}/objects/incoming-x && echo o > {origin}/objects/incoming-x/pack",
              SCRATCH_ORIGIN=origin)
    assert res.returncode == 0, res.stderr
    assert (origin / "objects" / "incoming-x" / "pack").read_text() == "o\n"


def test_scratch_origin_grant_covers_only_that_origin(layout):
    heads = layout["mc_home"] / "heads"
    res = _sh(
        layout,
        f"echo x > {layout['other_origin']}/forged; echo y > {heads}/scratch-repos; echo z > {heads}/gh-token",
        SCRATCH_ORIGIN=layout["scratch_origin"],
    )
    assert res.returncode != 0
    for p in (layout["other_origin"] / "forged", heads / "scratch-repos", heads / "gh-token"):
        assert not p.exists(), p


def test_real_repo_gets_no_origin_write(layout):
    """SCRATCH_ORIGIN empty (every real repo): the origin stays read-only."""
    origin = layout["scratch_origin"]
    res = _sh(layout, f"echo x > {origin}/objects/forged")
    assert res.returncode != 0
    assert not (origin / "objects" / "forged").exists()
    # reads still work (git ls-remote / fetch)
    assert _sh(layout, f"ls {origin}/objects").returncode == 0


def test_missing_scratch_origin_param_fails_closed(layout):
    """The profile never silently widens: without the parameter it refuses to load."""
    params = {"RUN": layout["run"], "WT": layout["run"] / "wt", "CLONE_GIT": layout["clone_git"],
              "VAULT_JOBS": layout["jobs"], "REAL_HOME": layout["real_home"], "MC_HOME": layout["mc_home"],
              "USER_TMP": layout["real_home"] / "tmp"}
    argv = [str(SANDBOX), "-f", str(PROFILE)]
    for k, v in params.items():
        argv += ["-D", f"{k}={v}"]
    res = subprocess.run([*argv, "/bin/sh", "-c", "echo ran"], capture_output=True, text=True)
    assert "ran" not in res.stdout
