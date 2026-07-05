"""Read-only Task-Workspace browsing endpoints (workspace/list, workspace/content)."""
import os
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.board import Board
from app.models.task import Task

from tests.conftest import test_engine


@pytest.fixture
def workspaces_root(tmp_path, monkeypatch):
    """Redirect mc_home() (and thus the ``workspaces`` FsRoot) to a temp dir."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    return tmp_path / ".mc" / "workspaces"


async def _mk_task(workspace_path: str | None):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}",
                      auto_dispatch_enabled=False)
        s.add(board)
        await s.commit()
        task = Task(board_id=board.id, title="T", status="inbox", workspace_path=workspace_path)
        s.add(task)
        await s.commit()
        await s.refresh(task)
        return board, task


def _seed(root, sub="proj"):
    ws = root / sub
    (ws / "src").mkdir(parents=True)
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("gitconfig")
    (ws / "src" / "main.py").write_text("print(1)")
    (ws / ".env").write_text("SECRET=1")
    (ws / "README.md").write_text("# hi")
    return ws


@pytest.mark.asyncio
async def test_list_happy_path(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/list"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is True
    names = {e["name"] for e in data["entries"]}
    assert names == {"src", "README.md"}
    assert ".git" not in names
    assert ".env" not in names


@pytest.mark.asyncio
async def test_list_subpath(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/list",
        params={"subpath": "src"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is True
    assert {e["name"] for e in data["entries"]} == {"main.py"}


@pytest.mark.asyncio
async def test_exists_false_when_workspace_path_none(auth_client: AsyncClient, workspaces_root):
    board, task = await _mk_task(None)
    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/list"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"exists": False, "subpath": "", "entries": []}


@pytest.mark.asyncio
async def test_exists_false_when_directory_deleted(auth_client: AsyncClient, workspaces_root, tmp_path):
    ws = workspaces_root / "gone"
    ws.mkdir(parents=True)
    board, task = await _mk_task(str(ws))
    ws.rmdir()

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/list"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is False


@pytest.mark.asyncio
async def test_list_traversal_rejected(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/list",
        params={"subpath": "../.."},
    )
    assert resp.status_code in (400, 404)


@pytest.mark.asyncio
async def test_list_filtered_subpath_404(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/list",
        params={"subpath": ".git"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_workspace_outside_workspaces_root_exists_false(auth_client: AsyncClient, workspaces_root, tmp_path):
    outside = tmp_path / "etc"
    outside.mkdir()
    board, task = await _mk_task(str(outside))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/list"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is False


@pytest.mark.asyncio
async def test_content_happy_path(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/content",
        params={"subpath": "README.md"},
    )
    assert resp.status_code == 200
    assert resp.content == b"# hi"
    assert "attachment" not in resp.headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_content_download_attachment(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/content",
        params={"subpath": "README.md", "download": "true"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("content-disposition", "").startswith("attachment")


@pytest.mark.asyncio
async def test_content_env_file_404(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/content",
        params={"subpath": ".env"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_content_nested_git_file_404(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/content",
        params={"subpath": ".git/config"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_content_traversal_rejected(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/content",
        params={"subpath": "../../etc/passwd"},
    )
    assert resp.status_code in (400, 404)


@pytest.mark.asyncio
async def test_task_not_in_board_404(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    _, task = await _mk_task(str(ws))
    other_board_id = uuid.uuid4()

    resp = await auth_client.get(
        f"/api/v1/boards/{other_board_id}/tasks/{task.id}/workspace/list"
    )
    assert resp.status_code == 404


# ── C1: symlink escape of the credential filter ─────────────────────────────
# Without the fix, a symlink whose *name* passes ``_path_is_filtered`` but
# whose *target* resolves to a filtered/sensitive file leaks that file: the
# old code only ever checked the requested subpath string, never whether any
# segment on the way there was a symlink.

@pytest.mark.asyncio
async def test_list_symlink_to_sensitive_file_hidden(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    os.symlink(ws / ".env", ws / "harmless.txt")
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/list")
    assert resp.status_code == 200
    names = {e["name"] for e in resp.json()["entries"]}
    assert "harmless.txt" not in names


@pytest.mark.asyncio
async def test_content_symlink_to_sensitive_file_404(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    os.symlink(ws / ".env", ws / "harmless.txt")
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/content",
        params={"subpath": "harmless.txt"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_symlinked_directory_not_navigable(auth_client: AsyncClient, workspaces_root):
    """A symlinked directory must not be listed as a navigable folder either."""
    ws = _seed(workspaces_root)
    other = workspaces_root / "other_dir"
    other.mkdir()
    (other / "secret.txt").write_text("nope")
    os.symlink(other, ws / "linked_dir")
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/list")
    assert resp.status_code == 200
    names = {e["name"] for e in resp.json()["entries"]}
    assert "linked_dir" not in names


@pytest.mark.asyncio
async def test_content_symlink_outside_root_still_blocked(auth_client: AsyncClient, workspaces_root):
    """Regression guard: a symlink pointing outside the workspace root must
    stay blocked exactly like before (previously via ``is_relative_to``, now
    also via the upfront symlink rejection)."""
    ws = _seed(workspaces_root)
    os.symlink("/etc/hosts", ws / "x.txt")
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/content",
        params={"subpath": "x.txt"},
    )
    assert resp.status_code in (400, 404)


# ── C2: case-sensitive denylist on a case-insensitive filesystem (APFS) ─────
# Without the fix, the deny checks compare names verbatim. On APFS a request
# for ``ID_RSA``/``.ENV``/``server.PEM`` bypasses the (case-sensitive) filter
# string-match, then the filesystem still opens the same-named lowercase
# file because APFS treats the names as identical — a leak.

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "real_name,requested",
    [("id_rsa", "ID_RSA"), (".env", ".ENV"), ("server.pem", "server.PEM")],
)
async def test_content_case_insensitive_sensitive_names_404(
    auth_client: AsyncClient, workspaces_root, real_name, requested
):
    ws = _seed(workspaces_root)
    (ws / real_name).write_text("secret")
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/content",
        params={"subpath": requested},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_content_case_insensitive_skip_dir_404(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/content",
        params={"subpath": ".Git/config"},
    )
    assert resp.status_code == 404


# ── MINOR: nosniff header on file content responses ─────────────────────────

@pytest.mark.asyncio
async def test_content_nosniff_header(auth_client: AsyncClient, workspaces_root):
    ws = _seed(workspaces_root)
    board, task = await _mk_task(str(ws))

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/workspace/content",
        params={"subpath": "README.md"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("x-content-type-options") == "nosniff"
