"""Behavioral coverage for TelegramBotService._resolve_approval's blocker
decision branch — PR #478 Nacharbeit (Architektur C 3/3).

No existing test exercised the actual DB effect of this code path (only
static import/source checks in test_telegram_bot_no_rpc_29_07.py). Converting
task.status = "in_progress"/"failed" to lock_and_set() here relies on the
precondition `task.status == "blocked"` checked immediately above each write
— this test proves both branches still work end-to-end.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.approval import Approval
from app.models.board import Board
from app.models.task import Task
from app.services.telegram_bot import telegram_bot
from app.utils import utcnow
from tests.conftest import test_engine


@pytest.fixture(autouse=True)
def _patch_engine():
    """_resolve_approval does `from app.database import engine` INSIDE the
    function body (not a module-level import in telegram_bot.py), so it
    re-resolves app.database.engine on every call — patch it there."""
    with patch("app.database.engine", test_engine):
        yield


async def _make_blocked_task_with_approval():
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    approval_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="TG Resolve Board", slug=f"tg-resolve-{uuid.uuid4().hex[:8]}"))
        s.add(Agent(id=agent_id, name="Cody", role="developer", board_id=board_id))
        s.add(Task(
            id=task_id, board_id=board_id, title="Blocked task",
            status="blocked", assigned_agent_id=agent_id,
        ))
        s.add(Approval(
            id=approval_id, board_id=board_id, task_id=task_id, agent_id=agent_id,
            action_type="blocker_decision", description="blockiert",
            status="pending", payload={"blocker_type": "technical_problem"},
            expires_at=utcnow() + timedelta(hours=24),
        ))
        await s.commit()
    return task_id, approval_id


@pytest.mark.asyncio
async def test_resolve_approval_approve_unblocks_to_in_progress():
    task_id, approval_id = await _make_blocked_task_with_approval()

    resolved = await telegram_bot._resolve_approval(approval_id, "approve")
    assert resolved == "resolved"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.status == "in_progress"
        approval = await s.get(Approval, approval_id)
        assert approval.status == "approved"


@pytest.mark.asyncio
async def test_resolve_approval_reject_fails_the_task():
    task_id, approval_id = await _make_blocked_task_with_approval()

    resolved = await telegram_bot._resolve_approval(approval_id, "reject")
    assert resolved == "resolved"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.status == "failed"
        approval = await s.get(Approval, approval_id)
        assert approval.status == "rejected"


@pytest.mark.asyncio
async def test_resolve_approval_already_resolved_is_noop():
    _task_id, approval_id = await _make_blocked_task_with_approval()

    first = await telegram_bot._resolve_approval(approval_id, "approve")
    assert first == "resolved"

    second = await telegram_bot._resolve_approval(approval_id, "approve")
    assert second == "already_resolved"


@pytest.mark.asyncio
async def test_resolve_approval_task_write_409_returns_task_conflict():
    """M5 (PR #478 review): the approval already committed as 'approved'
    when the blocker-decision task write hits lock_and_set()'s 409 (another
    writer moved the task off 'blocked' between this function's own
    task.status == "blocked" check and the locked re-read). Before the fix
    that HTTPException propagated out of _resolve_approval uncaught, past
    _handle_callback, into the poll loop's broad `except Exception` —
    answer_callback_query/update_resolved_telegram/emit_event never ran,
    approval resolved but task stuck. Now it must be caught, the approval
    stays resolved, and the caller gets a distinct "task_conflict" signal
    instead of a raised exception or the generic "resolved" state."""
    task_id, approval_id = await _make_blocked_task_with_approval()

    from fastapi import HTTPException

    async def _raise_409(*args, **kwargs):
        raise HTTPException(status_code=409, detail={"error": "invalid_transition"})

    # _resolve_approval does `from app.services.task_state import
    # lock_and_set` INSIDE the function body (same reason the _patch_engine
    # fixture above patches app.database.engine, not a telegram_bot-local
    # name) -- patch it at its source.
    with patch("app.services.task_state.lock_and_set", side_effect=_raise_409):
        resolved = await telegram_bot._resolve_approval(approval_id, "approve")

    assert resolved == "task_conflict"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = await s.get(Approval, approval_id)
        assert approval.status == "approved", "approval must stay resolved despite the task conflict"
        task = await s.get(Task, task_id)
        assert task.status == "blocked", "task write never happened -- status untouched"
