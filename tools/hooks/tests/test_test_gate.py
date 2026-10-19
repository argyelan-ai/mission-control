"""test-gate.sh must BLOCK (exit 2) a push / PR creation while a test file
changed on the branch is red - and must stay out of the way otherwise.
These are the hook's sabotage probes: a broken test has to lead to a block."""
import pytest

from conftest import commit, git, run_hook, worktree

GREEN = "def test_ok():\n    assert 1 + 1 == 2\n"
RED = "def test_broken():\n    assert 1 + 1 == 3\n"


def test_red_backend_test_blocks_push(repo):
    commit(repo, "backend/tests/test_new.py", RED)
    r = run_hook("test-gate.sh", f"git -C {repo} push -u origin fix/branch")
    assert r.returncode == 2, r.stderr
    assert "BLOCKED" in r.stderr
    assert "test_broken" in r.stderr


def test_green_backend_test_lets_push_through(repo):
    commit(repo, "backend/tests/test_new.py", GREEN)
    r = run_hook("test-gate.sh", f"git -C {repo} push")
    assert r.returncode == 0, r.stderr
    assert "1 suite(s) affected by this branch green" in r.stderr


def test_fixing_the_test_unblocks(repo):
    commit(repo, "backend/tests/test_new.py", RED)
    assert run_hook("test-gate.sh", f"git -C {repo} push").returncode == 2
    commit(repo, "backend/tests/test_new.py", GREEN, "fix")
    assert run_hook("test-gate.sh", f"git -C {repo} push").returncode == 0


def test_gh_pr_create_is_gated_too_via_cd(repo):
    commit(repo, "tools/x/tests/test_tool.py", RED)
    r = run_hook("test-gate.sh", f"cd {repo} && gh pr create --fill")
    assert r.returncode == 2, r.stderr


def test_hook_cwd_is_used_when_command_names_no_dir(repo):
    commit(repo, "backend/tests/test_new.py", RED)
    r = run_hook("test-gate.sh", "git push", cwd=repo)
    assert r.returncode == 2, r.stderr


def test_red_shell_test_blocks(repo):
    commit(repo, "scripts/tests/test_thing.sh", "#!/bin/bash\necho nope\nexit 1\n")
    r = run_hook("test-gate.sh", f"git -C {repo} push")
    assert r.returncode == 2, r.stderr


def test_other_commands_are_not_gated(repo):
    commit(repo, "backend/tests/test_new.py", RED)
    for cmd in (f"git -C {repo} status", f"git -C {repo} commit -m x", "ls", "echo pushover"):
        r = run_hook("test-gate.sh", cmd)
        assert r.returncode == 0, (cmd, r.stderr)
        assert "BLOCKED" not in r.stderr


def test_unchanged_red_test_on_main_is_not_this_branchs_fault(repo):
    # A red test that already sits on origin/main is not touched by the branch.
    commit(repo, "backend/tests/test_old.py", RED)
    from conftest import git
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    commit(repo, "README.md", "doc change\n")
    assert run_hook("test-gate.sh", f"git -C {repo} push").returncode == 0


def test_no_origin_main_skips_with_a_warning(repo):
    from conftest import git
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    commit(repo, "backend/tests/test_new.py", RED)
    r = run_hook("test-gate.sh", f"git -C {repo} push")
    assert r.returncode == 0
    assert "gate skipped" in r.stderr


def test_garbage_stdin_never_blocks():
    import subprocess
    from conftest import HOOKS
    r = subprocess.run(["bash", str(HOOKS / "test-gate.sh")], input="not json", capture_output=True, text=True)
    assert r.returncode == 0


# --- Kritik 19.10.: bypasses, blind spots, time budget ----------------------


def test_git_global_options_before_push_do_not_bypass(repo):
    commit(repo, "backend/tests/test_new.py", RED)
    for cmd in (f"git -c core.x=1 -C {repo} push", f"git --no-pager -C {repo} push",
                f"cd {repo} && git -c http.extraHeader=x push origin HEAD"):
        r = run_hook("test-gate.sh", cmd)
        assert r.returncode == 2, (cmd, r.stderr)


def test_changed_source_runs_its_existing_test(repo):
    # The branch only touches app code; the test that covers it already
    # exists on origin/main. Breaking existing behaviour must still block.
    from conftest import git
    commit(repo, "backend/app/services/pricing.py", "def price():\n    return 1\n")
    commit(repo, "backend/tests/test_pricing.py",
           "from app.services.pricing import price\n\ndef test_price():\n    assert price() == 1\n")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    commit(repo, "backend/app/services/pricing.py", "def price():\n    return 2\n", "break it")
    r = run_hook("test-gate.sh", f"git -C {repo} push")
    assert r.returncode == 2, r.stderr
    assert "test_pricing.py" in r.stderr


