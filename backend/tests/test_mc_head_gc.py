"""``mc-head gc`` — retention report, dry run by default (docs/decisions/085
Nachtrag 2026-10-04 §4; bauplan §2.3/§2.5).

Drives the real host script against a temporary ``MC_HOME`` with a local
bare git origin as the scratch origin (same pattern as
``test_mc_head_scratch_origin.py``), plus direct, in-process calls into the
loaded module for the cases that need to monkeypatch an internal function
(the sabotage probes) rather than the environment.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.heads_host_helpers import (
    MC_HEAD,
    make_origin,
    mark_scratch,
    run_head,
    seed_clone,
    write_spec,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="host script is POSIX-only")

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _load():
    loader = importlib.machinery.SourceFileLoader("mc_head_gc", str(MC_HEAD))
    spec = importlib.util.spec_from_loader("mc_head_gc", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def scratch(tmp_path: Path) -> dict:
    tmp_path = tmp_path.resolve()
    mc_home = tmp_path / "mc"
    so = mc_home / "heads" / "scratch-origin"
    so.mkdir(parents=True)
    origin, _ = make_origin(so, "probe")
    full_name = "scratch/probe"
    clone = seed_clone(mc_home, origin, full_name)
    mark_scratch(mc_home, full_name)
    return {"mc_home": mc_home, "origin": origin, "full_name": full_name, "clone": clone, "tmp": tmp_path}


def _protect_and_cache(run_dir: Path) -> None:
    """Every "never touched" path (bauplan §2.3) plus the cache allowlist,
    so a test can assert the protected half survived an ``--apply`` run."""
    for name in ("job.md", "procedure.md", "run-record.md", "step.txt", "hook-passed.txt", "head-settings.json"):
        (run_dir / name).write_text(f"# {name}\n")
    (run_dir / "head.env").write_text("ANTHROPIC_API_KEY=test-fake-not-a-real-key\n")
    claude_session = run_dir / "claude-config" / "projects" / "wtdir" / "session.jsonl"
    claude_session.parent.mkdir(parents=True, exist_ok=True)
    claude_session.write_text('{"type":"user"}\n')
    omp_session = run_dir / "omp-sessions" / "sess.jsonl"
    omp_session.parent.mkdir(parents=True, exist_ok=True)
    omp_session.write_text('{"type":"session"}\n')
    for rel in ("home/.cache/uv/pkg.bin", "home/Library/Caches/x/data.bin", "home/.npm/_cacache/y/z.bin"):
        p = run_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 4096)


def _write_status(run_dir: Path, *, exited_ago_s: float, supervisor_pid: int = 999999) -> None:
    wrapper = run_dir / ".wrapper"
    wrapper.mkdir(exist_ok=True)
    now = time.time()
    (wrapper / "status.json").write_text(json.dumps({
        "run_id": run_dir.name, "phase": "exited", "exit_code": 0,
        "exited_at": _iso(now - exited_ago_s), "started_at": _iso(now - exited_ago_s - 600),
        "supervisor_pid": supervisor_pid, "pr_url": None,
    }))


def _make_run(
    mc_home: Path, *, repo_full_name: str, branch_suffix: str, origin: Path | None = None,
    dirty=False, untracked=False, detached=False, pushed=True, exited_ago_s=15 * 86400,
    scratch_repo=True, **spec_over,
) -> tuple[str, Path]:
    run_id = write_spec(
        mc_home, repo_full_name=repo_full_name,
        branch=f"mc-head/2026-10-04-gc-{branch_suffix}", job_folder=f"2026-10-04-gc-{branch_suffix}",
        **spec_over,
    )
    run_dir = mc_home / "heads" / run_id
    clone = mc_home / "heads" / "clones" / repo_full_name.replace("/", "--")
    if not (clone / ".git").exists():
        if origin is None:
            origin, _ = make_origin(mc_home / "heads" / "scratch-origin" if scratch_repo else mc_home, branch_suffix)
        seed_clone(mc_home, origin, repo_full_name)
        if scratch_repo:
            mark_scratch(mc_home, repo_full_name)
    spec = json.loads((run_dir / "spec.json").read_text())
    wt = run_dir / "wt"
    subprocess.run(["git", "-C", str(clone), "fetch", "-q", "origin"], check=True)
    subprocess.run(
        ["git", "-C", str(clone), "worktree", "add", "-q", "-b", spec["branch"], str(wt), f"origin/{spec['base_branch']}"],
        check=True,
    )
    if not dirty:
        # Commit (and maybe push) BEFORE planting the untracked file below —
        # `git add .` here must never accidentally sweep it up, or "dirty"
        # becomes "clean" by construction instead of by a real git check.
        (wt / "change.txt").write_text("a real change\n")
        subprocess.run(["git", "-C", str(wt), "add", "."], check=True)
        subprocess.run([*GIT, "-C", str(wt), "commit", "-q", "-m", "fix: change"], check=True)
        if pushed:
            subprocess.run(["git", "-C", str(wt), "push", "-q", "-u", "origin", spec["branch"]], check=True)
    if dirty:
        (wt / "README.md").write_text("dirty change, never committed\n")
    if untracked:
        (wt / "scratchpad.txt").write_text("untracked\n")
    if detached:
        sha = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        subprocess.run(["git", "-C", str(wt), "checkout", "-q", sha], check=True)
    _write_status(run_dir, exited_ago_s=exited_ago_s)
    _protect_and_cache(run_dir)
    return run_id, wt


def _tree_signature(root: Path) -> dict:
    sig = {}
    for p in sorted(root.rglob("*")):
        try:
            st = p.lstat()
        except OSError:
            continue
        rel = str(p.relative_to(root))
        kind = "link" if stat.S_ISLNK(st.st_mode) else ("dir" if stat.S_ISDIR(st.st_mode) else "file")
        size = st.st_size if kind == "file" else None
        sig[rel] = (kind, size)
    return sig


def _report(mc_home: Path) -> dict:
    return json.loads((mc_home / "heads" / "gc-report.json").read_text())


def _run_for(report: dict, run_id: str) -> dict:
    return next(r for r in report["runs"] if r["run_id"] == run_id)


# ── dry run: reports what it would do, touches nothing ──────────────────


def test_dry_run_lists_caches_and_old_pushed_worktree_and_changes_nothing(scratch):
    mc_home = scratch["mc_home"]
    run_id, wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="old", origin=scratch["origin"])
    heads_dir = mc_home / "heads"
    before = _tree_signature(heads_dir)

    res = run_head(mc_home, "gc")
    assert res.returncode == 0, res.stderr

    # gc always creates its own lock file + the report — neither is a run
    # folder's content, so both are excluded from the "changed nothing" proof.
    # ``clones/`` (shared git plumbing, never a run's own data) is excluded
    # too: the worktree-safety hardening on PR #751 makes EVERY gc pass,
    # dry run included, call ``sanitize_clone()`` before it runs a single
    # git command against a worktree (rewriting the clone's ``.git/config``
    # to a canonical form — the fix IS that rewrite, it cannot be skipped
    # just because nothing will be deleted this time) and fetch a
    # throwaway check ref into the clone to verify ancestry safely. Both
    # are idempotent, host-owned plumbing, never a byte of run-folder data.
    def _keep(k: str) -> bool:
        return k not in ignore and not k.startswith("clones/")

    ignore = {"gc-report.json", "locks", "locks/gc.lock"}
    before_excl_clones = {k: v for k, v in before.items() if _keep(k)}
    after_excl_report = {k: v for k, v in _tree_signature(heads_dir).items() if _keep(k)}
    assert after_excl_report == before_excl_clones, "dry run must not change a single byte of the run folders"

    report = _report(mc_home)
    assert report["mode"] == "dry_run"
    run_report = _run_for(report, run_id)
    kinds = {a["kind"] for a in run_report["actions"]}
    assert "cache" in kinds and "worktree" in kinds
    assert all(a["would_remove"] for a in run_report["actions"])
    assert str(wt) in [a["path"] for a in run_report["actions"]]


# ── --apply: removes exactly the allowlist, protects everything else ────


def test_apply_removes_caches_and_old_pushed_worktree_keeps_everything_else(scratch):
    mc_home = scratch["mc_home"]
    run_id, wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="apply", origin=scratch["origin"])
    run_dir = mc_home / "heads" / run_id
    clone = mc_home / "heads" / "clones" / scratch["full_name"].replace("/", "--")

    res = run_head(mc_home, "gc", "--apply")
    assert res.returncode == 0, res.stderr

    assert not wt.exists()
    assert not (run_dir / "home" / ".cache").exists()
    assert not (run_dir / "home" / "Library" / "Caches").exists()
    assert not (run_dir / "home" / ".npm" / "_cacache").exists()

    worktrees = subprocess.run(
        ["git", "-C", str(clone), "worktree", "list", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout
    assert str(wt) not in worktrees
    branches = subprocess.run(
        ["git", "-C", str(clone), "branch", "--list"], capture_output=True, text=True, check=True
    ).stdout
    spec = json.loads((run_dir / "spec.json").read_text())
    assert spec["branch"] in branches, "the local branch stays — Weitermachen re-checks it out"

    for name in ("job.md", "procedure.md", "run-record.md", "step.txt", "hook-passed.txt",
                 "head-settings.json", "head.env", "spec.json"):
        assert (run_dir / name).exists(), f"{name} must never be removed"
    assert (run_dir / "claude-config" / "projects" / "wtdir" / "session.jsonl").exists()
    assert (run_dir / "omp-sessions" / "sess.jsonl").exists()

    report = _report(mc_home)
    assert report["mode"] == "apply"


def test_sabotage_widening_the_cache_allowlist_deletes_a_protected_dir(scratch):
    """Sabotage, run directly against the loaded module: adding
    ``claude-config`` to the cache allowlist must turn this from a protected
    directory into a deleted one — proving the allowlist, not luck, is what
    keeps transcripts alive through ``--apply``."""
    mc_home = scratch["mc_home"]
    run_id, _wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="sabotage", origin=scratch["origin"])
    run_dir = mc_home / "heads" / run_id
    mod = _load()
    os.environ["MC_HOME"] = str(mc_home)
    try:
        mod.GC_CACHE_RELATIVE_PATHS = mod.GC_CACHE_RELATIVE_PATHS + ("claude-config",)
        mod._run_gc(apply=True)
    finally:
        del os.environ["MC_HOME"]
    assert not (run_dir / "claude-config").exists(), "sabotage must be visible: a transcript directory got removed"


# ── kept, with the right reason ──────────────────────────────────────────


def test_dirty_worktree_is_kept(scratch):
    mc_home = scratch["mc_home"]
    run_id, wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="dirty", origin=scratch["origin"], dirty=True)
    run_head(mc_home, "gc", "--apply")
    assert wt.exists()
    kept = _run_for(_report(mc_home), run_id)["kept"]
    assert any(k["path"] == str(wt) and k["reason"] == "dirty" for k in kept)


def test_untracked_file_counts_as_dirty(scratch):
    mc_home = scratch["mc_home"]
    run_id, wt = _make_run(
        mc_home, repo_full_name=scratch["full_name"], branch_suffix="untracked", origin=scratch["origin"], untracked=True,
    )
    run_head(mc_home, "gc", "--apply")
    assert wt.exists()
    kept = _run_for(_report(mc_home), run_id)["kept"]
    assert any(k["path"] == str(wt) and k["reason"] == "dirty" for k in kept)


def test_head_not_pushed_is_kept_not_pushed(scratch):
    mc_home = scratch["mc_home"]
    run_id, wt = _make_run(
        mc_home, repo_full_name=scratch["full_name"], branch_suffix="notpushed", origin=scratch["origin"], pushed=False,
    )
    run_head(mc_home, "gc", "--apply")
    assert wt.exists()
    kept = _run_for(_report(mc_home), run_id)["kept"]
    assert any(k["path"] == str(wt) and k["reason"] == "not_pushed" for k in kept)


def test_sabotage_removing_the_ancestry_check_misclassifies_not_pushed(scratch):
    """Sabotage: without the merge-base ancestry check, an unpushed branch
    looks exactly as "safe" as a pushed one — this is what proves the check
    is load-bearing, not decorative."""
    mc_home = scratch["mc_home"]
    run_id, wt = _make_run(
        mc_home, repo_full_name=scratch["full_name"], branch_suffix="notpushed2", origin=scratch["origin"], pushed=False,
    )
    mod = _load()
    os.environ["MC_HOME"] = str(mc_home)
    try:
        mod._wt_head_in_scratch_origin = lambda spec, wt: True  # SABOTAGE
        report = mod._run_gc(apply=False)
    finally:
        del os.environ["MC_HOME"]
    run_report = next(r for r in report["runs"] if r["run_id"] == run_id)
    assert any(a["path"] == str(wt) for a in run_report["actions"]), (
        "with the ancestry check disabled, the unpushed worktree is wrongly offered for removal"
    )


def test_detached_head_is_kept(scratch):
    mc_home = scratch["mc_home"]
    run_id, wt = _make_run(
        mc_home, repo_full_name=scratch["full_name"], branch_suffix="detached", origin=scratch["origin"], detached=True,
    )
    run_head(mc_home, "gc", "--apply")
    assert wt.exists()
    kept = _run_for(_report(mc_home), run_id)["kept"]
    assert any(k["path"] == str(wt) and k["reason"] == "detached" for k in kept)


def test_real_github_style_repo_never_loses_its_worktree_in_v1(tmp_path):
    mc_home = tmp_path / "mc"
    mc_home.mkdir()
    run_id, wt = _make_run(mc_home, repo_full_name="owner/real", branch_suffix="real", scratch_repo=False)
    res = run_head(mc_home, "gc", "--apply")
    assert res.returncode == 0, res.stderr
    assert wt.exists(), "v1 removes caches only for a real (non-scratch) repo, never the worktree"
    run_dir = mc_home / "heads" / run_id
    assert not (run_dir / "home" / ".cache").exists()
    kept = _run_for(_report(mc_home), run_id)["kept"]
    assert any(k["path"] == str(wt) and k["reason"] == "real_repo_v1" for k in kept)


def test_young_clean_pushed_worktree_is_kept_for_age_without_budget_pressure(scratch):
    mc_home = scratch["mc_home"]
    run_id, wt = _make_run(
        mc_home, repo_full_name=scratch["full_name"], branch_suffix="young", origin=scratch["origin"], exited_ago_s=3600 * 2,
    )
    run_head(mc_home, "gc", "--apply")
    assert wt.exists()
    kept = _run_for(_report(mc_home), run_id)["kept"]
    assert any(k["path"] == str(wt) and k["reason"] == "age" for k in kept)


def test_running_run_is_skipped_entirely(scratch):
    mc_home = scratch["mc_home"]
    run_id, wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="running", origin=scratch["origin"])
    (mc_home / "heads" / run_id / ".wrapper" / "status.json").write_text(
        json.dumps({"run_id": run_id, "phase": "running", "supervisor_pid": os.getpid()})
    )
    run_head(mc_home, "gc", "--apply")
    assert wt.exists()
    report = _report(mc_home)
    assert not any(r["run_id"] == run_id for r in report["runs"]), "an active run must not even appear in the report"


def test_predecessor_of_a_running_continue_is_skipped(scratch):
    mc_home = scratch["mc_home"]
    old_id, old_wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="pred", origin=scratch["origin"])
    new_id = write_spec(
        mc_home, repo_full_name=scratch["full_name"], mode="continue", restarted_from=old_id,
        branch=json.loads((mc_home / "heads" / old_id / "spec.json").read_text())["branch"],
    )
    (mc_home / "heads" / new_id / ".wrapper").mkdir(exist_ok=True)
    (mc_home / "heads" / new_id / ".wrapper" / "status.json").write_text(
        json.dumps({"run_id": new_id, "phase": "running", "supervisor_pid": os.getpid()})
    )
    run_head(mc_home, "gc", "--apply")
    assert old_wt.exists(), "the predecessor's worktree must survive while its continuation is still running"
    report = _report(mc_home)
    assert not any(r["run_id"] == old_id for r in report["runs"])


# ── budget: oldest first, until under budget ─────────────────────────────


def _measure(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def test_budget_pressure_removes_oldest_eligible_worktree_first(scratch):
    mc_home = scratch["mc_home"]
    ids_and_wts = []
    for i, age_days in enumerate((2, 5, 8)):  # all younger than the 14-day age rule
        run_id, wt = _make_run(
            mc_home, repo_full_name=scratch["full_name"], branch_suffix=f"budget{i}", origin=scratch["origin"],
            exited_ago_s=age_days * 86400,
        )
        ids_and_wts.append((run_id, wt))
    (id2, wt2), (id5, wt5), (id8, wt8) = ids_and_wts

    # Measure exactly, rather than guess: "floor" is everything gc NEVER
    # removes regardless of budget (spec.json, job.md, transcripts, …) —
    # caches are always removed first and worktrees are the only
    # budget-sensitive part. Setting the budget to (floor + the youngest
    # worktree's own bytes) + 1 guarantees the two older worktrees must
    # come off to clear it, and that clearing stops there.
    heads_dir = mc_home / "heads"
    total_before = _measure(heads_dir)
    wt_bytes = {rid: _measure(wt) for rid, wt in ids_and_wts}
    cache_bytes = {rid: _measure(mc_home / "heads" / rid / "home") for rid, wt in ids_and_wts}
    floor = total_before - sum(wt_bytes.values()) - sum(cache_bytes.values())
    budget = floor + wt_bytes[id2] + 1

    res = run_head(mc_home, "gc", "--apply", env_extra={"MC_HEAD_GC_BUDGET_BYTES": str(budget)})
    assert res.returncode == 0, res.stderr

    assert not wt8.exists(), "oldest must go first"
    assert not wt5.exists(), "second-oldest must also go under tight budget"
    assert wt2.exists(), "youngest stays while it is the only one needed to clear the budget"


# ── symlinked cache target is never followed ─────────────────────────────


def test_symlinked_cache_dir_target_is_untouched(scratch):
    mc_home = scratch["mc_home"]
    run_id, _wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="symlink", origin=scratch["origin"])
    run_dir = mc_home / "heads" / run_id
    foreign = scratch["tmp"] / "foreign-cache"
    foreign.mkdir()
    (foreign / "keepme.bin").write_bytes(b"do not touch")
    cache_dir = run_dir / "home" / ".cache"
    import shutil as _shutil

    _shutil.rmtree(cache_dir)
    os.symlink(foreign, cache_dir)

    res = run_head(mc_home, "gc", "--apply")
    assert res.returncode == 0, res.stderr
    assert (foreign / "keepme.bin").exists(), "a symlinked cache dir must never be followed into a foreign target"
    kept = _run_for(_report(mc_home), run_id)["kept"]
    assert any(k["path"] == str(cache_dir) and k["reason"] == "symlink" for k in kept)


# ── cmd_watch wiring: throttled, scharf only with the env var ────────────


def test_watch_runs_gc_dry_by_default_and_apply_with_the_env_var(scratch):
    mc_home = scratch["mc_home"]
    run_id, wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="watch", origin=scratch["origin"])
    (mc_home / "heads" / "spool").mkdir(exist_ok=True)

    res = run_head(mc_home, "watch")
    assert res.returncode == 0, res.stderr
    assert _report(mc_home)["mode"] == "dry_run"
    assert wt.exists(), "watch without the apply env var must not delete anything"

    res2 = run_head(mc_home, "watch", env_extra={"MC_HEAD_GC_APPLY": "1", "MC_HEAD_GC_THROTTLE_S": "0"})
    assert res2.returncode == 0, res2.stderr
    assert _report(mc_home)["mode"] == "apply"
    assert not wt.exists(), "watch WITH MC_HEAD_GC_APPLY=1 must actually delete"


def test_watch_throttles_a_second_gc_pass(scratch):
    mc_home = scratch["mc_home"]
    (mc_home / "heads" / "spool").mkdir(exist_ok=True)
    run_head(mc_home, "watch")
    report_path = mc_home / "heads" / "gc-report.json"
    first_written = report_path.read_text()
    first_mtime = report_path.stat().st_mtime
    os.utime(report_path, (first_mtime, first_mtime))

    run_head(mc_home, "watch")  # default throttle is 6h — must not touch the report again
    assert report_path.read_text() == first_written


# ── symlinked INTERMEDIATE cache directory is never followed ────────────
# (review finding on PR #751: the old check only looked at the FINAL
# component — ``home/.cache`` itself — never at ``home`` or ``home/Library``,
# both of which a head can also replace with a symlink to escape the run
# folder entirely.)


@pytest.mark.parametrize(
    "swap_rel,cache_rel",
    [
        ("home", "home/.cache"),
        ("home/Library", "home/Library/Caches"),
    ],
)
def test_symlinked_intermediate_dir_above_a_cache_path_is_untouched(scratch, swap_rel, cache_rel):
    mc_home = scratch["mc_home"]
    # branch names are lowercase-only (mc-head's own PATTERNS["branch"]
    # regex) — a suffix derived from "home/Library" must not leak its
    # capital "L" into it, or load_spec silently drops the whole run.
    suffix_tag = swap_rel.replace("/", "-").lower()
    run_id, _wt = _make_run(
        mc_home, repo_full_name=scratch["full_name"], branch_suffix=f"symlink-mid-{suffix_tag}",
        origin=scratch["origin"],
    )
    run_dir = mc_home / "heads" / run_id
    foreign = scratch["tmp"] / f"foreign-{swap_rel.replace('/', '-')}"
    # The sentinel sits where the REAL cache path would resolve to if the
    # symlink were followed — same relative suffix below the swapped
    # component, so a successful escape would delete exactly this file.
    suffix = Path(cache_rel).relative_to(swap_rel)
    (foreign / suffix.parent).mkdir(parents=True, exist_ok=True)
    (foreign / suffix).write_bytes(b"do not touch")
    import shutil as _shutil

    target = run_dir / swap_rel
    _shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(foreign, target)

    res = run_head(mc_home, "gc", "--apply")
    assert res.returncode == 0, res.stderr
    assert (foreign / suffix).exists(), f"a symlinked {swap_rel!r} must never let gc escape the run folder"
    kept = _run_for(_report(mc_home), run_id)["kept"]
    assert any(k["path"] == str(run_dir / cache_rel) and k["reason"] == "symlink" for k in kept)


# ── worktree git calls never execute code a head planted (review finding,
# PR #751): a forged wt/.git pointing OUTSIDE the clone, at a repo carrying
# its own filter driver, must never be followed by gc's unsandboxed git
# calls — not even in a DRY RUN. ─────────────────────────────────────────


def _evil_repo_with_filter_bomb(tmp: Path, marker: Path, tracked_name: str, committed_text: str) -> Path:
    """A repo OUTSIDE the clone, tracking ``tracked_name`` with
    ``committed_text``, whose CLEAN filter touches ``marker``. The filter is
    configured only AFTER the initial commit (so constructing the repo
    itself never fires it — ``git add``/``git commit`` would otherwise run
    straight through the freshly-configured filter and make any later
    "did gc trigger this" check meaningless)."""
    evil = tmp / "evilrepo"
    evil.mkdir(exist_ok=True)
    git = ["git", "-C", str(evil)]
    subprocess.run(["git", "init", "-q", str(evil)], check=True)
    (evil / tracked_name).write_text(committed_text)
    subprocess.run([*git, "add", tracked_name], check=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x"], check=True
    )
    subprocess.run([*git, "config", "filter.x.clean", f"sh -c 'touch {marker}'"], check=True)
    (evil / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (evil / ".git" / "info" / "attributes").write_text("* filter=x\n")
    return evil


def _forge_wt_gitdir(wt: Path, evil: Path) -> None:
    """Exactly what a head's sandbox can do: ``wt/.git`` is a regular file
    directly under ``WT``, which the sandbox profile grants full write
    access to (``head.sb``'s ``CLONE_GIT``/``WT`` subpaths)."""
    (wt / ".git").write_text(f"gitdir: {evil / '.git'}\n")


def test_control_a_forged_gitdir_really_does_execute_a_planted_filter(scratch, tmp_path):
    """Not a regression guard by itself — proof that the construction below
    is genuinely dangerous: a plain, unguarded git command that needs the
    worktree file's CONTENT (``diff``, unlike ``status``, cannot answer from
    size/mtime alone) against a forged ``wt/.git`` executes the attacker's
    filter. This is the exact capability ``_wt_safe_gitdir``/``_wt_git``
    below exist to deny gc's own git calls."""
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "f.txt").write_text("real worktree content, not evil's\n")
    marker = tmp_path / "PWNED-control"
    evil = _evil_repo_with_filter_bomb(tmp_path, marker, "f.txt", "evil committed content\n")
    _forge_wt_gitdir(wt, evil)

    subprocess.run(["git", "-C", str(wt), "diff"], capture_output=True)
    assert marker.exists(), "control failed: the forged-gitdir filter-bomb construction does not fire"


