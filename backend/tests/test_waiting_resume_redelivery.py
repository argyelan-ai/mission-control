"""waiting→in_progress must reach the assigned agent (09.09.2026 incident).

A task parked via `mc park` (or by the operator for a deploy window) sits in
`waiting`. When the operator flips it back to `in_progress`, nothing used to
happen on the agent side: no comment, no redispatch. The lead sat next to its
own in_progress task with an empty prompt for two hours until a manual
restart triggered the startup recovery.

The fix reuses the blocked→in_progress ladder (`resolve_unblock_action`):
alive+idle agent → "continue" comment (delivered via poll), dead agent →
full redispatch, busy agent → requeue. These tests mirror
test_unblock_liveness_redispatch.py for the `waiting` origin state and the
operator PATCH path.
"""
import asyncio
import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token, generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment
from app.models.user import User
from tests.conftest import test_engine


async def _setup(session: AsyncSession, *, target_last_seen: dt.datetime | None, origin: str = "waiting"):
    board = Board(name="Park Board", slug=f"park-{uuid.uuid4().hex[:8]}", blocker_triage_minutes=0)
    session.add(board)
    await session.commit()
    await session.refresh(board)

    _, target_hash = generate_agent_token()
    target = Agent(
        name="the lead",
        role="lead",
        board_id=board.id,
        agent_token_hash=target_hash,
        is_board_lead=True,
        scopes=["tasks:read", "tasks:write", "tasks:manage"],
        last_seen_at=target_last_seen,
        heartbeat_config={"interval": "5m"},
    )
    session.add(target)
    await session.commit()
    await session.refresh(target)

    task = Task(
        board_id=board.id,
        assigned_agent_id=target.id,
        title="Parked probe",
        status=origin,
        dispatched_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=30),
        ack_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=30),
    )
    session.add(task)
    target.current_task_id = task.id
    session.add(target)
    await session.commit()
    await session.refresh(task)

    admin_id = uuid.uuid4()
    session.add(User(id=admin_id, email=f"op-{admin_id.hex[:6]}@mc.local", name="Op", role="admin", is_active=True))
    await session.commit()
    return board, target, task, create_access_token(str(admin_id), "admin")


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["waiting", "blocked"])
async def test_operator_resume_posts_continue_comment(client: AsyncClient, async_session, origin):
    """Alive + idle agent → the resume is delivered as a system_notify
    comment (poll.sh pastes new comments into the pane).

    `blocked` is covered too: the operator path added the comment but never
    committed it (latent since W2-B), so the UI unblock button silently
    reached nobody either."""
    fresh = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=10)
    board, target, task, admin_token = await _setup(async_session, target_last_seen=fresh, origin=origin)

    with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch:
        resp = await client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200, resp.text
    mock_dispatch.assert_not_called()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.status == "in_progress"
        comments = (await s.exec(select(TaskComment).where(TaskComment.task_id == task.id))).all()
        notify = [c for c in comments if c.comment_type in ("unblock_notify", "system_notify") and "UNBLOCKED" in c.content]
        assert notify, "resume from waiting must post the continue comment"
        assert ("fortgesetzt" if origin == "waiting" else "entblockt") in notify[0].content
        assert "arbeite sofort" in notify[0].content


@pytest.mark.asyncio
async def test_operator_resume_from_waiting_redispatches_dead_agent(client: AsyncClient, async_session):
    """Agent process gone (stale last_seen_at) → full redispatch with
    handshake reset, no unread comment."""
    stale = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=2)
    board, target, task, admin_token = await _setup(async_session, target_last_seen=stale)

    with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch, \
         patch("app.utils.create_tracked_task") as mock_tracked:
        mock_tracked.side_effect = lambda coro, name=None: asyncio.ensure_future(coro)
        resp = await client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200, resp.text
        await asyncio.sleep(0)

    mock_dispatch.assert_called_once()
    assert mock_dispatch.call_args[0][0] == task.id

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatched_at is None
        assert refreshed.ack_at is None
        comments = (await s.exec(select(TaskComment).where(TaskComment.task_id == task.id))).all()
        assert not any(c.comment_type == "unblock_notify" for c in comments)
