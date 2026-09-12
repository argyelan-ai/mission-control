"""GET /boards/{b}/tasks/{t}/git-branch-diff — the cockpit's Changes column."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_requires_auth(client: AsyncClient):
    resp = await client.get(f"/api/v1/boards/{uuid.uuid4()}/tasks/{uuid.uuid4()}/git-branch-diff")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_404_without_workspace(auth_client: AsyncClient, make_board, make_task):
    board = await make_board(slug="bd-nows")
    task = await make_task(board.id, workspace_path=None)
    resp = await auth_client.get(f"/api/v1/boards/{board.id}/tasks/{task.id}/git-branch-diff")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rejects_option_like_base(auth_client: AsyncClient, make_board, make_task):
    board = await make_board(slug="bd-base")
    task = await make_task(board.id, workspace_path="/tmp/ws")
    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/git-branch-diff", params={"base": "--output=/etc/x"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_returns_service_result(auth_client: AsyncClient, make_board, make_task):
    board = await make_board(slug="bd-ok")
    task = await make_task(board.id, workspace_path="/tmp/ws")
    payload = {"base": "main", "merge_base": "abc", "commits": 2,
               "stats": {"files": 1, "additions": 3, "deletions": 0}, "files": []}
    with patch("app.services.git_service.git_service.get_branch_diff", new=AsyncMock(return_value=payload)) as m:
        resp = await auth_client.get(f"/api/v1/boards/{board.id}/tasks/{task.id}/git-branch-diff")
    assert resp.status_code == 200
    assert resp.json() == payload
    m.assert_awaited_once_with("/tmp/ws", base="main")
