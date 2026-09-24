"""Shared helpers for the mc-head host-script tests (head launcher, spec §6.3).

The script lives in ``scripts/head/mc-head`` (Python 3.9 stdlib, so the
macOS system python can run it from launchd). Tests drive it as a
subprocess against a temporary ``MC_HOME`` and a local bare git repo as
``origin`` — no network, no GitHub, no real harness.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MC_HEAD = REPO_ROOT / "scripts" / "head" / "mc-head"


PASSTHROUGH_SANDBOX = """#!/bin/sh
# Test stand-in for sandbox-exec: drops -f <profile> and -D k=v, runs the rest.
# mc-head requires a sandbox for EVERY run; tests that are not about the
# sandbox itself use this (portable, also on Linux CI). The real profile is
# exercised by test_mc_head_sandbox.py and the scratch-origin e2e test.
while [ $# -gt 0 ]; do
  case "$1" in
    -f|-D) shift 2 ;;
    *) break ;;
  esac
done
exec "$@"
"""


def passthrough_sandbox(where: Path) -> Path:
    if not where.is_dir():  # e.g. a deliberately nonexistent MC_HOME
        return Path("/nonexistent/fake-sandbox-exec")
    p = where / "fake-sandbox-exec"
    if not p.exists():
        p.write_text(PASSTHROUGH_SANDBOX)
        p.chmod(0o755)
    return p


def run_head(mc_home: Path, *args: str, env_extra: dict | None = None, timeout: float = 60):
    """Runs mc-head with the sandbox ON (passthrough stand-in unless the test
    passes MC_HEAD_SANDBOX_EXEC=/usr/bin/sandbox-exec, or MC_HEAD_SANDBOX=0)."""
    env = {
        "MC_HEAD_SANDBOX": "1",
        "MC_HEAD_SANDBOX_EXEC": str(passthrough_sandbox(mc_home.parent if mc_home.exists() else mc_home)),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(mc_home.parent),
        "MC_HOME": str(mc_home),
        "MC_HEAD_NO_TMUX": "1",
        "MC_HEAD_HEARTBEAT_S": "1",
        "MC_HEAD_KILL_GRACE_S": "2",
        "MC_HEAD_GH": "/nonexistent/gh",
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(MC_HEAD), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def make_origin(tmp: Path, name: str = "demo") -> tuple[Path, str]:
    """Bare origin + dedicated clone under $MC_HOME/heads/clones (pre-seeded,
    so mc-head never needs ``gh repo clone``)."""
    origin = tmp / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp / f"{name}-seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("demo\n")
    git = ["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    subprocess.run([*git, "push", "-q", str(origin), "main"], check=True)
    return origin, f"owner/{name}"


def seed_clone(mc_home: Path, origin: Path, full_name: str) -> Path:
    clone = mc_home / "heads" / "clones" / full_name.replace("/", "--")
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return clone


def mark_scratch(mc_home: Path, full_name: str) -> None:
    f = mc_home / "heads" / "scratch-repos"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(full_name + "\n")


def write_spec(mc_home: Path, **overrides) -> str:
    run_id = overrides.pop("run_id", None) or str(uuid.uuid4())
    spec = {
        "run_id": run_id,
        "task_id": None,
        "repo_full_name": "owner/demo",
        "base_branch": "main",
        "branch": f"mc-head/2026-09-23-demo-{run_id[:4]}",
        "harness": "omp",
        "runtime_slug": "local-slot",
        "model": "GLM-5.3-Flash-EXL3",
        "base_url": "http://127.0.0.1:9/v1",
        "box_keys": [str(uuid.UUID(int=1))],
        "recipe_slug": None,
        "time_limit_s": 600,
        "restarted_from": None,
        "mode": "fresh",
        "job_folder": f"2026-09-23-demo-{run_id[:4]}",
        "created_by": None,
        "created_at": "2026-09-23T10:00:00Z",
    }
    spec.update(overrides)
    run = mc_home / "heads" / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "spec.json").write_text(json.dumps(spec))
    (run / "job.md").write_text("# Job\nSay hello.\n")
    (run / "procedure.md").write_text("# Procedure\n")
    (run / "head.env").write_text("")
    return run_id


def read_status(mc_home: Path, run_id: str) -> dict | None:
    p = mc_home / "heads" / run_id / ".wrapper" / "status.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


def wait_phase(mc_home: Path, run_id: str, phase: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = read_status(mc_home, run_id)
        if last and last.get("phase") == phase:
            return last
        time.sleep(0.2)
    raise AssertionError(f"phase {phase!r} not reached, last status: {last}")


def fake_harness(tmp: Path, body: str) -> Path:
    """A shell script that stands in for omp/claude. It receives the exact
    argv the harness table builds and logs it to ``$PWD/../argv.txt``."""
    p = tmp / "fake-harness"
    p.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > ../argv.txt\nenv > ../env.txt\n" + body + "\n")
    p.chmod(0o755)
    return p
