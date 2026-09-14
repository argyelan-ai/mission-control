"""Silent-mailbox watchdog: report-only for inbox > 0, in_progress == 0.

Same family as the #518 silent-card watchdog (``_check_silent_cards``), one
grain coarser: an agent's whole mailbox instead of a single card. A card in
`inbox` with none of the same agent's cards `in_progress` for 15 minutes is
reported once to the Board Lead via ``watchdog_notify``. Status is never
changed. Dedup is DB-based (last ``task.silent_mailbox`` ActivityEvent vs.
last real mailbox activity), same reasoning as #518 (a Redis TTL is what
stacked identical reminders overnight there).

Conventions: in-memory SQLite ``test_engine``, ``make_board`` /
``make_agent`` / ``make_task``. No fleet names.
"""
from __future__ import annotations

import inspect
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.utils import utcnow


@asynccontextmanager
async def _session():
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


async def _run_check(session):
    from app.services.watchdog.core import WatchdogService

    with patch("app.services.watchdog.task_monitor.emit_event",
               new_callable=AsyncMock) as emit:
        svc = WatchdogService()
        await svc._check_silent_mailbox(session)
    return emit


async def _comments(task_id, comment_type: str | None = None):
    from app.models.task import TaskComment

    async with _session() as s:
        q = select(TaskComment).where(TaskComment.task_id == task_id)
        if comment_type is not None:
            q = q.where(TaskComment.comment_type == comment_type)
        return list((await s.exec(q)).all())


async def _all_notify_comments():
    from app.models.task import TaskComment

    async with _session() as s:
        q = select(TaskComment).where(TaskComment.comment_type == "watchdog_notify")
        return list((await s.exec(q)).all())


async def _reload_task(task_id):
    from app.models.task import Task

    async with _session() as s:
        return await s.get(Task, task_id)


async def _setup_board_and_lead(make_board, make_agent, *, with_lead=True):
    board = await make_board(
        name="Mailbox Board",
        slug=f"smb-{uuid.uuid4().hex[:8]}",
    )
    lead = None
    if with_lead:
        lead = await make_agent(
            name="Lead", board_id=board.id, is_board_lead=True, role="lead",
        )
    worker = await make_agent(
        name="Worker", board_id=board.id, is_board_lead=False, role="developer",
    )
    return board, lead, worker


# ── Wiring ──────────────────────────────────────────────────────────────


def test_silent_mailbox_wired_in_check_all():
    """Must run in _check_all, right after the #518 silent-card check and
    before orphan recovery — same "visible before a later healer touches
    it" reasoning as #518."""
    from app.services.watchdog.core import WatchdogService

    src = inspect.getsource(WatchdogService._check_all)
    assert "_check_silent_mailbox" in src
    assert src.index("_check_silent_cards") < src.index("_check_silent_mailbox")
    assert src.index("_check_silent_mailbox") < src.index("_recover_orphaned_tasks")


# ── Happy path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_silent_mailbox_reports_once_to_lead(make_board, make_agent, make_task):
    """4 inbox cards, 0 in_progress, silent for 20min (> 15min threshold)."""
    now = utcnow()
    past = now - timedelta(minutes=20)
    board, lead, worker = await _setup_board_and_lead(make_board, make_agent)

    tasks = []
    for i in range(4):
        t = await make_task(
            board_id=board.id,
            title=f"Queued {i}",
            status="inbox",
            assigned_agent_id=worker.id,
            created_at=past,
        )
        tasks.append(t)

    async with _session() as s:
        emit = await _run_check(s)

    for t in tasks:
        refreshed = await _reload_task(t.id)
        assert refreshed.status == "inbox", "watchdog must not change status"

    notes = await _all_notify_comments()
    assert len(notes) == 1
    assert notes[0].author_type == "system"
    assert "STILLE SCHLANGE" in notes[0].content
    assert lead.name in notes[0].content
    assert "NICHT" in notes[0].content
    emit.assert_awaited()
    assert any(
        call.args[1] == "task.silent_mailbox" for call in emit.await_args_list
    )


