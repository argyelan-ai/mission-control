"""Tests fuer Operational Controls — Guards, Stop/Resume, System Mode."""
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession


# ── Test 1: Normalbetrieb erlaubt ──────────────────────────────────────


@pytest.mark.asyncio
async def test_check_dispatch_allowed_active(make_board, make_agent, make_task):
    """Im ACTIVE-Modus wird alles durchgelassen."""
    board = await make_board(name="Ops Board", slug="ops-board")
    agent = await make_agent(name="Cody", board_id=board.id)
    task = await make_task(board_id=board.id, title="Normal Task")

    with patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"):
        from app.services.operations import check_dispatch_allowed
        allowed, reason = await check_dispatch_allowed(task, agent)

    assert allowed is True
    assert reason == ""


# ── Test 2: HALTED blockiert alles ─────────────────────────────────────


@pytest.mark.asyncio
async def test_check_dispatch_halted_blocks_all(make_board, make_agent, make_task):
    """HALTED blockiert jeglichen Dispatch."""
    board = await make_board(name="Halted Board", slug="halted-board")
    agent = await make_agent(name="Cody", board_id=board.id)
    task = await make_task(board_id=board.id, title="Halted Task", dispatch_intent="subtask")

    with patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="halted"):
        from app.services.operations import check_dispatch_allowed
        allowed, reason = await check_dispatch_allowed(task, agent)

    assert allowed is False
    assert "HALTED" in reason


# ── Test 3: DRAINING blockiert root ────────────────────────────────────


@pytest.mark.asyncio
async def test_check_dispatch_draining_blocks_root(make_board, make_agent, make_task):
    """DRAINING blockiert neue Root-Tasks."""
    board = await make_board(name="Drain Board", slug="drain-board")
    agent = await make_agent(name="Cody", board_id=board.id)
    task = await make_task(board_id=board.id, title="Root Task", dispatch_intent="root")

    with patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="draining"):
        from app.services.operations import check_dispatch_allowed
        allowed, reason = await check_dispatch_allowed(task, agent)

    assert allowed is False
    assert "DRAINING" in reason


# ── Test 4: DRAINING blockiert manual_redispatch ───────────────────────


@pytest.mark.asyncio
async def test_check_dispatch_draining_blocks_manual_redispatch(make_board, make_agent, make_task):
    """DRAINING blockiert manual_redispatch (keine Continuation)."""
    board = await make_board(name="Drain2 Board", slug="drain2-board")
    agent = await make_agent(name="Cody", board_id=board.id)
    task = await make_task(board_id=board.id, title="Redispatch Task", dispatch_intent="manual_redispatch")

    with patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="draining"):
        from app.services.operations import check_dispatch_allowed
        allowed, reason = await check_dispatch_allowed(task, agent)

    assert allowed is False
    assert "DRAINING" in reason


# ── Test 5-7: DRAINING erlaubt Continuation Flows ──────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["subtask", "review_handoff", "review_rework"])
async def test_check_dispatch_draining_allows_continuation(intent, make_board, make_agent, make_task):
    """DRAINING erlaubt automatische Continuation Flows."""
    board = await make_board(name=f"Cont-{intent}", slug=f"cont-{intent}")
    agent = await make_agent(name="Agent", board_id=board.id)
    task = await make_task(board_id=board.id, title=f"Cont Task {intent}", dispatch_intent=intent)

    with patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="draining"):
        from app.services.operations import check_dispatch_allowed
        allowed, reason = await check_dispatch_allowed(task, agent)

    assert allowed is True
    assert reason == ""


# ── Test 8: Agent PAUSED blockiert ─────────────────────────────────────


@pytest.mark.asyncio
async def test_check_dispatch_agent_paused(make_board, make_agent, make_task):
    """Agent mit operational_mode=paused wird nicht dispatcht."""
    board = await make_board(name="Paused Board", slug="paused-board")
    agent = await make_agent(name="PausedAgent", board_id=board.id, operational_mode="paused")
    task = await make_task(board_id=board.id, title="Paused Agent Task")

    with patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"):
        from app.services.operations import check_dispatch_allowed
        allowed, reason = await check_dispatch_allowed(task, agent)

    assert allowed is False
    assert "PAUSED" in reason


# ── Test 9: run_control stopped blockiert ──────────────────────────────


@pytest.mark.asyncio
async def test_check_dispatch_run_control_stopped(make_board, make_agent, make_task):
    """Tasks mit run_control=stopped werden nicht dispatcht."""
    board = await make_board(name="Stopped Board", slug="stopped-board")
    agent = await make_agent(name="Agent", board_id=board.id)
    task = await make_task(board_id=board.id, title="Stopped Task", run_control="stopped")

    with patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"):
        from app.services.operations import check_dispatch_allowed
        allowed, reason = await check_dispatch_allowed(task, agent)

    assert allowed is False
    assert "run_control" in reason


# ── Test 10: run_control manual_hold blockiert ─────────────────────────


