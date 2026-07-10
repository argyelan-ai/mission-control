"""Tests for Workstream W2-B3: normalize operator-facing comment formats to
`**Label:**` markdown so the frontend's parseComment (frontend-v2/src/lib/
parseComment.ts) picks them up as structured sections. parseComment only
recognizes `**Label**` at the START of a line — plain `Label:` text was
invisible to it.

Three sites normalized (German labels kept):
  1. blocker_triage.py::start_lead_triage (Lead-Triage notify)
  2. agent_task_status.py lead-FYI on escalation (blocked → operator-Approval)
  3. agent_scoped.py KLAERUNGSFRAGE lead-FYI (mc clarification)
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import TaskComment
from tests.conftest import test_engine
from tests.test_blocker_triage import (
    _block_payload,
    _comments_for,
    _make_board_with_agents,
    _make_task,
    _patch_telegram,
    _patch_triage_redis,
)


# ── Site 1: blocker_triage.start_lead_triage ────────────────────────────


@pytest.mark.asyncio
async def test_lead_triage_notify_uses_bold_labels(client: AsyncClient, fake_redis):
    board_id, worker, worker_token, lead, _ = await _make_board_with_agents()
    task = await _make_task(board_id, assigned_agent_id=worker.id)

    with _patch_triage_redis(fake_redis), _patch_telegram():
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
            headers={"Authorization": f"Bearer {worker_token}"},
            json=_block_payload(),
        )
    assert resp.status_code == 200, resp.text

    notes = await _comments_for(task.id, "blocker_lead_notify")
    assert len(notes) == 1
    content = notes[0].content
    assert "**Typ:**" in content
    assert "**Frage:**" in content
    assert "**Detail:**" in content
    assert "**Task-ID:**" in content
    # Old plain-text labels must be gone (not just present as a substring of
    # the bold form — check the exact plain prefix at line start).
    for plain_label in ("\nTyp: ", "\nFrage: ", "\nDetail: ", "\nTask-ID: "):
        assert plain_label not in content


# ── Site 2: agent_task_status.py lead-FYI on escalation ─────────────────


@pytest.mark.asyncio
async def test_escalation_lead_fyi_uses_bold_labels(client: AsyncClient, fake_redis):
    """decision_needed → direct operator escalation + lead-FYI comment; the
    FYI must use the bold-label convention for Typ + Task-ID."""
    board_id, worker, worker_token, lead, _ = await _make_board_with_agents()
    task = await _make_task(board_id, assigned_agent_id=worker.id)

    with _patch_triage_redis(fake_redis), _patch_telegram():
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
            headers={"Authorization": f"Bearer {worker_token}"},
            json=_block_payload("decision_needed"),
        )
    assert resp.status_code == 200, resp.text

    notes = await _comments_for(task.id, "blocker_lead_notify")
    assert len(notes) == 1
    content = notes[0].content
    assert "**Typ:**" in content
    assert "**Task-ID:**" in content
    assert "decision_needed" in content


# ── Site 3: agent_scoped.py KLAERUNGSFRAGE lead-FYI ──────────────────────


@pytest.mark.asyncio
async def test_klaerungsfrage_lead_fyi_uses_bold_labels(client: AsyncClient, async_session):
    """mc clarification (agent asks a question) with a lead present → the
    lead-FYI TaskComment must use bold labels for Frage + Task-ID."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board = Board(name="Klaerung Board", slug=f"klaerung-{uuid.uuid4().hex[:8]}")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    worker_raw, worker_hash = generate_agent_token()
    worker = Agent(
        name="Worker",
        role="developer",
        board_id=board.id,
        agent_token_hash=worker_hash,
        is_board_lead=False,
        scopes=["tasks:read", "tasks:write", "tasks:help"],
    )
    async_session.add(worker)

    lead_raw, lead_hash = generate_agent_token()
    lead = Agent(
        name="Boss",
        role="lead",
        board_id=board.id,
        agent_token_hash=lead_hash,
        is_board_lead=True,
        scopes=["tasks:read", "tasks:write", "tasks:manage"],
    )
    async_session.add(lead)
    await async_session.commit()
    await async_session.refresh(worker)
    await async_session.refresh(lead)

    task = Task(
        board_id=board.id,
        assigned_agent_id=worker.id,
        title="Klaerungsfrage-Probe",
        status="in_progress",
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    # Give the worker its current_task_id so the endpoint's "active task"
    # resolution finds it.
    worker.current_task_id = task.id
    async_session.add(worker)
    await async_session.commit()

    with patch("app.services.telegram_bot.telegram_bot.send_approval_telegram", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/clarification",
            headers={"Authorization": f"Bearer {worker_raw}"},
            json={"question": "Welche Node-Version soll ich pinnen?", "options": None},
        )
    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        res = await s.exec(
            select(TaskComment).where(
                TaskComment.task_id == task.id,
                TaskComment.comment_type == "blocker_lead_notify",
            )
        )
        notes = list(res.all())
    assert len(notes) == 1
    content = notes[0].content
    assert "**Frage:**" in content
    assert "**Task-ID:**" in content
    assert "Welche Node-Version soll ich pinnen?" in content