def test_dry_run_forged_worktree_gitdir_is_kept_as_foreign_gitdir_and_never_executed(scratch):
    """End-to-end reproduction of the review finding's own PoC shape: plain
    ``mc-head gc`` (no ``--apply``) must never run a single git command
    against a forged ``wt/.git`` — reported ``kept: foreign_gitdir``, not
    silently "clean" or "pushed" through it."""
    mc_home, tmp = scratch["mc_home"], scratch["tmp"]
    run_id, wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="gitdir-bomb", origin=scratch["origin"])
    marker = tmp / "PWNED"
    evil = _evil_repo_with_filter_bomb(tmp, marker, "change.txt", "evil committed content\n")
    _forge_wt_gitdir(wt, evil)

    res = run_head(mc_home, "gc")  # DRY RUN
    assert res.returncode == 0, res.stderr
    assert not marker.exists(), "gc must never run a git command against a forged wt/.git, dry run or not"
    kept = _run_for(_report(mc_home), run_id)["kept"]
    assert any(k["path"] == str(wt) and k["reason"] == "foreign_gitdir" for k in kept)


def test_apply_forged_worktree_gitdir_is_never_removed_or_executed(scratch):
    """Same construction, with ``--apply`` — the worktree must survive
    (never "removable" through the forged gitdir) and the filter must still
    never fire."""
    mc_home, tmp = scratch["mc_home"], scratch["tmp"]
    run_id, wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="gitdir-bomb-apply", origin=scratch["origin"])
    marker = tmp / "PWNED2"
    evil = _evil_repo_with_filter_bomb(tmp, marker, "change.txt", "evil committed content\n")
    _forge_wt_gitdir(wt, evil)

    res = run_head(mc_home, "gc", "--apply")
    assert res.returncode == 0, res.stderr
    assert not marker.exists()
    assert wt.exists(), "a forged gitdir must never make gc treat the worktree as removable"
    kept = _run_for(_report(mc_home), run_id)["kept"]
    assert any(k["path"] == str(wt) and k["reason"] == "foreign_gitdir" for k in kept)


