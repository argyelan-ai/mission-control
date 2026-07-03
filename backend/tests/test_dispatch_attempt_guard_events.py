"""Tests for the differentiation of missing_header vs stale_value in the
dispatch attempt guard (agent_task_status.py).

Bug 2026-05-18: On `missing_header` (agent sends PATCH without header —
typically because it uses raw curl instead of `mc done`), a
`task.missing_dispatch_attempt_id` event was emitted + pushed to the
per-agent Discord channel. On every 409 — i.e. the agent recovered itself
(mc CLI on the next attempt), but the operator saw a Discord event per 409.

New semantics:
- missing_header → 409, NO event (only log.warning). Self-healing is enough.
- stale_value → 409 + event (severity=warning), because it's a real run conflict.

Tests pin both paths.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_dispatched_task() -> dict:
    """Creates board + worker (own task) + task with dispatch_attempt_id set."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    attempt_id = str(uuid.uuid4())
    token_raw, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Guard Board", slug=f"dg-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=agent_id, name="Worker", role="researcher",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
            current_task_id=task_id,
            emoji="🔍",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Briefing", status="in_progress",
            assigned_agent_id=agent_id,
            dispatch_attempt_id=attempt_id,
        ))
        await s.commit()

    return {
        "board_id": board_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "token": token_raw,
    }


async def _count_guard_events(event_type: str, task_id: uuid.UUID) -> int:
    """Counts activity_events for a task + event_type."""
    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(
            select(ActivityEvent)
            .where(ActivityEvent.event_type == event_type)
            .where(ActivityEvent.task_id == task_id)
        )).all()
        return len(rows)


# ────────────────────────────────────────────────────────────────────
# 1. missing_header → 409, no event (only logs)
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_header_returns_409_without_event(client, fake_redis):
    """Agent sends PATCH without X-Dispatch-Attempt-Id → 409, no Discord spam."""
    data = await _setup_dispatched_task()

    r = await client.patch(
        f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
        json={"status": "done"},
        headers={"Authorization": f"Bearer {data['token']}"},
    )

    assert r.status_code == 409, r.text
    body = r.json()
    # Hint explicitly names mc CLI (previously only a curl snippet)
    assert "mc done" in body["detail"]
    assert "mc-CLI" in body["detail"] or "mc-cli" in body["detail"].lower()

    # NO event in the DB
    n = await _count_guard_events("task.missing_dispatch_attempt_id", data["task_id"])
    assert n == 0, "missing_header darf kein activity_event emittieren"


# ────────────────────────────────────────────────────────────────────
# 2. stale_value → 409 + activity_event (severity=warning)
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stale_value_returns_409_and_emits_event(client, fake_redis):
    """Agent sends an old attempt_id value → 409 + event (real conflict)."""
    data = await _setup_dispatched_task()
    wrong_id = str(uuid.uuid4())

    r = await client.patch(
        f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
        json={"status": "done"},
        headers={
            "Authorization": f"Bearer {data['token']}",
            "X-Dispatch-Attempt-Id": wrong_id,
        },
    )

    assert r.status_code == 409, r.text
    body = r.json()
    assert "Stale" in body["detail"] or "veraltet" in body["detail"]

    # Event emitted
    n = await _count_guard_events("task.stale_update_rejected", data["task_id"])
    assert n == 1, "stale_value MUSS ein activity_event emittieren"


# ────────────────────────────────────────────────────────────────────
# 3. correct header → 200, no event
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_correct_header_passes_without_event(client, fake_redis):
    """Happy path: correct header → PATCH goes through, no guard events.

    Updates `priority` (instead of `status`) to bypass the status lifecycle
    guards (reflection requirement etc.) — here we only test the
    dispatch-attempt header guard.
    """
    data = await _setup_dispatched_task()

    r = await client.patch(
        f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
        json={"priority": "high"},
        headers={
            "Authorization": f"Bearer {data['token']}",
            "X-Dispatch-Attempt-Id": data["attempt_id"],
        },
    )

    assert r.status_code in (200, 201), r.text
    n_missing = await _count_guard_events("task.missing_dispatch_attempt_id", data["task_id"])
    n_stale = await _count_guard_events("task.stale_update_rejected", data["task_id"])
    assert n_missing == 0
    assert n_stale == 0
