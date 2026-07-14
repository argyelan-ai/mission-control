"""Tests for the global Files router (/api/v1/files)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings


def _seed_vault(tmp_path):
    vault = tmp_path / ".mc" / "vault"
    sub = vault / "sub"
    sub.mkdir(parents=True)
    (vault / "note.md").write_text("# Hello MC")
    (sub / "deep.txt").write_text("deep")
    return vault


def _seed_deliverable_dirs(tmp_path, names):
    """Create bare directories under ~/.mc/deliverables (the on-disk task layout)."""
    root = tmp_path / ".mc" / "deliverables"
    root.mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / n).mkdir(exist_ok=True)
    return root


async def test_roots_excludes_sensitive(auth_client: AsyncClient):
    resp = await auth_client.get("/api/v1/files/roots")
    assert resp.status_code == 200
    keys = {r["key"] for r in resp.json()["roots"]}
    assert "vault" in keys
    assert "deliverables" in keys
    for sensitive in ("secrets", "agents", "logs", "backups", "browser-profiles"):
        assert sensitive not in keys


async def test_roots_expose_deletable_flag(auth_client: AsyncClient):
    """The delete-gating UI depends on a per-root `deletable` boolean in /roots."""
    resp = await auth_client.get("/api/v1/files/roots")
    assert resp.status_code == 200
    roots = {r["key"]: r for r in resp.json()["roots"]}
    assert all("deletable" in r for r in roots.values()), "every root must carry deletable"
    for k in ("deliverables", "media", "shared-artifacts", "mcp-screenshots", "storyboard-images"):
        assert roots[k]["deletable"] is True, f"{k} must be deletable"
    for k in ("vault", "workspaces", "attachments", "shared-deliverables"):
        assert roots[k]["deletable"] is False, f"{k} must NOT be deletable"


async def test_unauthenticated_rejected(client: AsyncClient):
    resp = await client.get("/api/v1/files/roots")
    assert resp.status_code == 401


async def test_list_lists_files(auth_client: AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_vault(tmp_path)
    resp = await auth_client.get("/api/v1/files/list", params={"root": "vault", "subpath": ""})
    assert resp.status_code == 200
    data = resp.json()
    names = {e["name"] for e in data["entries"]}
    assert names == {"sub", "note.md"}
    # dirs first
    assert data["entries"][0]["name"] == "sub"
    assert data["entries"][0]["is_directory"] is True


async def test_list_traversal_rejected(auth_client: AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_vault(tmp_path)
    resp = await auth_client.get(
        "/api/v1/files/list", params={"root": "vault", "subpath": "../../etc"}
    )
    assert resp.status_code == 400


async def test_list_sensitive_root_404(auth_client: AsyncClient):
    for bad in ("secrets", "agents", "does-not-exist"):
        resp = await auth_client.get("/api/v1/files/list", params={"root": bad, "subpath": ""})
        assert resp.status_code == 404, bad


async def test_content_download_sets_attachment(auth_client: AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_vault(tmp_path)
    resp = await auth_client.get(
        "/api/v1/files/content",
        params={"root": "vault", "subpath": "note.md", "download": "true"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("content-disposition", "").startswith("attachment")
    assert resp.content == b"# Hello MC"


async def test_content_inline_no_attachment(auth_client: AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_vault(tmp_path)
    resp = await auth_client.get(
        "/api/v1/files/content", params={"root": "vault", "subpath": "note.md"}
    )
    assert resp.status_code == 200
    assert "attachment" not in resp.headers.get("content-disposition", "")


async def test_meta_native_open_hidden_when_unreachable(auth_client: AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_vault(tmp_path)

    async def _unreachable():
        return False

    monkeypatch.setattr("app.routers.files._native_open_reachable", _unreachable)
    resp = await auth_client.get(
        "/api/v1/files/meta", params={"root": "vault", "subpath": "note.md"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["native_open_available"] is False
    assert data["reachable"] is True
    assert data["is_directory"] is False


async def test_search_finds_indexed_file(auth_client: AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_vault(tmp_path)
    # populate the index
    r = await auth_client.post("/api/v1/files/reindex")
    assert r.status_code == 200
    resp = await auth_client.get("/api/v1/files/search", params={"q": "note", "root": "vault"})
    assert resp.status_code == 200
    names = {x["name"] for x in resp.json()["results"]}
    assert "note.md" in names


# --- /list task-UUID resolution (deliverables root) -------------------------


async def _seed_board_agent_task(session: AsyncSession, *, title: str, agent_name: str | None):
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board = Board(name="B", slug="b")
    session.add(board)
    await session.commit()
    await session.refresh(board)

    agent_id = None
    if agent_name is not None:
        agent = Agent(name=agent_name, board_id=board.id, role="developer")
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        agent_id = agent.id

    task = Task(board_id=board.id, title=title, assigned_agent_id=agent_id)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def test_list_resolves_task_uuid_dir(
    auth_client: AsyncClient, session: AsyncSession, tmp_path, monkeypatch
):
    """A deliverables dir named after a live task UUID gets a human label + slug."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    task = await _seed_board_agent_task(session, title="Build the thing", agent_name="Cody")

    _seed_deliverable_dirs(tmp_path, [str(task.id)])
    resp = await auth_client.get(
        "/api/v1/files/list", params={"root": "deliverables", "subpath": ""}
    )
    assert resp.status_code == 200
    entry = {e["name"]: e for e in resp.json()["entries"]}[str(task.id)]
    assert entry["display_name"] == "Build the thing"
    assert entry["agent_slug"] == "cody"  # Agent.slug auto-filled from name
    assert entry["task_id"] == str(task.id)