@pytest.mark.asyncio
async def test_second_tick_does_not_restack(make_board, make_agent, make_task):
    now = utcnow()
    past = now - timedelta(minutes=20)
    board, _lead, worker = await _setup_board_and_lead(make_board, make_agent)
    await make_task(
        board_id=board.id, title="Queued", status="inbox",
        assigned_agent_id=worker.id, created_at=past,
    )

    async with _session() as s:
        await _run_check(s)
    async with _session() as s:
        await _run_check(s)

    notes = await _all_notify_comments()
    assert len(notes) == 1, "exactly one notify per silent episode, not a stack"


@pytest.mark.asyncio
async def test_silent_card_check_cannot_see_the_dead_mailbox(
    make_board, make_agent, make_task,
):
    """The #575 gap proof, both guards side by side (incident 2026-09-14:
    an agent sat >2h with five inbox cards, zero in_progress, on idle).

    ``_check_silent_cards`` selects ``SILENT_CARD_STATUSES`` =
    ``("in_progress", "waiting")`` — with the agent's cards all in
    ``inbox`` its candidate set is EMPTY, so the state is structurally
    invisible to it. Only the mailbox check reports — and exactly once.
    """
    now = utcnow()
    past = now - timedelta(minutes=20)
    board, lead, worker = await _setup_board_and_lead(make_board, make_agent)
    for i in range(5):
        await make_task(
            board_id=board.id,
            title=f"Queued {i}",
            status="inbox",
            assigned_agent_id=worker.id,
            created_at=past,
        )

    async with _session() as s:
        from app.services.watchdog.core import WatchdogService

        with patch("app.services.watchdog.task_monitor.emit_event",
                   new_callable=AsyncMock):
            svc = WatchdogService()
            await svc._check_silent_cards(s)
            await svc._check_silent_mailbox(s)

    notes = await _all_notify_comments()
    assert not any("STILLE KARTE" in n.content for n in notes), (
        "_check_silent_cards must NOT report the inbox-only state"
    )
    assert len(notes) == 1, "the mailbox guard reports, exactly once"
    assert "STILLE SCHLANGE" in notes[0].content
    assert lead.name in notes[0].content


# ── False-positive guards (DoD) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_without_any_cards_is_not_flagged(make_board, make_agent, make_task):
    """An agent with no cards at all is not dead — and one whose only card
    is `done` (no inbox at all) must not be swept in either."""
    board, _lead, worker = await _setup_board_and_lead(make_board, make_agent)
    past = utcnow() - timedelta(hours=2)
    await make_task(
        board_id=board.id, title="Old done card", status="done",
        assigned_agent_id=worker.id, created_at=past, completed_at=past,
    )

    async with _session() as s:
        await _run_check(s)

    assert await _all_notify_comments() == []


@pytest.mark.asyncio
async def test_agent_in_progress_is_not_flagged(make_board, make_agent, make_task):
    """Episode-end case #1: the agent picked up a card (in_progress > 0)."""
    now = utcnow()
    past = now - timedelta(minutes=45)
    board, _lead, worker = await _setup_board_and_lead(make_board, make_agent)
    await make_task(
        board_id=board.id, title="Queued", status="inbox",
        assigned_agent_id=worker.id, created_at=past,
    )
    await make_task(
        board_id=board.id, title="Working", status="in_progress",
        assigned_agent_id=worker.id, ack_at=now, started_at=now,
    )

    async with _session() as s:
        await _run_check(s)

    assert await _all_notify_comments() == []


@pytest.mark.asyncio
async def test_just_finished_card_is_not_flagged(make_board, make_agent, make_task):
    """The other DoD-named non-death case: agent just finished a card and
    hasn't pulled the next one yet. The queued card LOOKS old (20min), but
    the agent finished something 2 minutes ago — that resets the clock for
    the whole mailbox, not just the finished card."""
    now = utcnow()
    old = now - timedelta(minutes=20)
    recent = now - timedelta(minutes=2)
    board, _lead, worker = await _setup_board_and_lead(make_board, make_agent)
    await make_task(
        board_id=board.id, title="Queued", status="inbox",
        assigned_agent_id=worker.id, created_at=old,
    )
    await make_task(
        board_id=board.id, title="Just finished", status="done",
        assigned_agent_id=worker.id, created_at=old, completed_at=recent,
    )

    async with _session() as s:
        await _run_check(s)

    assert await _all_notify_comments() == []


