"""A2–A5, A7 — mc-head start/stop/restart/result/watch with a fake harness.

Everything runs against a temporary MC_HOME and a local bare repo as origin
(registered as a scratch repo). The fake harness gets the exact argv of the
harness table, so the table itself is under test too.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
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


@pytest.fixture
def env(tmp_path: Path):
    mc_home = tmp_path / "mc"
    mc_home.mkdir()
    origin, full_name = make_origin(tmp_path)
    clone = seed_clone(mc_home, origin, full_name)
    mark_scratch(mc_home, full_name)
    return {"mc_home": mc_home, "origin": origin, "full_name": full_name, "clone": clone, "tmp": tmp_path}


def _extra(harness: Path, **more) -> dict:
    d = {"MC_HEAD_BIN_OMP": str(harness), "MC_HEAD_BIN_CLAUDE": str(harness), "SECRET_TOKEN_X": "leak-me"}
    d.update(more)
    return d


# ── A2: start, worktree, env, guards ────────────────────────────────────


def test_start_runs_harness_in_worktree_and_exits(env):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "echo hello from head; echo 'step 1/7 plan' > \"$MC_HEAD_RUN_DIR/step.txt\"")
    run_id = write_spec(mc_home)
    res = run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    assert res.returncode == 0, res.stdout + res.stderr
    status = wait_phase(mc_home, run_id, "exited")
    assert status["exit_code"] == 0
    assert status["reason"] is None
    run = mc_home / "heads" / run_id
    # worktree on the head branch, from origin/main
    branch = subprocess.run(
        ["git", "-C", str(run / "wt"), "branch", "--show-current"], capture_output=True, text=True
    ).stdout.strip()
    assert branch.startswith("mc-head/")
    assert "hello from head" in (run / "head.log").read_text()
    assert (run / "step.txt").read_text().strip() == "step 1/7 plan"
    # harness table: omp × local
    argv = (run / "argv.txt").read_text().splitlines()
    assert argv[:4] == ["--profile", "mc-head", "--model", "mc-openai/GLM-5.3-Flash-EXL3"]
    assert "-p" in argv and "--auto-approve" in argv
    # the session is kept in the run folder: the token harvester reads usage from it
    assert "--no-session" not in argv
    assert argv[argv.index("--session-dir") + 1] == str(run / "omp-sessions")
    assert (run / "omp-sessions").is_dir()
    assert argv[argv.index("--append-system-prompt") + 1] == str(run / "procedure.md")
    assert argv[argv.index("--max-time") + 1] == "630"
    assert "Say hello." in "\n".join(argv)
    # env -i: own HOME, shims first on PATH, nothing inherited
    env_lines = dict(
        line.split("=", 1) for line in (run / "env.txt").read_text().splitlines() if "=" in line
    )
    assert env_lines["HOME"] == str(run / "home")
    assert env_lines["PATH"].split(":")[0] == str(run / "bin")
    assert env_lines["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "SECRET_TOKEN_X" not in env_lines
    assert "MC_HOME" not in env_lines
    # omp profile rendered into the head's own HOME, never the operator's
    models = run / "home" / ".omp" / "profiles" / "mc-head" / "agent" / "models.yml"
    assert "baseUrl: http://127.0.0.1:9/v1" in models.read_text()
    assert oct(models.stat().st_mode & 0o777) == "0o600"


def test_claude_pair_uses_bare_settings_and_own_config_dir(env):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "cat > ../stdin.txt")
    run_id = write_spec(mc_home, harness="claude")
    run = mc_home / "heads" / run_id
    (run / "head.env").write_text(
        "ANTHROPIC_BASE_URL='http://127.0.0.1:9'\nANTHROPIC_API_KEY='placeholder'\nEVIL=1\nGH_TOKEN=smuggled\n"
    )
    assert run_head(mc_home, "start", run_id, env_extra=_extra(harness)).returncode == 0
    wait_phase(mc_home, run_id, "exited")
    argv = (run / "argv.txt").read_text().splitlines()
    assert argv[:3] == ["-p", "--bare", "--settings"]
    assert argv[argv.index("--append-system-prompt-file") + 1] == str(run / "procedure.md")
    # the job arrives on stdin, never as an argument a variadic flag could eat
    assert "Say hello." not in "\n".join(argv)
    # job.md = the backend's job + the launcher's kz brief section (unavailable here)
    assert (run / "stdin.txt").read_text() == (run / "job.md").read_text()
    assert (run / "stdin.txt").read_text().startswith("# Job\nSay hello.\n")
    settings = json.loads((run / "head-settings.json").read_text())
    assert "Bash(gh pr merge*)" in settings["permissions"]["deny"]
    assert "Bash(git push origin HEAD:main*)" in settings["permissions"]["deny"]
    # Live probe 2026-09-23: the legacy "prefix:*" form did NOT match the
    # head-branch push — only the wildcard form does.
    rules = settings["permissions"]["allow"] + settings["permissions"]["deny"]
    assert not [r for r in rules if ":*)" in r]
    assert "Read(~/.ssh/**)" in settings["permissions"]["deny"]
    assert not any(a == "Bash" or a == "Bash(*)" for a in settings["permissions"]["allow"])
    env_lines = dict(l.split("=", 1) for l in (run / "env.txt").read_text().splitlines() if "=" in l)
    assert env_lines["CLAUDE_CONFIG_DIR"] == str(run / "claude-config")
    assert env_lines["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    assert "EVIL" not in env_lines
    # head.env cannot smuggle a GitHub token — only the operator file can
    assert "GH_TOKEN" not in env_lines


def test_pre_push_hook_refuses_main_and_force(env):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "true")
    run_id = write_spec(mc_home)
    assert run_head(mc_home, "start", run_id, env_extra=_extra(harness)).returncode == 0
    wait_phase(mc_home, run_id, "exited")
    wt = mc_home / "heads" / run_id / "wt"
    git = ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    (wt / "x.txt").write_text("x\n")
    subprocess.run([*git, "add", "x.txt"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
    to_main = subprocess.run([*git, "push", "origin", "HEAD:main"], capture_output=True, text=True)
    assert to_main.returncode != 0
    assert "refused" in to_main.stderr
    ok = subprocess.run([*git, "push", "origin", "HEAD:refs/heads/mc-head/demo-ok"], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    subprocess.run([*git, "commit", "-q", "--amend", "-m", "rewritten"], check=True)
    force = subprocess.run(
        [*git, "push", "--force", "origin", "HEAD:refs/heads/mc-head/demo-ok"], capture_output=True, text=True
    )
    assert force.returncode != 0 and "non-fast-forward" in force.stderr


def test_shims_block_tools_and_gh_merge(env, tmp_path):
    mc_home = env["mc_home"]
    fake_gh = tmp_path / "gh"
    fake_gh.write_text("#!/bin/sh\necho REAL-GH \"$@\"\n")
    fake_gh.chmod(0o755)
    harness = fake_harness(env["tmp"], "true")
    run_id = write_spec(mc_home)
    assert run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_GH=str(fake_gh))).returncode == 0
    wait_phase(mc_home, run_id, "exited")
    bindir = mc_home / "heads" / run_id / "bin"
    for tool in ("docker", "ssh", "sudo", "launchctl"):
        r = subprocess.run([str(bindir / tool), "ps"], capture_output=True, text=True)
        assert r.returncode == 126 and "blocked" in r.stderr
    merge = subprocess.run([str(bindir / "gh"), "pr", "merge", "1", "--admin"], capture_output=True, text=True)
    assert merge.returncode == 126
    lst = subprocess.run([str(bindir / "gh"), "pr", "list"], capture_output=True, text=True)
    assert lst.returncode == 0 and "REAL-GH pr list" in lst.stdout


def test_real_repo_needs_identity_and_omp_needs_sandbox(env):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "true")
    run_id = write_spec(mc_home, repo_full_name="owner/real-repo")
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    assert read_status(mc_home, run_id)["reason"] == "gh_identity_missing"
    (mc_home / "heads" / "gh-token").write_text("github_pat_fake\n")
    run_id2 = write_spec(mc_home, repo_full_name="owner/real-repo")
    run_head(mc_home, "start", run_id2, env_extra=_extra(harness, MC_HEAD_SANDBOX="0"))
    assert read_status(mc_home, run_id2)["reason"] == "sandbox_required"


@pytest.mark.parametrize("harness_name", ["omp", "claude"])
def test_scratch_repo_also_needs_the_sandbox(env, harness_name):
    """Review finding: without the sandbox a scratch head could write
    heads/scratch-repos (list a real repo) or .wrapper/status.json (fake a
    result). Every run needs the sandbox, scratch included."""
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "true")
    run_id = write_spec(mc_home, harness=harness_name)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_SANDBOX="0"))
    assert read_status(mc_home, run_id)["reason"] == "sandbox_required"
    assert not (mc_home / "heads" / run_id / "argv.txt").exists()
    # a missing sandbox binary counts as no sandbox
    run_id2 = write_spec(mc_home, harness=harness_name)
    run_head(mc_home, "start", run_id2, env_extra=_extra(harness, MC_HEAD_SANDBOX_EXEC="/nonexistent/sandbox-exec"))
    assert read_status(mc_home, run_id2)["reason"] == "sandbox_required"


def test_claude_on_a_real_repo_also_needs_the_sandbox(env):
    """Review blocker: Claude's allow list runs code the head wrote itself
    (pytest, npm test) — without the sandbox that code reads real secrets."""
    mc_home = env["mc_home"]
    (mc_home / "heads" / "gh-token").write_text("github_pat_fake\n")
    harness = fake_harness(env["tmp"], "true")
    run_id = write_spec(mc_home, repo_full_name="owner/real-repo", harness="claude")
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_SANDBOX="0"))
    assert read_status(mc_home, run_id)["reason"] == "sandbox_required"
    assert not (mc_home / "heads" / run_id / "argv.txt").exists()


def _fake_gh(tmp: Path, perms: str, rules: str) -> Path:
    gh = tmp / "fake-gh-api"
    gh.write_text(
        "#!/bin/sh\n"
        "echo \"$@\" >> \"$(dirname \"$0\")/gh-calls.txt\"\n"
        "case \"$2\" in\n"
        f"  repos/owner/real-repo) echo '{{\"permissions\": {perms}}}' ;;\n"
        f"  repos/owner/real-repo/rules/branches/main) echo '{rules}' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)
    return gh


@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(), reason="sandbox-exec only on macOS")
@pytest.mark.parametrize(
    "perms,rules",
    [
        ('{"admin": true, "push": true}', '[{"type": "pull_request"}]'),
        ('{"admin": false, "maintain": true, "push": true}', '[{"type": "pull_request"}]'),
        ('{"admin": false, "push": true}', '[{"type": "deletion"}]'),
        ('{"admin": false, "push": true}', '[]'),
    ],
)
def test_strong_identity_or_unprotected_base_branch_is_refused(env, perms, rules):
    """GH_TOKEN is in the head's env — only a weak identity + a GitHub rule on
    the base branch hold against push-to-main or merge via the REST API."""
    mc_home = env["mc_home"]
    (mc_home / "heads" / "gh-token").write_text("github_pat_fake\n")
    gh = _fake_gh(env["tmp"], perms, rules)
    harness = fake_harness(env["tmp"], "true")
    run_id = write_spec(mc_home, repo_full_name="owner/real-repo", harness="claude")
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_GH=str(gh), MC_HEAD_SANDBOX="1"))
    assert read_status(mc_home, run_id)["reason"] == "gh_identity_unsafe"
    assert not (mc_home / "heads" / "identity-ok").exists()


@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(), reason="sandbox-exec only on macOS")
def test_weak_identity_on_protected_branch_passes_the_gate_and_is_cached(env):
    mc_home = env["mc_home"]
    (mc_home / "heads" / "gh-token").write_text("github_pat_fake\n")
    gh = _fake_gh(env["tmp"], '{"admin": false, "maintain": false, "push": true}',
                  '[{"type": "deletion"}, {"type": "required_status_checks"}]')
    harness = fake_harness(env["tmp"], "true")
    extra = _extra(harness, MC_HEAD_GH=str(gh), MC_HEAD_SANDBOX="1")
    run_id = write_spec(mc_home, repo_full_name="owner/real-repo", harness="claude")
    run_head(mc_home, "start", run_id, env_extra=extra)
    # past every gate: fails later at the (fake) clone, not at a gate
    assert read_status(mc_home, run_id)["reason"] == "prepare_failed"
    calls = (env["tmp"] / "gh-calls.txt").read_text().count("api ")
    run_id2 = write_spec(mc_home, repo_full_name="owner/real-repo", harness="claude")
    run_head(mc_home, "start", run_id2, env_extra=extra)
    assert read_status(mc_home, run_id2)["reason"] == "prepare_failed"
    assert (env["tmp"] / "gh-calls.txt").read_text().count("api ") == calls  # cached
    # a new token file invalidates the cache
    time.sleep(0.05)
    (mc_home / "heads" / "gh-token").write_text("github_pat_other\n")
    os.utime(mc_home / "heads" / "gh-token", (time.time() + 5, time.time() + 5))
    run_id3 = write_spec(mc_home, repo_full_name="owner/real-repo", harness="claude")
    run_head(mc_home, "start", run_id3, env_extra=extra)
    assert (env["tmp"] / "gh-calls.txt").read_text().count("api ") > calls


def test_claude_settings_deny_the_real_home_and_scope_edits_to_the_worktree(env):
    """HOME=<run>/home makes every "~/" rule point at the throw-away folder:
    the rendered settings must repeat them with the REAL home."""
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "true")
    run_id = write_spec(mc_home, harness="claude")
    assert run_head(mc_home, "start", run_id, env_extra=_extra(harness)).returncode == 0
    wait_phase(mc_home, run_id, "exited")
    run = mc_home / "heads" / run_id
    real_home = mc_home.parent  # run_head sets HOME to it
    perms = json.loads((run / "head-settings.json").read_text())["permissions"]
    assert f"Read(/{real_home}/.ssh/**)" in perms["deny"]
    assert f"Read(/{real_home}/.mc/secrets/**)" in perms["deny"]
    assert f"Read(/{real_home}/.netrc)" in perms["deny"]
    assert "Read(~/.ssh/**)" in perms["deny"]  # the ~ form stays
    assert "Edit" not in perms["allow"] and "Write" not in perms["allow"]
    assert f"Edit(/{run}/wt/**)" in perms["allow"]
    assert f"Write(/{run}/run-record.md)" in perms["allow"]


# ── A3: box lock with owner ─────────────────────────────────────────────

SLEEPER = "trap 'exit 0' TERM; while :; do sleep 0.1; done"


def test_second_head_on_same_box_is_busy_and_lock_is_released(env):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], SLEEPER)
    first = write_spec(mc_home)
    assert run_head(mc_home, "start", first, env_extra=_extra(harness)).returncode == 0
    wait_phase(mc_home, first, "running")
    second = write_spec(mc_home)
    run_head(mc_home, "start", second, env_extra=_extra(harness))
    status2 = wait_phase(mc_home, second, "exited")
    assert status2["reason"] == "box_busy"
    lock = mc_home / "heads" / "locks" / str(uuid.UUID(int=1))
    assert lock.is_dir()
    assert run_head(mc_home, "stop", first, env_extra=_extra(harness)).returncode == 0
    assert read_status(mc_home, first)["reason"] == "stopped"
    assert not lock.exists()


def test_stale_lock_of_dead_owner_is_taken_over(env):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "true")
    dead = subprocess.Popen(["true"])
    dead.wait()
    lock = mc_home / "heads" / "locks" / str(uuid.UUID(int=1))
    lock.mkdir(parents=True)
    (lock / "owner.json").write_text(json.dumps({"pid": dead.pid, "start": "gone", "run_id": "x"}))
    run_id = write_spec(mc_home)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["reason"] is None, status
    assert not lock.exists()


# ── A4: progress watchdog, hard limit, stop, restart ────────────────────
#
# A head is stopped when nothing moved for ``no_progress_s`` (head.log,
# step.txt or any file in the worktree) — "no_progress". The wall-clock
# ``time_limit_s`` stays as a generous emergency brake — "hard_limit".
# MC_HEAD_MAX_NO_PROGRESS_S / MC_HEAD_MAX_TIME_S are the operator caps the
# tests use to get second-scale limits past the 60 s spec minimum.


def test_quiet_head_is_stopped_for_no_progress(env):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "trap '' TERM; while :; do sleep 0.1; done")
    run_id = write_spec(mc_home, time_limit_s=3600, no_progress_s=1200)
    t0 = time.time()
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_MAX_NO_PROGRESS_S="2"))
    status = wait_phase(mc_home, run_id, "exited", timeout=30)
    assert status["reason"] == "no_progress", status
    assert time.time() - t0 < 20
    assert status["last_progress_at"]
    with pytest.raises(ProcessLookupError):
        os.kill(status["pid"], 0)


def test_busy_head_is_stopped_by_the_hard_limit(env):
    mc_home = env["mc_home"]
    # Rewrites a file in the worktree all the time — never "no progress".
    harness = fake_harness(env["tmp"], "trap '' TERM; while :; do date > busy.txt; sleep 0.2; done")
    run_id = write_spec(mc_home, time_limit_s=3600, no_progress_s=1200)
    t0 = time.time()
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_MAX_TIME_S="3"))
    status = wait_phase(mc_home, run_id, "exited", timeout=30)
    assert status["reason"] == "hard_limit", status
    assert time.time() - t0 < 20
    with pytest.raises(ProcessLookupError):
        os.kill(status["pid"], 0)


def _long_quiet_worker(body: str, rounds: int = 12) -> str:
    """Silent on stdout for ~6 s, doing ``body`` every 0.5 s, then exit 0."""
    return f"i=0; while [ $i -lt {rounds} ]; do {body}; sleep 0.5; i=$((i+1)); done; exit 0"


def test_file_changes_deep_in_the_worktree_count_as_progress(env):
    mc_home = env["mc_home"]
    # Overwriting an existing nested file changes only that file's mtime —
    # the watchdog has to walk the tree, a top-level check would miss it.
    harness = fake_harness(env["tmp"], "mkdir -p src/deep/er; " + _long_quiet_worker("date > src/deep/er/work.py"))
    run_id = write_spec(mc_home, no_progress_s=1200)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_MAX_NO_PROGRESS_S="2"))
    status = wait_phase(mc_home, run_id, "exited", timeout=40)
    assert status["reason"] is None, status
    assert status["exit_code"] == 0


def test_step_file_counts_as_progress(env):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], _long_quiet_worker('echo "step $i" > "$MC_HEAD_RUN_DIR/step.txt"'))
    run_id = write_spec(mc_home, no_progress_s=1200)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_MAX_NO_PROGRESS_S="2"))
    status = wait_phase(mc_home, run_id, "exited", timeout=40)
    assert status["reason"] is None, status


def test_output_counts_as_progress(env):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], _long_quiet_worker('echo "working $i"'))
    run_id = write_spec(mc_home, no_progress_s=1200)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_MAX_NO_PROGRESS_S="2"))
    status = wait_phase(mc_home, run_id, "exited", timeout=40)
    assert status["reason"] is None, status


def test_work_log_counts_as_progress(env):
    """A single long command (test suite, build, download) prints nothing
    until it ends under ``claude -p`` / ``omp -p``, and the head cannot touch
    step.txt while its tool call runs. The procedure tells it to pipe such
    commands through ``tee -a <run>/work.log`` — that file is a sign too."""
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], _long_quiet_worker('echo "test $i" >> "$MC_HEAD_RUN_DIR/work.log"'))
    run_id = write_spec(mc_home, no_progress_s=1200)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_MAX_NO_PROGRESS_S="2"))
    status = wait_phase(mc_home, run_id, "exited", timeout=40)
    assert status["reason"] is None, status
    assert status["last_output_at"] and status["last_output_at"] > status["started_at"]


def test_a_work_log_symlink_is_not_a_sign(env, tmp_path):
    """work.log is head-writable like step.txt: a symlink to a file that
    changes elsewhere must not keep the head alive."""
    mc_home = env["mc_home"]
    outside = tmp_path / "outside-tick"
    ticker = subprocess.Popen(["sh", "-c", f"while :; do date > {outside}; sleep 0.2; done"])
    try:
        harness = fake_harness(
            env["tmp"], f'ln -sf {outside} "$MC_HEAD_RUN_DIR/work.log"; trap \'\' TERM; while :; do sleep 0.1; done'
        )
        run_id = write_spec(mc_home, no_progress_s=1200)
        run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_MAX_NO_PROGRESS_S="2"))
        status = wait_phase(mc_home, run_id, "exited", timeout=30)
        assert status["reason"] == "no_progress", status
    finally:
        ticker.kill()
        ticker.wait()


def test_a_future_mtime_in_the_worktree_is_not_progress(env):
    """One file stamped in the future (unpacked archive, clock skew) must not
    count as progress forever — the head is still stopped when quiet."""
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "touch -t 209901010000 future.txt; trap '' TERM; while :; do sleep 0.1; done")
    run_id = write_spec(mc_home, time_limit_s=3600, no_progress_s=1200)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_MAX_NO_PROGRESS_S="2"))
    status = wait_phase(mc_home, run_id, "exited", timeout=30)
    assert status["reason"] == "no_progress", status


def test_changes_deep_in_a_skipped_tool_folder_do_not_count(env):
    """node_modules & co. are not walked: rewriting a file deep inside is no
    progress (only the folder's own mtime counts)."""
    mc_home = env["mc_home"]
    harness = fake_harness(
        env["tmp"],
        "mkdir -p node_modules/pkg/lib; date > node_modules/pkg/lib/x.js; "
        "trap '' TERM; while :; do date > node_modules/pkg/lib/x.js; sleep 0.2; done",
    )
    run_id = write_spec(mc_home, time_limit_s=3600, no_progress_s=1200)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_MAX_NO_PROGRESS_S="2"))
    status = wait_phase(mc_home, run_id, "exited", timeout=30)
    assert status["reason"] == "no_progress", status


