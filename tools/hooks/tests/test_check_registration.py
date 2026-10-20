"""check-registration.py must turn red for every way the hooks are registered
but do not protect (missing, a checkout's placeholder, a narrow pre-filter,
a timeout that kills test-gate) - and green for the canonical entry it prints."""
import json
import subprocess
import sys

from conftest import HOOKS

TOOL = HOOKS / "check-registration.py"


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], capture_output=True, text=True)


def settings(tmp_path, hooks, name="settings.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": hooks}]}}))
    return p


def canonical_hooks(repo="/r"):
    r = run("--print", "--repo", repo)
    assert r.returncode == 0
    return json.loads(r.stdout)["PreToolUse"][0]["hooks"]


def test_canonical_entry_is_green(tmp_path):
    r = run(settings(tmp_path, canonical_hooks()))
    assert r.returncode == 0, r.stderr


def test_canonical_entry_really_runs_the_hook_from_origin_main(repo):
    # The printed command must work: it fetches the script from origin/main
    # of <repo> and blocks (exit 2) a commit with --no-verify.
    from conftest import git
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    hooks = canonical_hooks(str(repo))
    # the throwaway repo has no tools/hooks on origin/main: copy the real script in
    (repo / "tools/hooks").mkdir(parents=True)
    (repo / "tools/hooks/secrets-check.sh").write_text((HOOKS / "secrets-check.sh").read_text())
    git(repo, "add", "tools/hooks/secrets-check.sh")
    git(repo, "commit", "-q", "-m", "hooks")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    cmd = next(h["command"] for h in hooks if "secrets-check.sh" in h["command"])
    payload = json.dumps({"tool_input": {"command": f"git -C {repo} commit --no-verify -m x"}})
    r = subprocess.run(["bash", "-c", cmd], input=payload, capture_output=True, text=True)
    assert r.returncode == 2, r.stderr
    payload = json.dumps({"tool_input": {"command": "ls"}})
    assert subprocess.run(["bash", "-c", cmd], input=payload, capture_output=True, text=True).returncode == 0


def test_missing_hook_is_red(tmp_path):
    only_one = [h for h in canonical_hooks() if "secrets-check" in h["command"]]
    r = run(settings(tmp_path, only_one))
    assert r.returncode == 1
    assert "test-gate.sh: not registered" in r.stderr


def test_checkout_path_instead_of_origin_main_is_red(tmp_path):
    hooks = [{"type": "command", "command": "bash /r/tools/hooks/test-gate.sh", "timeout": 300},
             {"type": "command", "command": "bash /r/tools/hooks/secrets-check.sh", "timeout": 30}]
    r = run(settings(tmp_path, hooks))
    assert r.returncode == 1
    assert "not origin/main" in r.stderr


def test_narrow_prefilter_is_red(tmp_path):
    hooks = canonical_hooks()
    hooks[0] = dict(hooks[0], command="bash -c 'p=$(cat); case \"$p\" in *push*) ;; *) exit 0;; esac; "
                                      "git -C /r show origin/main:tools/hooks/test-gate.sh | bash'")
    r = run(settings(tmp_path, hooks))
    assert r.returncode == 1
    assert "pre-filter" in r.stderr


def test_short_timeout_is_red(tmp_path):
    hooks = canonical_hooks()
    for h in hooks:
        if "test-gate" in h["command"]:
            h["timeout"] = 60
    r = run(settings(tmp_path, hooks))
    assert r.returncode == 1
    assert "timeout" in r.stderr


def test_files_are_merged_like_claude_code_does(tmp_path):
    a, b = canonical_hooks()
    r = run(settings(tmp_path, [a], "user.json"), settings(tmp_path, [b], "project.json"))
    assert r.returncode == 0, r.stderr


def _entry_with_broken_origin(repo, tmp_path):
    from conftest import git
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")  # main has no tools/hooks
    return next(h["command"] for h in canonical_hooks(str(repo)) if "test-gate.sh" in h["command"])


def test_entry_fails_closed_for_a_push_when_the_hook_cannot_be_loaded(repo, tmp_path):
    cmd = _entry_with_broken_origin(repo, tmp_path)
    payload = json.dumps({"tool_input": {"command": "bash -lc 'git push'"}})
    r = subprocess.run(["bash", "-c", cmd], input=payload, capture_output=True, text=True)
    assert r.returncode == 2, r.stderr
    assert "could not load" in r.stderr


def test_entry_warns_but_lets_other_commands_run_when_the_hook_cannot_be_loaded(repo, tmp_path):
    cmd = _entry_with_broken_origin(repo, tmp_path)
    payload = json.dumps({"tool_input": {"command": "ls -la"}})
    r = subprocess.run(["bash", "-c", cmd], input=payload, capture_output=True, text=True)
    assert r.returncode == 0
    assert "could not load" in r.stderr


def test_entry_that_opens_silently_is_red(tmp_path):
    hooks = canonical_hooks()
    for h in hooks:
        h["command"] = ("bash -c 'f=$(mktemp) || exit 0; git -C /r show origin/main:tools/hooks/"
                        + ("test-gate.sh" if "test-gate" in h["command"] else "secrets-check.sh")
                        + " > \"$f\" || { rm -f \"$f\"; exit 0; }; bash \"$f\"'")
    r = run(settings(tmp_path, hooks))
    assert r.returncode == 1
    assert "fails open" in r.stderr
