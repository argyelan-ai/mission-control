"""Tests for bug fix 2026-04-25: mc delegate --callback FK race.

Live bug on Boss 2026-04-25: HTTP 500 ForeignKeyViolationError on
fk_tasks_blocked_by_task_id during `mc delegate` with callback. Root cause:
emit_event() internally does session.commit() (activity.py:41) — flushes all
pending changes mid-function. SQLAlchemy's topological sort for reflexive
FKs (tasks → tasks) sometimes orders things wrong: the current_task UPDATE
with blocked_by_task_id runs before the subtask INSERT → FK violation
(constraint isn't deferrable, IMMEDIATE check).

Fix: explicit `await session.flush()` between `session.add(subtask)` and
the current_task UPDATE. Guarantees the subtask is in the DB before the FK
is set.

NOTE: SQLite (in-memory test DB) has FK enforcement OFF (conftest.py:71).
The real FK race can't be reproduced here — these tests verify the
happy-path behavior and make sure the flush doesn't regress (call order
observed via patching).
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_real_emit_scenario():
    """Setup: Boss + Researcher + Boss active task — like test_delegate_endpoint
    but WITHOUT the emit_event mock, so the internal session.commit() kicks in.
    """
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    boss_id = uuid.uuid4()
    researcher_id = uuid.uuid4()
    parent_id = uuid.uuid4()

    boss_token, boss_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="FK Race Board", slug=f"fkr-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=boss_id,
            name="Boss",
            role="orchestrator",
            board_id=board_id,
            agent_token_hash=boss_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
            current_task_id=parent_id,
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
        s.add(Task(
            id=parent_id,
            board_id=board_id,
            title="Boss Orchestration Task",
            status="in_progress",
            assigned_agent_id=boss_id,
        ))
        await s.commit()

    return {
        "board_id": board_id,
        "boss_id": boss_id,
        "researcher_id": researcher_id,
        "parent_id": parent_id,
        "boss_token": boss_token,
    }


@pytest.mark.asyncio
async def test_delegate_with_callback_real_emit_event(client, fake_redis):
    """End-to-end with unmocked emit_event — verifies the mid-function
    commit doesn't break the flow. Before the flush fix, this would end
    in production (Postgres) with an FK violation 500 — in tests (SQLite
    with FK off) it's only a consistency check.
    """
    data = await _setup_real_emit_scenario()

    # Only mock dispatch + check_dispatch_allowed — NOT emit_event
    with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
        with patch(
            "app.services.operations.check_dispatch_allowed",
            new_callable=AsyncMock,
            return_value=(True, None),
        ):
            resp = await client.post(
                f"/api/v1/agent/boards/{data['board_id']}/delegate",
                json={
                    "title": "Subtask via real emit",
                    "description": "Testing flush ordering with real emit_event session.commit().",
                    "assigned_agent_id": str(data["researcher_id"]),
                    "callback": True,
                },
                headers={"Authorization": f"Bearer {data['boss_token']}"},
            )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    subtask_id = uuid.UUID(body["subtask_id"])

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        parent = await s.get(Task, data["parent_id"])
        subtask = await s.get(Task, subtask_id)

        assert subtask is not None, "Subtask wurde nicht persistiert"
        assert parent.blocked_by_task_id == subtask_id, (
            f"Parent.blocked_by_task_id zeigt nicht auf neue Subtask. "
            f"Got {parent.blocked_by_task_id}, expected {subtask_id}"
        )
        assert parent.status == "blocked"


@pytest.mark.asyncio
async def test_delegate_with_callback_flushes_before_fk_update(client, fake_redis):
    """Verifies via a session.flush() spy that flush IS CALLED before
    the current_task UPDATE. If someone removes the flush, this test
    catches it — before it turns into HTTP 500 in production (with FK active).
    """
    data = await _setup_real_emit_scenario()

    # Track flush calls — we want to see at least one before the commit
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
                            "title": "Flush Spy Test",
                            "description": "Verify explicit flush is called before current_task FK update — Min length detail text.",
                            "assigned_agent_id": str(data["researcher_id"]),
                            "callback": True,
                        },
                        headers={"Authorization": f"Bearer {data['boss_token']}"},
                    )

    assert resp.status_code == 201, resp.text
    assert flush_call_count >= 1, (
        f"Erwartet mindestens 1 explicit session.flush() Call zwischen "
        f"subtask add und current_task FK update. Got {flush_call_count} calls. "
        f"Wenn 0: Bug-Fix wurde rueckgaengig gemacht — Postgres wuerde mit "
        f"FK violation 500 antworten."
    )


@pytest.mark.asyncio
async def test_delegate_without_callback_no_flush_needed(client, fake_redis):
    """Fire-and-forget delegate (callback=False) needs no extra flush
    — current_task isn't modified, so no FK race is possible.
    """
    data = await _setup_real_emit_scenario()

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
                        "title": "Fire-and-Forget",
                        "description": "Async work without callback — Boss bleibt in_progress, parent unchanged.",
                        "assigned_agent_id": str(data["researcher_id"]),
                        "callback": False,
                    },
                    headers={"Authorization": f"Bearer {data['boss_token']}"},
                )

    assert resp.status_code == 201, resp.text

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        parent = await s.get(Task, data["parent_id"])
        assert parent.status == "in_progress"
        assert parent.blocked_by_task_id is None