def test_new_entries_directly_in_a_skipped_folder_count(env):
    """Installing into node_modules adds entries there — its own mtime moves."""
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "mkdir -p node_modules; " + _long_quiet_worker("touch node_modules/pkg$i"))
    run_id = write_spec(mc_home, no_progress_s=1200)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_MAX_NO_PROGRESS_S="2"))
    status = wait_phase(mc_home, run_id, "exited", timeout=40)
    assert status["reason"] is None, status


def test_scan_cap_bounds_the_walk_without_crashing(env):
    """With the scan cap at 1 entry the walk stops early: a change deep in
    the tree is not seen, the watchdog still works (no crash, no_progress)."""
    mc_home = env["mc_home"]
    harness = fake_harness(
        env["tmp"],
        "mkdir -p src/deep/er; date > src/deep/er/work.py; "
        "trap '' TERM; while :; do date > src/deep/er/work.py; sleep 0.2; done",
    )
    run_id = write_spec(mc_home, time_limit_s=3600, no_progress_s=1200)
    run_head(
        mc_home, "start", run_id,
        env_extra=_extra(harness, MC_HEAD_MAX_NO_PROGRESS_S="2", MC_HEAD_PROGRESS_SCAN_MAX="1"),
    )
    status = wait_phase(mc_home, run_id, "exited", timeout=30)
    assert status["reason"] == "no_progress", status