def test_sabotage_skipping_the_gitdir_validation_lets_wt_git_reach_the_forged_repo(scratch):
    """Sabotage, run directly against the loaded module: short-circuiting
    ``_wt_safe_gitdir`` back to the old "always trust wt/.git" behaviour
    must let ``_wt_git`` actually run a content-reading command (``diff``)
    against the forged repo and fire its filter — proving the validation,
    not luck, is what stops it. ``_wt_decision`` itself never calls
    ``diff`` (that is the honest control above, isolating the dangerous
    CAPABILITY from gc's specific, narrower call list), so this probes
    ``_wt_git`` directly rather than through a full ``_run_gc`` pass."""
    mc_home, tmp = scratch["mc_home"], scratch["tmp"]
    run_id, wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="gitdir-bomb-sabotage", origin=scratch["origin"])
    marker = tmp / "PWNED3"
    evil = _evil_repo_with_filter_bomb(tmp, marker, "change.txt", "evil committed content\n")
    _forge_wt_gitdir(wt, evil)

    mod = _load()
    os.environ["MC_HOME"] = str(mc_home)
    try:
        spec = json.loads((mc_home / "heads" / run_id / "spec.json").read_text())
        mod._wt_safe_gitdir = lambda spec, wt: evil / ".git"  # SABOTAGE: always "validated"
        mod._wt_git(spec, wt, ["diff"])
    finally:
        del os.environ["MC_HOME"]
    assert marker.exists(), "sabotage must be visible: _wt_git reached the forged repo and ran its filter"


