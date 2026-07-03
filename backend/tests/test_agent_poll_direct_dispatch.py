"""Tests for the /agent/me/poll prompt-delivery fix.

Regression for the bug where tasks dispatched directly to in_progress
(via "direkt" UI option, or after recover-task re-dispatch) never had
their prompt delivered because poll returned state=working without a
prompt. Fix: if ack_at is NULL, treat the task as "needs prompt
delivery" and return state=new_task.
"""
import datetime as dt
import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task


async def _make_board_and_agent(session: AsyncSession, *, agent_runtime="host"):
    board = Board(name="B", slug="b")
    session.add(board)
    await session.commit()
    await session.refresh(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Boss-{uuid.uuid4().hex[:6]}",
        agent_runtime=agent_runtime,
        agent_token_hash=token_hash,
        board_id=board.id,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return board, agent, raw_token


async def _make_task(
    session: AsyncSession,
    *,
    board: Board,
    agent: Agent,
    status: str,
    dispatched_at: dt.datetime | None,
    ack_at: dt.datetime | None,
):
    task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title="Direct-dispatch probe",
        status=status,
        dispatched_at=dispatched_at,
        ack_at=ack_at,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_poll_delivers_prompt_when_ack_missing(client: AsyncClient, async_session):
    """Direct dispatch: task in_progress + dispatched_at set + ack_at None → new_task with prompt."""
    board, agent, token = await _make_board_and_agent(async_session)
    now = dt.datetime.now(tz=dt.timezone.utc)
    await _make_task(
        async_session,
        board=board,
        agent=agent,
        status="in_progress",
        dispatched_at=now,
        ack_at=None,
    )

    with patch(
        "app.services.dispatch.build_agent_task_prompt",
        return_value="prompt text",
    ):
        resp = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "new_task", body
    assert body["task"]["prompt"] == "prompt text"


@pytest.mark.asyncio
async def test_poll_sets_ack_at_after_delivery(client: AsyncClient, async_session):
    """After delivering the prompt, ack_at is set — subsequent polls return working."""
    board, agent, token = await _make_board_and_agent(async_session)
    now = dt.datetime.now(tz=dt.timezone.utc)
    task = await _make_task(
        async_session,
        board=board,
        agent=agent,
        status="in_progress",
        dispatched_at=now,
        ack_at=None,
    )

    with patch("app.services.dispatch.build_agent_task_prompt", return_value="x"):
        first = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert first.status_code == 200
    assert first.json()["state"] == "new_task"

    await async_session.refresh(task)
    assert task.ack_at is not None
    # Dispatched_at stays from the original dispatch — audit trail preserved.
    assert task.dispatched_at is not None
    assert task.status == "in_progress"

    # Second poll: ack_at now set → state=working (no prompt)
    with patch("app.services.dispatch.build_agent_task_prompt", return_value="x"):
        second = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert second.json()["state"] == "working"
    assert "task" not in second.json()


@pytest.mark.asyncio
async def test_poll_working_when_ack_already_set(client: AsyncClient, async_session):
    """Happy path: in_progress + ack_at set → working, no prompt delivery."""
    board, agent, token = await _make_board_and_agent(async_session)
    now = dt.datetime.now(tz=dt.timezone.utc)
    await _make_task(
        async_session,
        board=board,
        agent=agent,
        status="in_progress",
        dispatched_at=now,
        ack_at=now,
    )

    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "working"


@pytest.mark.asyncio
async def test_poll_claims_inbox_task_and_sets_dispatched_at_only(client: AsyncClient, async_session):
    """Inbox path (Plan 26-02 / HERM-10 F1+F3): poll claims the task → delivers
    the prompt + sets dispatched_at, but **status stays 'inbox'** and
    **ack_at stays NULL**. Status only flips via the agent's explicit PATCH
    status:in_progress (= Migration 0018 ACK handshake).
    """
    board, agent, token = await _make_board_and_agent(async_session)
    task = await _make_task(
        async_session,
        board=board,
        agent=agent,
        status="inbox",
        dispatched_at=None,
        ack_at=None,
    )

    with patch("app.services.dispatch.build_agent_task_prompt", return_value="x"):
        resp = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["state"] == "new_task"

    await async_session.refresh(task)
    # F1 fix (Plan 26-02): status MUST stay inbox until agent's own PATCH.
    assert task.status == "inbox"
    # dispatched_at IS set on poll — that's the legitimate dispatch timestamp.
    assert task.dispatched_at is not None
    # F3 fix (Plan 26-02): ack_at MUST stay NULL — only the agent's PATCH sets it.
    assert task.ack_at is None


@pytest.mark.asyncio
async def test_poll_revert_preserves_dispatched_at_on_failure(client: AsyncClient, async_session):
    """If prompt generation fails for a directly-dispatched task, dispatched_at
    stays intact so the next poll retries (instead of losing the audit trail)."""
    board, agent, token = await _make_board_and_agent(async_session)
    original_dispatched = dt.datetime(2026, 4, 19, 12, 0, 0, tzinfo=dt.timezone.utc)
    task = await _make_task(
        async_session,
        board=board,
        agent=agent,
        status="in_progress",
        dispatched_at=original_dispatched,
        ack_at=None,
    )

    with patch(
        "app.services.dispatch.build_agent_task_prompt",
        side_effect=RuntimeError("boom"),
    ):
        resp = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 500

    await async_session.refresh(task)
    assert task.status == "in_progress", "status must stay — task was not inbox"
    # SQLite drops tz info — compare naive.
    assert (
        task.dispatched_at.replace(tzinfo=None)
        == original_dispatched.replace(tzinfo=None)
    ), "dispatched_at audit preserved"
    assert task.ack_at is None, "ack_at rolled back so next poll retries"
