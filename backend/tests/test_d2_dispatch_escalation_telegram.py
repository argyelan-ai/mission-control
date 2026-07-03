"""Tests for D-2: Telegram direct push on dispatch_escalation approval.

Background: Sparky's frontend audit created a dispatch_escalation approval
at 09:43 — the operator only saw it at 12:00 local time (2h 17min). Activity
events with severity=warning land in the UI inbox badge, but there's no push
when the operator isn't actively in the UI. The D-2 fix calls
telegram_bot.send_approval_telegram directly so the operator has a push
channel with inline buttons.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


@pytest.mark.asyncio
async def test_dispatch_escalation_pushes_telegram(make_board, make_agent, make_task):
    """_create_dispatch_approval calls telegram_bot.send_approval_telegram."""
    from app.services.task_runner import task_runner
    from app.models.task import Task
    from app.models.approval import Approval
    from sqlmodel.ext.asyncio.session import AsyncSession
    from sqlmodel import select
    from tests.conftest import test_engine

    board = await make_board()
    agent = await make_agent(
        name="Sparky", board_id=board.id, agent_runtime="cli-bridge",
scopes=["tasks:read", "tasks:write", "heartbeat"],
    )
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=_now() - timedelta(minutes=20),
        dispatch_attempt_id=str(uuid.uuid4()),
    )

    captured_calls = []

    async def _fake_send_approval(approval_id, agent_name, task_title, blocker_comment):
        captured_calls.append({
            "approval_id": approval_id,
            "agent_name": agent_name,
            "task_title": task_title,
            "blocker_comment": blocker_comment,
        })

    with patch("app.services.telegram_bot.telegram_bot.send_approval_telegram",
               new=AsyncMock(side_effect=_fake_send_approval)):
        with patch("app.services.activity.broadcast", new_callable=AsyncMock):
            async with AsyncSession(test_engine, expire_on_commit=False) as s:
                await task_runner._create_dispatch_approval(
                    s, await s.get(Task, task.id), agent, 20.0, "kein ACK nach Dispatch",
                )

    # Approval was created
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Approval).where(Approval.task_id == task.id)
        )).all()
    assert len(approvals) == 1
    assert approvals[0].action_type == "dispatch_escalation"

    # Telegram push was called
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["approval_id"] == approvals[0].id
    assert call["agent_name"] == "Sparky"
    assert call["task_title"] == task.title
    assert "20min" in call["blocker_comment"] or "20 min" in call["blocker_comment"].lower()
    assert "kein ACK" in call["blocker_comment"]


@pytest.mark.asyncio
async def test_telegram_failure_does_not_block_approval(make_board, make_agent, make_task):
    """If Telegram fails (no bot configured / network down) → the approval
    is still created, backend logs a warning. Resilient design."""
    from app.services.task_runner import task_runner
    from app.models.task import Task
    from app.models.approval import Approval
    from sqlmodel.ext.asyncio.session import AsyncSession
    from sqlmodel import select
    from tests.conftest import test_engine

    board = await make_board()
    agent = await make_agent(
        name="Sparky", board_id=board.id, agent_runtime="cli-bridge",
scopes=["tasks:read", "tasks:write"],
    )
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=_now() - timedelta(minutes=20),
        dispatch_attempt_id=str(uuid.uuid4()),
    )

    with patch("app.services.telegram_bot.telegram_bot.send_approval_telegram",
               new=AsyncMock(side_effect=RuntimeError("Telegram API down"))):
        with patch("app.services.activity.broadcast", new_callable=AsyncMock):
            async with AsyncSession(test_engine, expire_on_commit=False) as s:
                # MUST NOT raise
                await task_runner._create_dispatch_approval(
                    s, await s.get(Task, task.id), agent, 20.0, "kein ACK",
                )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Approval).where(Approval.task_id == task.id)
        )).all()
    assert len(approvals) == 1  # approval created anyway