@pytest.mark.asyncio
async def test_waiting_card_is_not_flagged(make_board, make_agent, make_task):
    """DoD-named case: a card in `waiting` for the same agent is deliberate
    wait, not silence — even though the inbox card itself is old."""
    now = utcnow()
    past = now - timedelta(minutes=45)
    board, _lead, worker = await _setup_board_and_lead(make_board, make_agent)
    await make_task(
        board_id=board.id, title="Queued", status="inbox",
        assigned_agent_id=worker.id, created_at=past,
    )
    await make_task(
        board_id=board.id, title="Waiting for answer", status="waiting",
        assigned_agent_id=worker.id, updated_at=past,
    )

    async with _session() as s:
        await _run_check(s)

    assert await _all_notify_comments() == []


@pytest.mark.asyncio
async def test_blocked_card_is_not_flagged(make_board, make_agent, make_task):
    """Same DoD-named case, `blocked` variant."""
    now = utcnow()
    past = now - timedelta(minutes=45)
    board, _lead, worker = await _setup_board_and_lead(make_board, make_agent)
    await make_task(
        board_id=board.id, title="Queued", status="inbox",
        assigned_agent_id=worker.id, created_at=past,
    )
    await make_task(
        board_id=board.id, title="Blocked", status="blocked",
        assigned_agent_id=worker.id, updated_at=past,
    )

    async with _session() as s:
        await _run_check(s)

    assert await _all_notify_comments() == []


@pytest.mark.asyncio
async def test_fresh_inbox_card_is_not_flagged(make_board, make_agent, make_task):
    """Under the 15-minute threshold — not silent yet."""
    now = utcnow()
    recent = now - timedelta(minutes=5)
    board, _lead, worker = await _setup_board_and_lead(make_board, make_agent)
    await make_task(
        board_id=board.id, title="Just queued", status="inbox",
        assigned_agent_id=worker.id, created_at=recent,
    )

    async with _session() as s:
        await _run_check(s)

    assert await _all_notify_comments() == []


@pytest.mark.asyncio
async def test_no_board_lead_skips_quietly(make_board, make_agent, make_task):
    now = utcnow()
    past = now - timedelta(minutes=45)
    board, _lead, worker = await _setup_board_and_lead(
        make_board, make_agent, with_lead=False,
    )
    await make_task(
        board_id=board.id, title="Queued", status="inbox",
        assigned_agent_id=worker.id, created_at=past,
    )

    async with _session() as s:
        await _run_check(s)

    assert await _all_notify_comments() == []


@pytest.mark.asyncio
async def test_stopped_inbox_card_does_not_count_as_queued(
    make_board, make_agent, make_task,
):
    """A card an operator explicitly stopped isn't 'queued work' — it must
    not, on its own, make the mailbox look silent."""
    now = utcnow()
    past = now - timedelta(minutes=45)
    board, _lead, worker = await _setup_board_and_lead(make_board, make_agent)
    await make_task(
        board_id=board.id, title="Stopped", status="inbox",
        assigned_agent_id=worker.id, created_at=past, run_control="stopped",
    )

    async with _session() as s:
        await _run_check(s)

    assert await _all_notify_comments() == []


@pytest.mark.asyncio
async def test_archived_board_is_skipped(make_board, make_agent, make_task):
    now = utcnow()
    past = now - timedelta(minutes=45)
    board, _lead, worker = await _setup_board_and_lead(make_board, make_agent)
    async with _session() as s:
        from app.models.board import Board
        b = await s.get(Board, board.id)
        b.is_archived = True
        s.add(b)
        await s.commit()
    await make_task(
        board_id=board.id, title="Queued", status="inbox",
        assigned_agent_id=worker.id, created_at=past,
    )

    async with _session() as s:
        await _run_check(s)

    assert await _all_notify_comments() == []