async def test_list_deleted_task_uuid_stays_null(
    auth_client: AsyncClient, tmp_path, monkeypatch
):
    """A UUID dir whose task no longer exists resolves to all-null (not an error)."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    orphan = uuid.uuid4()
    _seed_deliverable_dirs(tmp_path, [str(orphan)])
    resp = await auth_client.get(
        "/api/v1/files/list", params={"root": "deliverables", "subpath": ""}
    )
    assert resp.status_code == 200
    entry = {e["name"]: e for e in resp.json()["entries"]}[str(orphan)]
    assert entry["display_name"] is None
    assert entry["agent_slug"] is None
    assert entry["task_id"] is None


async def test_list_non_uuid_dir_stays_null(
    auth_client: AsyncClient, tmp_path, monkeypatch
):
    """A non-UUID directory name under deliverables is never resolved."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_deliverable_dirs(tmp_path, ["reports"])
    resp = await auth_client.get(
        "/api/v1/files/list", params={"root": "deliverables", "subpath": ""}
    )
    assert resp.status_code == 200
    entry = {e["name"]: e for e in resp.json()["entries"]}["reports"]
    assert entry["display_name"] is None
    assert entry["agent_slug"] is None
    assert entry["task_id"] is None


async def test_list_task_without_agent_resolves_title_only(
    auth_client: AsyncClient, session: AsyncSession, tmp_path, monkeypatch
):
    """Title resolves even when the task has no assigned agent → agent_slug null."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    task = await _seed_board_agent_task(session, title="Orphaned work", agent_name=None)
    _seed_deliverable_dirs(tmp_path, [str(task.id)])
    resp = await auth_client.get(
        "/api/v1/files/list", params={"root": "deliverables", "subpath": ""}
    )
    assert resp.status_code == 200
    entry = {e["name"]: e for e in resp.json()["entries"]}[str(task.id)]
    assert entry["display_name"] == "Orphaned work"
    assert entry["agent_slug"] is None
    assert entry["task_id"] == str(task.id)


async def test_list_other_root_never_resolves(
    auth_client: AsyncClient, tmp_path, monkeypatch
):
    """UUID-named dirs under a non-deliverables root are never resolved."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_vault(tmp_path)
    (tmp_path / ".mc" / "vault" / str(uuid.uuid4())).mkdir()
    resp = await auth_client.get(
        "/api/v1/files/list", params={"root": "vault", "subpath": ""}
    )
    assert resp.status_code == 200
    for e in resp.json()["entries"]:
        assert e["display_name"] is None
        assert e["agent_slug"] is None
        assert e["task_id"] is None


