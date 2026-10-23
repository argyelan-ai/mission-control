"""mc-head prepares a fresh scratch repo itself (docs/specs/head-launcher.md §9).

Live finding 2026-10-04: a head on a scratch repo outside MC ended with a
bare ``prepare_failed`` — no clone existed, and mc-head only knew
``gh repo clone``. The operator had to create the bare origin under
``heads/scratch-origin/`` and the clone under ``heads/clones/`` by hand.

Now, for a listed ``scratch/<name>`` repo without a clone, mc-head builds
both: the bare origin from the run's ``scratch_source`` (the repos row's
source, written by the backend) — or reuses an existing bare origin — and
clones it. No source → reason ``scratch_source_missing`` with a sentence
the operator can act on. The paths are exactly the hand-made ones, so the
sandbox grant (``SCRATCH_ORIGIN``) is unchanged.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.heads_host_helpers import (
    MC_HEAD,
    fake_harness,
    mark_scratch,
    run_head,
    wait_phase,
    write_spec,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="host script is POSIX-only")

PUSH = (
    'echo change > change.txt && git add change.txt'
    ' && git -c user.name=t -c user.email=t@example.invalid commit -q -m "fix: change"'
    ' && git push -q -u origin "$(git branch --show-current)"'
)
FULL_NAME = "scratch/tool"


def _load(monkeypatch, mc_home: Path):
    monkeypatch.setenv("MC_HOME", str(mc_home))
    loader = importlib.machinery.SourceFileLoader("mc_head_scratch_auto", str(MC_HEAD))
    spec = importlib.util.spec_from_loader("mc_head_scratch_auto", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _source_repo(tmp: Path, branch: str = "main") -> Path:
    """An ordinary working repo somewhere on the host — the operator's tool."""
    src = tmp / "operator-tool"
    subprocess.run(["git", "init", "-q", "-b", branch, str(src)], check=True)
    (src / "tool.py").write_text("print('hi')\n")
    git = ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    return src


@pytest.fixture
def fresh(tmp_path: Path) -> dict:
    """Listed scratch repo with NO bare origin and NO clone yet."""
    tmp_path = tmp_path.resolve()
    mc_home = tmp_path / "mc"
    (mc_home / "heads").mkdir(parents=True)
    mark_scratch(mc_home, FULL_NAME)
    return {
        "mc_home": mc_home,
        "tmp": tmp_path,
        "source": _source_repo(tmp_path),
        "mirror": mc_home / "heads" / "scratch-origin" / "tool.git",
        "clone": mc_home / "heads" / "clones" / "scratch--tool",
    }


def _extra(harness: Path, **more) -> dict:
    d = {"MC_HEAD_BIN_OMP": str(harness), "MC_HEAD_BIN_CLAUDE": str(harness)}
    d.update(more)
    return d


def _origin_url(clone: Path) -> str:
    return subprocess.run(["git", "-C", str(clone), "config", "--get", "remote.origin.url"],
                          capture_output=True, text=True, check=True).stdout.strip()


# ── the happy path: nothing exists, the source is on the run ────────────


def test_fresh_scratch_repo_is_prepared_and_the_head_runs(fresh):
    mc_home = fresh["mc_home"]
    harness = fake_harness(fresh["tmp"], PUSH)
    run_id = write_spec(mc_home, repo_full_name=FULL_NAME, scratch_source=str(fresh["source"]))
    res = run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["exit_code"] == 0, (status, res.stdout, res.stderr)
    # bare origin + clone at exactly the hand-made paths
    assert subprocess.run(["git", "--git-dir", str(fresh["mirror"]), "rev-parse", "--is-bare-repository"],
                          capture_output=True, text=True).stdout.strip() == "true"
    assert _origin_url(fresh["clone"]) == str(fresh["mirror"])
    # the origin does not point back at the operator's source
    assert subprocess.run(["git", "--git-dir", str(fresh["mirror"]), "config", "--get", "remote.origin.url"],
                          capture_output=True, text=True).returncode != 0
    # the head's push landed on the local origin → the scratch result counts
    assert status["scratch_branch_pushed"] is True
    # the operator's source is untouched
    assert subprocess.run(["git", "-C", str(fresh["source"]), "branch", "--list", "mc-head/*"],
                          capture_output=True, text=True).stdout.strip() == ""
    # logged (stderr of the watcher run lands in launchd's log)
    assert "scratch repo scratch/tool: bare origin created" in res.stderr
    assert "scratch repo scratch/tool: clone created" in res.stderr
    assert status.get("scratch_prepared") == {"origin": "created", "clone": "created"}


