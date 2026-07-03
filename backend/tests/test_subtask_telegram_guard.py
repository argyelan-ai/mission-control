"""Tests for the subtask send guard (routing rule "whoever dispatches, sends").

Before this patch, both the Researcher (as subtask worker) and Boss (as
orchestrator) sent `mc telegram` — a double hit for the user. These tests
pin the new boundary:

| # | Scenario                                       | Behavior                   |
|---|------------------------------------------------|----------------------------|
| 1 | Subtask + autonomous_telegram=False            | mc telegram → 422 (guard)  |
| 2 | Subtask + autonomous_telegram=True              | mc telegram → 200          |
| 3 | Standalone task (parent_task_id IS NULL)        | mc telegram → 200          |
| 4 | Parent task itself (parent IS NULL, has subs)   | mc telegram → 200          |
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


def _mock_reports() -> MagicMock:
    mock = MagicMock()
    mock.configured = True
    mock.send = AsyncMock(return_value={"ok": True, "result": {"message_id": 42}})
    return mock


async def _setup_subtask(*, autonomous: bool):
    """Creates board + worker + parent + subtask. Worker has current_task_id=subtask."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    subtask_id = uuid.uuid4()
    token_raw, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Guard Board", slug=f"gd-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=agent_id, name="Worker", role="researcher",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
            provision_status="provisioned",
            current_task_id=subtask_id,
            emoji="🔍",
        ))
        s.add(Task(id=parent_id, board_id=board_id, title="Parent", status="in_progress"))
        s.add(Task(
            id=subtask_id, board_id=board_id, title="Subtask",
            status="in_progress", assigned_agent_id=agent_id,
            parent_task_id=parent_id,
            autonomous_telegram=autonomous,
        ))
        await s.commit()

    return {"board_id": board_id, "agent_id": agent_id, "subtask_id": subtask_id, "token": token_raw}


async def _setup_standalone(*, role: str = "researcher"):
    """Creates board + worker + standalone task (no parent_task_id)."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    token_raw, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Solo Board", slug=f"so-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=agent_id, name="Solo", role=role,
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
            provision_status="provisioned",
            current_task_id=task_id,
            emoji="🤖",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Standalone",
            status="in_progress", assigned_agent_id=agent_id,
        ))
        await s.commit()

    return {"board_id": board_id, "agent_id": agent_id, "task_id": task_id, "token": token_raw}


# ────────────────────────────────────────────────────────────────────
# 1. Subtask + autonomous_telegram=False → 422
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subtask_send_telegram_blocked_by_default(client, fake_redis):
    """Default subtask may not send directly to the operator — Boss consolidates."""
    data = await _setup_subtask(autonomous=False)

    with patch("app.services.telegram_reports.telegram_reports", _mock_reports()):
        r = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "🔍 Sub fertig"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 422, r.text
    body = r.json()
    assert "Subtask sendet kein Telegram" in body["detail"]
    assert "autonomous_telegram=true" in body["detail"]


# ────────────────────────────────────────────────────────────────────
# 2. Subtask + autonomous_telegram=True → 200
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subtask_send_telegram_allowed_when_autonomous(client, fake_redis):
    """Long-running watch tasks: Boss sets the flag, worker is allowed to send."""
    data = await _setup_subtask(autonomous=True)
    mock_reports = _mock_reports()

    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        r = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "🔍 Autonomes Update — Channel-Event erkannt"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200, r.text
    mock_reports.send.assert_awaited_once()


# ────────────────────────────────────────────────────────────────────
# 3. Standalone task (no parent) → 200
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_standalone_task_send_telegram_passes(client, fake_redis):
    """Scheduled/standalone tasks (e.g. morning briefing) → worker = sender."""
    data = await _setup_standalone()
    mock_reports = _mock_reports()

    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        r = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "🔍 Morning Briefing — heute"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200, r.text
    mock_reports.send.assert_awaited_once()


# ────────────────────────────────────────────────────────────────────
# 4. Parent task itself (no parent, but has subtasks) → 200
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orchestrator_parent_task_send_telegram_passes(client, fake_redis):
    """Boss works on the parent task (parent_task_id IS NULL) → it may send,
    even if subtasks hang below it. This is the consolidation message."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    boss_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    sub_id = uuid.uuid4()
    token_raw, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Orch Board", slug=f"or-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=boss_id, name="Boss", role="board_lead",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
            provision_status="provisioned",
            is_board_lead=True,
            current_task_id=parent_id,
            emoji="🤖",
        ))
        s.add(Task(
            id=parent_id, board_id=board_id, title="Parent",
            status="in_progress", assigned_agent_id=boss_id,
        ))
        s.add(Task(
            id=sub_id, board_id=board_id, title="Sub",
            status="done", parent_task_id=parent_id,
        ))
        await s.commit()

    mock_reports = _mock_reports()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        r = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "🤖 Boss · Final-Report mit Researcher's Output"},
            headers={"Authorization": f"Bearer {token_raw}"},
        )

    assert r.status_code == 200, r.text
    mock_reports.send.assert_awaited_once()
