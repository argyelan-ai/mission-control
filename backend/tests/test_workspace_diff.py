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
from app.services.workspace_diff import NoWorkspaceError, find_repo_root

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

    # A code, not a sentence: the UI words "uncommitted changes" itself, in
    # the operator's language (PRINCIPLES §6.3). Was "Uncommitted changes".
    assert result["message"] == ""
    assert result["scope"] == "worktree"
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


def test_worktree_diff_includes_new_untracked_file(repo):
    # Changed 04.10.2026: a file the agent just created is the most common
    # "what just happened in the chat" — plain `git diff HEAD` semantics hid
    # it until somebody ran `git add`. Ignored files stay hidden.
    (repo / "c.txt").write_text("new file\nsecond\n")
    (repo / ".gitignore").write_text("ignored.log\n")
    (repo / "ignored.log").write_text("noise\n")

    result = wd.workspace_diff(repo, scope="worktree")

    by_name = {f["filename"]: f for f in result["files"]}
    assert set(by_name) == {"c.txt", ".gitignore"}
    c = by_name["c.txt"]
    assert c["additions"] == 2 and c["deletions"] == 0
    assert [l["content"] for l in c["hunks"][0]["lines"]] == ["new file", "second"]
    assert c["hunks"][0]["lines"][0]["new_no"] == 1
    assert result["stats"]["additions"] == 3


