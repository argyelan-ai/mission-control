"""Task GIT section lists only the task's own commits (27.10.2026).

A task workspace is a worktree of the agent's repo on a fresh task branch.
The section showed the base branch's history (`git log` of HEAD): 10 old
commits of other work on a task that had not committed anything. Real git
repos here (no mocked subprocess) — the bug was in which range is asked.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.git_service import GitService

OLD = "2025-02-01T10:00:00+00:00"


def _git(cwd: Path, *args: str, date: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Old Author", "GIT_AUTHOR_EMAIL": "old@example.invalid",
           "GIT_COMMITTER_NAME": "Old Author", "GIT_COMMITTER_EMAIL": "old@example.invalid"}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _commit(cwd: Path, name: str, msg: str, date: str | None = None) -> None:
    (cwd / name).write_text(msg)
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", msg, date=date)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "agent-repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for i in range(3):
        _commit(root, f"old{i}.txt", f"old work {i}", date=OLD)
    return root


@pytest.fixture
def service(monkeypatch) -> GitService:
    s = GitService()

    async def no_auth() -> None:
        return None

    monkeypatch.setattr(s, "_ensure_git_auth", no_auth)
    return s


def _task_worktree(repo: Path, tmp_path: Path, branch: str = "task/bench-lighthouse") -> Path:
    wt = tmp_path / "worktrees" / "bench-lighthouse"
    _git(repo, "worktree", "add", "-q", "-b", branch, str(wt), "main")
    return wt


@pytest.mark.asyncio
async def test_fresh_task_branch_lists_no_base_history(repo, tmp_path, service):
    wt = _task_worktree(repo, tmp_path)
    info = await service.get_task_git_info(str(wt))
    assert info["branch"] == "task/bench-lighthouse"
    assert info["commits"] == []
    assert info["last_commit"] == ""
    assert info["ahead"] == 0


@pytest.mark.asyncio
async def test_task_branch_lists_only_its_own_commits(repo, tmp_path, service):
    wt = _task_worktree(repo, tmp_path)
    _commit(wt, "index.html", "feat: lighthouse page")
    info = await service.get_task_git_info(str(wt))
    assert [c["message"] for c in info["commits"]] == ["feat: lighthouse page"]
    assert info["ahead"] == 1


@pytest.mark.asyncio
async def test_named_branch_is_measured_against_the_base(repo, tmp_path, service):
    wt = _task_worktree(repo, tmp_path)
    _commit(wt, "a.txt", "feat: task work")
    info = await service.get_task_git_info(str(repo), branch_name="task/bench-lighthouse")
    assert [c["message"] for c in info["commits"]] == ["feat: task work"]


@pytest.mark.asyncio
async def test_work_on_the_base_branch_counts_from_the_task_start(repo, service):
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    _commit(repo, "new.txt", "fix: done for this task")
    info = await service.get_task_git_info(str(repo), since=started)
    assert [c["message"] for c in info["commits"]] == ["fix: done for this task"]


@pytest.mark.asyncio
async def test_base_branch_without_a_start_lists_nothing(repo, service):
    info = await service.get_task_git_info(str(repo))
    assert info["commits"] == []