def test_omp_max_time_leaves_the_hard_limit_to_the_wrapper(env):
    """omp gets --max-time a little above the wrapper's effective hard limit
    (operator cap included), so the wrapper stops first with "hard_limit"."""
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "echo ok")
    run_id = write_spec(mc_home, time_limit_s=3600)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_MAX_TIME_S="120"))
    wait_phase(mc_home, run_id, "exited")
    argv = (mc_home / "heads" / run_id / "argv.txt").read_text().splitlines()
    assert argv[argv.index("--max-time") + 1] == "150"


def test_a_symlink_out_of_the_worktree_is_not_followed(env, tmp_path):
    """A head must not keep itself alive through a file outside its worktree
    (the wrapper runs unsandboxed: it never walks through a symlink)."""
    mc_home = env["mc_home"]
    outside = tmp_path / "outside"
    outside.mkdir()
    ticker = subprocess.Popen(["sh", "-c", f"while :; do date > {outside}/tick; sleep 0.2; done"])
    try:
        harness = fake_harness(env["tmp"], f"ln -s {outside} escape; trap '' TERM; while :; do sleep 0.1; done")
        run_id = write_spec(mc_home, no_progress_s=1200)
        run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_MAX_NO_PROGRESS_S="2"))
        status = wait_phase(mc_home, run_id, "exited", timeout=30)
        assert status["reason"] == "no_progress", status
    finally:
        ticker.kill()
        ticker.wait()


