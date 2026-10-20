"""Shared helpers for the Claude Code hook tests: a throwaway git repo with an
origin/main ref, and a runner that feeds the hook its JSON on stdin."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "ci@example.com")
    git(r, "config", "user.name", "CI")
    git(r, "config", "commit.gpgsign", "false")
    (r / "README.md").write_text("base\n")
    git(r, "add", "README.md")
    git(r, "commit", "-q", "-m", "base")
    git(r, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(r, "checkout", "-q", "-b", "fix/branch")
    return r


def commit(repo, rel, text, msg="change"):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", msg)


def worktree(repo, branch, path):
    """A second checkout of the same repository with <branch> (new, from HEAD)."""
    git(repo, "worktree", "add", "-q", "-b", branch, str(path))
    return path


def run_hook(name, command, cwd=None, env_extra=None):
    env = dict(os.environ)
    # the session running the tests must not leak its own repo into the hook
    for k in ("MC_HOOK_REPO", "CLAUDE_PROJECT_DIR", "MC_TEST_GATE_BUDGET", "MC_TEST_GATE_MAX_FILES"):
        env.pop(k, None)
    env["MC_TEST_GATE_PYTHON"] = sys.executable
    env["MC_TEST_GATE_TIMEOUT"] = "60"
    env.update(env_extra or {})
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd:
        payload["cwd"] = str(cwd)
    return subprocess.run(
        ["bash", str(HOOKS / name)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
