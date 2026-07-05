"""Loops L2 (ADR-051): Telegram-Runden-Reports, Schedule-Trigger, Tag-Backlog.

Migration: 0144_loop_telegram_reports.py (loops.telegram_reports, default true).
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.board import Board
from app.models.loop import Loop
from app.models.scheduled_job import ScheduledJob
from app.models.tag import Tag, TagAssignment
from app.models.task import Task

from tests.conftest import test_engine


async def _mk_board(**kw) -> Board:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(
            id=uuid.uuid4(), name="LoopBoard", slug=f"lb-{uuid.uuid4().hex[:6]}",
            auto_dispatch_enabled=False,
            **kw,
        )
        s.add(board)
        await s.commit()
        return board


async def _mk_loop(board: Board, **kw) -> Loop:
    defaults = dict(
        board_id=board.id, name="Polish loop",
        goal="Verbessere die Qualität Runde für Runde.",
        backlog_source="markdown", backlog_md="- [ ] Item A",
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


def _mock_configured_reports_service():
    """Same helper pattern as test_report_back_gate.py."""
    mock = MagicMock()
    mock.configured = True
    mock.send = AsyncMock(return_value={"ok": True})
    return mock


def _mock_unconfigured_reports_service():
    mock = MagicMock()
    mock.configured = False
    mock.send = AsyncMock()
    return mock


# ── (a) Telegram-Runden-Reports ────────────────────────────────────────

@pytest.mark.asyncio
async def test_round_completion_sends_telegram_report(fake_redis):
    board = await _mk_board()
    loop = await _mk_loop(board, max_rounds=1)  # 1 round → loop finishes this round
    await _tick(fake_redis)  # start round 1
    fresh = await _get_loop(loop.id)
    await _set_task_status(fresh.current_task_id, "done")

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        await _tick(fake_redis)  # evaluate round 1

    mock_reports.send.assert_awaited_once()
    text = mock_reports.send.await_args.args[0]
    assert loop.name in text
    assert "1/1" in text
    assert "DONE" in text


@pytest.mark.asyncio
async def test_telegram_reports_opt_out_skips_send(fake_redis):
    board = await _mk_board()
    loop = await _mk_loop(board, max_rounds=1, telegram_reports=False)
    await _tick(fake_redis)
    fresh = await _get_loop(loop.id)
    await _set_task_status(fresh.current_task_id, "done")

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        await _tick(fake_redis)

    mock_reports.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_report_skipped_when_not_configured(fake_redis):
    """Not configured (no token/chat) → no crash, no send."""
    board = await _mk_board()
    loop = await _mk_loop(board, max_rounds=1)
    await _tick(fake_redis)
    fresh = await _get_loop(loop.id)
    await _set_task_status(fresh.current_task_id, "done")

    mock_reports = _mock_unconfigured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        await _tick(fake_redis)

    mock_reports.send.assert_not_awaited()


# ── (b) Schedule-Trigger (start_loop action) ────────────────────────────

def _job(**kw) -> ScheduledJob:
    defaults = dict(
        id=uuid.uuid4(), name="Nightly loop kickoff",
        schedule_type="daily", schedule_time="03:00",
        action_type="start_loop",
    )
    defaults.update(kw)
    return ScheduledJob(**defaults)


@pytest.mark.asyncio
async def test_schedule_starts_draft_loop():
    from app.services.scheduler import SchedulerService

    board = await _mk_board()
    loop = await _mk_loop(board, status="draft")
    job = _job(task_payload={"loop_id": str(loop.id)})

    svc = SchedulerService.__new__(SchedulerService)
    with patch("app.services.loop_runner.emit_event", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            success, error, detail = await svc._do_start_loop(s, job)

    assert success is True and error is None
    assert detail == {"loop_id": str(loop.id), "action": "started"}
    assert (await _get_loop(loop.id)).status == "running"


@pytest.mark.asyncio
async def test_schedule_start_loop_noop_when_already_running():
    from app.services.scheduler import SchedulerService

    board = await _mk_board()
    loop = await _mk_loop(board, status="running")
    job = _job(task_payload={"loopId": str(loop.id)})  # camelCase tolerant

    svc = SchedulerService.__new__(SchedulerService)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        success, error, detail = await svc._do_start_loop(s, job)

    assert success is True and error is None
    assert detail["action"] == "noop_already_running"
    assert (await _get_loop(loop.id)).status == "running"  # unchanged, no restart


@pytest.mark.asyncio
async def test_schedule_start_loop_missing_loop_id_fails():
    from app.services.scheduler import SchedulerService

    svc = SchedulerService.__new__(SchedulerService)
    job = _job(task_payload={})
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        success, error, detail = await svc._do_start_loop(s, job)

    assert success is False
    assert "loop_id" in error


@pytest.mark.asyncio
async def test_schedule_start_loop_conflict_when_other_loop_active():
    from app.services.scheduler import SchedulerService

    board = await _mk_board()
    active = await _mk_loop(board, status="running", name="Already running")
    other = await _mk_loop(board, status="draft", name="Second")
    job = _job(task_payload={"loop_id": str(other.id)})

    svc = SchedulerService.__new__(SchedulerService)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        success, error, detail = await svc._do_start_loop(s, job)

    assert success is False
    assert active.name in error
    assert (await _get_loop(other.id)).status == "draft"


# ── (c) Tag-Backlog ──────────────────────────────────────────────────────

async def _mk_tag_task(board: Board, slug: str, title: str) -> Task:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        tag_result = (await s.exec(select(Tag).where(Tag.slug == slug))).first()
        if tag_result is None:
            tag_result = Tag(name=slug, slug=slug)
            s.add(tag_result)
            await s.commit()
            await s.refresh(tag_result)
        task = Task(board_id=board.id, title=title, status="inbox")
        s.add(task)
        await s.commit()
        await s.refresh(task)
        s.add(TagAssignment(tag_id=tag_result.id, task_id=task.id))
        await s.commit()
        return task


@pytest.mark.asyncio
async def test_tag_backlog_lists_open_tasks_in_round_brief(fake_redis):
    board = await _mk_board()
    task = await _mk_tag_task(board, "polish", "Fix flaky checkout test")
    loop = await _mk_loop(
        board, backlog_source="tag", backlog_tag="polish", backlog_md=None,
    )

    await _tick(fake_redis)

    fresh = await _get_loop(loop.id)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        round_task = await s.get(Task, fresh.current_task_id)
    assert "polish" in round_task.description
    assert task.title in round_task.description
    assert str(task.id) in round_task.description


@pytest.mark.asyncio
async def test_tag_backlog_empty_tells_agent_to_check(fake_redis):
    board = await _mk_board()
    loop = await _mk_loop(
        board, backlog_source="tag", backlog_tag="ghost-tag", backlog_md=None,
    )

    await _tick(fake_redis)

    fresh = await _get_loop(loop.id)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        round_task = await s.get(Task, fresh.current_task_id)
    assert "Kein offener Task mit Tag" in round_task.description
    assert "BACKLOG LEER" in round_task.description


@pytest.mark.asyncio
async def test_tag_backlog_excludes_dispatched_tasks(fake_redis):
    board = await _mk_board()
    task = await _mk_tag_task(board, "polish", "Already dispatched task")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        from app.utils import utcnow
        t.dispatched_at = utcnow()
        s.add(t)
        await s.commit()

    loop = await _mk_loop(
        board, backlog_source="tag", backlog_tag="polish", backlog_md=None,
    )
    await _tick(fake_redis)

    fresh = await _get_loop(loop.id)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        round_task = await s.get(Task, fresh.current_task_id)
    assert task.title not in round_task.description
    assert "Kein offener Task mit Tag" in round_task.description


@pytest.mark.asyncio
async def test_router_requires_backlog_tag_for_tag_source(auth_client: AsyncClient):
    board = await _mk_board()
    r = await auth_client.post("/api/v1/loops", json={
        "board_id": str(board.id), "name": "x", "goal": "g",
        "backlog_source": "tag",
    })
    assert r.status_code == 400
    assert "backlog_tag" in r.json()["detail"]


@pytest.mark.asyncio
async def test_router_accepts_tag_source_with_backlog_tag(auth_client: AsyncClient):
    board = await _mk_board()
    r = await auth_client.post("/api/v1/loops", json={
        "board_id": str(board.id), "name": "x", "goal": "g",
        "backlog_source": "tag", "backlog_tag": "polish",
    })
    assert r.status_code == 201
    assert r.json()["backlog_tag"] == "polish"
    assert r.json()["telegram_reports"] is True


@pytest.mark.asyncio
async def test_create_tag_loop_rejects_unknown_tag(auth_client, make_board):
    """Review-Fund: unbekannter Tag-Slug → 400 statt still-leerem Backlog."""
    board = await make_board()
    r = await auth_client.post("/api/v1/loops", json={
        "board_id": str(board.id), "name": "L", "goal": "g",
        "backlog_source": "tag", "backlog_tag": "gibt-es-nicht",
    })
    assert r.status_code == 400
    assert "existiert nicht" in r.json()["detail"]
