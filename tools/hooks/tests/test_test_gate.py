"""test-gate.sh must BLOCK (exit 2) a push / PR creation while a test file
changed on the branch is red - and must stay out of the way otherwise.
These are the hook's sabotage probes: a broken test has to lead to a block."""
from conftest import commit, run_hook

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
    assert "1 test suite(s) touched by this branch green" in r.stderr


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
    from conftest import git
    commit(repo, "backend/tests/test_old.py", RED)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "rm", "-q", "backend/tests/test_old.py")
    git(repo, "commit", "-q", "-m", "drop red test")
    r = run_hook("test-gate.sh", f"git -C {repo} push")
    assert r.returncode == 0
    assert "deleted" in r.stderr and "backend/tests/test_old.py" in r.stderr


def test_exhausted_time_budget_blocks_instead_of_being_killed(repo):
    slow = "import time\n\ndef test_slow():\n    time.sleep(4)\n"
    commit(repo, "backend/tests/test_slow.py", slow)
    commit(repo, "tools/x/tests/test_tool.py", GREEN)
    r = run_hook("test-gate.sh", f"git -C {repo} push", env_extra={"MC_TEST_GATE_BUDGET": "2"})
    assert r.returncode == 2, r.stderr
    assert "budget" in r.stderr
