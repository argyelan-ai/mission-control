"""Loops L1 (ADR-051): Runner-Rundenzyklus, Circuit-Breaker, Gates, Router."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.approval import Approval
from app.models.board import Board
from app.models.loop import Loop, LoopRound
from app.models.task import Task, TaskComment

from tests.conftest import test_engine


async def _mk_board(**kw) -> Board:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(
            id=uuid.uuid4(), name="LoopBoard", slug=f"lb-{uuid.uuid4().hex[:6]}",
            auto_dispatch_enabled=False,  # Tests: kein echter Dispatch
            **kw,
        )
        s.add(board)
        await s.commit()
        return board


async def _mk_loop(board: Board, **kw) -> Loop:
    defaults = dict(
        board_id=board.id, name="Polish loop",
        goal="Verbessere die Qualität Runde für Runde.",
        backlog_source="markdown", backlog_md="- [ ] Item A\n- [ ] Item B",
        status="running", max_rounds=5,
    )
    defaults.update(kw)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        loop = Loop(**defaults)
        s.add(loop)
        await s.commit()
        await s.refresh(loop)
        return loop


def _runner():
    from app.services.loop_runner import LoopRunnerService
    return LoopRunnerService()


async def _tick(fake_redis):
    runner = _runner()
    with patch("app.services.loop_runner.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_create.emit_event", new_callable=AsyncMock), \
         patch("app.services.telegram_bot.telegram_bot.send_approval_telegram",
               new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await runner.tick(s)


async def _get_loop(loop_id) -> Loop:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(Loop, loop_id)


async def _set_task_status(task_id, status_val):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        task.status = status_val
        s.add(task)
        await s.commit()


async def _pending_gates() -> list[Approval]:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return list((await s.exec(
            select(Approval).where(
                Approval.action_type == "loop_gate",
                Approval.status == "pending",
            )
        )).all())


# ── Runner: Rundenzyklus ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_tick_starts_round_one(fake_redis):
    board = await _mk_board()
    loop = await _mk_loop(board)

    await _tick(fake_redis)

    fresh = await _get_loop(loop.id)
    assert fresh.current_round_no == 1
    assert fresh.current_task_id is not None

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, fresh.current_task_id)
        rounds = (await s.exec(
            select(LoopRound).where(LoopRound.loop_id == loop.id)
        )).all()
    assert task is not None
    assert "Loop round 1/5" in task.title
    assert "Ziel des Loops" in (task.description or "")
    assert "Item A" in task.description
    assert task.is_auto_created is True
    assert task.assigned_agent_id is None  # Board-Lead-first
    assert len(rounds) == 1 and rounds[0].round_no == 1


@pytest.mark.asyncio
async def test_running_round_is_left_alone(fake_redis):
    board = await _mk_board()
    loop = await _mk_loop(board)
    await _tick(fake_redis)  # startet Runde 1

    await _tick(fake_redis)  # Runde läuft (inbox) → nichts passiert
    fresh = await _get_loop(loop.id)
    assert fresh.current_round_no == 1
    assert fresh.rounds_completed == 0


@pytest.mark.asyncio
async def test_done_round_starts_next_with_report_continuity(fake_redis):
    board = await _mk_board()
    loop = await _mk_loop(board)
    await _tick(fake_redis)
    loop1 = await _get_loop(loop.id)

    # Runde 1 liefert Reflexion + done
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=loop1.current_task_id, author_type="agent",
            comment_type="reflection", content="Item A erledigt, sauber getestet.",
        ))
        await s.commit()
    await _set_task_status(loop1.current_task_id, "done")

    await _tick(fake_redis)  # wertet Runde 1 aus + startet Runde 2

    fresh = await _get_loop(loop.id)
    assert fresh.rounds_completed == 1
    assert fresh.current_round_no == 2
    assert fresh.consecutive_failed_rounds == 0

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        round1 = (await s.exec(
            select(LoopRound).where(
                LoopRound.loop_id == loop.id, LoopRound.round_no == 1)
        )).first()
        task2 = await s.get(Task, fresh.current_task_id)
    assert round1.outcome == "done"
    assert "Item A erledigt" in round1.report
    # Kontinuität: Report der Runde 1 steht im Brief von Runde 2
    assert "Reports der letzten 1 Runden" in task2.description
    assert "Item A erledigt" in task2.description


@pytest.mark.asyncio
async def test_circuit_breaker_pauses_and_creates_gate(fake_redis):
    board = await _mk_board()
    loop = await _mk_loop(board, pause_on_failed_rounds=2)

    for expected_fails in (1, 2):
        await _tick(fake_redis)  # startet Runde
        fresh = await _get_loop(loop.id)
        await _set_task_status(fresh.current_task_id, "failed")
        await _tick(fake_redis)  # wertet aus
        fresh = await _get_loop(loop.id)
        if expected_fails < 2:
            assert fresh.status == "running"
            assert fresh.consecutive_failed_rounds == expected_fails

    assert fresh.status == "paused"
    assert fresh.consecutive_failed_rounds == 2
    gates = await _pending_gates()
    assert len(gates) == 1
    assert gates[0].payload["reason"] == "circuit_breaker"

    # Pausierter Loop startet ohne Operator-Go keine neue Runde
    await _tick(fake_redis)
    assert (await _get_loop(loop.id)).current_round_no == fresh.current_round_no


@pytest.mark.asyncio
async def test_max_rounds_finishes_loop(fake_redis):
    board = await _mk_board()
    loop = await _mk_loop(board, max_rounds=1)
    await _tick(fake_redis)
    fresh = await _get_loop(loop.id)
    await _set_task_status(fresh.current_task_id, "done")
    await _tick(fake_redis)

    fresh = await _get_loop(loop.id)
    assert fresh.status == "done"
    assert fresh.finished_at is not None
    assert fresh.rounds_completed == 1


@pytest.mark.asyncio
async def test_backlog_empty_reflection_finishes_loop(fake_redis):
    board = await _mk_board()
    loop = await _mk_loop(board, max_rounds=10, stop_on_backlog_empty=True)
    await _tick(fake_redis)
    fresh = await _get_loop(loop.id)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=fresh.current_task_id, author_type="agent",
            comment_type="reflection",
            content="Alles erledigt — BACKLOG LEER.",
        ))
        await s.commit()
    await _set_task_status(fresh.current_task_id, "done")
    await _tick(fake_redis)

    assert (await _get_loop(loop.id)).status == "done"


@pytest.mark.asyncio
async def test_scheduled_human_gate(fake_redis):
    board = await _mk_board()
    loop = await _mk_loop(board, human_every_n_rounds=1, max_rounds=5)
    await _tick(fake_redis)
    fresh = await _get_loop(loop.id)
    await _set_task_status(fresh.current_task_id, "done")
    await _tick(fake_redis)

    fresh = await _get_loop(loop.id)
    assert fresh.status == "waiting_gate"
    gates = await _pending_gates()
    assert len(gates) == 1 and gates[0].payload["reason"] == "scheduled_gate"


@pytest.mark.asyncio
async def test_deleted_round_task_counts_as_failed_via_endpoint(
    auth_client: AsyncClient, fake_redis,
):
    """Review-Fund M1: Der ECHTE Delete-Endpoint muss die Fehlrunden-Wertung
    auslösen — blosses FK-Nullen würde den Circuit-Breaker umgehen."""
    board = await _mk_board()
    loop = await _mk_loop(board, pause_on_failed_rounds=99)
    await _tick(fake_redis)
    fresh = await _get_loop(loop.id)

    with patch("app.services.loop_runner.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_create.emit_event", new_callable=AsyncMock):
        r = await auth_client.delete(
            f"/api/v1/boards/{board.id}/tasks/{fresh.current_task_id}"
        )
    assert r.status_code in (200, 204), r.text

    fresh = await _get_loop(loop.id)
    assert fresh.consecutive_failed_rounds == 1
    assert fresh.rounds_completed == 1
    assert fresh.current_round_no == 2  # nächste Runde direkt gestartet
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        round1 = (await s.exec(
            select(LoopRound).where(
                LoopRound.loop_id == loop.id, LoopRound.round_no == 1)
        )).first()
    assert round1.outcome == "failed"
    assert round1.task_id is None  # FK gelöst


@pytest.mark.asyncio
async def test_deleted_task_direct_db_still_counts_as_failed(fake_redis):
    """Safety-Net: Task verschwindet am Endpoint vorbei (direktes DB-Delete)
    → der Runner-Tick wertet die Runde trotzdem als Fehlrunde."""
    board = await _mk_board()
    loop = await _mk_loop(board, pause_on_failed_rounds=99)
    await _tick(fake_redis)
    fresh = await _get_loop(loop.id)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, fresh.current_task_id)
        await s.delete(task)
        await s.commit()

    await _tick(fake_redis)
    fresh = await _get_loop(loop.id)
    assert fresh.consecutive_failed_rounds == 1
    assert fresh.current_round_no == 2


# ── loop_gate-Resolve ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_approve_resumes_loop(auth_client: AsyncClient, fake_redis):
    board = await _mk_board()
    loop = await _mk_loop(board, status="paused", consecutive_failed_rounds=2)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            board_id=board.id, action_type="loop_gate",
            description="x", payload={"loop_id": str(loop.id), "reason": "circuit_breaker"},
        )
        s.add(approval)
        await s.commit()
        await s.refresh(approval)

    r = await auth_client.patch(
        f"/api/v1/approvals/{approval.id}", json={"status": "approved"}
    )
    assert r.status_code == 200, r.text
    fresh = await _get_loop(loop.id)
    assert fresh.status == "running"
    assert fresh.consecutive_failed_rounds == 0


@pytest.mark.asyncio
async def test_gate_reject_keeps_paused(auth_client: AsyncClient, fake_redis):
    board = await _mk_board()
    loop = await _mk_loop(board, status="waiting_gate")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            board_id=board.id, action_type="loop_gate",
            description="x", payload={"loop_id": str(loop.id), "reason": "scheduled_gate"},
        )
        s.add(approval)
        await s.commit()
        await s.refresh(approval)

    r = await auth_client.patch(
        f"/api/v1/approvals/{approval.id}", json={"status": "rejected"}
    )
    assert r.status_code == 200, r.text
    assert (await _get_loop(loop.id)).status == "paused"


@pytest.mark.asyncio
async def test_gate_approve_via_telegram_quick_resolve(auth_client: AsyncClient, fake_redis):
    """Telegram-Quick-Resolve hat einen EIGENEN Resolve-Pfad — der muss den
    Loop genauso weiterschalten wie resolve_approval (Review-Fund)."""
    board = await _mk_board()
    loop = await _mk_loop(board, status="paused", consecutive_failed_rounds=2)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            board_id=board.id, action_type="loop_gate",
            description="x", payload={"loop_id": str(loop.id), "reason": "circuit_breaker"},
        )
        s.add(approval)
        await s.commit()
        await s.refresh(approval)

    with patch(
        "app.routers.approvals.consume_action_token",
        new=AsyncMock(return_value={"approval_id": str(approval.id), "action": "approve"}),
    ), patch(
        "app.services.telegram_bot.telegram_bot.update_resolved_telegram",
        new_callable=AsyncMock,
    ):
        r = await auth_client.post(
            f"/api/v1/approvals/{approval.id}/quick-resolve/confirm",
            data={"token": "tok"},
        )
    assert r.status_code == 200, r.text
    fresh = await _get_loop(loop.id)
    assert fresh.status == "running"
    assert fresh.consecutive_failed_rounds == 0


# ── Router ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_requires_backlog_for_markdown(auth_client: AsyncClient):
    board = await _mk_board()
    r = await auth_client.post("/api/v1/loops", json={
        "board_id": str(board.id), "name": "x", "goal": "g",
        "backlog_source": "markdown",
    })
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_one_active_loop_per_board(auth_client: AsyncClient):
    board = await _mk_board()
    active = await _mk_loop(board, status="running")
    other = await _mk_loop(board, status="draft", name="Second loop")

    r = await auth_client.post(f"/api/v1/loops/{other.id}/start")
    assert r.status_code == 409
    assert active.name in r.json()["detail"]

    # Anderes Board → kein Konflikt
    board2 = await _mk_board()
    third = await _mk_loop(board2, status="draft", name="Third")
    r2 = await auth_client.post(f"/api/v1/loops/{third.id}/start")
    assert r2.status_code == 200
    assert r2.json()["status"] == "running"


@pytest.mark.asyncio
async def test_start_supersedes_pending_gate(auth_client: AsyncClient):
    board = await _mk_board()
    loop = await _mk_loop(board, status="paused")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Approval(
            board_id=board.id, action_type="loop_gate",
            description="x", payload={"loop_id": str(loop.id)},
        ))
        await s.commit()

    r = await auth_client.post(f"/api/v1/loops/{loop.id}/start")
    assert r.status_code == 200
    assert await _pending_gates() == []


@pytest.mark.asyncio
async def test_stop_and_delete_flow(auth_client: AsyncClient):
    board = await _mk_board()
    loop = await _mk_loop(board, status="running", rounds_completed=3)

    r0 = await auth_client.delete(f"/api/v1/loops/{loop.id}")
    assert r0.status_code == 409  # aktiv → erst stoppen

    r1 = await auth_client.post(f"/api/v1/loops/{loop.id}/stop")
    assert r1.status_code == 200 and r1.json()["status"] == "done"

    r2 = await auth_client.delete(f"/api/v1/loops/{loop.id}")
    assert r2.status_code == 204

    r3 = await auth_client.get(f"/api/v1/loops/{loop.id}")
    assert r3.status_code == 404


@pytest.mark.asyncio
async def test_detail_includes_rounds(auth_client: AsyncClient):
    board = await _mk_board()
    loop = await _mk_loop(board)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(LoopRound(loop_id=loop.id, round_no=1, outcome="done", report="R1"))
        await s.commit()

    r = await auth_client.get(f"/api/v1/loops/{loop.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == loop.name
    assert len(body["rounds"]) == 1
    assert body["rounds"][0]["report"] == "R1"


@pytest.mark.asyncio
async def test_aborted_round_is_terminal_and_counts_as_failed(fake_redis):
    """An aborted round task must not hang the loop forever — it completes
    the round with outcome 'aborted' and counts toward the circuit breaker."""
    board = await _mk_board()
    loop = await _mk_loop(board)
    await _tick(fake_redis)  # startet Runde 1

    fresh = await _get_loop(loop.id)
    await _set_task_status(fresh.current_task_id, "aborted")
    await _tick(fake_redis)  # wertet die abgebrochene Runde aus

    fresh = await _get_loop(loop.id)
    assert fresh.rounds_completed == 1
    assert fresh.consecutive_failed_rounds == 1
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rounds = (await s.exec(
            select(LoopRound).where(LoopRound.loop_id == loop.id)
        )).all()
    assert rounds[0].outcome == "aborted"
