"""Tests for bug fix 2026-08-07: root delegation (no active task) FK race.

Regression of the fix in test_delegate_callback_no_fk_race.py (live bug
2026-04-25). Commit 5d86477 ("Delegation: follow-up orders become tasks,
never comments on closed ones", #284) introduced root-mode delegation for a
Board Lead with no active task — but the explicit `await session.flush()`
that protects the subtask INSERT lived *inside* the `if with_callback and
current_task is not None:` branch, which root delegation never enters
(with_callback is forced False, current_task is None). So the subtask was
only `session.add()`-ed, never flushed, before `emit_event()` — which
internally does `session.commit()` (activity.py:41).

Real production error (Postgres, FK enforced):
    asyncpg.exceptions.ForeignKeyViolationError: insert or update on table
    "activity_events" violates foreign key constraint
    "activity_events_task_id_fkey"
    DETAIL: Key (task_id)=(...) is not present in table "tasks".

`activity_events.task_id` is a raw FK column with no ORM relationship(), so
SQLAlchemy's topological sort for the commit-triggered autoflush has no way
to know the ActivityEvent row depends on the Task row — it can order the
INSERTs either way.

Fix: the flush is now unconditional, right after `session.add(subtask)`,
covering every path (root, fire-and-forget, callback) — not just the
callback branch.

NOTE: SQLite (in-memory test DB) has FK enforcement OFF (conftest.py:71,
same reason documented in test_delegate_callback_no_fk_race.py) — the real
FK violation can't be reproduced here. `test_root_delegate_flushes_before_emit_event`
is the actual red/green regression test: it spies on `AsyncSession.flush()`
and asserts it's called at least once for the root path. Without the fix
this is a genuine 0 (red) — the root branch never called flush at all before
the fix, so the assertion fails independently of FK enforcement. With the
fix it's called once (green).
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_root_scenario():
    """Board Lead (Boss) with NO active task — the root-delegation trigger."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    boss_id = uuid.uuid4()
    researcher_id = uuid.uuid4()

    boss_token, boss_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Root Delegation Board", slug=f"rd-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=boss_id,
            name="Boss",
            role="orchestrator",
            board_id=board_id,
            agent_token_hash=boss_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
            current_task_id=None,  # <-- root trigger: no active task
        ))
        s.add(Agent(
            id=researcher_id,
            name="Researcher",
            role="researcher",
            board_id=board_id,
            agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
        ))
        await s.commit()

    return {
        "board_id": board_id,
        "boss_id": boss_id,
        "researcher_id": researcher_id,
        "boss_token": boss_token,
    }


@pytest.mark.asyncio
async def test_root_delegate_with_real_emit_event_persists_subtask(client, fake_redis):
    """End-to-end with unmocked emit_event — the exact call path that hit
    ForeignKeyViolationError in production. Verifies the subtask is actually
    persisted and the endpoint returns 201, not 500.
    """
    data = await _setup_root_scenario()

    with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
        with patch(
            "app.services.operations.check_dispatch_allowed",
            new_callable=AsyncMock,
            return_value=(True, None),
        ):
            resp = await client.post(
                f"/api/v1/agent/boards/{data['board_id']}/delegate",
                json={
                    "title": "Root subtask via real emit",
                    "description": "Testing root-mode flush ordering with real emit_event session.commit().",
                    "assigned_agent_id": str(data["researcher_id"]),
                    "callback": True,  # ignored in root mode — forced to with_callback=False
                },
                headers={"Authorization": f"Bearer {data['boss_token']}"},
            )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["your_status"] == "no_task"
    subtask_id = uuid.UUID(body["subtask_id"])

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        subtask = await s.get(Task, subtask_id)
        assert subtask is not None, "Subtask wurde nicht persistiert"
        assert subtask.parent_task_id is None
        assert subtask.callback_agent_id is None


@pytest.mark.asyncio
async def test_root_delegate_flushes_before_emit_event(client, fake_redis):
    """The actual red/green regression test (see module docstring for why
    SQLite can't reproduce the FK violation directly).

    Before the fix: root mode never entered the `with_callback and
    current_task is not None` branch, so flush_call_count == 0 here ->
    assertion FAILS (red).
    After the fix: the flush is unconditional right after
    session.add(subtask), so flush_call_count >= 1 -> PASSES (green).
    """
    data = await _setup_root_scenario()

    flush_call_count = 0
    original_flush = AsyncSession.flush

    async def counting_flush(self, *args, **kwargs):
        nonlocal flush_call_count
        flush_call_count += 1
        return await original_flush(self, *args, **kwargs)

    with patch.object(AsyncSession, "flush", counting_flush):
        with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
            with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
                with patch(
                    "app.services.operations.check_dispatch_allowed",
                    new_callable=AsyncMock,
                    return_value=(True, None),
                ):
                    resp = await client.post(
                        f"/api/v1/agent/boards/{data['board_id']}/delegate",
                        json={
                            "title": "Root Flush Spy Test",
                            "description": "Verify explicit flush is called before emit_event in root mode too.",
                            "assigned_agent_id": str(data["researcher_id"]),
                            "callback": False,
                        },
                        headers={"Authorization": f"Bearer {data['boss_token']}"},
                    )

    assert resp.status_code == 201, resp.text
    assert flush_call_count >= 1, (
        f"Erwartet mindestens 1 explicit session.flush() Call zwischen "
        f"subtask add und emit_event() im Root-Pfad. Got {flush_call_count} calls. "
        f"Wenn 0: der Root-Pfad ueberspringt den Flush wieder — Postgres wuerde "
        f"mit ForeignKeyViolationError auf activity_events_task_id_fkey antworten."
    )


@pytest.mark.asyncio
async def test_root_delegate_requires_board_lead(client, fake_redis):
    """Non-lead agent without an active task still gets the 409 — root mode
    is a Board-Lead-only escape hatch, not a general bypass.
    """
    data = await _setup_root_scenario()

    from app.models.agent import Agent
    from app.auth import generate_agent_token

    non_lead_token, non_lead_hash = generate_agent_token()
    non_lead_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Agent(
            id=non_lead_id,
            name="NonLead",
            role="developer",
            board_id=data["board_id"],
            agent_token_hash=non_lead_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
            current_task_id=None,
        ))
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/boards/{data['board_id']}/delegate",
        json={
            "title": "Should be rejected",
            "description": "Non-lead without active task must not be able to root-delegate.",
            "assigned_agent_id": str(data["researcher_id"]),
        },
        headers={"Authorization": f"Bearer {non_lead_token}"},
    )

    assert resp.status_code == 409, resp.text
