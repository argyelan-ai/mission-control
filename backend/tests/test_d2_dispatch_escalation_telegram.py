"""Tests fuer D-2: Telegram-Direct-Push bei dispatch_escalation Approval.

Hintergrund: Sparky's Frontend-Audit hatte 09:43 eine dispatch_escalation
Approval erstellt — der Operator sah sie erst 12:00 lokal (2h 17min). Activity-Event
mit severity=warning landet im UI Inbox-Badge, aber kein Push wenn der Operator
nicht aktiv im UI ist. D-2 fix ruft telegram_bot.send_approval_telegram
direkt auf damit der Operator einen Push-Kanal mit Inline-Buttons hat.
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
    """_create_dispatch_approval ruft telegram_bot.send_approval_telegram auf."""
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

    # Approval entstanden
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Approval).where(Approval.task_id == task.id)
        )).all()
    assert len(approvals) == 1
    assert approvals[0].action_type == "dispatch_escalation"

    # Telegram push wurde aufgerufen
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["approval_id"] == approvals[0].id
    assert call["agent_name"] == "Sparky"
    assert call["task_title"] == task.title
    assert "20min" in call["blocker_comment"] or "20 min" in call["blocker_comment"].lower()
    assert "kein ACK" in call["blocker_comment"]


@pytest.mark.asyncio
async def test_telegram_failure_does_not_block_approval(make_board, make_agent, make_task):
    """Wenn Telegram fehlschlaegt (kein bot configured / Netzwerk weg) →
    Approval entsteht trotzdem, Backend logged Warning. Resilient design."""
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
                # MUSS NICHT raisen
                await task_runner._create_dispatch_approval(
                    s, await s.get(Task, task.id), agent, 20.0, "kein ACK",
                )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Approval).where(Approval.task_id == task.id)
        )).all()
    assert len(approvals) == 1  # Approval trotzdem da
