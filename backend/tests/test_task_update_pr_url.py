"""Operator PATCH accepts pr_url so the PR merge monitor can close the card.

Head cards land in review without a PR link (heads push to a scratch mirror;
the PR is opened later). Whoever opens the PR records it with
``PATCH /boards/{b}/tasks/{t} {"pr_url": ...}``; pr_number follows from the URL.
Only GitHub pull-request URLs are accepted.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token
from app.models.board import Board
from app.models.task import Task
from app.models.user import User

from .conftest import test_engine


async def _setup() -> tuple[Board, Task, dict]:
    user_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="PR Board", slug=f"pr-{uuid.uuid4().hex[:6]}")
        s.add(board)
        s.add(User(id=user_id, email=f"u-{user_id.hex[:6]}@mc.local", name="Op", role="admin", is_active=True))
        await s.commit()
        task = Task(id=uuid.uuid4(), board_id=board.id, title="Head card", status="review")
        s.add(task)
        await s.commit()
    return board, task, {"Authorization": f"Bearer {create_access_token(str(user_id), 'admin')}"}


@pytest.mark.asyncio
async def test_patch_sets_pr_url_and_number(client: AsyncClient):
    board, task, headers = await _setup()
    url = "https://github.com/acme/widgets/pull/724"
    resp = await client.patch(f"/api/v1/boards/{board.id}/tasks/{task.id}", json={"pr_url": url}, headers=headers)
    assert resp.status_code == 200, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        db_task = await s.get(Task, task.id)
    assert db_task.pr_url == url
    assert db_task.pr_number == 724
    assert db_task.status == "review"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "https://github.com/acme/widgets",
    "https://gitlab.com/acme/widgets/-/merge_requests/3",
    "javascript:alert(1)",
])
async def test_patch_rejects_non_pr_urls(client: AsyncClient, bad: str):
    board, task, headers = await _setup()
    resp = await client.patch(f"/api/v1/boards/{board.id}/tasks/{task.id}", json={"pr_url": bad}, headers=headers)
    assert resp.status_code == 422, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        db_task = await s.get(Task, task.id)
    assert db_task.pr_url is None