def test_spec_without_no_progress_field_still_runs(env):
    """Specs written by an older backend have no ``no_progress_s``: the
    wrapper falls back to its default instead of refusing the run."""
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "echo ok")
    run_id = write_spec(mc_home)
    assert "no_progress_s" not in json.loads((mc_home / "heads" / run_id / "spec.json").read_text())
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["reason"] is None, status


def test_restart_waits_for_old_run_never_two_heads_in_one_worktree(env, tmp_path):
    mc_home = env["mc_home"]
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    body = (
        f"if [ -e {marker_dir}/active ]; then echo OVERLAP >> {marker_dir}/overlap; fi\n"
        f"touch {marker_dir}/active\n"
        f"trap 'sleep 1; rm -f {marker_dir}/active; exit 0' TERM\n"
        "while :; do sleep 0.1; done"
    )
    harness = fake_harness(env["tmp"], body)
    old = write_spec(mc_home)
    assert run_head(mc_home, "start", old, env_extra=_extra(harness)).returncode == 0
    wait_phase(mc_home, old, "running")
    time.sleep(0.5)
    old_spec = json.loads((mc_home / "heads" / old / "spec.json").read_text())
    new = write_spec(mc_home, mode="continue", restarted_from=old, branch=old_spec["branch"], harness="claude")
    res = run_head(mc_home, "restart", old, new, env_extra=_extra(harness))
    assert res.returncode == 0, res.stdout + res.stderr
    wait_phase(mc_home, new, "running")
    time.sleep(0.5)
    assert not (marker_dir / "overlap").exists()
    # continue mode: the SAME worktree (moved), same branch
    wt_new = mc_home / "heads" / new / "wt"
    assert wt_new.is_dir() and not (mc_home / "heads" / old / "wt").exists()
    assert read_status(mc_home, old)["reason"] == "stopped"
    run_head(mc_home, "stop", new, env_extra=_extra(harness))


