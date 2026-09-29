"""Scratch repo whose origin is a LOCAL bare repo (<heads_root>/scratch-origin/).

Live finding: such a run did the whole procedure but ended failed/no_pr —
(1) the sandbox denied `git push` into the local bare origin, (2) a local
origin never has a GitHub PR, yet "passed" required a PR URL.

mc-head now (a) grants the sandbox write access to exactly that origin and
(b) reports ``scratch_branch_pushed`` when the head branch sits on the local
origin with commits beyond the base branch. Real repos are unchanged.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from tests.heads_host_helpers import (
    MC_HEAD,
    fake_harness,
    make_origin,
    mark_scratch,
    run_head,
    seed_clone,
    wait_phase,
    write_spec,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="host script is POSIX-only")

PUSH = (
    'echo change > change.txt && git add change.txt'
    ' && git -c user.name=t -c user.email=t@example.invalid commit -q -m "fix: change"'
    ' && git push -q -u origin "$(git branch --show-current)"'
)


def _load():
    loader = importlib.machinery.SourceFileLoader("mc_head_scratch", str(MC_HEAD))
    spec = importlib.util.spec_from_loader("mc_head_scratch", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def scratch(tmp_path: Path) -> dict:
    """Scratch repo ``scratch/probe`` with its bare origin under heads/scratch-origin/."""
    tmp_path = tmp_path.resolve()
    mc_home = tmp_path / "mc"
    so = mc_home / "heads" / "scratch-origin"
    so.mkdir(parents=True)
    origin, _ = make_origin(so, "probe")
    full_name = "scratch/probe"
    clone = seed_clone(mc_home, origin, full_name)
    mark_scratch(mc_home, full_name)
    return {"mc_home": mc_home, "origin": origin, "full_name": full_name, "clone": clone, "tmp": tmp_path}


def _set_origin(clone: Path, url: str) -> None:
    subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin", url], check=True)


# ── which runs count as "scratch repo with a local origin" ──────────────


def test_scratch_origin_path_and_file_url(scratch, monkeypatch):
    monkeypatch.setenv("MC_HOME", str(scratch["mc_home"]))
    mod = _load()
    spec = {"repo_full_name": scratch["full_name"]}
    assert mod.scratch_origin(spec) == scratch["origin"]
    _set_origin(scratch["clone"], f"file://{scratch['origin']}")
    assert mod.scratch_origin(spec) == scratch["origin"]


def test_no_scratch_origin_for_real_or_foreign_origins(scratch, tmp_path, monkeypatch):
    mc_home = scratch["mc_home"]
    monkeypatch.setenv("MC_HOME", str(mc_home))
    mod = _load()
    # a real repo (not listed) with the very same local origin
    real_clone = seed_clone(mc_home, scratch["origin"], "owner/real")
    assert mod.scratch_origin({"repo_full_name": "owner/real"}) is None
    assert real_clone.exists()
    spec = {"repo_full_name": scratch["full_name"]}
    # listed, but the origin lies outside heads/scratch-origin/
    outside, _ = make_origin(scratch["tmp"], "outside")
    _set_origin(scratch["clone"], str(outside))
    assert mod.scratch_origin(spec) is None
    # listed, origin is a GitHub URL
    _set_origin(scratch["clone"], "https://github.com/scratch/probe.git")
    assert mod.scratch_origin(spec) is None
    # the scratch-origin folder itself is not an origin
    _set_origin(scratch["clone"], str(mc_home / "heads" / "scratch-origin"))
    assert mod.scratch_origin(spec) is None
    # a symlink inside scratch-origin that points elsewhere does not count
    link = mc_home / "heads" / "scratch-origin" / "evil.git"
    link.symlink_to(outside)
    _set_origin(scratch["clone"], str(link))
    assert mod.scratch_origin(spec) is None
    # a "../" path that climbs out of scratch-origin does not count either
    _set_origin(scratch["clone"], str(mc_home / "heads" / "scratch-origin" / ".." / "clones"))
    assert mod.scratch_origin(spec) is None


def test_sandbox_param_only_for_scratch_local_origin(scratch, monkeypatch):
    mc_home = scratch["mc_home"]
    monkeypatch.setenv("MC_HOME", str(mc_home))
    mod = _load()
    monkeypatch.setattr(mod, "sandbox_enabled", lambda: True)

    def param(argv: list) -> str:
        values = [a.split("=", 1)[1] for a in argv if a.startswith("SCRATCH_ORIGIN=")]
        assert len(values) == 1, argv
        return values[0]

    run = mc_home / "heads" / "r1"
    assert param(mod.sandbox_prefix({"repo_full_name": scratch["full_name"]}, run)) == str(scratch["origin"])
    seed_clone(mc_home, scratch["origin"], "owner/real")
    assert param(mod.sandbox_prefix({"repo_full_name": "owner/real"}, run)) == ""


# ── result: a pushed branch is the result (no PR possible) ──────────────


def _extra(harness: Path, **more) -> dict:
    d = {"MC_HEAD_BIN_OMP": str(harness), "MC_HEAD_BIN_CLAUDE": str(harness)}
    d.update(more)
    return d


def test_pushed_branch_on_local_origin_is_reported(scratch):
    mc_home = scratch["mc_home"]
    harness = fake_harness(scratch["tmp"], PUSH)
    run_id = write_spec(mc_home, repo_full_name=scratch["full_name"])
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["exit_code"] == 0, status
    assert status["scratch_branch_pushed"] is True
    assert status["pr_url"] is None


def test_no_push_means_no_scratch_result(scratch):
    mc_home = scratch["mc_home"]
    harness = fake_harness(
        scratch["tmp"], 'git -c user.name=t -c user.email=t@example.invalid commit -q --allow-empty -m local-only'
    )
    run_id = write_spec(mc_home, repo_full_name=scratch["full_name"])
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["scratch_branch_pushed"] is False


def test_pushed_branch_without_new_commits_does_not_count(scratch):
    mc_home = scratch["mc_home"]
    harness = fake_harness(scratch["tmp"], 'git push -q -u origin "$(git branch --show-current)"')
    run_id = write_spec(mc_home, repo_full_name=scratch["full_name"])
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["scratch_branch_pushed"] is False


def test_real_repo_push_to_a_local_origin_is_not_a_scratch_result(tmp_path):
    """Unlisted repo (the default test layout): unchanged — needs a PR."""
    mc_home = tmp_path / "mc"
    mc_home.mkdir()
    origin, full_name = make_origin(tmp_path)
    seed_clone(mc_home, origin, full_name)
    mark_scratch(mc_home, full_name)  # listed, but origin is outside scratch-origin/
    harness = fake_harness(tmp_path, PUSH)
    run_id = write_spec(mc_home)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["scratch_branch_pushed"] is False
    assert status["pr_url"] is None


@pytest.fixture
def scratch_outside_tmp():
    """Same layout, but OUTSIDE every temp root the sandbox always allows
    (/private/tmp, /private/var/folders): only the SCRATCH_ORIGIN rule can
    make the push work (review finding: under tmp_path the e2e test stayed
    green without the rule)."""
    import shutil
    import uuid

    base = Path.home() / ".cache" / f"mc-head-test-{uuid.uuid4().hex[:12]}"
    base.mkdir(parents=True)
    base = base.resolve()
    try:
        assert not str(base).startswith(("/private/tmp", "/private/var/folders", "/tmp", "/var/folders"))
        mc_home = base / "mc"
        so = mc_home / "heads" / "scratch-origin"
        so.mkdir(parents=True)
        origin, _ = make_origin(so, "probe")
        full_name = "scratch/probe"
        clone = seed_clone(mc_home, origin, full_name)
        mark_scratch(mc_home, full_name)
        yield {"mc_home": mc_home, "origin": origin, "full_name": full_name, "clone": clone, "tmp": base}
    finally:
        shutil.rmtree(base, ignore_errors=True)


REAL_SANDBOX = "/usr/bin/sandbox-exec"


@pytest.mark.skipif(not Path(REAL_SANDBOX).exists(), reason="sandbox-exec only on macOS")
def test_push_to_local_origin_works_inside_the_real_sandbox(scratch_outside_tmp):
    """End to end with the real profile: the push lands on the local origin."""
    sc = scratch_outside_tmp
    mc_home = sc["mc_home"]
    harness = fake_harness(sc["tmp"], PUSH + " 2> push.err; cp push.err \"$MC_HEAD_RUN_DIR/step.txt\"")
    run_id = write_spec(mc_home, repo_full_name=sc["full_name"])
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_SANDBOX_EXEC=REAL_SANDBOX))
    status = wait_phase(mc_home, run_id, "exited")
    argv = (mc_home / "heads" / run_id / ".wrapper" / "argv.json").read_text()
    assert argv.startswith(f'["{REAL_SANDBOX}"')
    assert f"SCRATCH_ORIGIN={sc['origin']}" in argv
    err = (mc_home / "heads" / run_id / "step.txt").read_text()
    assert "unable to create temporary object directory" not in err, err
    assert status["exit_code"] == 0, (status, err)
    assert status["scratch_branch_pushed"] is True


@pytest.mark.skipif(not Path(REAL_SANDBOX).exists(), reason="sandbox-exec only on macOS")
def test_same_push_fails_in_the_real_sandbox_without_the_grant(scratch_outside_tmp):
    """Control: the same push with the origin OUTSIDE heads/scratch-origin/
    gets SCRATCH_ORIGIN="" — refused exactly like in the live run."""
    import shutil

    sc = scratch_outside_tmp
    mc_home = sc["mc_home"]
    elsewhere = sc["tmp"] / "elsewhere" / "probe.git"
    elsewhere.parent.mkdir()
    shutil.move(str(sc["origin"]), str(elsewhere))
    subprocess.run(["git", "-C", str(sc["clone"]), "remote", "set-url", "origin", str(elsewhere)], check=True)
    harness = fake_harness(sc["tmp"], PUSH + " 2> push.err; cp push.err \"$MC_HEAD_RUN_DIR/step.txt\"")
    run_id = write_spec(mc_home, repo_full_name=sc["full_name"])
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_SANDBOX_EXEC=REAL_SANDBOX))
    status = wait_phase(mc_home, run_id, "exited")
    argv = (mc_home / "heads" / run_id / ".wrapper" / "argv.json").read_text()
    assert "SCRATCH_ORIGIN=," in argv or 'SCRATCH_ORIGIN="' in argv
    assert "unable to create temporary object directory" in (mc_home / "heads" / run_id / "step.txt").read_text()
    assert status["scratch_branch_pushed"] is False
