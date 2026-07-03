"""Bug 4 — user-side POST /boards/{board_id}/tasks/{task_id}/comments
must respect comment_type from the payload.

Live-Bug: The operator sent {"comment_type": "feedback", ...} via curl/Postman and
the response came back with comment_type='message' — Pydantic silently
dropped the unknown field because CommentCreate didn't declare it.
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_user_and_task(s: AsyncSession):
    from app.models.board import Board
    from app.models.task import Task
    from app.models.user import User
    from app.auth import create_access_token, hash_password

    board = Board(id=uuid.uuid4(), name="Test", slug="t")
    s.add(board)
    user = User(
        id=uuid.uuid4(),
        email="mark@local",
        name="Operator",
        password_hash=hash_password("pw"),
        role="admin",
        is_active=True,
    )
    s.add(user)
    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="T",
        status="inbox",
    )
    s.add(task)
    await s.commit()
    token = create_access_token(str(user.id), user.role)
    return board, task, token


@pytest.mark.asyncio
async def test_user_post_comment_respects_feedback_type(client: AsyncClient):
    """POST with comment_type=feedback → response shows feedback (not message)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, task, token = await _setup_user_and_task(s)

    resp = await client.post(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/comments",
        json={"content": "feedback text", "comment_type": "feedback"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["comment_type"] == "feedback", (
        f"comment_type wurde silent gedroppt — body={body}"
    )
    assert body["content"] == "feedback text"


@pytest.mark.asyncio
async def test_user_post_comment_respects_handoff_type(client: AsyncClient):
    """Cross-bug check (Bug 4 + Bug 9): handoff from the user also works."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, task, token = await _setup_user_and_task(s)

    resp = await client.post(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/comments",
        json={"content": "wake up worker", "comment_type": "handoff"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["comment_type"] == "handoff"


@pytest.mark.asyncio
async def test_user_post_comment_default_message_when_omitted(client: AsyncClient):
    """Backward compat: without comment_type → default 'message'."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, task, token = await _setup_user_and_task(s)

    resp = await client.post(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/comments",
        json={"content": "kein type angegeben"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["comment_type"] == "message"


@pytest.mark.asyncio
async def test_user_post_comment_rejects_unknown_type(client: AsyncClient):
    """Validator rejects when comment_type is not in ALL_COMMENT_TYPES."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, task, token = await _setup_user_and_task(s)

    resp = await client.post(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/comments",
        json={"content": "x", "comment_type": "totally_made_up"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text
