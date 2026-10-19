"""The command reader shared by test-gate.sh and secrets-check.sh.

Both hooks must see a push / commit / PR creation in every spelling an agent
can type - otherwise the gate is a door with a side entrance. Each case below
is one way around the first version of the hooks (review of 19.10.2026)."""
import pytest

from conftest import HOOKS

BEGIN, END = "# >>> cmdparse", "# <<< cmdparse"


def block(name):
    text = (HOOKS / name).read_text()
    assert BEGIN in text and END in text, f"{name} has no cmdparse block"
    return text[text.index(BEGIN):text.index(END) + len(END)]


@pytest.fixture(scope="module")
def analyze():
    ns = {}
    exec(compile(block("test-gate.sh"), "cmdparse", "exec"), ns)
    return ns["analyze"]


def test_both_hooks_carry_the_same_reader():
    # Each hook is fetched as ONE file (git show origin/main:<file>), so the
    # reader is embedded twice. A drift would open a gap in one of them.
    assert block("test-gate.sh") == block("secrets-check.sh")


def kinds(events):
    return [k for k, _ in events]


PUSH_FORMS = [
    "git push",
    '"git" push',
    "g''it push",
    "/usr/bin/git push",
    "\\git push",
    "command git push",
    "env A=1 git push",
    "timeout 60 git push",
    "nice -n 5 git push",
    "if true; then git push; fi",
    "echo x | xargs git push",
    "git --no-pager -c x=1 push",
    "git -c alias.p=push p",
    "echo $(git push)",
    "x=`git push`",
    'eval "git push"',
    "bash -c 'git push'",
    "sh -c \"git push origin HEAD\"",
    "zsh -c 'git push'",
    "bash <<'EOF'\ngit push\nEOF",
    "G=git; $G push",
    "git push 2>&1 | tail -1",
    "git send-pack origin HEAD",
    "python3 -c \"import subprocess; subprocess.run(['git', 'push'])\"",
    "node -e \"require('child_process').execSync('git push')\"",
]


@pytest.mark.parametrize("cmd", PUSH_FORMS)
def test_every_spelling_of_push_is_seen(analyze, cmd):
    assert "push" in kinds(analyze(cmd, "/tmp")), cmd


@pytest.mark.parametrize("cmd", [
    "gh pr create --fill", "gh pr new --fill", "gh -R o/r pr create --head x",
    "gh api repos/o/r/pulls -f head=x -f base=main",
    "gh api --method POST repos/o/r/pulls --input body.json",
    "curl -X POST https://api.github.com/repos/o/r/pulls -d @b.json",
])
def test_every_spelling_of_pr_creation_is_seen(analyze, cmd):
    assert "pr" in kinds(analyze(cmd, "/tmp")), cmd


@pytest.mark.parametrize("cmd", [
    "echo 'git push'", "grep -r 'git push' .", "git log | grep push", "git status",
    "git commit -F- <<'EOF'\nmentions git push and gh pr create\nEOF",
    "gh api repos/o/r/pulls", "gh pr list", "echo pushover", "git config --get core.hooksPath",
])
def test_text_that_only_mentions_it_is_not_a_push(analyze, cmd):
    assert not {"push", "pr", "bypass", "hidden"} & set(kinds(analyze(cmd, "/tmp"))), cmd


@pytest.mark.parametrize("cmd,where", [
    ("git -C /a/b push", "/a/b"),
    ("cd /a/b && git push", "/a/b"),
    ("cd /a/b; git push", "/a/b"),
    ("(cd /a/b && git push)", "/a/b"),
    ("pushd /a/b >/dev/null && git push", "/a/b"),
    ("bash -c 'cd /a/b && git push'", "/a/b"),
    ("W=/a/b; git -C \"$W\" push", "/a/b"),
    ("cd /a && git -C b push", "/a/b"),
    ("git -C $(pwd) push", "/tmp"),
])
def test_the_target_checkout_is_followed(analyze, cmd, where):
    ev = [e for k, e in analyze(cmd, "/tmp") if k == "push"]
    assert ev and ev[0]["dir"] == where, (cmd, ev)


