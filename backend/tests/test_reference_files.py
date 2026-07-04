"""ADR-053: Referenz-Dateien für Tasks & Projekte (Upload, Vererbung, Dispatch)."""
import io
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.board import Board, Project
from app.models.reference_file import ReferenceFile
from app.models.task import Task

from tests.conftest import test_engine


@pytest.fixture
def refs_root(tmp_path, monkeypatch):
    """Alle mc_home()-Aufrufe auf ein Temp-Verzeichnis umbiegen."""
    from app.config import settings
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    return tmp_path / ".mc" / "references"


async def _mk_entities(with_project=True):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}",
                      auto_dispatch_enabled=False)
        s.add(board)
        await s.commit()
        project = None
        if with_project:
            project = Project(board_id=board.id, name="P")
            s.add(project)
            await s.commit()
            await s.refresh(project)
        task = Task(board_id=board.id, title="T", status="inbox",
                    project_id=project.id if project else None)
        s.add(task)
        await s.commit()
        await s.refresh(task)
        return board, project, task


def _png(name="beispiel.png", content=b"\x89PNG fakebytes"):
    return {"file": (name, io.BytesIO(content), "image/png")}


@pytest.mark.asyncio
async def test_upload_list_download_delete_roundtrip(auth_client: AsyncClient, refs_root):
    _, _, task = await _mk_entities()

    r = await auth_client.post(
        "/api/v1/references/upload",
        files=_png(),
        data={"task_id": str(task.id), "note": "So soll das Layout aussehen"},
    )
    assert r.status_code == 201, r.text
    ref = r.json()
    assert ref["original_name"] == "beispiel.png"
    assert ref["note"] == "So soll das Layout aussehen"
    assert ref["abs_path"].endswith(ref["rel_path"])
    assert (refs_root / ref["rel_path"]).is_file()

    r2 = await auth_client.get(f"/api/v1/references?task_id={task.id}")
    assert [x["id"] for x in r2.json()] == [ref["id"]]

    r3 = await auth_client.get(f"/api/v1/references/{ref['id']}/download")
    assert r3.status_code == 200
    assert r3.content == b"\x89PNG fakebytes"
    assert "attachment" in r3.headers.get("content-disposition", "")

    r4 = await auth_client.delete(f"/api/v1/references/{ref['id']}")
    assert r4.status_code == 204
    assert not (refs_root / ref["rel_path"]).exists()


@pytest.mark.asyncio
async def test_upload_validation(auth_client: AsyncClient, refs_root):
    _, _, task = await _mk_entities()

    # MIME nicht erlaubt
    r = await auth_client.post(
        "/api/v1/references/upload",
        files={"file": ("x.exe", io.BytesIO(b"MZ"), "application/x-msdownload")},
        data={"task_id": str(task.id)},
    )
    assert r.status_code == 415

    # Path-Traversal im Namen
    r2 = await auth_client.post(
        "/api/v1/references/upload",
        files={"file": ("../../etc-passwd.png", io.BytesIO(b"x"), "image/png")},
        data={"task_id": str(task.id)},
    )
    assert r2.status_code == 400

    # task_id UND project_id → 400
    r3 = await auth_client.post(
        "/api/v1/references/upload",
        files=_png(),
        data={"task_id": str(task.id), "project_id": str(uuid.uuid4())},
    )
    assert r3.status_code == 400


@pytest.mark.asyncio
async def test_task_list_inherits_project_references(auth_client: AsyncClient, refs_root):
    _, project, task = await _mk_entities()

    rp = await auth_client.post(
        "/api/v1/references/upload", files=_png("projekt-logo.png"),
        data={"project_id": str(project.id)},
    )
    rt = await auth_client.post(
        "/api/v1/references/upload", files=_png("task-mock.png"),
        data={"task_id": str(task.id)},
    )
    assert rp.status_code == 201 and rt.status_code == 201

    r = await auth_client.get(f"/api/v1/references?task_id={task.id}")
    body = r.json()
    assert len(body) == 2
    own = [x for x in body if not x["inherited"]]
    inh = [x for x in body if x["inherited"]]
    assert own[0]["original_name"] == "task-mock.png"
    assert inh[0]["original_name"] == "projekt-logo.png"