def test_changed_source_without_matching_test_runs_nothing(repo):
    from conftest import git
    commit(repo, "backend/app/x.py", "A = 1\n")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    commit(repo, "backend/app/x.py", "A = 2\n")
    r = run_hook("test-gate.sh", f"git -C {repo} push")
    assert r.returncode == 0  # no matching test -> nothing to run, no block


def test_deleted_test_is_reported(repo):
    # Round 2: a silently deleted test now blocks (see test_deleted_test_needs_a_reason).
    commit(repo, "backend/tests/test_old.py", RED)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "rm", "-q", "backend/tests/test_old.py")
    git(repo, "commit", "-q", "-m", "drop red test")
    r = run_hook("test-gate.sh", f"git -C {repo} push")
    assert r.returncode == 2
    assert "deletes test file" in r.stderr and "backend/tests/test_old.py" in r.stderr


def test_exhausted_time_budget_blocks_instead_of_being_killed(repo):
    slow = "import time\n\ndef test_slow():\n    time.sleep(4)\n"
    commit(repo, "backend/tests/test_slow.py", slow)
    commit(repo, "tools/x/tests/test_tool.py", GREEN)
    r = run_hook("test-gate.sh", f"git -C {repo} push", env_extra={"MC_TEST_GATE_BUDGET": "2"})
    assert r.returncode == 2, r.stderr
    assert "budget" in r.stderr


# --- Runde 2 (tore-dicht): side entrances and tests that really are affected --


BYPASS_PUSHES = [
    '"git" -C {r} push',
    "command git -C {r} push",
    "timeout 60 git -C {r} push",
    "(cd {r} && git push)",
    "cd {r}; git push",
    "pushd {r} >/dev/null && git push 2>&1 | tail -1",
    "bash -c 'cd {r} && git push'",
    'eval "git -C {r} push"',
    "R={r}; G=git; $G -C \"$R\" push",
    "git -c alias.ship=push -C {r} ship",
    "bash <<'EOF'\ncd {r}\ngit push\nEOF",
    "cd {r} && gh pr new --fill",
    "cd {r} && gh api repos/o/r/pulls -f head=fix/branch -f base=main",
    "cd {r} && python3 -c \"import subprocess; subprocess.run(['git', 'push'])\"",
]


@pytest.mark.parametrize("form", BYPASS_PUSHES)
def test_side_entrances_are_gated(repo, form):
    commit(repo, "backend/tests/test_new.py", RED)
    r = run_hook("test-gate.sh", form.format(r=repo))
    assert r.returncode == 2, (form, r.stderr)
    assert "test_broken" in r.stderr


def test_script_file_with_push_is_gated(repo, tmp_path):
    commit(repo, "backend/tests/test_new.py", RED)
    (tmp_path / "ship.sh").write_text(f"#!/bin/bash\ngit -C {repo} push origin HEAD\n")
    r = run_hook("test-gate.sh", f"bash {tmp_path}/ship.sh")
    assert r.returncode == 2, r.stderr


def test_configured_git_alias_is_resolved(repo):
    commit(repo, "backend/tests/test_new.py", RED)
    git(repo, "config", "alias.up", "push")
    r = run_hook("test-gate.sh", f"git -C {repo} up")
    assert r.returncode == 2, r.stderr


def test_unknown_variable_target_blocks(repo):
    r = run_hook("test-gate.sh", 'git -C "$SOME_UNSET_DIR" push', cwd=repo)
    assert r.returncode == 2
    assert "variable" in r.stderr
    r = run_hook("test-gate.sh", "$PUSHER push", cwd=repo)
    assert r.returncode == 2


def test_push_of_another_branch_tests_that_branchs_worktree(repo, tmp_path):
    # The checkout the command runs in is clean; the pushed branch is red and
    # lives in another worktree.
    wt = worktree(repo, "fix/red", tmp_path / "wt-red")
    commit(wt, "backend/tests/test_new.py", RED)
    r = run_hook("test-gate.sh", f"git -C {repo} push origin fix/red")
    assert r.returncode == 2, r.stderr
    assert "test_broken" in r.stderr
    r = run_hook("test-gate.sh", f"git -C {repo} push origin fix/red:fix/red")
    assert r.returncode == 2, r.stderr


def test_push_of_branch_checked_out_nowhere_blocks(repo):
    git(repo, "branch", "fix/lonely")  # stays at origin/main
    commit(repo, "README.md", "moved on\n")  # this checkout is somewhere else now
    r = run_hook("test-gate.sh", f"git -C {repo} push origin fix/lonely")
    assert r.returncode == 2
    assert "not checked out" in r.stderr


def test_branch_deletion_and_tags_are_not_tested(repo):
    commit(repo, "backend/tests/test_new.py", RED)
    git(repo, "tag", "v1")
    for cmd in (f"git -C {repo} push origin --delete fix/old", f"git -C {repo} push origin :fix/old",
                f"git -C {repo} push origin v1", f"git -C {repo} push --tags"):
        assert run_hook("test-gate.sh", cmd).returncode == 0, cmd


