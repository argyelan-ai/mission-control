"""Tests fuer die Differenzierung von missing_header vs stale_value im
Dispatch-Attempt-Guard (agent_task_status.py).

Bug 2026-05-18: Bei `missing_header` (Agent sendet PATCH ohne Header — typisch
weil er raw curl statt `mc done` benutzt) wurde ein
`task.missing_dispatch_attempt_id` Event emittiert + per-Agent-Discord-Channel
gepusht. Auf jedem 409 — d.h. der Agent recoverte sich (mc CLI im naechsten
Versuch), aber der Operator sah ein Discord-Event pro 409.

Neue Semantik:
- missing_header → 409, KEIN Event (nur log.warning). Self-Healing reicht.
- stale_value → 409 + Event (severity=warning), weil ein echter Run-Konflikt.

Tests pinnen beide Pfade.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_dispatched_task() -> dict:
    """Erstellt Board + Worker (own task) + Task mit gesetzter dispatch_attempt_id."""
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
    """Zaehlt activity_events fuer einen task + event_type."""
    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(
            select(ActivityEvent)
            .where(ActivityEvent.event_type == event_type)
            .where(ActivityEvent.task_id == task_id)
        )).all()
        return len(rows)


# ────────────────────────────────────────────────────────────────────
# 1. missing_header → 409, kein Event (nur Logs)
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_header_returns_409_without_event(client, fake_redis):
    """Agent sendet PATCH ohne X-Dispatch-Attempt-Id → 409, kein Discord-Spam."""
    data = await _setup_dispatched_task()

    r = await client.patch(
        f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
        json={"status": "done"},
        headers={"Authorization": f"Bearer {data['token']}"},
    )

    assert r.status_code == 409, r.text
    body = r.json()
    # Hint nennt mc-CLI explizit (vorher nur curl-Snippet)
    assert "mc done" in body["detail"]
    assert "mc-CLI" in body["detail"] or "mc-cli" in body["detail"].lower()

    # KEIN Event in der DB
    n = await _count_guard_events("task.missing_dispatch_attempt_id", data["task_id"])
    assert n == 0, "missing_header darf kein activity_event emittieren"


# ────────────────────────────────────────────────────────────────────
# 2. stale_value → 409 + activity_event (severity=warning)
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stale_value_returns_409_and_emits_event(client, fake_redis):
    """Agent sendet alten attempt_id-Wert → 409 + Event (echter Konflikt)."""
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

    # Event emittiert
    n = await _count_guard_events("task.stale_update_rejected", data["task_id"])
    assert n == 1, "stale_value MUSS ein activity_event emittieren"


# ────────────────────────────────────────────────────────────────────
# 3. korrekter Header → 200, kein Event
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_correct_header_passes_without_event(client, fake_redis):
    """Happy-Path: korrekter Header → PATCH durch, keine Guard-Events.

    Update auf `priority` (statt `status`) um die Status-Lifecycle-Guards
    (Reflection-Pflicht etc.) zu umgehen — wir testen hier nur den
    Dispatch-Attempt-Header-Guard.
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
