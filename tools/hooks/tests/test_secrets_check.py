"""secrets-check.sh must BLOCK (exit 2) a commit / push that would publish a
credential, and must never echo the value. Fake secrets are assembled at
runtime so this file itself never contains one (gitleaks scans the tree)."""
from conftest import commit, git, run_hook

FAKE_GH = "gh" + "p_" + "A1b2C3d4" * 5          # 40 chars after the prefix
FAKE_ANT = "sk-" + "ant-" + "x9Y8z7W6" * 4
FAKE_KEY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"


def stage(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(repo, "add", "-f", rel)  # -f: a global gitignore may hide .env*


def test_staged_token_blocks_commit_and_value_is_not_printed(repo):
    stage(repo, "app/config.py", f'TOKEN = "{FAKE_GH}"\n')
    r = run_hook("secrets-check.sh", f"git -C {repo} commit -m wip")
    assert r.returncode == 2, r.stderr
    assert "app/config.py: GitHub token" in r.stderr
    assert FAKE_GH not in r.stderr + r.stdout


def test_private_key_and_anthropic_key_block(repo):
    stage(repo, "k.txt", FAKE_KEY + "\n")
    stage(repo, "a.py", f"KEY='{FAKE_ANT}'\n")
    r = run_hook("secrets-check.sh", f"cd {repo} && git commit -m wip")
    assert r.returncode == 2
    assert "private key block" in r.stderr and "Anthropic key" in r.stderr


def test_clean_commit_passes(repo):
    stage(repo, "app/config.py", 'TOKEN = os.environ["GITHUB_TOKEN"]\n')
    assert run_hook("secrets-check.sh", f"git -C {repo} commit -m ok").returncode == 0


def test_staged_env_file_blocks_but_example_passes(repo):
    stage(repo, ".env.example", "A=\n")
    assert run_hook("secrets-check.sh", f"git -C {repo} commit -m ok").returncode == 0
    stage(repo, "deploy/.env", "A=1\n")
    r = run_hook("secrets-check.sh", f"git -C {repo} commit -m bad")
    assert r.returncode == 2
    assert "deploy/.env: secret-bearing file" in r.stderr


def test_push_scans_branch_diff_against_origin_main(repo):
    commit(repo, "notes.md", f"oops {FAKE_GH}\n")
    r = run_hook("secrets-check.sh", f"git -C {repo} push")
    assert r.returncode == 2
    assert "notes.md: GitHub token" in r.stderr


def test_removed_secret_line_does_not_block(repo):
    commit(repo, "notes.md", f"oops {FAKE_GH}\n")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    commit(repo, "notes.md", "clean now\n")
    assert run_hook("secrets-check.sh", f"git -C {repo} push").returncode == 0


def test_other_commands_untouched(repo):
    stage(repo, "app/config.py", f'TOKEN = "{FAKE_GH}"\n')
    assert run_hook("secrets-check.sh", f"git -C {repo} status").returncode == 0
    assert run_hook("secrets-check.sh", "cat app/config.py", cwd=repo).returncode == 0


# --- Kritik 19.10.: bypasses and false alarms -------------------------------


def write(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_add_all_and_commit_in_one_command_blocks_untracked_token(repo):
    # The hook runs BEFORE the command, so nothing is staged yet.
    write(repo, "app/new.py", f'TOKEN = "{FAKE_GH}"\n')
    r = run_hook("secrets-check.sh", f"cd {repo} && git add -A && git commit -m wip")
    assert r.returncode == 2, r.stderr
    assert "app/new.py: GitHub token" in r.stderr
    assert FAKE_GH not in r.stderr


def test_commit_am_blocks_unstaged_token_in_tracked_file(repo):
    write(repo, "README.md", f"oops {FAKE_GH}\n")  # tracked, modified, not staged
    for cmd in (f"git -C {repo} commit -am wip", f"git -C {repo} commit --all -m wip",
                f"git -C {repo} commit -a -m wip", f"git -C {repo} commit -m wip -a",
                f"git -C {repo} commit -m '-a looks like a flag' --all"):
        r = run_hook("secrets-check.sh", cmd)
        assert r.returncode == 2, (cmd, r.stderr)
        assert "README.md: GitHub token" in r.stderr


def test_plain_commit_ignores_unstaged_changes(repo):
    # Without -a / git add, unstaged lines are not part of the commit.
    write(repo, "README.md", f"oops {FAKE_GH}\n")
    assert run_hook("secrets-check.sh", f"git -C {repo} commit -m ok").returncode == 0


def test_git_global_options_before_the_verb_do_not_bypass(repo):
    stage(repo, "app/config.py", f'TOKEN = "{FAKE_GH}"\n')
    for cmd in (f"git -c core.x=1 -C {repo} commit -m wip",
                f"git --no-pager -C {repo} commit -m wip",
                f"cd {repo} && git -c user.name=x commit -m wip"):
        r = run_hook("secrets-check.sh", cmd)
        assert r.returncode == 2, (cmd, r.stderr)
    commit(repo, "notes.md", f"oops {FAKE_GH}\n")
    r = run_hook("secrets-check.sh", f"cd {repo} && git --no-pager push")
    assert r.returncode == 2, r.stderr


def test_obvious_test_values_pass(repo):
    stage(repo, "tests/test_slack.py", 'T = "xoxb-TEST-0000-not-a-real-token"\nU = "xoxb-TEST-dummy-value"\n')
    r = run_hook("secrets-check.sh", f"git -C {repo} commit -m ok")
    assert r.returncode == 0, r.stderr


def test_allow_marker_needs_a_reason(repo):
    stage(repo, "tests/test_x.py", f'T = "{FAKE_GH}"  # secrets-check: allow\n')
    r = run_hook("secrets-check.sh", f"git -C {repo} commit -m wip")
    assert r.returncode == 2
    assert "reason" in r.stderr
    stage(repo, "tests/test_x.py", f'T = "{FAKE_GH}"  # secrets-check: allow - redaction fixture\n')
    r = run_hook("secrets-check.sh", f"git -C {repo} commit -m ok")
    assert r.returncode == 0, r.stderr


# --- Runde 2 (tore-dicht): switching checks off, other spellings ------------

import pytest  # noqa: E402

from conftest import worktree  # noqa: E402


@pytest.mark.parametrize("form", [
    "git -C {r} push --no-verify",
    "git -C {r} commit --no-verify -m x",
    "git -C {r} commit -nm x",
    "git -c core.hooksPath=/dev/null -C {r} push",
    "git -C {r} config core.hooksPath /dev/null",
    "git -C {r} config --unset core.hooksPath",
    "MC_LEAK_SCAN_SKIP=1 git -C {r} push",
    "GIT_CONFIG_PARAMETERS=\"'core.hooksPath=/x'\" git -C {r} push",
    "bash -c 'git -C {r} push --no-verify'",
])
def test_switching_a_check_off_blocks(repo, form):
    # nothing secret anywhere: the block is about the bypass itself
    r = run_hook("secrets-check.sh", form.format(r=repo))
    assert r.returncode == 2, (form, r.stderr)
    assert "dodge" in r.stderr


@pytest.mark.parametrize("form", [
    "git -C {r} commit -m '-n is text'", "git -C {r} push -n", "git -C {r} config --get core.hooksPath",
])
def test_lookalikes_pass(repo, form):
    assert run_hook("secrets-check.sh", form.format(r=repo)).returncode == 0, form


@pytest.mark.parametrize("form", [
    '"git" -C {r} commit -m x',
    "bash -c 'cd {r} && git commit -m x'",
    "(cd {r} && git commit -m x)",
    "eval \"git -C {r} commit -m x\"",
    "timeout 30 git -C {r} commit -m x",
])
def test_other_spellings_of_commit_are_scanned(repo, form):
    stage(repo, "app/config.py", f'TOKEN = "{FAKE_GH}"\n')
    r = run_hook("secrets-check.sh", form.format(r=repo))
    assert r.returncode == 2, (form, r.stderr)
    assert "GitHub token" in r.stderr


def test_push_of_another_branch_scans_that_branch(repo, tmp_path):
    wt = worktree(repo, "fix/leak", tmp_path / "wt")
    commit(wt, "notes.md", f"oops {FAKE_GH}\n")
    # the checkout the command runs in is clean
    r = run_hook("secrets-check.sh", f"git -C {repo} push origin fix/leak")
    assert r.returncode == 2, r.stderr
    assert "notes.md: GitHub token" in r.stderr


def test_commit_with_unknown_checkout_blocks(repo):
    r = run_hook("secrets-check.sh", 'git -C "$NOT_SET_X" commit -m x', cwd=repo)
    assert r.returncode == 2
    assert "variable" in r.stderr