def test_prepared_repo_gets_the_sandbox_grant(fresh, monkeypatch):
    """Same SCRATCH_ORIGIN as a hand-made layout: the sandbox rule is unchanged."""
    mc_home = fresh["mc_home"]
    harness = fake_harness(fresh["tmp"], "true")
    run_id = write_spec(mc_home, repo_full_name=FULL_NAME, scratch_source=str(fresh["source"]))
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    wait_phase(mc_home, run_id, "exited")
    mod = _load(monkeypatch, mc_home)
    assert mod.scratch_origin({"repo_full_name": FULL_NAME}) == fresh["mirror"]


def test_second_start_reuses_origin_and_clone(fresh):
    mc_home = fresh["mc_home"]
    harness = fake_harness(fresh["tmp"], "true")
    first = write_spec(mc_home, repo_full_name=FULL_NAME, scratch_source=str(fresh["source"]))
    run_head(mc_home, "start", first, env_extra=_extra(harness))
    wait_phase(mc_home, first, "exited")
    marker = fresh["mirror"] / "mc-test-marker"
    marker.write_text("x")
    clone_inode = fresh["clone"].stat().st_ino
    second = write_spec(mc_home, repo_full_name=FULL_NAME, scratch_source=str(fresh["source"]))
    res = run_head(mc_home, "start", second, env_extra=_extra(harness))
    status = wait_phase(mc_home, second, "exited")
    assert status["exit_code"] == 0, (status, res.stderr)
    assert marker.exists() and fresh["clone"].stat().st_ino == clone_inode
    assert "created" not in res.stderr
    assert status.get("scratch_prepared") is None


def test_existing_bare_origin_without_clone_needs_no_source(fresh):
    """The hand-made case: origin is there, only the clone is missing."""
    mc_home = fresh["mc_home"]
    fresh["mirror"].parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(fresh["source"]), str(fresh["mirror"])], check=True)
    harness = fake_harness(fresh["tmp"], "true")
    run_id = write_spec(mc_home, repo_full_name=FULL_NAME)  # no scratch_source at all
    res = run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["exit_code"] == 0, (status, res.stderr)
    assert _origin_url(fresh["clone"]) == str(fresh["mirror"])
    assert status.get("scratch_prepared") == {"origin": "existing", "clone": "created"}


# ── clear refusals instead of a bare prepare_failed ─────────────────────


def test_no_source_and_no_origin_is_a_clear_refusal(fresh):
    mc_home = fresh["mc_home"]
    run_id = write_spec(mc_home, repo_full_name=FULL_NAME, scratch_source=None)
    res = run_head(mc_home, "start", run_id)
    status = wait_phase(mc_home, run_id, "exited")
    assert status["reason"] == "scratch_source_missing", (status, res.stdout)
    assert status["detail"] == "scratch repo scratch/tool has no source; add it under Repos"
    assert not fresh["mirror"].exists() and not fresh["clone"].exists()


@pytest.mark.parametrize("case", ["not_a_repo", "missing_path", "no_base_branch"])
def test_unusable_source_leaves_nothing_behind(fresh, case):
    mc_home = fresh["mc_home"]
    if case == "not_a_repo":
        source = fresh["tmp"] / "plain-folder"
        source.mkdir()
    elif case == "missing_path":
        source = fresh["tmp"] / "does-not-exist"
    else:
        shutil.rmtree(fresh["source"])
        source = _source_repo(fresh["tmp"], branch="trunk")
    run_id = write_spec(mc_home, repo_full_name=FULL_NAME, scratch_source=str(source))
    res = run_head(mc_home, "start", run_id)
    status = wait_phase(mc_home, run_id, "exited")
    assert status["reason"] == "scratch_source_failed", (status, res.stdout)
    assert "scratch repo scratch/tool" in status["detail"]
    if case == "no_base_branch":
        assert "has no branch main" in status["detail"]
    assert not fresh["mirror"].exists() and not fresh["clone"].exists()
    # no half-made temp folders left next to them either
    for parent in (fresh["mirror"].parent, fresh["clone"].parent):
        assert not parent.exists() or list(parent.iterdir()) == []


def test_mirror_path_that_is_a_symlink_is_refused(fresh):
    mc_home = fresh["mc_home"]
    elsewhere = fresh["tmp"] / "elsewhere.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(fresh["source"]), str(elsewhere)], check=True)
    fresh["mirror"].parent.mkdir(parents=True)
    fresh["mirror"].symlink_to(elsewhere)
    run_id = write_spec(mc_home, repo_full_name=FULL_NAME, scratch_source=str(fresh["source"]))
    run_head(mc_home, "start", run_id)
    status = wait_phase(mc_home, run_id, "exited")
    assert status["reason"] == "scratch_source_failed"
    assert not fresh["clone"].exists()