def test_untracked_symlink_is_listed_without_its_target_content(repo, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must never reach the panel\n")
    (repo / "link.txt").symlink_to(outside)

    result = wd.workspace_diff(repo, scope="worktree")

    link = next(f for f in result["files"] if f["filename"] == "link.txt")
    assert link["hunks"] == [] and link["additions"] == 0
    assert "must never reach" not in str(result)


def test_untracked_binary_file_is_listed_without_content(repo):
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02binary")

    result = wd.workspace_diff(repo, scope="worktree")

    blob = next(f for f in result["files"] if f["filename"] == "blob.bin")
    assert blob["hunks"] == []


def test_worktree_diff_in_a_repo_without_commits(tmp_path):
    fresh = tmp_path / "fresh"
    _init_repo(fresh)
    (fresh / "start.py").write_text("print('hi')\n")

    result = wd.workspace_diff(fresh, scope="worktree")

    assert [f["filename"] for f in result["files"]] == ["start.py"]


def test_last_commit_in_a_repo_without_commits_is_empty_not_an_error(tmp_path):
    fresh = tmp_path / "fresh"
    _init_repo(fresh)

    result = wd.workspace_diff(fresh, scope="last-commit")

    assert result["scope"] == "last-commit"
    assert result["hash"] == "" and result["files"] == []


# ── scope=last-commit ────────────────────────────────────────────────────────


def test_last_commit_diff_shows_initial_commit(repo):
    result = wd.workspace_diff(repo, scope="last-commit")

    assert result["scope"] == "last-commit"
    # ISO time for the UI to format in the operator's locale — `date` (git's
    # English "7 months ago") stays for older readers.
    from datetime import datetime
    assert datetime.fromisoformat(result["committed_at"]).tzinfo is not None
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


# ── find_repo_root ────────────────────────────────────────────────────────────
# Vorfall 11.09.2026: Diff-Panel zeigte „Kein Workspace", obwohl der Agent
# längst Änderungen hatte — agent.workspace_path ist der Agenten-STAMM
# (~/.mc/workspaces/<slug>), das Repo lag unter <task-dir>/repo/. Das Repo
# muss also eine Ebene tiefer gesucht werden.


def test_find_repo_root_returns_repo_itself(repo):
    assert find_repo_root(repo) == repo


def test_find_repo_root_descends_one_level_into_single_child_repo(tmp_path, repo):
    # tmp_path/repo ist das Repo; tmp_path selbst ist keins
    assert find_repo_root(tmp_path) == repo


def _make_stale(repo: Path, when: float = 1_600_000_000) -> None:
    """Back-dates every file git activity is read from, as if the repo had
    not been touched for years."""
    import os
    gd = wd.git_dir(repo)
    for p in [repo / ".git", *( [gd, gd / "HEAD", gd / "index", gd / "logs" / "HEAD"] if gd else [])]:
        if p.exists():
            os.utime(p, (when, when))


def test_find_repo_root_picks_most_recently_used_child_repo(tmp_path):
    old = tmp_path / "old-repo"
    new = tmp_path / "new-repo"
    _init_repo(old)
    _init_repo(new)
    _make_stale(old)
    assert find_repo_root(tmp_path) == new


def _commit_file(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text)
    _git("add", name, cwd=repo)
    _git("commit", "-m", message, cwd=repo)


def test_find_repo_root_counts_a_worktree_checkouts_own_activity(tmp_path):
    """A `git worktree` checkout has a `.git` FILE whose mtime never changes
    on commits. Ranking by that mtime hid every worktree behind older clones
    (Sparky's layout: ~70 worktrees next to two clones)."""
    import os
    main = tmp_path / "main-clone"
    _init_repo(main)
    _commit_file(main, "a.txt", "a\n", "base")
    wt = tmp_path / "feature-wt"
    _git("worktree", "add", "-b", "feature", str(wt), cwd=main)
    other = tmp_path / "other-clone"
    _init_repo(other)
    _commit_file(other, "o.txt", "o\n", "other")

    _make_stale(main)
    _make_stale(other, 1_700_000_000)
    os.utime(wt / ".git", (1_600_000_000, 1_600_000_000))
    _commit_file(wt, "f.txt", "fresh\n", "fresh work in the worktree")

    assert find_repo_root(tmp_path) == wt


def test_git_dir_resolves_a_worktree_file(tmp_path):
    main = tmp_path / "m"
    _init_repo(main)
    _commit_file(main, "a.txt", "a\n", "base")
    wt = tmp_path / "wt"
    _git("worktree", "add", "-b", "x", str(wt), cwd=main)

    assert wd.git_dir(main) == main / ".git"
    assert wd.git_dir(wt).resolve() == (main / ".git" / "worktrees" / "wt").resolve()


def test_current_branch_name_and_detached_head(repo):
    assert wd.current_branch(repo) == "main"
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    _git("checkout", "--detach", cwd=repo)
    assert wd.current_branch(repo) == sha[:7]


def test_repos_under_two_levels_skipping_dot_and_dependency_folders(tmp_path):
    _init_repo(tmp_path / "direct")
    _init_repo(tmp_path / "worktrees" / "deep")
    _init_repo(tmp_path / "a" / "b" / "too-deep")
    _init_repo(tmp_path / ".hidden" / "x")
    _init_repo(tmp_path / "node_modules" / "pkg")
    _init_repo(tmp_path / "direct" / "nested-inside-repo")

    found = {p.relative_to(tmp_path).as_posix() for p in wd.repos_under(tmp_path)}

    assert found == {"direct", "worktrees/deep"}


def test_enclosing_repo_climbs_up_but_never_past_the_boundary(tmp_path):
    repo_dir = tmp_path / "ws" / "proj"
    _init_repo(repo_dir)
    (repo_dir / "src" / "pkg").mkdir(parents=True)
    ws = tmp_path / "ws"

    assert wd.enclosing_repo(repo_dir / "src" / "pkg", ws) == repo_dir.resolve()
    assert wd.enclosing_repo(ws, ws) is None
    # A repo ABOVE the boundary must not be found.
    _init_repo(tmp_path / "outer")
    (tmp_path / "outer" / "agent-ws").mkdir()
    assert wd.enclosing_repo(tmp_path / "outer" / "agent-ws", tmp_path / "outer" / "agent-ws") is None
    # A path outside the boundary is refused outright.
    assert wd.enclosing_repo(tmp_path / "outer", ws) is None


# ── host_path_for_agent_cwd / display_path ───────────────────────────────────


def test_docker_cwd_maps_onto_the_workspace_root(tmp_path):
    root = tmp_path / "ws"
    assert wd.host_path_for_agent_cwd("cli-bridge", root, "/workspace") == root
    assert wd.host_path_for_agent_cwd("cli-bridge", root, "/workspace/proj/src") == root / "proj" / "src"


@pytest.mark.parametrize(
    "cwd",
    ["/home/agent", "/workspace-ref/x", "/workspace/../etc", "relative/path", "", None],
)
def test_docker_cwd_outside_the_workspace_maps_to_nothing(tmp_path, cwd):
    assert wd.host_path_for_agent_cwd("cli-bridge", tmp_path / "ws", cwd) is None


def test_docker_cwd_without_a_workspace_root_maps_to_nothing():
    assert wd.host_path_for_agent_cwd("cli-bridge", None, "/workspace") is None


def test_host_cwd_is_accepted_only_inside_the_mc_home(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "_host_home", lambda: tmp_path)
    inside = str(tmp_path / ".mc" / "checkouts" / "proj")
    assert wd.host_path_for_agent_cwd("host", None, inside) == Path(inside)
    assert wd.host_path_for_agent_cwd("host", None, str(tmp_path / "Documents")) is None
    assert wd.host_path_for_agent_cwd("host", None, str(tmp_path / ".mc" / ".." / "x")) is None


def test_display_path_uses_the_agents_own_view(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "_host_home", lambda: tmp_path)
    root = tmp_path / ".mc" / "workspaces" / "a"
    assert wd.display_path("cli-bridge", root, root / "proj") == "/workspace/proj"
    assert wd.display_path("cli-bridge", root, root) == "/workspace"
    assert wd.display_path("host", None, tmp_path / ".mc" / "checkouts" / "p") == "~/.mc/checkouts/p"


# ── choose_repo ──────────────────────────────────────────────────────────────


def test_choose_repo_running_task_wins(tmp_path):
    task = tmp_path / "task"
    _init_repo(task)
    sess = tmp_path / "ws" / "chat"
    _init_repo(sess)

    choice = wd.choose_repo(running_task=task, session_dir=sess, boundary=tmp_path / "ws")

    assert choice == wd.RepoChoice(task, "task")


def test_choose_repo_session_folder_inside_a_repo_wins_over_fresher_repos(tmp_path):
    ws = tmp_path / "ws"
    sess_repo = ws / "chat-proj"
    _init_repo(sess_repo)
    _make_stale(sess_repo)
    (sess_repo / "sub").mkdir()
    fresh = ws / "fresh"
    _init_repo(fresh)

    choice = wd.choose_repo(
        running_task=None, session_dir=sess_repo / "sub", boundary=ws, fallbacks=(ws,)
    )

    assert choice.kind == "session" and choice.path == sess_repo.resolve()


def test_choose_repo_most_recent_git_activity_beats_the_last_finished_task(tmp_path):
    """The incident of 04.10.2026: the chat committed in a fresh repo under
    the agent's workspace, the panel showed the months-old commit of the
    last FINISHED task — because that task row always won."""
    ws = tmp_path / "ws"
    chat_repo = ws / "scratch"
    _init_repo(chat_repo)
    _commit_file(chat_repo, "hello.txt", "hi\n", "chat commit")
    finished = tmp_path / "elsewhere" / "old-task"
    _init_repo(finished)
    _commit_file(finished, "fib.py", "x\n", "months old")
    _make_stale(finished)

    choice = wd.choose_repo(
        running_task=None, session_dir=ws, boundary=ws, fallbacks=(ws, finished)
    )

    assert choice == wd.RepoChoice(chat_repo, "recent")


def test_choose_repo_raises_when_nothing_is_a_repo(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(NoWorkspaceError):
        wd.choose_repo(running_task=None, session_dir=None, boundary=None, fallbacks=(tmp_path,))


def test_find_repo_root_raises_when_nothing_is_a_repo(tmp_path):
    (tmp_path / "notes").mkdir()
    with pytest.raises(NoWorkspaceError):
        find_repo_root(tmp_path)



# ══════════════════════════════════════════════════════════════════════════
# Router: GET /agents/{id}/chat/diff
# ══════════════════════════════════════════════════════════════════════════


async def test_diff_router_200_worktree_scope(auth_client: AsyncClient, make_agent, repo):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(repo))
    (repo / "a.txt").write_text("line1\nCHANGED\nline3\n")

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/diff")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message"] == ""
    assert body["scope"] == "worktree"
    assert body["files"][0]["filename"] == "a.txt"
    assert body["source"]["kind"] == "recent"
    assert body["source"]["repo"] == repo.name
    assert body["source"]["branch"] == "main"


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