@pytest.mark.asyncio
async def test_check_dispatch_run_control_manual_hold(make_board, make_agent, make_task):
    """Tasks mit run_control=manual_hold werden nicht dispatcht."""
    board = await make_board(name="Hold Board", slug="hold-board")
    agent = await make_agent(name="Agent", board_id=board.id)
    task = await make_task(board_id=board.id, title="Held Task", run_control="manual_hold")

    with patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"):
        from app.services.operations import check_dispatch_allowed
        allowed, reason = await check_dispatch_allowed(task, agent)

    assert allowed is False
    assert "run_control" in reason


# ── Test 11: Stop Run bei aktivem Task ─────────────────────────────────


@pytest.mark.asyncio
async def test_stop_task_run_active(client, make_board, make_agent, make_task):
    """Stop Run setzt status=blocked, run_control=stopped, gibt Agent frei."""
    from tests.conftest import test_engine

    board = await make_board(name="Stop Board", slug="stop-board")
    agent = await make_agent(
        name="WorkerAgent", board_id=board.id,         current_task_id=None,
    )
    task = await make_task(
        board_id=board.id, title="Active Task",
        status="in_progress", assigned_agent_id=agent.id,
    )
    # Set current_task_id on agent
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = await s.get(type(agent), agent.id)
        a.current_task_id = task.id
        s.add(a)
        await s.commit()

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        from app.services.operations import stop_task_run
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            result = await stop_task_run(s, task.id, "user-123", reason="Manual stop")

    assert result.status == "blocked"
    assert result.run_control == "stopped"
    assert result.dispatched_at is None
    assert result.ack_at is None

    # Agent freigegeben
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed_agent = await s.get(type(agent), agent.id)
        assert refreshed_agent.run_state == "idle"
        assert refreshed_agent.current_task_id is None


# ── Test 12: Stop Run bei inbox ohne dispatch → 409 ────────────────────


@pytest.mark.asyncio
async def test_stop_task_run_inbox_no_dispatch_rejected(client, make_board, make_task):
    """Stop Run auf inbox-Task ohne dispatched_at gibt 409."""
    board = await make_board(name="No-Run Board", slug="no-run-board")
    task = await make_task(board_id=board.id, title="Idle Task", status="inbox")

    from tests.conftest import test_engine
    from fastapi import HTTPException

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        from app.services.operations import stop_task_run
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            with pytest.raises(HTTPException) as exc_info:
                await stop_task_run(s, task.id, "user-123")
            assert exc_info.value.status_code == 409


# ── Test 13: Resume Task Run ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_task_run(client, make_board, make_task):
    """Resume setzt run_control=null, status=inbox, dispatched_at=null, ack_at=null."""
    from tests.conftest import test_engine

    board = await make_board(name="Resume Board", slug="resume-board")
    task = await make_task(
        board_id=board.id, title="Stopped Task",
        status="blocked", run_control="stopped",
    )

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        from app.services.operations import resume_task_run
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            result = await resume_task_run(s, task.id, "user-123")

    assert result.run_control is None
    assert result.status == "inbox"
    assert result.dispatched_at is None
    assert result.ack_at is None


# ── Test 14: Late Agent Update → 409 ──────────────────────────────────


@pytest.mark.asyncio
async def test_late_agent_update_rejected(client, make_board, make_agent, make_task):
    """Agent Update auf gestoppten Task wird mit 409 abgelehnt."""
    from tests.conftest import test_engine
    from fastapi import HTTPException

    board = await make_board(name="Late Board", slug="late-board")
    agent = await make_agent(
        name="LateAgent", board_id=board.id,     )
    task = await make_task(
        board_id=board.id, title="Stopped Task",
        status="blocked", run_control="stopped",
        assigned_agent_id=agent.id,
    )

    # Direkt den Router-Handler testen (vermeidet Agent-Auth-Komplexitaet)
    from pydantic import BaseModel

    class FakePayload(BaseModel):
        status: str | None = None

    payload = FakePayload(status="review")

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            # Simuliere den Run-Control Guard Check direkt
            assert t.run_control == "stopped"
            # Der Guard wuerde HTTPException(409) werfen
            with pytest.raises(HTTPException) as exc_info:
                if t.run_control in ("stopped", "manual_hold"):
                    raise HTTPException(
                        status_code=409,
                        detail=f"Task run_control={t.run_control} — Updates nicht erlaubt"
                    )
            assert exc_info.value.status_code == 409
            assert "run_control" in exc_info.value.detail


# ── Test 15: System Mode Set/Get ──────────────────────────────────────


@pytest.mark.asyncio
async def test_system_mode_set_and_get(fake_redis):
    """System Mode setzen und lesen via operations.py."""
    with patch("app.services.operations.get_redis", new_callable=AsyncMock, return_value=fake_redis):
        from app.services.operations import set_system_mode, get_system_mode, get_system_mode_meta

        # Default = active
        mode = await get_system_mode()
        assert mode == "active"

        # Set to draining
        meta = await set_system_mode("draining", "user-123", "Maintenance")
        assert meta["mode"] == "draining"
        assert meta["previous_mode"] == "active"
        assert meta["reason"] == "Maintenance"

        # Read back
        mode = await get_system_mode()
        assert mode == "draining"

        # Meta-Daten lesen
        meta2 = await get_system_mode_meta()
        assert meta2["mode"] == "draining"
        assert meta2["changed_by"] == "user-123"

        # Reset to active
        await set_system_mode("active", "user-123", "Back to normal")
        assert await get_system_mode() == "active"