@pytest.mark.parametrize("cmd", ["git -C \"$NOT_SET_ANYWHERE\" push", "cd $NOT_SET_ANYWHERE && git push"])
def test_an_unknown_target_is_marked_unknown_not_guessed(analyze, cmd):
    ev = [e for k, e in analyze(cmd, "/tmp") if k == "push"]
    assert ev and ev[0]["dir"] is None


@pytest.mark.parametrize("cmd", [
    "git push --no-verify", "git commit --no-verify -m x", "git commit -nm x", "git commit -a -n -m x",
    "git -c core.hooksPath=/dev/null push", "git -c core.hookspath= commit -m x",
    "git config core.hooksPath /dev/null", "git config --unset core.hooksPath",
    "git config --global core.hooksPath /tmp/none",
    "MC_LEAK_SCAN_SKIP=1 git push", "export MC_LEAK_SCAN_SKIP=1",
    "GIT_CONFIG_PARAMETERS=\"'core.hooksPath=/x'\" git push",
])
def test_switching_hooks_off_is_a_bypass(analyze, cmd):
    assert "bypass" in kinds(analyze(cmd, "/tmp")), cmd


@pytest.mark.parametrize("cmd", ["git commit -m '-n is not a flag here'", "git commit -m x -- -n",
                                 "git push -n", "git config core.hooksPath"])
def test_lookalikes_are_not_a_bypass(analyze, cmd):
    # git push -n is a dry run, not --no-verify; reading hooksPath is fine
    assert "bypass" not in kinds(analyze(cmd, "/tmp")), cmd


@pytest.mark.parametrize("cmd", ["$GIT push", "git $VERB origin", "${G} commit -m x"])
def test_git_behind_an_unknown_variable_is_hidden(analyze, cmd):
    assert "hidden" in kinds(analyze(cmd, "/tmp")), cmd


def test_script_file_is_read(analyze, tmp_path):
    (tmp_path / "ship.sh").write_text("#!/bin/bash\nset -e\ngit -C /a/b push origin HEAD\n")
    for cmd in ("bash ship.sh", "sh ./ship.sh", "source ship.sh", ". ship.sh", "./ship.sh"):
        ev = [e for k, e in analyze(cmd, str(tmp_path)) if k == "push"]
        assert ev and ev[0]["dir"] == "/a/b", cmd


def test_gh_pr_create_reads_head_and_repo(analyze):
    ev = dict(analyze("gh pr create -R owner/repo --head owner:fix/x --fill", "/tmp"))["pr"]
    assert ev["head"] == "fix/x" and ev["repo"] == "owner/repo"


def test_push_refspecs_and_flags(analyze):
    ev = dict(analyze("git push -u origin +fix/a:fix/a fix/b", "/tmp"))["push"]
    assert ev["remote"] == "origin" and ev["refs"] == ["+fix/a:fix/a", "fix/b"]
    assert "-u" in ev["flags"]


def test_commit_all_flag(analyze):
    assert dict(analyze("git commit -am x", "/tmp"))["commit"]["all"] is True
    assert dict(analyze("git commit -m '-a'", "/tmp"))["commit"]["all"] is False


def test_garbage_does_not_crash(analyze):
    for cmd in ("'", '"', "$(", "`", "<<EOF", "git -C", "gh", "(((", "\\"):
        analyze(cmd, "/tmp")


# --- Runde 2b (Kritik tore-dicht): the folder model must match the shell's --


@pytest.mark.parametrize("cmd", [
    "(cd /a/b); git push",
    "(cd /a/b && true); git push",
    "x=$(cd /a/b); git push",
    "bash -c 'cd /a/b'; git push",
    "cd /a/b & git push",
    "cd /a/b &>/dev/null & git push",
    "sh -c 'cd /a/b; true' && git push",
])
def test_a_cd_in_a_subshell_does_not_move_the_command_after_it(analyze, cmd):
    ev = [e for k, e in analyze(cmd, "/tmp") if k == "push"]
    assert ev and ev[0]["dir"] == "/tmp", (cmd, ev)


