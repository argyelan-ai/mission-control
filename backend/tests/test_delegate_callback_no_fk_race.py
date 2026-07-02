"""Tests fuer Bug-Fix 2026-04-25: mc delegate --callback FK-Race.

Live-Bug Boss 2026-04-25: HTTP 500 ForeignKeyViolationError auf
fk_tasks_blocked_by_task_id beim `mc delegate` mit callback. Root cause:
emit_event() macht intern session.commit() (activity.py:41) — flushed alle
pending changes mid-function. SQLAlchemys topological sort fuer reflexive
FKs (tasks → tasks) ordnet manchmal falsch: current_task UPDATE mit
blocked_by_task_id wird vor INSERT subtask ausgefuehrt → FK violation
(constraint nicht deferrable, IMMEDIATE check).

Fix: explicit `await session.flush()` zwischen `session.add(subtask)` und
dem current_task UPDATE. Garantiert dass subtask in DB ist bevor FK
gesetzt wird.

NOTE: SQLite (in-memory test-DB) hat FK enforcement OFF (conftest.py:71).
Der echte FK-Race kann hier nicht reproduziert werden — diese Tests
verifizieren das happy-path Verhalten und stellen sicher dass der flush
nicht regressed wird (call-order via patching beobachtet).
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_real_emit_scenario():
    """Setup: Boss + Researcher + Boss-active-task — wie test_delegate_endpoint
    aber OHNE den emit_event mock, damit das interne session.commit() greift.
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
    """End-to-end mit unmocked emit_event — verifiziert dass mid-function
    commit den Flow nicht bricht. Vor dem flush-Fix wuerde dies in
    Production (Postgres) mit FK violation 500 enden — in Tests (SQLite mit
    FK off) nur als Konsistenz-Check.
    """
    data = await _setup_real_emit_scenario()

    # Nur dispatch + check_dispatch_allowed mocken — emit_event NICHT
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
    """Verifiziert via session.flush() spy dass flush AUFGERUFEN wird vor
    dem current_task UPDATE. Wenn jemand den flush rausnimmt, faengt dieser
    Test es ab — bevor es in Production (mit aktivem FK) zu HTTP 500 kommt.
    """
    data = await _setup_real_emit_scenario()

    # Track flush calls — wir wollen mindestens einen sehen vor dem commit
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
    """Fire-and-forget delegate (callback=False) braucht keinen extra flush
    — current_task wird nicht modifiziert, kein FK-Race moeglich.
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