async def test_diff_router_prefers_current_task_workspace_over_agent_root(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, repo
):
    """agent.workspace_path = Stamm ohne Git; die laufende Task arbeitet in
    <stamm>/<task-slug>/repo/ — genau das Live-Bild vom 11.09.2026."""
    root = tmp_path / "agent-root"
    task_dir = root / "w4-restposten-abc123"
    task_dir.mkdir(parents=True)
    _init_repo(task_dir / "repo")
    (task_dir / "repo" / "f.txt").write_text("one\n")
    _git("add", "f.txt", cwd=task_dir / "repo")
    _git("commit", "-m", "task work", cwd=task_dir / "repo")
    (task_dir / "repo" / "f.txt").write_text("one\ntwo\n")

    board = await make_board()
    agent = await make_agent(name="Alpha", agent_runtime="cli-bridge", workspace_path=str(root))
    task = await make_task(
        board.id, title="W4", status="in_progress",
        assigned_agent_id=agent.id, workspace_path=str(task_dir),
    )
    from app.models.agent import Agent
    from tests.conftest import test_engine  # noqa: PLC0415
    from sqlmodel.ext.asyncio.session import AsyncSession
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        db_agent = await s.get(Agent, agent.id)
        db_agent.current_task_id = task.id
        s.add(db_agent)
        await s.commit()

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/diff")

    assert resp.status_code == 200, resp.text
    assert resp.json()["files"][0]["filename"] == "f.txt"


