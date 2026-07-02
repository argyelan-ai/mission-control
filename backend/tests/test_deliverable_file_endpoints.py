"""Tests fuer die neuen Deliverable-File-Endpoints."""
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from tests.conftest import test_engine  # noqa: F401 — needed for SQLite engine shared with auth_client


async def _make_board_task_deliverable(deliverable_type="file", path=None):
    """Hilfsfunktion: Board + Task + Agent + Deliverable in DB anlegen.

    Der Agent-Record wird mitangelegt, weil `_resolve_deliverable_fs_path`
    (in routers/tasks.py, ab bc9087d) den Agent via `deliverable.agent_id`
    lookupt um den Slug-basierten Dateipfad zu derivieren. Ohne Agent-Eintrag
    würde der Resolver None zurueckgeben und alle Endpoints 404 liefern.
    """
    from sqlmodel.ext.asyncio.session import AsyncSession as _AsyncSession
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task
    from app.models.deliverable import TaskDeliverable

    board_id = uuid.uuid4()
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()

    board = Board(id=board_id, name="Test", slug="test")
    task = Task(id=task_id, board_id=board_id, title="T")
    # Agent-Name "test" → Slug "test" → Pfad ~/.mc-deliverables/test/...
    # Fuer File-Tests uebergeben wir absolute Pfade (kein /deliverables/
    # Prefix), der Slug-Pfad ist dann irrelevant, aber der Agent-Lookup
    # muss trotzdem erfolgreich sein.
    agent = Agent(id=agent_id, name="test", role="test")
    deliverable = TaskDeliverable(
        id=deliverable_id,
        task_id=task_id,
        agent_id=agent_id,
        deliverable_type=deliverable_type,
        title="Test File",
        path=path,
    )
    async with _AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(board)
        s.add(task)
        s.add(agent)
        s.add(deliverable)
        await s.commit()
    return board_id, task_id, deliverable_id


@pytest.mark.asyncio
async def test_file_endpoint_serves_file(auth_client: AsyncClient, session, tmp_path):
    """GET /file liefert Dateiinhalt mit korrektem Content-Type."""
    f = tmp_path / "hello.txt"
    f.write_text("Hello MC")

    board_id, task_id, del_id = await _make_board_task_deliverable(path=str(f))

    resp = await auth_client.get(
        f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/file"
    )
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert resp.content == b"Hello MC"


