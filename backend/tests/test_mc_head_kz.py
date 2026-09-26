"""Roter Faden E2 — mc-head feeds the coherence tool ``kz`` into every head.

- ``mc-head start`` runs ``kz brief`` in the fresh worktree and appends the
  output (max. 60 lines) to job.md; kz missing / failing / hanging never
  stops the head, it leaves one "kz brief unavailable: …" line instead.
- The pre-push hook of the head clones runs ``kz check --fast`` when the repo
  has a ``.kohaerenz.yaml``; red findings block the push, a missing kz only warns.

A fake kz (shell script, set via MC_HEAD_KZ_BIN) logs its argv, so the exact
call is under test too.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.heads_host_helpers import (
    MC_HEAD,
    fake_harness,
    make_origin,
    mark_scratch,
    read_status,
    run_head,
    seed_clone,
    wait_phase,
    write_spec,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="host script is POSIX-only")

HEADING = "## Context brief (kz)"


@pytest.fixture
def env(tmp_path: Path):
    mc_home = tmp_path / "mc"
    mc_home.mkdir()
    origin, full_name = make_origin(tmp_path)
    clone = seed_clone(mc_home, origin, full_name)
    mark_scratch(mc_home, full_name)
    return {"mc_home": mc_home, "origin": origin, "full_name": full_name, "clone": clone, "tmp": tmp_path}


def _load():
    loader = importlib.machinery.SourceFileLoader("mc_head_kz", str(MC_HEAD))
    spec = importlib.util.spec_from_loader("mc_head_kz", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def fake_kz(tmp: Path, body: str) -> Path:
    """Fake kz: argv (one per line) into kz-argv.txt next to it, then ``body``."""
    p = tmp / "fake-kz"
    log = tmp / "kz-argv.txt"
    p.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{log}'\necho --- >> '{log}'\n{body}\n")
    p.chmod(0o755)
    return p


def _start(env, kz: str, job: str | None = None, **more) -> tuple[str, Path]:
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "true")
    run_id = write_spec(mc_home)
    run = mc_home / "heads" / run_id
    if job is not None:
        (run / "job.md").write_text(job)
    extra = {"MC_HEAD_BIN_OMP": str(harness), "MC_HEAD_BIN_CLAUDE": str(harness), "MC_HEAD_KZ_BIN": kz}
    extra.update(more)
    t0 = time.time()
    res = run_head(mc_home, "start", run_id, env_extra=extra)
    assert res.returncode == 0, res.stdout + res.stderr
    wait_phase(mc_home, run_id, "exited")
    run.joinpath(".elapsed").write_text(str(time.time() - t0))
    return run_id, run


# ── kz brief → job.md ────────────────────────────────────────────────────


def test_brief_is_appended_to_job_md_and_reaches_the_harness(env):
    kz = fake_kz(env["tmp"], "echo 'fresh: HEAD vs origin/main: 0 behind'; echo 'Rules for these paths: none'")
    job = "# Fix the readme\nChange `README.md`, not `nope/missing.py`.\n"
    run_id, run = _start(env, str(kz), job)
    text = (run / "job.md").read_text()
    assert text.startswith(job)
    assert HEADING in text
    assert "fresh: HEAD vs origin/main: 0 behind" in text.split(HEADING, 1)[1]
    argv = (env["tmp"] / "kz-argv.txt").read_text().split("---\n")[0].splitlines()
    assert argv[:4] == ["-C", str(run / "wt"), "brief", "--paths"]
    # scope paths = backticked paths from the job that exist in the worktree
    assert "README.md" in argv and "nope/missing.py" not in argv
    text_arg = next(a for a in argv if a.startswith("--text="))
    assert "Fix the readme" in text_arg
    # omp gets the job in argv: the brief is part of it
    assert "fresh: HEAD vs origin/main: 0 behind" in (run / "argv.txt").read_text()
    assert read_status(env["mc_home"], run_id)["kz_brief"] == "ok"


def test_brief_is_capped_at_60_lines(env):
    kz = fake_kz(env["tmp"], "i=1; while [ $i -le 100 ]; do echo \"brief line $i\"; i=$((i+1)); done")
    _, run = _start(env, str(kz))
    brief = (run / "job.md").read_text().split(HEADING, 1)[1]
    assert "brief line 60" in brief and "brief line 61" not in brief


def test_brief_is_not_appended_twice(tmp_path):
    """A second ``start`` of the same run (retry after a failed gate) replaces
    the section instead of stacking a second one."""
    mod = _load()
    (tmp_path / "job.md").write_text("# Job\nSay hello.\n")
    mod.write_kz_brief(tmp_path, ["first brief"], None)
    mod.write_kz_brief(tmp_path, None, "exit 2: boom")
    text = (tmp_path / "job.md").read_text()
    assert text.startswith("# Job\nSay hello.\n")
    assert text.count(HEADING) == 1
    assert "first brief" not in text and "kz brief unavailable: exit 2: boom" in text


def _assert_unavailable(env, run_id: str, run: Path, needle: str) -> None:
    text = (run / "job.md").read_text()
    assert HEADING in text
    line = next(l for l in text.splitlines() if l.startswith("kz brief unavailable: "))
    assert needle in line, line
    status = read_status(env["mc_home"], run_id)
    # the head still ran
    assert status["exit_code"] == 0 and status["reason"] is None
    assert status["kz_brief"].startswith("unavailable: ") and needle in status["kz_brief"]
    assert (run / "argv.txt").exists()


def test_missing_kz_never_stops_the_head(env):
    run_id, run = _start(env, "/nonexistent/kz")
    _assert_unavailable(env, run_id, run, "not found")


def test_kz_config_error_never_stops_the_head(env):
    kz = fake_kz(env["tmp"], "echo 'kz: config: unknown key(s) x' >&2; exit 2")
    run_id, run = _start(env, str(kz))
    _assert_unavailable(env, run_id, run, "exit 2")
    assert "unknown key" in (run / "job.md").read_text()


def test_hanging_kz_is_cut_off(env):
    kz = fake_kz(env["tmp"], "sleep 30")
    run_id, run = _start(env, str(kz), MC_HEAD_KZ_TIMEOUT_S="1")
    _assert_unavailable(env, run_id, run, "timed out")
    assert float((run / ".elapsed").read_text()) < 20


# ── pre-push: kz check --fast ────────────────────────────────────────────


def _commit_and_push(wt: Path, with_config: bool) -> subprocess.CompletedProcess:
    git = ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    if with_config:
        (wt / ".kohaerenz.yaml").write_text("version: 1\nstufe: 0\n")
    (wt / "x.txt").write_text("x\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
    return subprocess.run([*git, "push", "origin", "HEAD:refs/heads/mc-head/demo-kz"], capture_output=True, text=True)


def _prepared_wt(env, kz: str) -> Path:
    _, run = _start(env, kz)
    return run / "wt"


@pytest.mark.parametrize("rc", [1, 2])
def test_pre_push_blocks_on_red_kz_check(env, rc):
    kz = fake_kz(env["tmp"], f'case "$*" in *check*) echo "NEW links:AGENTS.md:x"; exit {rc} ;; esac')
    wt = _prepared_wt(env, str(kz))
    res = _commit_and_push(wt, with_config=True)
    assert res.returncode != 0
    assert "kz check" in res.stderr and "refused" in res.stderr
    calls = (env["tmp"] / "kz-argv.txt").read_text().split("---\n")
    assert any(c.splitlines()[-2:] == ["check", "--fast"] for c in calls if c.strip())


def test_pre_push_passes_on_green_kz_check(env):
    kz = fake_kz(env["tmp"], 'case "$*" in *check*) echo "kz check: OK (4 skipped)"; exit 0 ;; esac')
    wt = _prepared_wt(env, str(kz))
    res = _commit_and_push(wt, with_config=True)
    assert res.returncode == 0, res.stderr
    assert "check\n--fast" in (env["tmp"] / "kz-argv.txt").read_text()


def test_pre_push_skips_kz_without_kohaerenz_yaml(env):
    kz = fake_kz(env["tmp"], 'case "$*" in *check*) exit 1 ;; esac')
    wt = _prepared_wt(env, str(kz))
    (env["tmp"] / "kz-argv.txt").unlink()  # forget the brief call
    res = _commit_and_push(wt, with_config=False)
    assert res.returncode == 0, res.stderr
    assert not (env["tmp"] / "kz-argv.txt").exists()


def test_pre_push_only_warns_when_kz_is_missing(env):
    wt = _prepared_wt(env, "/nonexistent/kz")
    res = _commit_and_push(wt, with_config=True)
    assert res.returncode == 0, res.stderr
    assert "kz not found" in res.stderr


# ── kz for the head itself (step 6: kz check before the PR) ─────────────


def test_head_reaches_kz_through_its_bin_shim(env):
    """The head's PATH has no ~/.local/bin: a kz shim in <run>/bin forwards."""
    kz = fake_kz(env["tmp"], 'echo "fake kz $*"')
    _, run = _start(env, str(kz))
    res = subprocess.run([str(run / "bin" / "kz"), "check"], capture_output=True, text=True)
    assert res.returncode == 0 and "fake kz check" in res.stdout


def test_kz_shim_says_unavailable_when_kz_is_missing(env):
    _, run = _start(env, "/nonexistent/kz")
    res = subprocess.run([str(run / "bin" / "kz"), "check"], capture_output=True, text=True)
    assert res.returncode == 127 and "kz check unavailable" in res.stderr


def test_claude_head_may_run_kz_check():
    """Claude heads run with -p: a Bash command outside the allow list is denied."""
    import json

    settings = json.loads((MC_HEAD.parent / "claude-head-settings.json").read_text())
    assert "Bash(kz check*)" in settings["permissions"]["allow"]