# ── only listed scratch/<name> repos are prepared this way ──────────────


def test_unlisted_repo_with_a_source_is_not_prepared(fresh):
    """Not in heads/scratch-repos → the real-repo path, source ignored."""
    mc_home = fresh["mc_home"]
    (mc_home / "heads" / "scratch-repos").write_text("")
    run_id = write_spec(mc_home, repo_full_name=FULL_NAME, scratch_source=str(fresh["source"]))
    run_head(mc_home, "start", run_id)
    status = wait_phase(mc_home, run_id, "exited")
    assert status["reason"] == "gh_identity_missing"
    assert not fresh["mirror"].exists()


def test_listed_repo_outside_the_scratch_owner_keeps_the_old_path(fresh):
    mc_home = fresh["mc_home"]
    mark_scratch(mc_home, "owner/tool")
    run_id = write_spec(mc_home, repo_full_name="owner/tool", scratch_source=str(fresh["source"]))
    run_head(mc_home, "start", run_id)
    status = wait_phase(mc_home, run_id, "exited")
    assert status["reason"] == "prepare_failed"
    assert not fresh["mirror"].exists()


# ── the source on the spec is checked like every other spec field ───────


@pytest.mark.parametrize("source", [
    "ext::sh -c touch% /tmp/pwned",
    "-uevil",
    "relative/path",
    "/abs/../climb",
    "https://user:secret@example.invalid/x.git",
    "/path with space",
    "",
    42,
])
def test_bad_source_on_the_spec_is_spec_invalid(fresh, source):
    mc_home = fresh["mc_home"]
    run_id = write_spec(mc_home, repo_full_name=FULL_NAME, scratch_source=source)
    res = run_head(mc_home, "validate", run_id)
    assert res.returncode == 2 and "scratch_source" in res.stdout, res.stdout


@pytest.mark.parametrize("source", [
    "/abs/path/tool",
    "file:///abs/path/tool.git",
    "https://example.invalid/owner/tool.git",
    "ssh://git@example.invalid/owner/tool.git",
    "git@example.invalid:owner/tool.git",
])
def test_good_sources_validate(fresh, source):
    run_id = write_spec(fresh["mc_home"], repo_full_name=FULL_NAME, scratch_source=source)
    res = run_head(fresh["mc_home"], "validate", run_id)
    assert res.returncode == 0, res.stdout


def test_source_inside_heads_root_is_refused(fresh):
    """A run must never mirror the heads folder itself (runs, tokens, locks)."""
    inside = fresh["mc_home"] / "heads" / "clones" / "scratch--other"
    run_id = write_spec(fresh["mc_home"], repo_full_name=FULL_NAME, scratch_source=str(inside))
    res = run_head(fresh["mc_home"], "validate", run_id)
    assert res.returncode == 2 and "scratch_source" in res.stdout


# ── real sandbox: a prepared repo pushes exactly like a hand-made one ───

REAL_SANDBOX = "/usr/bin/sandbox-exec"


@pytest.mark.skipif(not Path(REAL_SANDBOX).exists(), reason="sandbox-exec only on macOS")
def test_prepared_repo_pushes_inside_the_real_sandbox():
    """Outside every temp root the sandbox always allows, so only the
    SCRATCH_ORIGIN grant can make the push work (same layout rule as
    test_mc_head_scratch_origin.py)."""
    base = (Path.home() / ".cache" / f"mc-head-test-{uuid.uuid4().hex[:12]}")
    base.mkdir(parents=True)
    base = base.resolve()
    try:
        mc_home = base / "mc"
        (mc_home / "heads").mkdir(parents=True)
        mark_scratch(mc_home, FULL_NAME)
        source = _source_repo(base)
        harness = fake_harness(base, PUSH + " 2> push.err; cp push.err \"$MC_HEAD_RUN_DIR/step.txt\"")
        run_id = write_spec(mc_home, repo_full_name=FULL_NAME, scratch_source=str(source))
        run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_SANDBOX_EXEC=REAL_SANDBOX))
        status = wait_phase(mc_home, run_id, "exited")
        argv = json.loads((mc_home / "heads" / run_id / ".wrapper" / "argv.json").read_text())
        assert argv[0] == REAL_SANDBOX
        mirror = mc_home / "heads" / "scratch-origin" / "tool.git"
        assert f"SCRATCH_ORIGIN={mirror}" in argv
        err = (mc_home / "heads" / run_id / "step.txt").read_text()
        assert status["exit_code"] == 0, (status, err)
        assert status["scratch_branch_pushed"] is True
    finally:
        shutil.rmtree(base, ignore_errors=True)