def test_gh_pr_create_from_outside_any_checkout(repo, tmp_path):
    commit(repo, "backend/tests/test_new.py", RED)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cmd = "gh pr create --head fix/branch --fill"
    # without knowing the repository: refuse rather than wave it through
    r = run_hook("test-gate.sh", cmd, cwd=elsewhere)
    assert r.returncode == 2 and "no local checkout" in r.stderr, r.stderr
    # with the hook's home repository known, the branch's worktree is tested
    r = run_hook("test-gate.sh", cmd, cwd=elsewhere, env_extra={"MC_HOOK_REPO": str(repo)})
    assert r.returncode == 2 and "test_broken" in r.stderr, r.stderr


def test_gh_pr_create_head_other_than_the_checkout(repo, tmp_path):
    wt = worktree(repo, "fix/red", tmp_path / "wt-red")
    commit(wt, "backend/tests/test_new.py", RED)
    r = run_hook("test-gate.sh", f"cd {repo} && gh pr create --head fix/red --fill")
    assert r.returncode == 2 and "test_broken" in r.stderr, r.stderr


def test_uncommitted_changes_block(repo):
    commit(repo, "backend/tests/test_new.py", GREEN)
    (repo / "backend/tests/test_new.py").write_text(RED)  # not committed
    r = run_hook("test-gate.sh", f"git -C {repo} push")
    assert r.returncode == 2
    assert "not committed" in r.stderr


def test_commit_and_push_in_one_command_tests_what_gets_committed(repo):
    (repo / "backend/tests").mkdir(parents=True)
    (repo / "backend/tests/test_new.py").write_text(RED)  # new, not yet added
    r = run_hook("test-gate.sh", f"cd {repo} && git add -A && git commit -m x && git push")
    assert r.returncode == 2, r.stderr
    assert "test_broken" in r.stderr


def test_deleted_test_needs_a_reason(repo):
    commit(repo, "backend/tests/test_old.py", RED)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "rm", "-q", "backend/tests/test_old.py")
    git(repo, "commit", "-q", "-m", "drop red test")
    r = run_hook("test-gate.sh", f"git -C {repo} push")
    assert r.returncode == 2, r.stderr
    assert "backend/tests/test_old.py" in r.stderr
    git(repo, "commit", "-q", "--allow-empty", "-m",
        "explain\n\nRemoved-Test: backend/tests/test_old.py - feature removed in this PR")
    r = run_hook("test-gate.sh", f"git -C {repo} push")
    assert r.returncode == 0, r.stderr


def test_deleted_test_together_with_its_subject_is_fine(repo):
    commit(repo, "backend/app/legacy.py", "X = 1\n")
    commit(repo, "backend/tests/test_legacy.py", "def test_x():\n    assert True\n")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "rm", "-q", "backend/tests/test_legacy.py", "backend/app/legacy.py")
    git(repo, "commit", "-q", "-m", "remove legacy")
    assert run_hook("test-gate.sh", f"git -C {repo} push").returncode == 0


def test_test_that_imports_the_changed_module_runs(repo):
    # Not named after the module - found through its import.
    commit(repo, "backend/app/services/pricing.py", "def price():\n    return 1\n")
    commit(repo, "backend/tests/test_billing.py",
           "from app.services.pricing import price\n\ndef test_bill():\n    assert price() == 1\n")
    commit(repo, "backend/tests/test_unrelated.py", RED)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    commit(repo, "backend/app/services/pricing.py", "def price():\n    return 2\n", "break it")
    r = run_hook("test-gate.sh", f"git -C {repo} push")
    assert r.returncode == 2, r.stderr
    assert "test_billing.py" in r.stderr
    assert "test_unrelated" not in r.stderr  # only the affected tests, not all


def test_component_code_runs_its_component_tests(repo):
    commit(repo, "tools/x/helper.py", "def f():\n    return 1\n")
    commit(repo, "tools/x/tests/test_tool.py",
           "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\n"
           "from helper import f\n\ndef test_f():\n    assert f() == 1\n")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    commit(repo, "tools/x/helper.py", "def f():\n    return 2\n", "break it")
    r = run_hook("test-gate.sh", f"git -C {repo} push")
    assert r.returncode == 2, r.stderr
    assert "test_tool.py" in r.stderr


def test_failure_that_is_already_on_main_does_not_block(repo):
    # The affected test is red on origin/main too: not this branch's fault.
    commit(repo, "backend/app/services/pricing.py", "def price():\n    return 1\n")
    commit(repo, "backend/tests/test_pricing.py",
           "from app.services.pricing import price\n\ndef test_price():\n    assert price() == 3\n")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    commit(repo, "backend/app/services/pricing.py", "def price():\n    return 1  # same\n", "touch")
    r = run_hook("test-gate.sh", f"git -C {repo} push")
    assert r.returncode == 0, r.stderr
    assert "origin/main too" in r.stderr
    assert "test-gate-base" not in git(repo, "worktree", "list")  # throwaway worktree removed