async def test_diff_router_falls_back_to_latest_task_workspace_when_idle(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path
):
    # Task-Workspace bewusst AUSSERHALB des Agenten-Stamms (z.B. Projekt-
    # Worktree) — nur der Task-Datensatz kennt den Pfad.
    root = tmp_path / "agent-root"
    root.mkdir()
    task_dir = tmp_path / "elsewhere" / "done-task"
    _init_repo(task_dir)
    (task_dir / "g.txt").write_text("x\n")
    _git("add", "g.txt", cwd=task_dir)
    _git("commit", "-m", "done work", cwd=task_dir)

    board = await make_board()
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(root))
    await make_task(
        board.id, title="Done", status="done",
        assigned_agent_id=agent.id, workspace_path=str(task_dir),
    )

    resp = await auth_client.get(
        f"/api/v1/agents/{agent.id}/chat/diff", params={"scope": "last-commit"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "done work"


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


# ── Router: the chat session decides (04.10.2026) ────────────────────────────


def _write_omp_session(sessions_root: Path, cwd: str, *, sink: bool = False) -> Path:
    """An omp session folder as the ACP driver leaves it: the native file
    with the real cwd, optionally the bridge sink file with an EMPTY cwd
    that is newer (the one find_active_session picks)."""
    import json
    folder = sessions_root / "--workspace--"
    folder.mkdir(parents=True, exist_ok=True)
    sid = "01a1-test-session"
    native = folder / f"2026-10-04T12-00-00-000Z_{sid}.jsonl"
    native.write_text(
        json.dumps({"type": "title", "title": ""}) + "\n"
        + json.dumps({"type": "session", "id": sid, "timestamp": "2026-10-04T12:00:00Z", "cwd": cwd}) + "\n"
    )
    if not sink:
        return native
    sink_file = folder / f"2026-10-04T12-00-00_{sid}.jsonl"
    sink_file.write_text(
        json.dumps({"type": "session", "id": sid, "timestamp": "2026-10-04T12:00:01Z", "cwd": "", "bridge": "acp"}) + "\n"
        + json.dumps({"type": "message", "id": "m1", "timestamp": "2026-10-04T12:05:00Z", "message": {"role": "user", "content": []}}) + "\n"
    )
    return sink_file


async def test_diff_router_follows_the_chat_session_not_the_finished_task(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch
):
    """End to end through the omp adapter: the chat session works in
    /workspace/scratch (recorded only in omp's own file — the newer ACP sink
    file has an empty cwd); a finished task points at an older repo that the
    old code always picked."""
    from app.services import omp_chat
    home = tmp_path / "home"
    monkeypatch.setattr(omp_chat, "_host_home", lambda: home)
    monkeypatch.setattr(wd, "_host_home", lambda: home)

    root = home / ".mc" / "workspaces" / "sparky"
    scratch = root / "scratch"
    _init_repo(scratch)
    _commit_file(scratch, "hello.txt", "probe\n", "test: chat commit")
    (scratch / "hello.txt").write_text("probe\nuncommitted\n")
    old_task = root / "worktrees" / "old-task"
    _init_repo(old_task)
    _commit_file(old_task, "fib.py", "x\n", "months old task commit")
    _make_stale(old_task)
    _write_omp_session(home / ".mc" / "agents" / "sparky" / "omp-sessions", "/workspace/scratch", sink=True)

    board = await make_board()
    agent = await make_agent(
        name="Sparky", slug="sparky", harness="omp", agent_runtime="cli-bridge",
        workspace_path=str(root),
    )
    await make_task(
        board.id, title="Old", status="done",
        assigned_agent_id=agent.id, workspace_path=str(old_task),
    )

    last = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/diff", params={"scope": "last-commit"})
    work = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/diff")

    assert last.status_code == 200, last.text
    body = last.json()
    assert body["message"] == "test: chat commit"
    assert body["source"] == {
        "kind": "session", "repo": "scratch", "branch": "main", "path": "/workspace/scratch",
    }
    assert work.status_code == 200, work.text
    assert [f["filename"] for f in work.json()["files"]] == ["hello.txt"]
    assert work.json()["source"]["repo"] == "scratch"


async def test_diff_router_without_a_session_takes_the_most_recent_repo(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path
):
    """No transcript at all: the freshest repository in the workspace beats
    the stale repository of the last finished task."""
    root = tmp_path / "agent-root"
    fresh = root / "scratch-diff-test"
    _init_repo(fresh)
    _commit_file(fresh, "hello.txt", "probe\n", "fresh work")
    finished = tmp_path / "elsewhere" / "done-task"
    _init_repo(finished)
    _commit_file(finished, "fib.py", "x\n", "months old")
    _make_stale(finished)

    board = await make_board()
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(root))
    await make_task(
        board.id, title="Done", status="done",
        assigned_agent_id=agent.id, workspace_path=str(finished),
    )

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/diff", params={"scope": "last-commit"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "fresh work"
    assert resp.json()["source"]["kind"] == "recent"


async def test_diff_router_host_agent_session_in_the_mc_home(
    auth_client: AsyncClient, make_agent, tmp_path, monkeypatch
):
    """Host agent (Boss shape): its session works in a checkout under the MC
    home — that checkout is shown, not some repo below its workspace."""
    from app.routers import agent_chat
    home = tmp_path / "home"
    monkeypatch.setattr(wd, "_host_home", lambda: home)
    checkout = home / ".mc" / "checkouts" / "proj"
    _init_repo(checkout)
    _commit_file(checkout, "x.txt", "x\n", "lead session commit")
    _make_stale(checkout)
    ws = home / ".mc" / "workspaces" / "boss"
    noise = ws / "old-project"
    _init_repo(noise)
    _commit_file(noise, "n.txt", "n\n", "unrelated fresher repo")
    monkeypatch.setattr(agent_chat, "_chat_session_cwd", lambda agent: str(checkout / "backend"))
    (checkout / "backend").mkdir()

    agent = await make_agent(name="Boss", agent_runtime="host", workspace_path=str(ws))

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/diff", params={"scope": "last-commit"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "lead session commit"
    assert resp.json()["source"]["kind"] == "session"
    assert resp.json()["source"]["path"] == "~/.mc/checkouts/proj"