def test_wt_safe_gitdir_rejects_the_forged_gitdir_directly(scratch):
    """Unit-level proof (no subprocess, no filter side-channel needed) that
    ``_wt_safe_gitdir`` itself is what refuses the forged pointer."""
    mc_home, tmp = scratch["mc_home"], scratch["tmp"]
    run_id, wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="gitdir-unit", origin=scratch["origin"])
    evil = _evil_repo_with_filter_bomb(tmp, tmp / "unused-marker", "change.txt", "evil committed content\n")
    _forge_wt_gitdir(wt, evil)

    mod = _load()
    os.environ["MC_HOME"] = str(mc_home)
    try:
        spec = json.loads((mc_home / "heads" / run_id / "spec.json").read_text())
        assert mod._wt_safe_gitdir(spec, wt) is None
        # And _wt_git refuses WITHOUT ever invoking subprocess against it.
        calls = []
        mod._git = lambda *a, **kw: calls.append(a) or subprocess.CompletedProcess(a, 0, "", "")
        res = mod._wt_git(spec, wt, ["diff"])
        assert res.returncode != 0 and calls == [], "a foreign gitdir must never reach a real git call"
    finally:
        del os.environ["MC_HOME"]


# ── a run whose clone is shared with another, still-active run is skipped ─


def test_worktree_inspection_is_skipped_while_another_run_on_the_same_clone_is_active(scratch):
    mc_home = scratch["mc_home"]
    old_id, old_wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="clone-active-old", origin=scratch["origin"])
    # A second, unrelated run on the SAME clone (different branch), still running.
    other_id = write_spec(
        mc_home, repo_full_name=scratch["full_name"],
        branch=f"mc-head/2026-10-04-gc-clone-active-other", job_folder="2026-10-04-gc-clone-active-other",
    )
    (mc_home / "heads" / other_id / ".wrapper").mkdir(exist_ok=True)
    (mc_home / "heads" / other_id / ".wrapper" / "status.json").write_text(
        json.dumps({"run_id": other_id, "phase": "running", "supervisor_pid": os.getpid()})
    )

    run_head(mc_home, "gc", "--apply")
    assert old_wt.exists(), "a worktree must be kept while ANOTHER run shares its clone and is still active"
    kept = _run_for(_report(mc_home), old_id)["kept"]
    assert any(k["path"] == str(old_wt) and k["reason"] == "clone_active" for k in kept)


# ── skipped runs are listed in the report, with their reason ─────────────


def test_skipped_runs_are_listed_in_the_report_with_their_reason(scratch):
    mc_home = scratch["mc_home"]
    running_id, _wt = _make_run(mc_home, repo_full_name=scratch["full_name"], branch_suffix="skip-running", origin=scratch["origin"])
    (mc_home / "heads" / running_id / ".wrapper" / "status.json").write_text(
        json.dumps({"run_id": running_id, "phase": "running", "supervisor_pid": os.getpid()})
    )
    young_id, _wt2 = _make_run(
        mc_home, repo_full_name=scratch["full_name"], branch_suffix="skip-young", origin=scratch["origin"],
        exited_ago_s=60,
    )

    run_head(mc_home, "gc", "--apply")
    report = _report(mc_home)
    skipped_by_id = {s["run_id"]: s["reason"] for s in report["skipped"]}
    assert skipped_by_id.get(running_id) == "not_exited"
    assert skipped_by_id.get(young_id) == "too_recent"
    assert not any(r["run_id"] in (running_id, young_id) for r in report["runs"])
