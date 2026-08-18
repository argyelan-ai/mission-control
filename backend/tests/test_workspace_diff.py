"""Tests for workspace_diff — structured git diff over an agent's
workspace. Builds a real tmp git repo per test (init, commit, modify) and
runs the real git binary; no mocking of subprocess.

Also covers the `GET /agents/{id}/chat/diff` router wiring (auth, 404
`no_workspace` contract, scope passthrough)."""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.services import workspace_diff as wd

# Module mixes sync (direct workspace_diff() calls) and async (router HTTP)
# tests — no module-level `pytestmark = pytest.mark.asyncio` here since
# pytest-asyncio's Mode.AUTO already runs `async def` tests without it, and
# applying it module-wide warns on every sync test.


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", "test@mc.local", cwd=path)
    _git("config", "user.name", "MC Test", cwd=path)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A repo with one commit touching two files, ready for scope=worktree
    and scope=last-commit tests."""
    repo_path = tmp_path / "repo"
    _init_repo(repo_path)

    (repo_path / "a.txt").write_text("line1\nline2\nline3\n")
    (repo_path / "b.txt").write_text("hello\nworld\n")
    _git("add", "a.txt", "b.txt", cwd=repo_path)
    _git("commit", "-m", "Initial commit", cwd=repo_path)
    return repo_path


# ── resolve_workspace_path ───────────────────────────────────────────────────


def test_resolve_workspace_path_absolute_passthrough():
    assert wd.resolve_workspace_path("/Users/testuser/.mc/workspaces/rex") == Path(
        "/Users/testuser/.mc/workspaces/rex"
    )


def test_resolve_workspace_path_tilde_expands_via_host_home(monkeypatch):
    monkeypatch.setattr(wd, "_host_home", lambda: Path("/Users/testuser"))
    assert wd.resolve_workspace_path("~/.mc/workspaces/rex") == Path(
        "/Users/testuser/.mc/workspaces/rex"
    )


# ── workspace_diff: no_workspace cases ───────────────────────────────────────


def test_workspace_diff_raises_when_dir_missing(tmp_path):
    with pytest.raises(wd.NoWorkspaceError):
        wd.workspace_diff(tmp_path / "does-not-exist", scope="worktree")


def test_workspace_diff_raises_when_not_a_git_repo(tmp_path):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    with pytest.raises(wd.NoWorkspaceError):
        wd.workspace_diff(plain_dir, scope="worktree")


# ── scope=worktree ───────────────────────────────────────────────────────────


def test_worktree_diff_no_changes_returns_empty(repo):
    result = wd.workspace_diff(repo, scope="worktree")
    assert result["files"] == []
    assert result["stats"] == {"files": 0, "additions": 0, "deletions": 0}
    assert result["hash"] == ""


def test_worktree_diff_modified_files(repo):
    (repo / "a.txt").write_text("line1\nCHANGED\nline3\n")
    (repo / "b.txt").write_text("hello\nworld\nnew line\n")

    result = wd.workspace_diff(repo, scope="worktree")

    assert result["message"] == "Uncommitted changes"
    assert result["stats"]["files"] == 2
    filenames = {f["filename"] for f in result["files"]}
    assert filenames == {"a.txt", "b.txt"}

    a_file = next(f for f in result["files"] if f["filename"] == "a.txt")
    assert a_file["additions"] == 1
    assert a_file["deletions"] == 1
    assert len(a_file["hunks"]) == 1
    hunk = a_file["hunks"][0]
    assert hunk["header"].startswith("@@ ")

    lines_by_type = {}
    for line in hunk["lines"]:
        lines_by_type.setdefault(line["type"], []).append(line)
    assert any(l["content"] == "CHANGED" and l["old_no"] is None for l in lines_by_type["add"])
    assert any(l["content"] == "line2" and l["new_no"] is None for l in lines_by_type["del"])
    # context lines carry both old_no and new_no
    ctx_lines = lines_by_type["ctx"]
    assert any(l["content"] == "line1" and l["old_no"] == 1 and l["new_no"] == 1 for l in ctx_lines)

    b_file = next(f for f in result["files"] if f["filename"] == "b.txt")
    assert b_file["additions"] == 1
    assert b_file["deletions"] == 0

    assert result["stats"]["additions"] == 2
    assert result["stats"]["deletions"] == 1


def test_worktree_diff_includes_staged_changes(repo):
    (repo / "a.txt").write_text("line1\nline2\nline3\nline4\n")
    _git("add", "a.txt", cwd=repo)

    result = wd.workspace_diff(repo, scope="worktree")

    assert result["stats"]["files"] == 1
    assert result["files"][0]["filename"] == "a.txt"
    assert result["files"][0]["additions"] == 1


def test_worktree_diff_new_untracked_file_not_included(repo):
    # git diff HEAD does not show untracked files — matches plain `git diff`
    # semantics (untracked files need `git add` first to appear).
    (repo / "c.txt").write_text("new file\n")

    result = wd.workspace_diff(repo, scope="worktree")

    assert result["files"] == []


# ── scope=last-commit ────────────────────────────────────────────────────────


def test_last_commit_diff_shows_initial_commit(repo):
    result = wd.workspace_diff(repo, scope="last-commit")

    assert result["message"] == "Initial commit"
    assert result["author"] == "MC Test"
    assert result["hash"]
    assert result["stats"]["files"] == 2
    filenames = {f["filename"] for f in result["files"]}
    assert filenames == {"a.txt", "b.txt"}

    a_file = next(f for f in result["files"] if f["filename"] == "a.txt")
    assert a_file["additions"] == 3
    assert a_file["deletions"] == 0
    hunk = a_file["hunks"][0]
    added_lines = [l["content"] for l in hunk["lines"] if l["type"] == "add"]
    assert added_lines == ["line1", "line2", "line3"]


def test_last_commit_diff_second_commit(repo):
    (repo / "a.txt").write_text("line1\nCHANGED\nline3\n")
    _git("add", "a.txt", cwd=repo)
    _git("commit", "-m", "Second commit", cwd=repo)

    result = wd.workspace_diff(repo, scope="last-commit")

    assert result["message"] == "Second commit"
    assert result["stats"]["files"] == 1
    assert result["files"][0]["filename"] == "a.txt"
    assert result["files"][0]["additions"] == 1
    assert result["files"][0]["deletions"] == 1


# ── caps: 200 files / 5000 lines per file ────────────────────────────────────


def test_worktree_diff_truncates_long_file_at_line_cap(repo, monkeypatch):
    monkeypatch.setattr(wd, "_MAX_LINES_PER_FILE", 10)

    lines = [f"line{i}\n" for i in range(50)]
    (repo / "a.txt").write_text("".join(lines))
    _git("add", "a.txt", cwd=repo)
    _git("commit", "-m", "expand a.txt", cwd=repo)

    changed = [f"line{i}\n" if i % 2 else f"CHANGED{i}\n" for i in range(50)]
    (repo / "a.txt").write_text("".join(changed))

    result = wd.workspace_diff(repo, scope="worktree")

    a_file = result["files"][0]
    all_lines = [l for hunk in a_file["hunks"] for l in hunk["lines"]]
    assert len(all_lines) <= 11  # 10 real lines + 1 synthetic truncation marker
    assert any(l["content"] == "… truncated" and l["type"] == "ctx" for l in all_lines)
    # file-level additions/deletions still come from numstat, unaffected by
    # the hunk-line truncation cap.
    assert a_file["additions"] == 25
    assert a_file["deletions"] == 25


def test_worktree_diff_truncates_file_count_at_cap(repo, monkeypatch):
    monkeypatch.setattr(wd, "_MAX_FILES", 2)

    for name in ("x1.txt", "x2.txt", "x3.txt"):
        (repo / name).write_text("orig\n")
    _git("add", "x1.txt", "x2.txt", "x3.txt", cwd=repo)
    _git("commit", "-m", "add three files", cwd=repo)

    for name in ("x1.txt", "x2.txt", "x3.txt"):
        (repo / name).write_text("changed\n")

    result = wd.workspace_diff(repo, scope="worktree")

    assert len(result["files"]) == 2
    assert result["stats"]["files"] == 2


# ══════════════════════════════════════════════════════════════════════════
# Router: GET /agents/{id}/chat/diff
# ══════════════════════════════════════════════════════════════════════════


async def test_diff_router_200_worktree_scope(auth_client: AsyncClient, make_agent, repo):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(repo))
    (repo / "a.txt").write_text("line1\nCHANGED\nline3\n")

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/diff")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message"] == "Uncommitted changes"
    assert body["files"][0]["filename"] == "a.txt"


async def test_diff_router_200_last_commit_scope(auth_client: AsyncClient, make_agent, repo):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(repo))

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/diff", params={"scope": "last-commit"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message"] == "Initial commit"


async def test_diff_router_404_no_workspace_path_set(auth_client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=None)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/diff")

    assert resp.status_code == 404
    assert resp.json() == {"reason": "no_workspace"}


async def test_diff_router_404_workspace_dir_missing(auth_client: AsyncClient, make_agent, tmp_path):
    agent = await make_agent(
        name="Rex", agent_runtime="cli-bridge", workspace_path=str(tmp_path / "gone")
    )

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/diff")

    assert resp.status_code == 404
    assert resp.json() == {"reason": "no_workspace"}


async def test_diff_router_404_workspace_not_a_git_repo(auth_client: AsyncClient, make_agent, tmp_path):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(plain_dir))

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/diff")

    assert resp.status_code == 404
    assert resp.json() == {"reason": "no_workspace"}


async def test_diff_router_422_invalid_scope(auth_client: AsyncClient, make_agent, repo):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(repo))

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/diff", params={"scope": "bogus"})

    assert resp.status_code == 422


async def test_diff_router_requires_auth(client: AsyncClient, make_agent, repo):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(repo))

    resp = await client.get(f"/api/v1/agents/{agent.id}/chat/diff")

    assert resp.status_code == 401


async def test_diff_router_404_for_unknown_agent(auth_client: AsyncClient):
    resp = await auth_client.get(f"/api/v1/agents/{uuid.uuid4()}/chat/diff")
    assert resp.status_code == 404
