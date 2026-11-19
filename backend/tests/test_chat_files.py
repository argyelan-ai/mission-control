"""chat_files — the composer's ``@`` file mention source.

``GET /agents/{id}/chat/files`` lists files of the folder the chat works in,
relative to it, best match first. These tests pin the safety rules (no
symlink is followed, ignored and sensitive folders stay out, caps hold, no
folder = 404) and the ranking."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.routers import chat_files


@pytest.fixture(autouse=True)
def _fresh_cache():
    chat_files._cache.clear()
    yield
    chat_files._cache.clear()


@pytest.fixture
def omp_home(tmp_path, monkeypatch):
    from app.services import omp_chat, transcript_chat
    from app.services import workspace_diff as wd
    home = tmp_path / "home"
    monkeypatch.setattr(omp_chat, "_host_home", lambda: home)
    monkeypatch.setattr(wd, "_host_home", lambda: home)
    monkeypatch.setattr(transcript_chat, "_host_home", lambda: home)
    return home


def _write_session(home: Path, cwd: str) -> None:
    folder = home / ".mc" / "agents" / "alpha" / "omp-sessions" / "--workspace--"
    folder.mkdir(parents=True, exist_ok=True)
    header = json.dumps({"type": "session", "version": 3, "id": "01a1-test-session",
                         "timestamp": "2026-10-12T16:00:00.000Z", "cwd": cwd})
    (folder / "2026-10-12T16-00-00-000Z_01a1-test-session.jsonl").write_text(header + "\n")


def _touch(root: Path, *rels: str) -> None:
    for rel in rels:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")


# ══════════════════════════════════════════════════════════════════════════
# walk_files — what is listed at all
# ══════════════════════════════════════════════════════════════════════════


def test_walk_lists_relative_paths_and_skips_ignored_folders(tmp_path, omp_home):
    _touch(tmp_path, "README.md", "src/app.py", "src/lib/util.ts",
           ".git/config", "node_modules/pkg/index.js", ".venv/bin/python",
           "web/node_modules/x.js", "src/__pycache__/app.cpython.pyc")

    files, truncated = chat_files.walk_files(tmp_path)

    assert sorted(files) == ["README.md", "src/app.py", "src/lib/util.ts"]
    assert truncated is False


def test_walk_never_follows_or_lists_a_symlink(tmp_path, omp_home):
    outside = tmp_path / "outside"
    _touch(outside, "secret.txt")
    root = tmp_path / "root"
    _touch(root, "a.txt")
    os.symlink(outside, root / "escape")
    os.symlink(outside / "secret.txt", root / "link.txt")

    files, _ = chat_files.walk_files(root)

    assert files == ["a.txt"]


def test_walk_never_enters_the_mc_homes_sensitive_folders(omp_home):
    mc = omp_home / ".mc"
    _touch(mc, "workspaces/w/notes.md", "secrets/token.txt", "agents/a/settings.json",
           "logs/x.log", "backups/db.sql", "browser-profiles/p/cookies")

    files, _ = chat_files.walk_files(mc)

    assert files == ["workspaces/w/notes.md"]


def test_walk_stops_at_the_file_cap_and_says_so(tmp_path, omp_home):
    _touch(tmp_path, *[f"f{i:02d}.txt" for i in range(10)])

    files, truncated = chat_files.walk_files(tmp_path, max_scan=4)

    assert len(files) == 4 and truncated is True


def test_walk_stops_at_the_depth_cap_and_says_so(tmp_path, omp_home):
    _touch(tmp_path, "top.txt", "a/b/c/deep.txt")

    files, truncated = chat_files.walk_files(tmp_path, max_depth=2)

    assert files == ["top.txt"] and truncated is True


# ══════════════════════════════════════════════════════════════════════════
# rank — fuzzy search
# ══════════════════════════════════════════════════════════════════════════

FILES = ["src/components/chat/Composer.tsx", "docs/composer-notes.md",
         "src/lib/api.ts", "README.md", "src/composer.ts", "backend/app/main.py"]


def test_rank_file_name_matches_beat_path_matches():
    assert chat_files.rank(FILES, "composer", 10) == [
        "src/composer.ts",  # name starts with it — shorter path first
        "docs/composer-notes.md",
        "src/components/chat/Composer.tsx",
    ]


def test_rank_finds_characters_in_order():
    assert chat_files.rank(FILES, "cmpos", 10)[0] == "src/composer.ts"
    assert set(chat_files.rank(FILES, "cmpsr", 10)) == {
        "src/components/chat/Composer.tsx", "src/composer.ts", "docs/composer-notes.md"}
    assert chat_files.rank(FILES, "zzz", 10) == []


def test_rank_empty_query_lists_shallow_files_first_and_respects_the_limit():
    assert chat_files.rank(FILES, "", 2) == ["README.md", "docs/composer-notes.md"]


# ══════════════════════════════════════════════════════════════════════════
# Router
# ══════════════════════════════════════════════════════════════════════════


async def test_router_lists_the_chat_session_folder_relative_to_it(
    auth_client: AsyncClient, make_agent, omp_home
):
    root = omp_home / ".mc" / "workspaces" / "alpha"
    _touch(root, "outside-the-project.txt", "proj/src/Composer.tsx", "proj/README.md",
           "proj/node_modules/x/index.js")
    _write_session(omp_home, "/workspace/proj")
    agent = await make_agent(name="Alpha", slug="alpha", harness="omp",
                             agent_runtime="cli-bridge", workspace_path=str(root))

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/files", params={"q": "comp"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["root"] == "/workspace/proj"
    assert body["files"] == [{"path": "src/Composer.tsx", "name": "Composer.tsx"}]
    assert body["truncated"] is False


async def test_router_without_a_session_folder_lists_the_agent_workspace(
    auth_client: AsyncClient, make_agent, omp_home
):
    root = omp_home / ".mc" / "workspaces" / "rex"
    _touch(root, "a.md", "b/c.md")
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(root))

    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/files")).json()

    assert [f["path"] for f in body["files"]] == ["a.md", "b/c.md"]
    assert body["root"] == "/workspace"


async def test_router_session_folder_outside_the_workspace_is_404(
    auth_client: AsyncClient, make_agent, omp_home
):
    """The agent recorded a folder MC may not read (``/home/agent``): no
    listing — and never the workspace as a stand-in."""
    root = omp_home / ".mc" / "workspaces" / "alpha"
    _touch(root, "a.md")
    _write_session(omp_home, "/home/agent")
    agent = await make_agent(name="Alpha", slug="alpha", harness="omp",
                             agent_runtime="cli-bridge", workspace_path=str(root))

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/files")

    assert resp.status_code == 404
    assert resp.json() == {"reason": "no_workspace"}


async def test_router_host_agent_workspace_outside_the_mc_home_is_404(
    auth_client: AsyncClient, make_agent, omp_home, tmp_path
):
    elsewhere = tmp_path / "elsewhere"
    _touch(elsewhere, "private.txt")
    agent = await make_agent(name="Hosty", agent_runtime="host", workspace_path=str(elsewhere))

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/files")

    assert resp.status_code == 404


async def test_router_caps_the_limit(auth_client: AsyncClient, make_agent, omp_home):
    root = omp_home / ".mc" / "workspaces" / "rex"
    _touch(root, "a.md")
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(root))

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/files", params={"limit": 101})

    assert resp.status_code == 422


async def test_router_requires_auth(client: AsyncClient, make_agent, tmp_path):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(tmp_path))
    assert (await client.get(f"/api/v1/agents/{agent.id}/chat/files")).status_code == 401


# ══════════════════════════════════════════════════════════════════════════
# Critic round 1 — big nested folders, git listing, symlinked roots, cache
# ══════════════════════════════════════════════════════════════════════════


def _git(root: Path, *args: str) -> None:
    import subprocess
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def test_walk_a_big_nested_folder_never_pushes_out_the_top_level_files(tmp_path, omp_home):
    """Breadth first: a deep folder that sorts first (``aaa``) cannot eat the
    whole budget before ``src/`` is reached (critic: nested worktrees)."""
    _touch(tmp_path, *[f"aaa/deep/more/f{i:03d}.txt" for i in range(60)])
    _touch(tmp_path, "src/main.py", "zzz.md")

    files, truncated = chat_files.walk_files(tmp_path, max_scan=20)

    assert "src/main.py" in files and "zzz.md" in files
    assert truncated is True


def test_walk_skips_nested_agent_worktrees(tmp_path, omp_home):
    _touch(tmp_path, "README.md", ".claude/worktrees/w1/README.md",
           ".worktrees/w2/README.md", ".claude/agents/builder.md")

    files, _ = chat_files.walk_files(tmp_path)

    assert sorted(files) == [".claude/agents/builder.md", "README.md"]


def test_git_repo_lists_tracked_and_untracked_files_but_not_ignored_ones(tmp_path, omp_home):
    """In a git repository the listing is ``git ls-files``: .gitignore holds
    (nested worktrees under an ignored ``.claude/``, ``.env``), a nested
    repository is not listed as a file, a tracked symlink is not listed."""
    root = tmp_path / "repo"
    _touch(root, "backend/app.py", ".gitignore", "notes/new.md")
    (root / ".gitignore").write_text(".claude/\n.env\n")
    _touch(root, ".env", *[f".claude/worktrees/w/f{i:03d}.py" for i in range(50)])
    _git(root, "init", "-q")
    os.symlink("/", root / "rootlink")
    _git(root, "add", "backend/app.py", ".gitignore", "rootlink")
    _git(root, "commit", "-qm", "init")
    nested = root / "vendor" / "other"
    _touch(nested, "x.py")
    _git(nested, "init", "-q")

    files, truncated = chat_files.walk_files(root, max_scan=10)

    assert sorted(files) == [".gitignore", "backend/app.py", "notes/new.md"]
    assert truncated is False


def test_git_listing_of_a_subfolder_is_relative_to_it(tmp_path, omp_home):
    root = tmp_path / "repo"
    _touch(root, "top.md", "proj/src/a.py")
    _git(root, "init", "-q")

    files, _ = chat_files.walk_files(root / "proj")

    assert files == ["src/a.py"]


def test_git_listing_says_truncated_at_the_cap(tmp_path, omp_home):
    root = tmp_path / "repo"
    _touch(root, *[f"f{i:02d}.txt" for i in range(10)])
    _git(root, "init", "-q")

    files, truncated = chat_files.walk_files(root, max_scan=4)

    assert len(files) == 4 and truncated is True


def _folders(runtime, root, cwd):
    from app.routers.agent_chat import _ChatFolders
    from app.services import workspace_diff as wd
    return _ChatFolders(runtime=runtime, root=root, cwd=cwd,
                        session_dir=wd.host_path_for_agent_cwd(runtime, root, cwd),
                        boundary=wd.cwd_boundary(runtime, root), running_task=None)


def test_listing_root_refuses_a_docker_session_folder_that_links_outside(tmp_path, omp_home):
    """``/workspace/link`` -> ``/`` (or any folder outside the workspace):
    the resolved folder is no longer the agent's workspace — nothing listed."""
    outside = tmp_path / "outside"
    _touch(outside, "SECRET_NAME.key")
    ws = omp_home / ".mc" / "workspaces" / "alpha"
    _touch(ws, "a.md")
    os.symlink(outside, ws / "link")

    assert chat_files.listing_root(_folders("cli-bridge", ws, "/workspace/link")) is None
    assert chat_files.listing_root(_folders("cli-bridge", ws, "/workspace")) == ws.resolve()


