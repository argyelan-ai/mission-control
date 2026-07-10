"""W2-B review fix CRITICAL B-1: dedicated tasks.blocked_at timestamp.

The poll grace window AND the watchdog's blocked-task escalation were keyed
off tasks.updated_at — a generic onupdate=NOW column. ANY metadata PATCH
(title, priority, labels) reset it, which:
  1. re-parked the agent for another full grace window, indefinitely — the
     exact zombie-blocked bug the grace window fixed, reintroduced sideways;
  2. suppressed the watchdog's operator escalation for the same reason.

Fix: tasks.blocked_at (migration 0150), maintained by a SQLAlchemy attribute
listener on Task.status — set ONLY on the →blocked transition, cleared on
leaving blocked. Poll grace window and _check_blocked_tasks both key off
blocked_at, with updated_at as fallback for legacy rows (blocked before the
migration, blocked_at NULL).
"""
from __future__ import annotations

import datetime as dt
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from tests.conftest import test_engine


def _naive_utcnow() -> datetime:
    return datetime.utcnow()


# ── Listener unit behavior ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_listener_sets_and_clears_blocked_at(async_session: AsyncSession):
    """→blocked stamps blocked_at; leaving blocked clears it. Covers every
    transition call site because it hooks attribute assignment itself."""
    board = Board(name="L", slug=f"l-{uuid.uuid4().hex[:8]}")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    task = Task(board_id=board.id, title="Listener probe", status="in_progress")
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)
    assert task.blocked_at is None

    task.status = "blocked"
    assert task.blocked_at is not None, "→blocked must stamp blocked_at"
    async_session.add(task)
    await async_session.commit()

    task.status = "in_progress"
    assert task.blocked_at is None, "leaving blocked must clear blocked_at"


@pytest.mark.asyncio
async def test_task_created_directly_blocked_gets_blocked_at(async_session: AsyncSession):
    board = Board(name="L2", slug=f"l2-{uuid.uuid4().hex[:8]}")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    task = Task(board_id=board.id, title="Born blocked", status="blocked")
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)
    # SQLModel __init__ bypasses attribute instrumentation — the
    # before_insert listener stamps blocked_at at flush time instead.
    assert task.blocked_at is not None


# ── The actual incident scenario: metadata PATCH must not reset the clock ──


async def _make_blocked_setup(session: AsyncSession):
    board = Board(name="B1", slug=f"b1-{uuid.uuid4().hex[:8]}")
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

    now = dt.datetime.now(tz=dt.timezone.utc)
    task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title="Old blocker",
        status="blocked",
        dispatched_at=now - dt.timedelta(minutes=30),
        ack_at=now - dt.timedelta(minutes=30),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    # Age the blocked transition past the 15min default grace window.
    from sqlmodel import update
    await session.exec(
        update(Task)
        .where(Task.id == task.id)
        .values(blocked_at=now - dt.timedelta(minutes=20))
    )
    await session.commit()
    await session.refresh(task)
    return board, agent, raw_token, task


@pytest.mark.asyncio
async def test_metadata_patch_does_not_repark_agent(auth_client: AsyncClient, async_session):
    """A title-only PATCH on a 20min-blocked task resets updated_at but must
    NOT reset the grace clock — the agent stays claimable (poll != working)."""
    board, agent, agent_token, task = await _make_blocked_setup(async_session)

    # Operator edits metadata (updated_at → now via onupdate).
    resp = await auth_client.patch(
        f"/api/v1/boards/{board.id}/tasks/{task.id}",
        json={"title": "Old blocker (renamed)"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.title == "Old blocker (renamed)"
        assert refreshed.blocked_at is not None, "metadata PATCH must not clear blocked_at"

    poll = await auth_client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert poll.status_code == 200
    body = poll.json()
    assert body["state"] != "working", (
        f"metadata PATCH re-parked the agent (grace clock reset via updated_at): {body}"
    )


@pytest.mark.asyncio
async def test_metadata_edit_does_not_suppress_watchdog_escalation(fake_redis):
    """Watchdog _check_blocked_tasks must escalate a 20min-blocked task even
    when updated_at is fresh (metadata edit) — the clock is blocked_at."""
    import json

    from app.models.approval import Approval
    from sqlmodel import select

    board_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(
            id=board_id, name="WB", slug=f"wb-{uuid.uuid4().hex[:6]}",
            blocker_triage_minutes=15,
        ))
        worker = Agent(
            id=uuid.uuid4(), name="Worker", role="developer",
            is_board_lead=False, board_id=board_id, agent_runtime="host",
        )
        s.add(worker)
        task = Task(
            id=uuid.uuid4(), board_id=board_id, title="Metadata-edited blocker",
            status="blocked", assigned_agent_id=worker.id,
        )
        s.add(task)
        await s.commit()

        # blocked 20min ago, but updated_at fresh (= metadata edit just now).
        t = await s.get(Task, task.id)
        t.blocked_at = _naive_utcnow() - timedelta(minutes=20)
        t.updated_at = _naive_utcnow()
        s.add(t)
        await s.commit()

    await fake_redis.set(
        f"mc:blocker:triage:{task.id}",
        json.dumps({
            "blocker_type": "technical_problem",
            "question": "Runtime-Abbruch",
            "blocker_comment": "omp-Turn endete ohne Sentinel",
        }),
    )

    from app.services.watchdog.core import WatchdogService

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.watchdog.task_monitor.get_redis",
                   AsyncMock(return_value=fake_redis)), \
             patch("app.services.watchdog.task_monitor.utcnow", _naive_utcnow), \
             patch("app.services.blocker_triage.get_redis",
                   AsyncMock(return_value=fake_redis)), \
             patch("app.services.blocker_triage.utcnow", _naive_utcnow), \
             patch("app.services.blocker_triage.emit_event", new_callable=AsyncMock), \
             patch("app.services.telegram_bot.telegram_bot.send_approval_telegram",
                   new_callable=AsyncMock):
            svc = WatchdogService()
            await svc._check_blocked_tasks(s)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = list((await s.exec(
            select(Approval).where(
                Approval.task_id == task.id,
                Approval.action_type == "blocker_decision",
            )
        )).all())
    assert len(approvals) == 1, (
        "fresh updated_at (metadata edit) must not suppress the escalation — "
        "the watchdog clock is blocked_at"
    )