@pytest.mark.parametrize("cmd", [
    "cd /a && cd - && git push",
    "pushd /a/b && popd && git push",
    "pushd && git push",
    "false && cd /a/b; git push",
    "true || cd /a/b; git push",
    "true || cd /a/b && git push",
    "cd /a/b || true; git push",
    "cd /a/b | cat; git push",
])
def test_a_cd_the_gate_cannot_follow_makes_the_folder_unknown(analyze, cmd):
    ev = [e for k, e in analyze(cmd, "/tmp") if k == "push"]
    assert ev and ev[0]["dir"] is None, (cmd, ev)


@pytest.mark.parametrize("cmd,where", [
    ("cd /a/b || exit 1; git push", "/a/b"),
    ("false && cd /a/b && git push", "/a/b"),
    ("if cd /a/b; then git push; fi", "/a/b"),
    ("cd /a/b &>/dev/null && git push", "/a/b"),
    ("cd /a/b && bash <<'EOF'\ngit push\nEOF", "/a/b"),
    ("cd /a/b && x=$(git push)", "/a/b"),
    ("(cd /x); (cd /a/b && git push)", "/a/b"),
])
def test_a_cd_the_gate_can_follow_still_counts(analyze, cmd, where):
    ev = [e for k, e in analyze(cmd, "/tmp") if k == "push"]
    assert ev and ev[0]["dir"] == where, (cmd, ev)


MORE_PUSH_FORMS = [
    "bash -lc 'git push'",
    "sh -ec 'git push'",
    "bash -c -- 'git push'",
    "bash --login -c 'git push'",
    "bash -o pipefail -c 'git push'",
    "cat <<'EOF' | bash\ngit push\nEOF",
    "echo 'git push' | bash",
    "echo 'git push' | bash -s",
    "bash <<< 'git push'",
    "find . -maxdepth 0 -exec git push \;",
    "find . -maxdepth 0 -execdir git push ';'",
    "awk 'BEGIN{system(\"git push\")}'",
    "timeout 9 bash -lc 'git push'",
    "echo \"g\"\"it push\" | zsh",
]


@pytest.mark.parametrize("cmd", MORE_PUSH_FORMS)
def test_more_spellings_of_push_are_seen(analyze, cmd):
    assert "push" in kinds(analyze(cmd, "/tmp")), cmd


def test_shell_reading_a_file_on_stdin_is_read(analyze, tmp_path):
    (tmp_path / "ship.sh").write_text("git -C /a/b push\n")
    for cmd in ("bash < ship.sh", "cat ship.sh | bash", "sh -s < ./ship.sh"):
        ev = [e for k, e in analyze(cmd, str(tmp_path)) if k == "push"]
        assert ev and ev[0]["dir"] == "/a/b", cmd


@pytest.mark.parametrize("cmd", [
    "echo push | xargs git",
    "echo push | xargs -I{} git {}",
    "echo fix/x | xargs git push origin",
    "printf '%s %s' git push | sh",
    "python3 -c \"import subprocess; subprocess.run(['g'+'it', 'pu'+'sh'])\"",
    "node -e \"require('child_process').execSync(['gi','t'].join('') + ' push')\"",
])
def test_git_whose_words_come_from_input_is_hidden(analyze, cmd):
    assert "hidden" in kinds(analyze(cmd, "/tmp")), cmd


@pytest.mark.parametrize("cmd", [
    "echo hi | bash", "ls | xargs wc -l", "git ls-files | xargs git add",
    "find . -name '*.py' -exec grep -l push {} +", "awk '/push/' log.txt",
    "python3 -c 'import subprocess; subprocess.run([\"ls\"])'",
    "(cd /a && make)", "cd - >/dev/null",
])
def test_harmless_lookalikes_stay_quiet(analyze, cmd):
    assert not {"push", "pr", "bypass", "hidden"} & set(kinds(analyze(cmd, "/tmp"))), cmd


@pytest.mark.parametrize("cmd,paths", [
    ("git commit -m x app.py", ["app.py"]),
    ("git commit -m x -- app.py b.py", ["app.py", "b.py"]),
    ("git commit -o app.py -m x", ["app.py"]),
    ("git commit --only -m x app.py", ["app.py"]),
    ("git commit -m x", []),
    ("git commit -F- -- a", ["a"]),
])
def test_commit_pathspecs_are_read(analyze, cmd, paths):
    assert dict(analyze(cmd, "/tmp"))["commit"]["paths"] == paths
