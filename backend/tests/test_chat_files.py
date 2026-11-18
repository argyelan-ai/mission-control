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
    assert chat_files.rank(FILES, "chcmp", 10) == ["src/components/chat/Composer.tsx"]
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
