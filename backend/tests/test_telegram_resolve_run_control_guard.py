"""9th reactivation path (task_lifecycle.task_still_reactivatable ledger,
follow-up to PR #556): TelegramBotService._resolve_approval's blocker-decision
branch checked only `task.status == "blocked"`, never `run_control` — the
same shape as the eight paths closed by PR #533/#556.

Reproduced via real verbs (mirrors test_callback_resume_hold_guard.py's
8th-path repro), not a hand-set run_control:
  1. Agent works the task (in_progress).
  2. A blocker_decision Approval is pending for this task (created by an
     earlier blocker report that hasn't been resolved yet).
  3. Operator stops the running task via stop_task_run — this lands the
     task at status="blocked", run_control="stopped" (documented
     stop_task_run behavior), assigned_agent_id retained.
  4. The operator then resolves the (older, still-pending) Telegram
     approval with "approve".

Pre-fix: the blocker-decision branch's only precondition
(task.status == "blocked") is satisfied, so it silently reactivates the
stopped task to "in_progress" -- exactly the deadlock PR #533 closed eight
other paths for (mc release doesn't undo it: it only accepts
run_control=="manual_hold").
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

import fakeredis.aioredis
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.approval import Approval
from app.models.task import Task
from app.services.telegram_bot import telegram_bot
from app.utils import utcnow
from tests.conftest import test_engine


@pytest.fixture(autouse=True)
def _patch_engine():
    """_resolve_approval does `from app.database import engine` INSIDE the
    function body — patch it at its source, same as
    test_telegram_resolve_approval_status.py."""
    with patch("app.database.engine", test_engine):
        yield


@pytest.fixture(autouse=True)
async def _patch_redis(monkeypatch):
    """stop_task_run and _resolve_approval both call emit_event(), which
    resolves Redis via get_redis() directly (not Depends) -- fake it so
    stop_task_run runs for real instead of being mocked away."""
    server = fakeredis.aioredis.FakeServer()
    fake = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)

    async def _fake_get_redis():
        return fake

    import app.redis_client as redis_mod
    import app.services.sse as sse_mod
    monkeypatch.setattr(redis_mod, "get_redis", _fake_get_redis)
    monkeypatch.setattr(sse_mod, "get_redis", _fake_get_redis)
    yield fake
    await fake.aclose()


async def _make_running_task_with_pending_approval(make_board, make_agent, make_task):
    board = await make_board(name="TG Run-Control Guard", slug=f"tg-rc-{uuid.uuid4().hex[:8]}")
    agent = await make_agent(name="Worker", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Running task with stale blocker approval",
        status="in_progress", assigned_agent_id=agent.id,
        dispatched_at=utcnow(), ack_at=utcnow(),
    )
    approval_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Approval(
            id=approval_id, board_id=board.id, task_id=task.id, agent_id=agent.id,
            action_type="blocker_decision", description="blockiert",
            status="pending", payload={"blocker_type": "technical_problem"},
            expires_at=utcnow() + timedelta(hours=24),
        ))
        await s.commit()
    return task, approval_id


@pytest.mark.asyncio
async def test_stopped_task_not_reactivated_by_telegram_resolve(make_board, make_agent, make_task):
    """Real-verb repro: stop_task_run -> Telegram-resolve "approve" must
    NOT flip the stopped task back to in_progress."""
    from app.services.operations import stop_task_run

    task, approval_id = await _make_running_task_with_pending_approval(make_board, make_agent, make_task)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await stop_task_run(s, task.id, "mark", reason="test-stop")
        await s.commit()

    resolved = await telegram_bot._resolve_approval(approval_id, "approve")
    assert resolved == "task_held"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
        assert fresh.status == "blocked", fresh.status
        assert fresh.run_control == "stopped", fresh.run_control
        approval = await s.get(Approval, approval_id)
        assert approval.status == "approved", (
            "approval itself must still resolve -- only the task-side "
            "reactivation is skipped"
        )


@pytest.mark.asyncio
async def test_stopped_task_can_still_be_rejected_via_telegram(make_board, make_agent, make_task):
    """Gegenrichtung zur Gegenrichtung (W1, PR #563 review): der Guard
    haengt bewusst an status == "approved". Faellt diese Bedingung weg,
    greift er auch auf "reject" -- und eine gestoppte Karte liesse sich
    per Telegram nicht mehr abbrechen. Genau der Weg, den der Operator
    nach einem Stop vom Telefon aus braucht.
    """
    from app.services.operations import stop_task_run

    task, approval_id = await _make_running_task_with_pending_approval(
        make_board, make_agent, make_task
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await stop_task_run(s, task.id, "mark", reason="test-stop")
        await s.commit()

    resolved = await telegram_bot._resolve_approval(approval_id, "reject")
    assert resolved == "resolved", resolved

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
        assert fresh.status == "failed", fresh.status
        assert fresh.assigned_agent_id is None, (
            "apply_terminal_unassign muss auch auf der gestoppten Karte laufen"
        )


@pytest.mark.asyncio
async def test_stopped_task_callback_reports_held_not_generic_conflict(
    make_board, make_agent, make_task, monkeypatch
):
    """N1 (PR #563 review, round 2): the ``task_held`` branch in
    _handle_callback (telegram_bot.py) had no test exercising the
    operator-facing message -- every existing test up to here only pins
    _resolve_approval's return value (see
    test_stopped_task_not_reactivated_by_telegram_resolve above), never
    what the callback handler actually tells the operator. Rex's sabotage
    probe S3 (silencing the ``elif resolved == "task_held"`` branch) left
    all three then-existing tests green, because the fallback ``else:``
    branch also returns without raising -- and would have handed Mark
    "Bereits erledigt." instead of the actionable "erst im Board
    freigeben" text that W2 (round 1) was written to introduce.
    """
    from app.services.operations import stop_task_run

    monkeypatch.setattr(settings, "telegram_chat_id", "12345", raising=False)

    task, approval_id = await _make_running_task_with_pending_approval(
        make_board, make_agent, make_task
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await stop_task_run(s, task.id, "mark", reason="test-stop")
        await s.commit()

    with patch.object(telegram_bot, "answer_callback_query") as mock_answer, \
            patch.object(telegram_bot, "update_resolved_telegram") as mock_update:
        await telegram_bot._handle_callback({
            "id": "cb-1",
            "from": {"username": "mark"},
            "message": {"chat": {"id": 12345}},
            "data": f"approve:{approval_id}",
        })

    mock_answer.assert_awaited_once()
    _callback_id, answer_text = mock_answer.await_args.args
    assert "gestoppt" in answer_text and "gehalten" in answer_text, answer_text
    assert "zwischenzeitlich" not in answer_text, (
        f"must not fall back to the task_conflict wording: {answer_text!r}"
    )

    mock_update.assert_awaited_once()
    resolver_note = mock_update.await_args.kwargs.get("resolver_note", "")
    assert "gestoppt/gehalten" in resolver_note, resolver_note


@pytest.mark.asyncio
async def test_unheld_blocked_task_still_reactivated_by_telegram_resolve(make_board, make_agent, make_task):
    """Counter-probe: a normally blocked task (run_control=None) must
    still be reactivated exactly as before -- the guard must not
    overreach into the ordinary case."""
    board = await make_board(name="TG Run-Control OK", slug=f"tg-rc-ok-{uuid.uuid4().hex[:8]}")
    agent = await make_agent(name="Worker2", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Normally blocked task", status="blocked",
        run_control=None, assigned_agent_id=agent.id,
    )
    approval_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Approval(
            id=approval_id, board_id=board.id, task_id=task.id, agent_id=agent.id,
            action_type="blocker_decision", description="blockiert",
            status="pending", payload={"blocker_type": "technical_problem"},
            expires_at=utcnow() + timedelta(hours=24),
        ))
        await s.commit()

    resolved = await telegram_bot._resolve_approval(approval_id, "approve")
    assert resolved == "resolved"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
        assert fresh.status == "in_progress", fresh.status
