"""Tests fuer Subagent-Hardening (Phase 1) — Spawn Tracking, Lifecycle, Recovery.

Testet die Aenderungen aus Phase 1.1-1.5:
- Spawn-Session-IDs werden persistiert (Phase 1.2)
- Spawn-Session-IDs werden bei Lifecycle-Events geloescht (Phase 1.2)
- Dependency-Zombie-Erkennung (Phase 1.5)
- Recovery-Dedup-Keys sind konsistent (Phase 1.5)
"""
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Test 1: Spawn-Tracking Lifecycle — clear on terminal states ────────


@pytest.mark.asyncio
async def test_clear_spawn_tracking_on_done(make_board, make_agent, make_task):
    """spawn_run_id/spawn_session_key werden bei status=done geloescht."""
    board = await make_board(name="Lifecycle Board", slug="lifecycle")
    agent = await make_agent(name="Worker", board_id=board.id, is_board_lead=False)
    task = await make_task(
        board_id=board.id, title="Track Task", status="in_progress",
        assigned_agent_id=agent.id,
        spawn_run_id="run-123",
        spawn_session_key="agent:cody:task-abc",
    )

    from app.services.task_lifecycle import clear_spawn_tracking
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        assert t.spawn_run_id == "run-123"
        assert t.spawn_session_key == "agent:cody:task-abc"

        clear_spawn_tracking(t)
        s.add(t)
        await s.commit()

        await s.refresh(t)
        assert t.spawn_run_id is None
        assert t.spawn_session_key is None


# ── Test 2: update_agent_active_task clears spawn on terminal ──────────


@pytest.mark.asyncio
async def test_update_agent_active_task_clears_spawn_on_done(make_board, make_agent, make_task):
    """update_agent_active_task loescht spawn-IDs wenn Task done wird."""
    board = await make_board(name="Active Board", slug="active")
    agent = await make_agent(
        name="Worker2", board_id=board.id, is_board_lead=False,
    )
    task = await make_task(
        board_id=board.id, title="Active Task", status="in_progress",
        assigned_agent_id=agent.id,
        spawn_run_id="run-456",
        spawn_session_key="agent:worker2:task-def",
    )

    from app.services.task_lifecycle import update_agent_active_task
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        with patch("app.config.settings") as mock_settings:
            mock_settings.use_subagent_dispatch = True
            await update_agent_active_task(s, agent.id, t, "done", "in_progress")
        await s.commit()

        await s.refresh(t)
        assert t.spawn_run_id is None
        assert t.spawn_session_key is None


# ── Test 3: Isolated session result persisted after chat_send_isolated ──




# ── Test 4: Dependency-Zombie erkennen ─────────────────────────────────


@pytest.mark.asyncio
async def test_dependency_zombie_creates_approval(fake_redis, make_board, make_agent, make_task):
    """Task der auf failed Dependency wartet → Approval wird erstellt."""
    board = await make_board(name="Zombie Board", slug="zombie")
    agent = await make_agent(name="ZombieWorker", board_id=board.id, is_board_lead=False)

    # Dependency-Task ist failed
    dep_task = await make_task(board_id=board.id, title="Failed Dep", status="failed")
    # Haupttask wartet auf Dependency
    main_task = await make_task(
        board_id=board.id, title="Waiting Task", status="inbox",
        assigned_agent_id=agent.id,
    )

    # Dependency erstellen
    from app.models.task import TaskDependency, Task
    from app.models.approval import Approval
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        dep = TaskDependency(
            id=uuid.uuid4(),
            task_id=main_task.id,
            depends_on_task_id=dep_task.id,
        )
        s.add(dep)
        await s.commit()

    # Watchdog-Check ausfuehren
    from app.services.watchdog.task_monitor import TaskMonitorMixin

    mixin = TaskMonitorMixin()

    with patch("app.services.watchdog.task_monitor.get_redis", return_value=fake_redis), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await mixin._check_dependency_zombies(s)

    # Approval muss existieren
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from sqlmodel import select
        result = await s.exec(
            select(Approval).where(
                Approval.task_id == main_task.id,
                Approval.action_type == "dependency_zombie",
            )
        )
        approval = result.first()
        assert approval is not None
        assert "Failed Dep" in approval.description
        assert "failed" in approval.description


# ── Test 5: Dependency-Zombie — keine false positives ──────────────────


@pytest.mark.asyncio
async def test_no_zombie_when_dependency_done(fake_redis, make_board, make_task):
    """Keine Zombie-Erkennung wenn Dependency done ist."""
    board = await make_board(name="OK Board", slug="ok-board")

    dep_task = await make_task(board_id=board.id, title="Done Dep", status="done")
    main_task = await make_task(board_id=board.id, title="OK Task", status="inbox")

    from app.models.task import TaskDependency
    from app.models.approval import Approval
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        dep = TaskDependency(
            id=uuid.uuid4(),
            task_id=main_task.id,
            depends_on_task_id=dep_task.id,
        )
        s.add(dep)
        await s.commit()

    from app.services.watchdog.task_monitor import TaskMonitorMixin
    mixin = TaskMonitorMixin()

    with patch("app.services.watchdog.task_monitor.get_redis", return_value=fake_redis), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await mixin._check_dependency_zombies(s)

    # Kein Approval erstellt
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from sqlmodel import select
        result = await s.exec(
            select(Approval).where(Approval.action_type == "dependency_zombie")
        )
        assert result.first() is None


# ── Test 6: Recovery-Dedup-Keys konsistent ─────────────────────────────


def test_redis_recovery_keys_consistent():
    """RedisKeys.recovery_attempt() generiert konsistente Keys."""
    from app.redis_client import RedisKeys

    task_id = "abc-123"

    # Alle Recovery-Typen pruefen
    assert RedisKeys.recovery_attempt(task_id, "aborted") == "mc:recovery:abc-123:aborted"
    assert RedisKeys.recovery_attempt(task_id, "session_loss") == "mc:recovery:abc-123:session_loss"
    assert RedisKeys.recovery_attempt(task_id, "spawn_timeout") == "mc:recovery:abc-123:spawn_timeout"
    assert RedisKeys.recovery_attempt(task_id, "dependency_zombie") == "mc:recovery:abc-123:dependency_zombie"

    # Alle starten mit mc:recovery: (konsistent)
    for rtype in ["aborted", "session_loss", "spawn_timeout", "dependency_zombie"]:
        key = RedisKeys.recovery_attempt(task_id, rtype)
        assert key.startswith("mc:recovery:")


# Phase 29: Test 7 (_check_spawn_timeouts) entfernt — Methode operierte auf
# Gateway-spezifischer task.spawn_session_key + sessions_list. Cli-bridge
# Agents haben kein vergleichbares Konstrukt. TODO Phase 31: equivalent
# cli-bridge task-queue timeout test.
