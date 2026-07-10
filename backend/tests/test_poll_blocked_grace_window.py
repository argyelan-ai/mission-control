"""Tests for Workstream W2-B1: blocked-task parking gets a GRACE WINDOW.

Live incident: /agent/me/poll returned state="working" for ANY blocked task,
regardless of age. A zombie blocked task from the previous day parked Sparky
indefinitely — a freshly dispatched task was never offered, workaround was a
manual unassign.

Fix: a blocked task only counts as "parking" (state=working) while FRESH —
task.updated_at (the blocked transition) within a grace window, default
15min, sourced from board.blocker_triage_minutes when set. Once stale, poll
ignores the blocked task for parking purposes and the agent becomes
claimable for new inbox work. in_progress tasks keep parking unconditionally
regardless of age. The blocked task itself must never be claimed as
new_task (claim path only selects status="inbox").
"""
import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task


async def _make_board_and_agent(session: AsyncSession, *, blocker_triage_minutes: int | None = None):
    kwargs = {"name": "B", "slug": f"b-{uuid.uuid4().hex[:8]}"}
    if blocker_triage_minutes is not None:
        kwargs["blocker_triage_minutes"] = blocker_triage_minutes
    board = Board(**kwargs)
    session.add(board)
    await session.commit()
    await session.refresh(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Sparky-{uuid.uuid4().hex[:6]}",
        agent_runtime="docker",
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
    updated_at: dt.datetime,
):
    task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title="Grace-window probe",
        status=status,
        dispatched_at=updated_at,
        ack_at=updated_at,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    # Age the blocked transition: since review fix B-1 the grace window is
    # keyed off the dedicated blocked_at column (stamped fresh by the
    # Task.status listener on creation), so tests force it via raw UPDATE.
    # updated_at is aged alongside for the legacy-fallback path.
    from sqlmodel import update
    values = {"updated_at": updated_at}
    if status == "blocked":
        values["blocked_at"] = updated_at
    await session.exec(
        update(Task).where(Task.id == task.id).values(**values)
    )
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_blocked_task_20min_old_does_not_park_agent(client: AsyncClient, async_session):
    """Blocked task older than the (default 15min) grace window → poll does
    NOT return working; the agent must be free to claim other inbox work."""
    board, agent, token = await _make_board_and_agent(async_session)
    stale = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=20)
    await _make_task(async_session, board=board, agent=agent, status="blocked", updated_at=stale)

    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] != "working", body
    assert body["state"] == "idle", body


@pytest.mark.asyncio
async def test_blocked_task_5min_old_still_parks_agent(client: AsyncClient, async_session):
    """Blocked task within the grace window → still parks (state=working)."""
    board, agent, token = await _make_board_and_agent(async_session)
    fresh = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=5)
    task = await _make_task(async_session, board=board, agent=agent, status="blocked", updated_at=fresh)

    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "working", body
    assert body["task_id"] == str(task.id)


@pytest.mark.asyncio
async def test_in_progress_task_always_parks_regardless_of_age(client: AsyncClient, async_session):
    """in_progress tasks keep parking unconditionally — no grace window."""
    board, agent, token = await _make_board_and_agent(async_session)
    ancient = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=48)
    task = await _make_task(async_session, board=board, agent=agent, status="in_progress", updated_at=ancient)

    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "working", body
    assert body["task_id"] == str(task.id)


@pytest.mark.asyncio
async def test_stale_blocked_task_never_claimed_as_new_task(client: AsyncClient, async_session):
    """Regression: once the grace window expires and the agent becomes
    claimable, poll must claim a genuine INBOX task — never re-deliver the
    blocked task itself as new_task (claim path only selects status=inbox)."""
    board, agent, token = await _make_board_and_agent(async_session)
    stale = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=20)
    blocked_task = await _make_task(
        async_session, board=board, agent=agent, status="blocked", updated_at=stale
    )

    inbox_task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title="Fresh inbox work",
        status="inbox",
    )
    async_session.add(inbox_task)
    await async_session.commit()
    await async_session.refresh(inbox_task)

    from unittest.mock import patch
    with patch("app.services.dispatch.build_agent_task_prompt", return_value="x"):
        resp = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "new_task", body
    assert body["task"]["id"] == str(inbox_task.id)
    assert body["task"]["id"] != str(blocked_task.id)


@pytest.mark.asyncio
async def test_grace_window_uses_board_blocker_triage_minutes(client: AsyncClient, async_session):
    """A board with a custom blocker_triage_minutes (e.g. 5) uses that value
    as the grace window instead of the 15min default."""
    board, agent, token = await _make_board_and_agent(async_session, blocker_triage_minutes=5)
    aged_10min = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=10)
    await _make_task(async_session, board=board, agent=agent, status="blocked", updated_at=aged_10min)

    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # 10min > board's 5min window → must NOT park.
    assert body["state"] != "working", body