# ── A5: result detection ────────────────────────────────────────────────


def test_question_with_exit_zero_is_detected(env):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], 'echo "Remove the endpoint?" > "$MC_HEAD_RUN_DIR/question.md"; exit 0')
    run_id = write_spec(mc_home)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["exit_code"] == 0
    assert status["question"] is True
    assert status["pr_url"] is None


def test_run_record_found_by_head_run_and_time_window(env, tmp_path):
    mc_home = env["mc_home"]
    jobs = mc_home / "vault" / "jobs"
    run_id = str(uuid.uuid4())
    other = str(uuid.uuid4())
    record = (
        "---\nid: job-x\ntype: run-record\nagent: head\ndate: 2026-09-23\n"
        f"head_run: {run_id}\n---\n\nStatus: passed\n"
    )
    body = (
        f"mkdir -p {jobs}/2026-09-23-old {jobs}/2026-09-23-other {jobs}/2026-09-23-mine\n"
        # an OLD record with the right id (outside the window) …
        f"printf '%s' '{record}' > {jobs}/2026-09-23-old/run-record.md\n"
        f"touch -t 202001010000 {jobs}/2026-09-23-old/run-record.md\n"
        # … a record of another run …
        f"printf '%s' '{record.replace(run_id, other)}' > {jobs}/2026-09-23-other/run-record.md\n"
        # … and ours.
        f"printf '%s' '{record}' > {jobs}/2026-09-23-mine/run-record.md\n"
    )
    harness = fake_harness(env["tmp"], body)
    write_spec(mc_home, run_id=run_id)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["run_record_path"] == str(jobs / "2026-09-23-mine" / "run-record.md")