@pytest.mark.asyncio
async def test_file_endpoint_path_traversal_rejected(auth_client: AsyncClient, session, tmp_path):
    """GET /file lehnt Pfade mit .. ab."""
    board_id, task_id, del_id = await _make_board_task_deliverable(path="/etc/../etc/passwd")

    resp = await auth_client.get(
        f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/file"
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_file_endpoint_missing_file_returns_404(auth_client: AsyncClient, session):
    """GET /file gibt 404 wenn Datei nicht existiert."""
    board_id, task_id, del_id = await _make_board_task_deliverable(path="/tmp/does_not_exist_mc_test_12345.txt")

    resp = await auth_client.get(
        f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/file"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_file_endpoint_subpath(auth_client: AsyncClient, session, tmp_path):
    """GET /file?subpath= liefert Datei innerhalb eines Verzeichnis-Deliverables."""
    d = tmp_path / "src"
    d.mkdir()
    (d / "main.py").write_text("print('hi')")

    board_id, task_id, del_id = await _make_board_task_deliverable(path=str(d))

    resp = await auth_client.get(
        f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/file",
        params={"subpath": "main.py"},
    )
    assert resp.status_code == 200
    assert resp.content == b"print('hi')"


@pytest.mark.asyncio
async def test_file_endpoint_subpath_traversal_rejected(auth_client: AsyncClient, session, tmp_path):
    """GET /file?subpath= lehnt Traversal aus dem Root ab."""
    d = tmp_path / "src"
    d.mkdir()

    board_id, task_id, del_id = await _make_board_task_deliverable(path=str(d))

    resp = await auth_client.get(
        f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/file",
        params={"subpath": "../../../etc/passwd"},
    )
    assert resp.status_code == 400


def _make_httpx_mock():
    """Erstellt einen AsyncMock der httpx.AsyncClient.post Methode."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post = AsyncMock(return_value=mock_response)
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = mock_post
    return mock_client, mock_post


def _force_docker_env():
    """Patch context manager: erzwingt in_docker=True im open_deliverable handler.

    Der handler prueft `os.path.exists('/.dockerenv') or sys.platform.startswith('linux')`.
    Im pytest-Lauf (macOS) ist beides False → Code nimmt den subprocess.Popen Pfad
    und httpx wird nie aufgerufen. Fuer die Tests forcen wir den Docker-Pfad
    indem wir sys.platform auf 'linux' patchen.
    """
    return patch("app.routers.tasks.sys.platform", "linux")


@pytest.mark.asyncio
async def test_open_endpoint_reveal(auth_client: AsyncClient, session, tmp_path):
    """POST /open mit reveal=true ruft host-helper mit reveal=True auf (Docker-Pfad)."""
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF")

    board_id, task_id, del_id = await _make_board_task_deliverable(path=str(f))

    mock_client, mock_post = _make_httpx_mock()
    with _force_docker_env(), patch("httpx.AsyncClient", return_value=mock_client):
        resp = await auth_client.post(
            f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/open",
            json={"reveal": True},
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mock_post.assert_called_once_with(
        "http://host.docker.internal:8765/open",
        json={"path": os.path.realpath(str(f)), "reveal": True},
        timeout=3.0,
    )


@pytest.mark.asyncio
async def test_open_endpoint_default_app(auth_client: AsyncClient, session, tmp_path):
    """POST /open mit reveal=false ruft host-helper mit reveal=False auf (Docker-Pfad)."""
    f = tmp_path / "video.mp4"
    f.write_bytes(b"\x00")

    board_id, task_id, del_id = await _make_board_task_deliverable(path=str(f))

    mock_client, mock_post = _make_httpx_mock()
    with _force_docker_env(), patch("httpx.AsyncClient", return_value=mock_client):
        resp = await auth_client.post(
            f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/open",
            json={"reveal": False},
        )

    assert resp.status_code == 200
    mock_post.assert_called_once_with(
        "http://host.docker.internal:8765/open",
        json={"path": os.path.realpath(str(f)), "reveal": False},
        timeout=3.0,
    )


@pytest.mark.asyncio
async def test_open_endpoint_with_subpath(auth_client: AsyncClient, session, tmp_path):
    """POST /open mit subpath oeffnet Datei innerhalb des Deliverable-Verzeichnisses."""
    d = tmp_path / "project"
    d.mkdir()
    (d / "main.py").write_text("x")

    board_id, task_id, del_id = await _make_board_task_deliverable(path=str(d))

    mock_client, mock_post = _make_httpx_mock()
    with _force_docker_env(), patch("httpx.AsyncClient", return_value=mock_client):
        resp = await auth_client.post(
            f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/open",
            json={"reveal": True, "subpath": "main.py"},
        )

    assert resp.status_code == 200
    expected_path = os.path.realpath(str(d / "main.py"))
    mock_post.assert_called_once_with(
        "http://host.docker.internal:8765/open",
        json={"path": expected_path, "reveal": True},
        timeout=3.0,
    )


@pytest.mark.asyncio
async def test_open_endpoint_subpath_traversal_rejected(auth_client: AsyncClient, session, tmp_path):
    """POST /open lehnt Traversal aus dem Root ab."""
    d = tmp_path / "project"
    d.mkdir()

    board_id, task_id, del_id = await _make_board_task_deliverable(path=str(d))

    resp = await auth_client.post(
        f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/open",
        json={"reveal": True, "subpath": "../../etc/passwd"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_directory_endpoint_lists_entries(auth_client: AsyncClient, session, tmp_path):
    """GET /directory listet Dateien und Ordner auf."""
    d = tmp_path / "src"
    d.mkdir()
    (d / "main.py").write_text("x" * 100)
    (d / "utils").mkdir()

    board_id, task_id, del_id = await _make_board_task_deliverable(path=str(d))

    resp = await auth_client.get(
        f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/directory"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["root_path"] == os.path.realpath(str(d))
    assert data["current_path"] == ""
    names = {e["name"] for e in data["entries"]}
    assert names == {"main.py", "utils"}
    main = next(e for e in data["entries"] if e["name"] == "main.py")
    assert main["type"] == "file"
    assert main["size"] == 100
    utils = next(e for e in data["entries"] if e["name"] == "utils")
    assert utils["type"] == "directory"
    assert utils["size"] is None


@pytest.mark.asyncio
async def test_directory_endpoint_subpath(auth_client: AsyncClient, session, tmp_path):
    """GET /directory?subpath= navigiert in Unterordner."""
    d = tmp_path / "src"
    d.mkdir()
    (d / "components").mkdir()
    (d / "components" / "Button.tsx").write_text("export default Button")

    board_id, task_id, del_id = await _make_board_task_deliverable(path=str(d))

    resp = await auth_client.get(
        f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/directory",
        params={"subpath": "components"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["current_path"] == "components"
    assert len(data["entries"]) == 1
    assert data["entries"][0]["name"] == "Button.tsx"


@pytest.mark.asyncio
async def test_directory_endpoint_traversal_rejected(auth_client: AsyncClient, session, tmp_path):
    """GET /directory lehnt Traversal-Subpath ab."""
    d = tmp_path / "src"
    d.mkdir()

    board_id, task_id, del_id = await _make_board_task_deliverable(path=str(d))

    resp = await auth_client.get(
        f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/directory",
        params={"subpath": "../../etc"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_directory_endpoint_requires_directory(auth_client: AsyncClient, session, tmp_path):
    """GET /directory gibt 400 wenn Deliverable-Pfad keine Directory ist."""
    f = tmp_path / "file.txt"
    f.write_text("hello")

    board_id, task_id, del_id = await _make_board_task_deliverable(path=str(f))

    resp = await auth_client.get(
        f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/directory"
    )
    assert resp.status_code == 400
