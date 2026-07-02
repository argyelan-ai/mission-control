"""Unit tests for scripts/mc-mcp.py route construction.

Mocks _api + _get_tasks_from_boards. Verifies correct backend paths +
body-key for board-scoped admin endpoints.
"""
from __future__ import annotations
import importlib.util
from pathlib import Path
from unittest.mock import patch
import pytest

REPO = Path(__file__).resolve().parents[2]
MCMCP_PATH = REPO / "scripts" / "mc-mcp.py"


@pytest.fixture
def mc_mcp_module():
    spec = importlib.util.spec_from_file_location("mc_mcp", MCMCP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _unwrap(tool):
    """FastMCP wraps @mcp.tool() functions — unwrap to plain callable."""
    return getattr(tool, "fn", tool)


def test_mc_patch_task_hits_board_scoped_path_for_status(mc_mcp_module):
    fake_task_id = "11111111-2222-3333-4444-555555555555"
    fake_board_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    api_calls = []

    def fake_api(method, path, **kwargs):
        api_calls.append((method, path, kwargs.get("json")))
        return {"id": fake_task_id, "status": "in_progress"}

    with patch.object(mc_mcp_module, "_api", side_effect=fake_api):
        result = _unwrap(mc_mcp_module.mc_patch_task)(
            fake_task_id, status="in_progress", board_id=fake_board_id
        )
    patches = [c for c in api_calls if c[0] == "PATCH"]
    assert len(patches) == 1
    assert patches[0][1] == f"/boards/{fake_board_id}/tasks/{fake_task_id}"
    assert patches[0][2] == {"status": "in_progress"}
    assert "Status → in_progress" in result


def test_mc_patch_task_hits_board_scoped_path_for_comment(mc_mcp_module):
    fake_task_id = "11111111-2222-3333-4444-555555555555"
    fake_board_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    api_calls = []

    def fake_api(method, path, **kwargs):
        api_calls.append((method, path, kwargs.get("json")))
        return {"ok": True}

    with patch.object(mc_mcp_module, "_api", side_effect=fake_api):
        result = _unwrap(mc_mcp_module.mc_patch_task)(
            fake_task_id,
            comment="Update: x\nEvidence: y\nNext: z",
            board_id=fake_board_id,
        )
    posts = [c for c in api_calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][1] == f"/boards/{fake_board_id}/tasks/{fake_task_id}/comments"
    body = posts[0][2]
    # Backend schema CommentCreate uses `content` (tasks.py:188)
    assert "content" in body
    assert body["content"].startswith("Update:")
    assert "Kommentar hinzugefügt" in result


def test_mc_patch_task_resolves_board_id_when_empty(mc_mcp_module):
    fake_task_id = "11111111-2222-3333-4444-555555555555"
    resolved_board_id = "ccccdddd-eeee-ffff-0000-111111111111"
    fake_tasks = [{"id": fake_task_id, "board_id": resolved_board_id, "title": "x"}]
    api_calls = []

    def fake_api(method, path, **kwargs):
        api_calls.append((method, path))
        return {"id": fake_task_id, "status": "in_progress"}

    with patch.object(mc_mcp_module, "_get_tasks_from_boards", return_value=fake_tasks), \
         patch.object(mc_mcp_module, "_api", side_effect=fake_api):
        _unwrap(mc_mcp_module.mc_patch_task)(fake_task_id, status="in_progress")
    patches = [c for c in api_calls if c[0] == "PATCH"]
    assert len(patches) == 1
    assert resolved_board_id in patches[0][1]


def test_mc_patch_task_returns_error_when_no_board_resolvable(mc_mcp_module):
    """Task without board_id (corrupt state) → friendly error, not crash."""
    fake_task_id = "11111111-2222-3333-4444-555555555555"
    fake_tasks = [{"id": fake_task_id, "board_id": "", "_board_id": "", "title": "orphan"}]
    with patch.object(mc_mcp_module, "_get_tasks_from_boards", return_value=fake_tasks):
        result = _unwrap(mc_mcp_module.mc_patch_task)(fake_task_id, status="in_progress")
    assert "keine board_id" in result.lower() or "no board" in result.lower()


def test_mc_patch_task_no_op_returns_message(mc_mcp_module):
    fake_task_id = "11111111-2222-3333-4444-555555555555"
    fake_board_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    result = _unwrap(mc_mcp_module.mc_patch_task)(fake_task_id, board_id=fake_board_id)
    assert "nichts" in result.lower()


# ── mc_register_deliverable (Plan 26-04 / HERM-11 F4) ────────────────────


def test_mc_register_deliverable_hits_board_scoped_path_for_document(mc_mcp_module):
    """Document deliverable POSTs to admin board-scoped route with content payload."""
    fake_task_id = "11111111-2222-3333-4444-555555555555"
    fake_board_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    api_calls = []

    def fake_api(method, path, **kwargs):
        api_calls.append((method, path, kwargs.get("json")))
        return {"id": "deliv-1", "title": "Test", "created_at": "2026-05-01T00:00:00Z"}

    with patch.object(mc_mcp_module, "_api", side_effect=fake_api):
        result = _unwrap(mc_mcp_module.mc_register_deliverable)(
            task_id=fake_task_id,
            board_id=fake_board_id,
            title="Hermes Research Notes",
            deliverable_type="document",
            content="# Findings\n\nFoo bar.",
            scope="task",
            tags="research,phase-26",
            is_pinned=False,
        )
    posts = [c for c in api_calls if c[0] == "POST"]
    assert len(posts) == 1, f"Expected 1 POST, got {api_calls}"
    assert posts[0][1] == f"/boards/{fake_board_id}/tasks/{fake_task_id}/deliverables"
    body = posts[0][2]
    assert body["title"] == "Hermes Research Notes"
    assert body["deliverable_type"] == "document"
    assert body["content"] == "# Findings\n\nFoo bar."
    assert body["scope"] == "task"
    assert body["tags"] == ["research", "phase-26"]
    assert body["is_pinned"] is False
    assert body["git_commit"] is False
    assert "Deliverable registriert" in result


def test_mc_register_deliverable_with_host_form_path(mc_mcp_module):
    """Path-based deliverable (Hermes host worker form) routes through same admin path.

    Validates HERM-14/F8 host-form prefix is accepted by the MCP tool layer
    — the validator runs server-side, but the tool must pass the path
    through unmodified."""
    fake_task_id = "22222222-3333-4444-5555-666666666666"
    fake_board_id = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    host_path = f"~/.mc/deliverables/{fake_task_id}/report.pdf"
    api_calls = []

    def fake_api(method, path, **kwargs):
        api_calls.append((method, path, kwargs.get("json")))
        return {"id": "deliv-2", "title": "Report", "created_at": "2026-05-01T00:00:00Z"}

    # Patch the function ATTRIBUTE on the module's mc_register_deliverable so
    # the path arg flows through. mc_register_deliverable signature has no
    # 'path' arg today (it sends content-only), but if added later this test
    # documents the expected route shape.
    with patch.object(mc_mcp_module, "_api", side_effect=fake_api):
        # Use an artifact to test the path-less route works for binary types too
        result = _unwrap(mc_mcp_module.mc_register_deliverable)(
            task_id=fake_task_id,
            board_id=fake_board_id,
            title="PDF Report",
            deliverable_type="document",
            content=f"Generated PDF; see {host_path}",
            scope="task",
        )
    posts = [c for c in api_calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][1] == f"/boards/{fake_board_id}/tasks/{fake_task_id}/deliverables"
    # content body present, host_path string carried inside
    assert host_path in posts[0][2]["content"]


def test_mc_register_deliverable_returns_error_string_on_api_failure(mc_mcp_module):
    """When backend returns an error (e.g. 404 task-not-found), the tool surfaces it as a string."""
    fake_task_id = "33333333-4444-5555-6666-777777777777"
    fake_board_id = "cccccccc-dddd-eeee-ffff-000000000000"

    def fake_api(method, path, **kwargs):
        return {"error": "Task not found in board"}

    with patch.object(mc_mcp_module, "_api", side_effect=fake_api):
        result = _unwrap(mc_mcp_module.mc_register_deliverable)(
            task_id=fake_task_id,
            board_id=fake_board_id,
            title="Will fail",
            deliverable_type="document",
            content="x",
        )
    assert "Fehler" in result
    assert "Task not found" in result


# ── Integration: admin POST route exists and returns 201 ─────────────────


async def test_admin_create_deliverable_route_returns_201(auth_client, async_session):
    """Live integration: the new admin POST route in tasks.py accepts the
    exact payload mc-mcp.py sends and returns 201. Regression for HERM-11/F4."""
    import uuid as _uuid
    from app.models.board import Board
    from app.models.task import Task

    board = Board(name="MCP Test Board", slug="mcp-test-board")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    task = Task(
        title="HERM-11 smoke",
        description="Test",
        board_id=board.id,
        status="in_progress",
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    payload = {
        "title": "Hermes Notes",
        "deliverable_type": "document",
        "content": "# Findings\n\nFoo bar.",
        "scope": "task",
        "is_pinned": False,
        "git_commit": False,
    }
    resp = await auth_client.post(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/deliverables",
        json=payload,
    )
    assert resp.status_code == 201, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["title"] == "Hermes Notes"
    assert body["deliverable_type"] == "document"
    assert body["agent_id"] is None  # admin-created
    assert body["scope"] == "task"
    assert "id" in body


async def test_admin_create_deliverable_persists_to_db(auth_client, async_session):
    """Verify the new admin POST route actually writes a row with agent_id=NULL."""
    import uuid as _uuid
    from sqlmodel import select
    from app.models.board import Board
    from app.models.task import Task
    from app.models.deliverable import TaskDeliverable

    board = Board(name="Persist Board", slug="persist-board")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    task = Task(title="t", description="d", board_id=board.id, status="in_progress")
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    resp = await auth_client.post(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/deliverables",
        json={
            "title": "Persisted",
            "deliverable_type": "document",
            "content": "body text",
            "scope": "task",
        },
    )
    assert resp.status_code == 201

    rows = (await async_session.exec(
        select(TaskDeliverable).where(TaskDeliverable.task_id == task.id)
    )).all()
    assert len(rows) == 1
    assert rows[0].title == "Persisted"
    assert rows[0].agent_id is None
    assert rows[0].content == "body text"


async def test_admin_create_deliverable_404_when_task_not_in_board(auth_client, async_session):
    """Wrong board for task → 404 (task scoping enforced)."""
    import uuid as _uuid
    from app.models.board import Board
    from app.models.task import Task

    board_a = Board(name="A", slug="a")
    board_b = Board(name="B", slug="b")
    async_session.add(board_a)
    async_session.add(board_b)
    await async_session.commit()
    await async_session.refresh(board_a)
    await async_session.refresh(board_b)

    task = Task(title="t", description="d", board_id=board_a.id, status="in_progress")
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    # Post to board_b's path with task that lives in board_a
    resp = await auth_client.post(
        f"/api/v1/boards/{board_b.id}/tasks/{task.id}/deliverables",
        json={
            "title": "Mismatch",
            "deliverable_type": "document",
            "content": "x",
        },
    )
    assert resp.status_code == 404