def test_run_record_in_run_folder_is_filed_into_the_vault(env):
    """The head writes <run>/run-record.md (the only place it may write it);
    the wrapper files a copy into vault jobs/<job_folder>/ — live finding
    2026-09-23: the model wrote the record into its run folder."""
    mc_home = env["mc_home"]
    run_id = str(uuid.uuid4())
    record = (
        "---\nid: job-x\ntype: run-record\nagent: head\ndate: 2026-09-23\n"
        f"head_run: {run_id}\n---\n\nStatus: passed\n"
    )
    harness = fake_harness(env["tmp"], f"printf '%s' '{record}' > \"$MC_HEAD_RUN_DIR/run-record.md\"")
    write_spec(mc_home, run_id=run_id, job_folder="2026-09-23-filed-ab12")
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    filed = mc_home / "vault" / "jobs" / "2026-09-23-filed-ab12" / "run-record.md"
    assert status["run_record_path"] == str(filed)
    assert f"head_run: {run_id}" in filed.read_text()


def test_foreign_run_record_in_run_folder_is_not_filed(env):
    mc_home = env["mc_home"]
    run_id = str(uuid.uuid4())
    record = "---\nid: x\ntype: run-record\nagent: head\ndate: d\nhead_run: someone-else\n---\n"
    harness = fake_harness(env["tmp"], f"printf '%s' '{record}' > \"$MC_HEAD_RUN_DIR/run-record.md\"")
    write_spec(mc_home, run_id=run_id)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["run_record_path"] is None
    assert not (mc_home / "vault" / "jobs").exists() or not any((mc_home / "vault" / "jobs").iterdir())