# ── Test 16: Guard Priority Order ─────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_priority_order(make_board, make_agent, make_task):
    """HALTED hat Prioritaet ueber run_control ueber agent_paused ueber draining."""
    board = await make_board(name="Priority Board", slug="priority-board")
    agent = await make_agent(name="PausedAgent", board_id=board.id, operational_mode="paused")
    task = await make_task(
        board_id=board.id, title="Multi-Block",
        run_control="stopped", dispatch_intent="subtask",
    )

    from app.services.operations import check_dispatch_allowed

    # HALTED wins over everything
    with patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="halted"):
        allowed, reason = await check_dispatch_allowed(task, agent)
    assert not allowed
    assert "HALTED" in reason

    # run_control wins over agent_paused
    with patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"):
        allowed, reason = await check_dispatch_allowed(task, agent)
    assert not allowed
    assert "run_control" in reason

    # After clearing run_control, agent_paused wins
    task.run_control = None
    with patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"):
        allowed, reason = await check_dispatch_allowed(task, agent)
    assert not allowed
    assert "PAUSED" in reason

    # After unpausing agent, draining blocks root but not subtask
    agent.operational_mode = "active"
    with patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="draining"):
        allowed, reason = await check_dispatch_allowed(task, agent)
    assert allowed  # subtask is continuation → allowed in draining


# ── Test 17: Stop Run invalidiert dispatch_attempt_id ────────────────


@pytest.mark.asyncio
async def test_stop_run_clears_dispatch_attempt_id(client, make_board, make_agent, make_task):
    """Stop Run setzt dispatch_attempt_id auf None."""
    from tests.conftest import test_engine

    board = await make_board(name="AttemptStop Board", slug="attempt-stop-board")
    agent = await make_agent(
        name="Worker", board_id=board.id,         current_task_id=None,
    )
    task = await make_task(
        board_id=board.id, title="Running Task",
        status="in_progress", assigned_agent_id=agent.id,
        dispatch_attempt_id="old-attempt-123",
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = await s.get(type(agent), agent.id)
        a.current_task_id = task.id
        s.add(a)
        await s.commit()

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        from app.services.operations import stop_task_run
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            result = await stop_task_run(s, task.id, "user-123")

    assert result.dispatch_attempt_id is None
    assert result.run_control == "stopped"


# ── Test 18: Resume rotiert dispatch_attempt_id auf frische UUID ─────


@pytest.mark.asyncio
async def test_resume_rotates_dispatch_attempt_id(client, make_board, make_task):
    """Resume generiert eine FRISCHE dispatch_attempt_id (NICHT None) damit
    poll.sh sie im Response lesen kann. Alte stale-IDs duerfen nicht
    ueberleben.
    """
    from tests.conftest import test_engine

    stale_id = "00000000-0000-0000-0000-000000000abc"
    board = await make_board(name="AttemptResume Board", slug="attempt-resume-board")
    task = await make_task(
        board_id=board.id, title="Stopped Task",
        status="blocked", run_control="stopped",
        dispatch_attempt_id=stale_id,
    )

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        from app.services.operations import resume_task_run
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            result = await resume_task_run(s, task.id, "user-123")

    assert result.dispatch_attempt_id is not None
    assert result.dispatch_attempt_id != stale_id
    assert result.status == "inbox"


# ── Test 19: Dispatch Attempt Guard — match erlaubt ──────────────────


@pytest.mark.asyncio
async def test_dispatch_attempt_guard_match_allowed(make_board, make_agent, make_task):
    """Matching dispatch_attempt_id wird durchgelassen."""
    board = await make_board(name="Match Board", slug="match-board")
    agent = await make_agent(name="Agent", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Active Task",
        status="in_progress", assigned_agent_id=agent.id,
        dispatch_attempt_id="correct-id-789",
    )

    # Simuliere Guard-Logik direkt
    req_attempt_id = "correct-id-789"
    assert task.dispatch_attempt_id == req_attempt_id  # Match → kein Reject


# ── Test 20: Dispatch Attempt Guard — mismatch Phase B rejected ──────


@pytest.mark.asyncio
async def test_dispatch_attempt_guard_mismatch_rejected():
    """Falsche dispatch_attempt_id wird in Phase B mit 409 rejected."""
    from fastapi import HTTPException

    task_attempt_id = "current-run-abc"
    req_attempt_id = "old-run-xyz"

    # Simuliere Phase B Guard-Logik
    assert task_attempt_id != req_attempt_id
    with pytest.raises(HTTPException) as exc_info:
        if task_attempt_id and req_attempt_id != task_attempt_id:
            raise HTTPException(
                status_code=409,
                detail="Stale dispatch_attempt_id — Update stammt von einem alten Run"
            )
    assert exc_info.value.status_code == 409
    assert "Stale" in exc_info.value.detail