def test_listing_root_refuses_a_host_session_folder_that_links_into_secrets(tmp_path, omp_home):
    mc = omp_home / ".mc"
    _touch(mc, "secrets/api.token", "workspaces/a/ok.md")
    os.symlink(mc / "secrets", mc / "workspaces" / "a" / "s")

    assert chat_files.listing_root(_folders("host", None, str(mc / "workspaces/a/s"))) is None
    assert chat_files.listing_root(_folders("host", None, str(mc / "workspaces/a"))) == \
        (mc / "workspaces" / "a").resolve()


def test_listing_root_refuses_a_host_session_folder_that_links_out_of_the_mc_home(
        tmp_path, omp_home):
    elsewhere = tmp_path / "elsewhere"
    _touch(elsewhere, "private.txt")
    (omp_home / ".mc" / "workspaces").mkdir(parents=True)
    os.symlink(elsewhere, omp_home / ".mc" / "workspaces" / "out")

    assert chat_files.listing_root(_folders("host", None, str(omp_home / ".mc/workspaces/out"))) is None


def test_cached_walk_runs_one_walk_for_parallel_keystrokes(tmp_path, monkeypatch):
    import threading
    import time as _time
    calls = []

    def slow_walk(root, **_kw):
        calls.append(root)
        _time.sleep(0.2)
        return ["a.md"], False

    monkeypatch.setattr(chat_files, "walk_files", slow_walk)
    out: list = []
    threads = [threading.Thread(target=lambda: out.append(chat_files._cached_walk(tmp_path)))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert out == [(["a.md"], False)] * 4


def test_cached_walk_drops_expired_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_files, "walk_files", lambda root, **_kw: ([], False))
    clock = [1000.0]
    monkeypatch.setattr(chat_files.time, "monotonic", lambda: clock[0])
    chat_files._cached_walk(tmp_path / "a")
    clock[0] += chat_files.CACHE_SECONDS + 1
    chat_files._cached_walk(tmp_path / "b")

    assert list(chat_files._cache) == [str(tmp_path / "b")]


def test_rank_in_order_characters_only_match_the_file_name():
    """``ping @boss``: a loose match over the whole path finds something for
    nearly any word — only the file name counts for the in-order match."""
    assert chat_files.rank(["src/components/chat/Composer.tsx"], "chcmp", 10) == []
    assert chat_files.rank(["backend/ops/sessions.py"], "boss", 10) == []
    assert chat_files.rank(["src/components/chat/Composer.tsx"], "cmpsr", 10) == [
        "src/components/chat/Composer.tsx"]