async def test_list_resolution_is_batched_no_n_plus_1(
    auth_client: AsyncClient, session: AsyncSession, tmp_path, monkeypatch
):
    """N UUID dirs must cost ONE tasks query + ONE agents query, not N+1."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board = Board(name="B", slug="b")
    session.add(board)
    await session.commit()
    await session.refresh(board)
    agent = Agent(name="Cody", board_id=board.id, role="developer")
    session.add(agent)
    await session.commit()
    await session.refresh(agent)

    names = []
    for i in range(6):
        t = Task(board_id=board.id, title=f"T{i}", assigned_agent_id=agent.id)
        session.add(t)
        await session.commit()
        await session.refresh(t)
        names.append(str(t.id))
    _seed_deliverable_dirs(tmp_path, names)

    counts = {"tasks": 0, "agents": 0}

    def _count(conn, cursor, statement, params, context, executemany):
        low = statement.lower()
        if "from tasks" in low:
            counts["tasks"] += 1
        if "from agents" in low:
            counts["agents"] += 1

    sync_engine = session.bind.sync_engine  # type: ignore[union-attr]
    event.listen(sync_engine, "before_cursor_execute", _count)
    try:
        resp = await auth_client.get(
            "/api/v1/files/list", params={"root": "deliverables", "subpath": ""}
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", _count)

    assert resp.status_code == 200
    assert counts["tasks"] == 1, counts
    assert counts["agents"] == 1, counts


# --- /search ?type= friendly-group filter -----------------------------------


def _seed_typed_files(tmp_path):
    """Seed one file per type-group under the media root (host-backed, indexable)."""
    root = tmp_path / ".mc" / "media"
    root.mkdir(parents=True, exist_ok=True)
    files = [
        "photo.png",      # image/png
        "clip.mp4",       # video/mp4
        "song.mp3",       # audio/mpeg
        "report.pdf",     # application/pdf
        "readme.md",      # markdown
        "main.py",        # code (mime text/x-python)
        "widget.tsx",     # code (mime often NULL — extension-only match)
        "styles.css",     # code (text/css)
        "config.json",    # code (application/json)
        "Dockerfile",     # code (no extension → exact-name match)
        "notes.txt",      # plain text — matches no friendly group
    ]
    for f in files:
        (root / f).write_text("x")
    return root


async def _search_names(auth_client, type_str):
    resp = await auth_client.get(
        "/api/v1/files/search", params={"type": type_str, "root": "media"}
    )
    assert resp.status_code == 200, resp.text
    return {r["name"] for r in resp.json()["results"]}


@pytest.mark.parametrize(
    "type_str,expected",
    [
        ("image", {"photo.png"}),
        ("video", {"clip.mp4"}),
        ("audio", {"song.mp3"}),
        ("pdf", {"report.pdf"}),
        ("markdown", {"readme.md"}),
        ("code", {"main.py", "widget.tsx", "styles.css", "config.json", "Dockerfile"}),
    ],
)
async def test_search_type_groups(
    auth_client: AsyncClient, tmp_path, monkeypatch, type_str, expected
):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_typed_files(tmp_path)
    r = await auth_client.post("/api/v1/files/reindex")
    assert r.status_code == 200
    names = await _search_names(auth_client, type_str)
    assert expected <= names, f"{type_str}: missing {expected - names}"
    # never leak across groups: notes.txt is in no friendly group
    assert "notes.txt" not in names
    # markdown must not be swept into code, and vice-versa
    if type_str == "code":
        assert "readme.md" not in names
    if type_str == "markdown":
        assert "main.py" not in names


async def test_search_unknown_type_falls_back_to_mime_substring(
    auth_client: AsyncClient, tmp_path, monkeypatch
):
    """Backward-compat: an unmapped value still matches as a raw mime substring."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_typed_files(tmp_path)
    r = await auth_client.post("/api/v1/files/reindex")
    assert r.status_code == 200
    # "python" is not a friendly group → legacy mime substring hits text/x-python
    names = await _search_names(auth_client, "python")
    assert "main.py" in names
    # and it must NOT accidentally pull unrelated files
    assert "photo.png" not in names


async def test_open_container_only_root_409(auth_client: AsyncClient):
    # shared-deliverables (Docker named volume) has no host path → can't reveal
    resp = await auth_client.post(
        "/api/v1/files/open", json={"root": "shared-deliverables", "subpath": "x", "reveal": True}
    )
    assert resp.status_code in (404, 409)  # 404 if path missing, 409 container-only