@pytest.mark.asyncio
async def test_dispatch_context_lists_reference_paths(refs_root, fake_redis, auth_client):
    from app.models.agent import Agent
    from app.services.task_context_builder import _load_dispatch_context

    board, project, task = await _mk_entities()
    await auth_client.post(
        "/api/v1/references/upload", files=_png("vorlage.png"),
        data={"task_id": str(task.id), "note": "Referenz-Layout"},
    )
    await auth_client.post(
        "/api/v1/references/upload", files=_png("brand.png"),
        data={"project_id": str(project.id)},
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(id=uuid.uuid4(), name="W", role="Developer",
                      board_id=board.id, agent_runtime="host", model="x")
        s.add(agent)
        await s.commit()
        t = await s.get(Task, task.id)
        ctx = await _load_dispatch_context(t, agent, s)

    assert "vorlage.png" in ctx.reference_files_context
    assert "brand.png" in ctx.reference_files_context  # vom Projekt geerbt
    assert "Referenz-Layout" in ctx.reference_files_context
    assert str(refs_root) in ctx.reference_files_context  # absolute Pfade


@pytest.mark.asyncio
async def test_html_and_svg_uploads_rejected(auth_client: AsyncClient, refs_root):
    """Review M1: aktive Inhalte wären Stored XSS via inline-servierendem
    Files-Browser — html/svg sind hart verboten."""
    _, _, task = await _mk_entities()
    for name, mime in (("x.html", "text/html"), ("x.svg", "image/svg+xml")):
        r = await auth_client.post(
            "/api/v1/references/upload",
            files={"file": (name, io.BytesIO(b"<svg/>"), mime)},
            data={"task_id": str(task.id)},
        )
        assert r.status_code == 415, f"{mime} muss abgelehnt werden"


@pytest.mark.asyncio
async def test_defer_dispatch_skips_auto_dispatch_then_manual(auth_client: AsyncClient, refs_root):
    """Review C2: defer_dispatch verhindert das Rennen Dispatch vs. Upload;
    POST /dispatch holt den Dispatch danach explizit nach."""
    from unittest.mock import AsyncMock, patch as _patch

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}",
                      auto_dispatch_enabled=True)
        s.add(board)
        await s.commit()

    with _patch("app.routers.tasks.auto_dispatch_task", new=AsyncMock()) as adt:
        r = await auth_client.post(f"/api/v1/boards/{board.id}/tasks", json={
            "title": "Mit Referenzen", "defer_dispatch": True,
        })
        assert r.status_code in (200, 201), r.text
        task_id = r.json()["id"]
        adt.assert_not_called()  # kein Auto-Dispatch trotz auto_dispatch_enabled

        r2 = await auth_client.post(f"/api/v1/boards/{board.id}/tasks/{task_id}/dispatch")
        assert r2.status_code == 200, r2.text
        adt.assert_called_once()

        # Doppel-Dispatch-Guard: nach dispatched_at → 409
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(Task, uuid.UUID(task_id))
            from app.utils import utcnow
            t.dispatched_at = utcnow()
            s.add(t)
            await s.commit()
        r3 = await auth_client.post(f"/api/v1/boards/{board.id}/tasks/{task_id}/dispatch")
        assert r3.status_code == 409


@pytest.mark.asyncio
async def test_compose_renderer_mounts_references_for_all_agents():
    """Review C1: ohne den Mount sind die Directive-Pfade im Docker-Fleet tot."""
    from app.services.compose_renderer import (
        _build_new_agent_block, _ensure_references_volume,
    )

    block = _build_new_agent_block("worker", "mc-claude-agent:latest", False)
    assert "${HOME}/.mc/references:${HOME}/.mc/references:ro" in block

    # Bestehender Service-Body ohne references-Mount → wird injiziert (idempotent)
    body = [
        "    environment:",
        "      - AGENT_NAME=worker",
        "    volumes:",
        "      - ${HOME}/.mc/workspaces/worker:/workspace",
    ]
    once = _ensure_references_volume(body)
    assert any("/.mc/references:" in l for l in once)
    assert _ensure_references_volume(once) == once  # idempotent


@pytest.mark.asyncio
async def test_task_delete_removes_references(auth_client: AsyncClient, refs_root):
    board, _, task = await _mk_entities()
    r = await auth_client.post(
        "/api/v1/references/upload", files=_png(),
        data={"task_id": str(task.id)},
    )
    rel = r.json()["rel_path"]
    assert (refs_root / rel).is_file()

    rd = await auth_client.delete(f"/api/v1/boards/{board.id}/tasks/{task.id}")
    assert rd.status_code in (200, 204), rd.text

    assert not (refs_root / rel).exists()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(
            select(ReferenceFile).where(ReferenceFile.task_id == task.id)
        )).all()
        # Live-Smoke-Fund: file_index.task_id-FK blockte den Task-Delete auf
        # Postgres (500) — Index-Provenance muss gelöst/entfernt sein.
        from app.models.file_index import FileIndexEntry
        idx = (await s.exec(
            select(FileIndexEntry).where(FileIndexEntry.task_id == task.id)
        )).all()
    assert rows == []
    assert idx == []


@pytest.mark.asyncio
async def test_reference_delete_cleans_file_index(auth_client: AsyncClient, refs_root):
    _, _, task = await _mk_entities()
    r = await auth_client.post(
        "/api/v1/references/upload", files=_png(),
        data={"task_id": str(task.id)},
    )
    ref = r.json()
    from app.models.file_index import FileIndexEntry
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        before = (await s.exec(
            select(FileIndexEntry).where(FileIndexEntry.rel_path == ref["rel_path"])
        )).all()
    assert len(before) == 1  # capture-at-write hat indexiert

    await auth_client.delete(f"/api/v1/references/{ref['id']}")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        after = (await s.exec(
            select(FileIndexEntry).where(FileIndexEntry.rel_path == ref["rel_path"])
        )).all()
    assert after == []