def test_last_output_follows_step_file_while_log_is_silent(env):
    """omp -p prints only at the end; step.txt is the sign of progress."""
    mc_home = env["mc_home"]
    harness = fake_harness(
        env["tmp"],
        "sleep 1.5; echo 'step 2/7' > \"$MC_HEAD_RUN_DIR/step.txt\"; sleep 2.5",
    )
    run_id = write_spec(mc_home)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    status = wait_phase(mc_home, run_id, "exited")
    started = status["started_at"]
    assert status["last_output_at"] and status["last_output_at"] > started


def test_second_head_after_a_pushed_head_branch_still_fetches(env):
    """Live finding 2026-09-23: with a 'head/' prefix, the first pushed branch
    creates refs/remotes/origin/head/… which collides with origin/HEAD on a
    case-insensitive disk (macOS) — every later fetch failed."""
    mc_home = env["mc_home"]
    harness = fake_harness(
        env["tmp"],
        'git -c user.name=t -c user.email=t@example.invalid commit -q --allow-empty -m x'
        ' && git push -q -u origin "$(git branch --show-current)"',
    )
    first = write_spec(mc_home)
    run_head(mc_home, "start", first, env_extra=_extra(harness))
    assert wait_phase(mc_home, first, "exited")["exit_code"] == 0
    second = write_spec(mc_home)
    run_head(mc_home, "start", second, env_extra=_extra(harness))
    status = wait_phase(mc_home, second, "exited")
    assert status["reason"] is None, status


def test_pr_url_is_found_by_the_wrapper(env, tmp_path):
    mc_home = env["mc_home"]
    (mc_home / "heads" / "gh-token").write_text("github_pat_fake\n")
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\ncase \"$*\" in *'pr list'*) echo '[{\"url\":\"https://github.com/owner/demo/pull/7\"}]';; esac\n"
    )
    fake_gh.chmod(0o755)
    harness = fake_harness(env["tmp"], "true")
    run_id = write_spec(mc_home)
    run_head(mc_home, "start", run_id, env_extra=_extra(harness, MC_HEAD_GH=str(fake_gh)))
    status = wait_phase(mc_home, run_id, "exited")
    assert status["pr_url"] == "https://github.com/owner/demo/pull/7"


# ── A7: spool watcher ───────────────────────────────────────────────────


def _spool(mc_home: Path, name: str, payload) -> Path:
    spool = mc_home / "heads" / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    p = spool / name
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return p


def test_watch_starts_a_spooled_run(env):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "true")
    run_id = write_spec(mc_home)
    _spool(mc_home, f"{run_id}.start.json", {"action": "start", "run_id": run_id, "repo_full_name": "evil/x"})
    assert run_head(mc_home, "watch", env_extra=_extra(harness)).returncode == 0
    wait_phase(mc_home, run_id, "exited")
    assert (mc_home / "heads" / "spool" / "done" / f"{run_id}.start.json").exists()


def test_watch_rejects_unknown_action_and_malformed_files(env):
    mc_home = env["mc_home"]
    run_id = write_spec(mc_home)
    _spool(mc_home, "a.json", {"action": "exec", "run_id": run_id, "cmd": "rm -rf ~"})
    _spool(mc_home, "b.json", "{not json")
    _spool(mc_home, "c.json", {"action": "start", "run_id": "../../x"})
    _spool(mc_home, "d.json", {"action": "restart", "run_id": run_id})
    assert run_head(mc_home, "watch").returncode == 0
    rejected = {p.name for p in (mc_home / "heads" / "spool" / "rejected").iterdir()}
    assert rejected == {"a.json", "b.json", "c.json", "d.json"}
    assert read_status(mc_home, run_id) is None
    log = (mc_home / "heads" / "spool" / "watch.log").read_text()
    assert "unknown action" in log and "malformed" in log


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system python")
def test_script_compiles_on_system_python():
    """launchd runs mc-head with the macOS system python (3.9)."""
    res = subprocess.run(
        ["/usr/bin/python3", "-c", f"import ast,sys; ast.parse(open({str(MC_HEAD)!r}).read())"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert run_head(Path("/nonexistent-mc"), "validate", "x").returncode == 2


# ── review fixes: stop never rewrites a finished run, never starts late ──


def test_stop_on_a_finished_run_keeps_its_result(env):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "true")
    run_id = write_spec(mc_home)
    assert run_head(mc_home, "start", run_id, env_extra=_extra(harness)).returncode == 0
    before = wait_phase(mc_home, run_id, "exited")
    assert before["reason"] is None
    run_head(mc_home, "stop", run_id, env_extra=_extra(harness))
    assert not (mc_home / "heads" / run_id / ".wrapper" / "stop-requested").exists()
    assert read_status(mc_home, run_id)["reason"] is None


def test_supervisor_does_not_start_the_harness_after_an_early_stop(env):
    """Stop arrives before the detached supervisor wrote its pid: cmd_stop
    answers "stopped" — the supervisor must then not start the harness."""
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "true")
    run_id = write_spec(mc_home)
    wdir = mc_home / "heads" / run_id / ".wrapper"
    wdir.mkdir(parents=True)
    (wdir / "stop-requested").write_text("now")
    run_head(mc_home, "_supervise", run_id, env_extra=_extra(harness))
    st = read_status(mc_home, run_id)
    assert st["phase"] == "exited" and st["reason"] == "stopped"
    assert not (mc_home / "heads" / run_id / "argv.txt").exists()


def test_wrapper_never_writes_or_files_through_a_planted_symlink(env):
    """head.log and run-record.md sit in the run folder, where the head can
    create files. A symlink there must not make the wrapper write elsewhere
    or file a foreign file into the vault."""
    mc_home = env["mc_home"]
    victim = env["tmp"] / "victim-profile"
    victim.write_text("original\n")
    harness = fake_harness(env["tmp"], "echo from-head")
    run_id = write_spec(mc_home)
    run = mc_home / "heads" / run_id
    os.symlink(victim, run / "head.log")
    run_head(mc_home, "start", run_id, env_extra=_extra(harness))
    st = wait_phase(mc_home, run_id, "exited")
    assert victim.read_text() == "original\n"
    assert st["reason"] == "wrapper_error"
    # run record: a symlink to a valid-looking record outside is not filed
    run_id2 = write_spec(mc_home)
    outside = env["tmp"] / "outside-record.md"
    outside.write_text(f"---\nhead_run: {run_id2}\n---\nstatus: passed\n")
    os.symlink(outside, mc_home / "heads" / run_id2 / "run-record.md")
    run_head(mc_home, "start", run_id2, env_extra=_extra(fake_harness(env["tmp"], "touch ../run-record.md")))
    st2 = wait_phase(mc_home, run_id2, "exited")
    assert st2["run_record_path"] is None
