"""Notice-only escalations: dispatch_escalation, lead_escalation, review_stuck
and dependency_zombie are watchdog-generated STOERUNGSMELDUNGEN, not operator
decisions. Today they create an `Approval` and ask Telegram/Slack yes/no —
77 of 92 approvals in 30 days went unanswered because these four types get
auto-superseded the moment the card leaves the triggering state (see
APPROVAL_VALID_STATES in approval_cleanup.py). Behind
`settings.notice_only_escalations_enabled` (default True) these four types
should raise a passive `operator.notice` activity event + a plain-text
report instead of creating an Approval / yes-no question.

Conventions: in-memory SQLite `test_engine`, `make_board`/`make_agent`/
`make_task`, `fake_redis`. No fleet names.
"""
from __future__ import annotations

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


async def _approvals(task_id, action_type: str | None = None):
    from app.models.approval import Approval

    async with _session() as s:
        q = select(Approval).where(Approval.task_id == task_id)
        if action_type is not None:
            q = q.where(Approval.action_type == action_type)
        return list((await s.exec(q)).all())


# ── 1. Constant is a subset of APPROVAL_VALID_STATES keys ──────────────


def test_notice_only_types_are_subset_of_valid_states():
    from app.services.approval_cleanup import (
        APPROVAL_VALID_STATES,
        NOTICE_ONLY_ACTION_TYPES,
    )

    assert NOTICE_ONLY_ACTION_TYPES == frozenset(
        {"dispatch_escalation", "lead_escalation", "review_stuck", "dependency_zombie"}
    )
    # lead_escalation is deliberately NOT in APPROVAL_VALID_STATES — it has
    # its own auto-close mechanism (LEAD_ESCALATION_CLOSE_ON_STATUS) and must
    # not supersede on a mere status flip. Only the other three route through
    # the generic valid-states retract path.
    assert (NOTICE_ONLY_ACTION_TYPES - {"lead_escalation"}).issubset(
        APPROVAL_VALID_STATES.keys()
    )


# ── 2. Flag default ─────────────────────────────────────────────────────


def test_flag_default_on():
    from app.config import settings

    assert settings.notice_only_escalations_enabled is True


# ── 3/4. raise_notice() itself ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_raise_notice_emits_event_and_sends_report(make_board, make_task):
    from app.services.operator_notices import raise_notice

    board = await make_board(name="Notices", slug=f"notices-{uuid.uuid4().hex[:8]}")
    task = await make_task(board_id=board.id, title="Some card", status="review")

    with patch("app.services.operator_notices.emit_event",
               new_callable=AsyncMock) as emit, \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(True, [])) as send:
        async with _session() as s:
            await raise_notice(
                s,
                action_type="review_stuck",
                task=task,
                title="Review STUCK",
                body="hanging for 200min",
            )

    emit.assert_awaited_once()
    _, kwargs = emit.call_args
    args = emit.call_args.args
    assert "operator.notice" in args
    assert kwargs.get("severity") == "warning"
    assert kwargs.get("task_id") == task.id
    detail = kwargs.get("detail") or {}
    assert detail.get("action_type") == "review_stuck"
    assert detail.get("title") == "Review STUCK"
    assert detail.get("body") == "hanging for 200min"

    send.assert_awaited_once()
    send_args, send_kwargs = send.call_args
    text = send_args[0] if send_args else send_kwargs.get("text")
    assert text.isascii(), "notice report text must be ASCII (no emoji/buttons)"
    assert "Review STUCK" in text
    assert task.title in text
    assert "hanging for 200min" in text


@pytest.mark.asyncio
async def test_raise_notice_survives_send_failure(make_board, make_task):
    from app.services.operator_notices import raise_notice

    board = await make_board(name="Notices2", slug=f"notices2-{uuid.uuid4().hex[:8]}")
    task = await make_task(board_id=board.id, title="Card", status="review")

    with patch("app.services.operator_notices.emit_event",
               new_callable=AsyncMock) as emit, \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, side_effect=RuntimeError("backend down")):
        async with _session() as s:
            # Must not raise despite send_report failing.
            await raise_notice(
                s,
                action_type="dispatch_escalation",
                task=task,
                title="Dispatch stuck",
                body="no ack",
            )

    emit.assert_awaited_once()


@pytest.mark.asyncio
async def test_raise_notice_logs_warning_when_not_delivered(make_board, make_task, caplog):
    """Pruefbericht Punkt 5: send_report can return (False, []) without
    raising (e.g. telegram_reports_enabled=False) — that must not vanish
    silently, a warning must be logged."""
    from app.services.operator_notices import raise_notice

    board = await make_board(name="Notices3", slug=f"notices3-{uuid.uuid4().hex[:8]}")
    task = await make_task(board_id=board.id, title="Card", status="review")

    with patch("app.services.operator_notices.emit_event", new_callable=AsyncMock), \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(False, [])), \
         caplog.at_level("WARNING", logger="mc.operator_notices"):
        async with _session() as s:
            await raise_notice(
                s, action_type="review_stuck", task=task,
                title="Review STUCK", body="not delivered",
            )

    assert any("did not deliver" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_raise_notice_anti_spam_marker_skips_second_report_in_window(
    make_board, make_task,
):
    """Pruefbericht Punkt 4: within the same TTL window, a second
    raise_notice() call for the SAME action_type on the SAME card must
    still emit the audit event, but must NOT send a second report."""
    from app.services.operator_notices import raise_notice

    board = await make_board(name="Notices4", slug=f"notices4-{uuid.uuid4().hex[:8]}")
    task = await make_task(board_id=board.id, title="Card", status="review")

    with patch("app.services.operator_notices.emit_event",
               new_callable=AsyncMock) as emit, \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(True, [])) as send:
        async with _session() as s:
            await raise_notice(
                s, action_type="review_stuck", task=task,
                title="Review STUCK", body="first tick",
            )
        async with _session() as s:
            await raise_notice(
                s, action_type="review_stuck", task=task,
                title="Review STUCK", body="second tick, same window",
            )

    assert emit.await_count == 2, "event is written every tick (audit trail)"
    send.assert_awaited_once(), "report is capped to once per TTL window"


@pytest.mark.asyncio
async def test_raise_notice_different_action_type_still_reports(make_board, make_task):
    """The per-type marker must not suppress a DIFFERENT action_type on the
    same card — only repeats of the SAME type within the window."""
    from app.services.operator_notices import raise_notice

    board = await make_board(name="Notices5", slug=f"notices5-{uuid.uuid4().hex[:8]}")
    task = await make_task(board_id=board.id, title="Card", status="review")

    with patch("app.services.operator_notices.emit_event", new_callable=AsyncMock), \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(True, [])) as send:
        async with _session() as s:
            await raise_notice(
                s, action_type="review_stuck", task=task,
                title="Review STUCK", body="a",
            )
        async with _session() as s:
            await raise_notice(
                s, action_type="dependency_zombie", task=task,
                title="Zombie", body="b",
            )

    assert send.await_count == 2


@pytest.mark.asyncio
async def test_notice_active_true_after_raise_false_after_retract(make_board, make_task):
    from app.services.operator_notices import notice_active, raise_notice, retract_notice

    board = await make_board(name="Notices6", slug=f"notices6-{uuid.uuid4().hex[:8]}")
    task = await make_task(board_id=board.id, title="Card", status="review")

    assert await notice_active(task.id) is False

    with patch("app.services.operator_notices.emit_event", new_callable=AsyncMock), \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(True, [])):
        async with _session() as s:
            await raise_notice(
                s, action_type="review_stuck", task=task,
                title="Review STUCK", body="x",
            )

    assert await notice_active(task.id) is True

    with patch("app.services.operator_notices.emit_event", new_callable=AsyncMock), \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(True, [])):
        async with _session() as s:
            await retract_notice(
                s, action_type="review_stuck", task=task,
                title="Entwarnung", body="y",
            )

    # retract_notice deletes BOTH markers (per-type and collective) — see
    # its docstring for why that's safe even if another type is active.
    assert await notice_active(task.id) is False


@pytest.mark.asyncio
async def test_retract_clears_collective_marker_even_with_other_type_active(
    make_board, make_task,
):
    """Documented behaviour (retract_notice docstring): retracting type A
    while type B is ALSO active on the same card clears notice_active()
    for the whole card, not just for A — until B's own next tick re-sets
    the collective marker and reports again."""
    from app.services.operator_notices import notice_active, raise_notice, retract_notice

    board = await make_board(name="Notices7", slug=f"notices7-{uuid.uuid4().hex[:8]}")
    task = await make_task(board_id=board.id, title="Card", status="review")

    with patch("app.services.operator_notices.emit_event", new_callable=AsyncMock), \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(True, [])):
        async with _session() as s:
            await raise_notice(
                s, action_type="review_stuck", task=task,
                title="Review STUCK", body="a",
            )
        async with _session() as s:
            await raise_notice(
                s, action_type="dependency_zombie", task=task,
                title="Zombie", body="b",
            )

    assert await notice_active(task.id) is True

    with patch("app.services.operator_notices.emit_event", new_callable=AsyncMock), \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(True, [])):
        async with _session() as s:
            await retract_notice(
                s, action_type="review_stuck", task=task,
                title="Entwarnung", body="a resolved",
            )

    # Documented trade-off: B is still logically active, but the shared
    # collective marker is gone until B's own next tick.
    assert await notice_active(task.id) is False

    # Simulate B's own dedup gate having elapsed (2h/4h/24h depending on
    # type, see docstring) — its per-type marker expires, so the NEXT time
    # B's watchdog fires, raise_notice() reports again and the collective
    # marker comes back.
    from app.redis_client import RedisKeys, get_redis
    redis = await get_redis()
    await redis.delete(RedisKeys.notice_marker(str(task.id), "dependency_zombie"))

    with patch("app.services.operator_notices.emit_event", new_callable=AsyncMock), \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(True, [])) as send_b:
        async with _session() as s:
            await raise_notice(
                s, action_type="dependency_zombie", task=task,
                title="Zombie", body="b still stuck",
            )

    send_b.assert_awaited_once(), "B's own dedup gate had elapsed — must report again"
    assert await notice_active(task.id) is True


# ── ASCII sharpened: exercise a REAL call site's f-strings, not a ───────
# hand-written ASCII literal (Pruefbericht Punkt 6.3 — task_runner.py's
# real title previously slipped in an em-dash that a synthetic literal
# would never catch).


@pytest.mark.asyncio
async def test_dispatch_escalation_notice_text_is_ascii(make_board, make_agent, make_task):
    from app.services.task_runner import task_runner
    from app.models.task import Task
    from app.config import settings
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    board = await make_board(name="AsciiCheck", slug=f"ascii-{uuid.uuid4().hex[:8]}")
    agent = await make_agent(
        name="WorkerC", board_id=board.id, agent_runtime="cli-bridge",
        scopes=["tasks:read", "tasks:write", "heartbeat"],
    )
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=utcnow() - timedelta(minutes=20),
        dispatch_attempt_id=str(uuid.uuid4()),
    )

    with patch.object(settings, "notice_only_escalations_enabled", True), \
         patch("app.services.operator_notices.emit_event", new_callable=AsyncMock), \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(True, [])) as send, \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await task_runner._create_dispatch_approval(
                s, await s.get(Task, task.id), agent, 20.0, "kein ACK nach Dispatch",
            )

    send.assert_awaited_once()
    text = send.call_args.args[0]
    assert text.isascii(), f"notice report text must be ASCII: {text!r}"


@pytest.mark.asyncio
async def test_review_stuck_notice_text_is_ascii(fake_redis, make_board, make_task):
    from app.services.watchdog.core import WatchdogService
    from app.config import settings

    board = await make_board(name="AsciiCheck2", slug=f"ascii2-{uuid.uuid4().hex[:8]}")
    task = await make_task(board_id=board.id, title="Stuck review", status="review")
    await _age_task(task.id, 200)

    with patch.object(settings, "notice_only_escalations_enabled", True), \
         patch("app.services.watchdog.task_monitor.get_redis",
               AsyncMock(return_value=fake_redis)), \
         patch("app.services.operator_notices.emit_event", new_callable=AsyncMock), \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(True, [])) as send, \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with _session() as s:
            await WatchdogService()._check_review_tasks(s)

    send.assert_awaited_once()
    text = send.call_args.args[0]
    assert text.isascii(), f"notice report text must be ASCII: {text!r}"


# ── Dedup markers in the notice path (Pruefbericht Punkt 8) ─────────────
# The notice-only branch must not accidentally skip the EXISTING Redis
# dedup that used to gate the Approval creation — that dedup is the whole
# anti-spam anchor (2h/4h/24h cooldowns per card).


@pytest.mark.asyncio
async def test_dispatch_escalation_notice_path_still_sets_ack_dedup_marker(
    fake_redis, make_board, make_agent, make_task,
):
    from app.services.task_runner import task_runner
    from app.models.task import Task
    from app.config import settings
    from app.redis_client import RedisKeys
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    board = await make_board(name="DedupA", slug=f"dedup-a-{uuid.uuid4().hex[:8]}")
    agent = await make_agent(
        name="WorkerD", board_id=board.id, agent_runtime="cli-bridge",
        scopes=["tasks:read", "tasks:write", "heartbeat"],
    )
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=utcnow() - timedelta(minutes=20),
        dispatch_attempt_id=str(uuid.uuid4()),
    )

    with patch.object(settings, "notice_only_escalations_enabled", True), \
         patch("app.services.task_runner.raise_notice", new_callable=AsyncMock), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await task_runner._handle_ack_timeout(
                s, await s.get(Task, task.id), agent, utcnow(), fake_redis,
            )

    assert await fake_redis.get(RedisKeys.dispatch_ack_check(str(task.id))), (
        "the 24h ACK-timeout dedup cooldown must still be set in the "
        "notice-only path — otherwise every watchdog tick re-notifies"
    )


@pytest.mark.asyncio
async def test_dependency_zombie_notice_path_still_sets_dedup_marker(
    fake_redis, make_board, make_agent, make_task,
):
    from app.services.watchdog.task_monitor import TaskMonitorMixin
    from app.config import settings
    from app.models.task import Task, TaskDependency
    from app.redis_client import RedisKeys
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    board = await make_board(name="DedupB", slug=f"dedup-b-{uuid.uuid4().hex[:8]}")
    agent = await make_agent(name="WorkerE", board_id=board.id, is_board_lead=False)

    dep_task = await make_task(board_id=board.id, title="Failed Dep3", status="failed")
    main_task = await make_task(
        board_id=board.id, title="Waiting Task3", status="inbox",
        assigned_agent_id=agent.id,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        dep = TaskDependency(
            id=uuid.uuid4(), task_id=main_task.id, depends_on_task_id=dep_task.id,
        )
        s.add(dep)
        await s.commit()

    mixin = TaskMonitorMixin()

    with patch.object(settings, "notice_only_escalations_enabled", True), \
         patch("app.services.watchdog.task_monitor.get_redis", return_value=fake_redis), \
         patch("app.services.watchdog.task_monitor.raise_notice",
               new_callable=AsyncMock), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await mixin._check_dependency_zombies(s)

    dedup_key = RedisKeys.recovery_attempt(str(main_task.id), "dependency_zombie")
    assert await fake_redis.get(dedup_key), (
        "the 4h dependency_zombie dedup cooldown must still be set in the "
        "notice-only path"
    )


# ── Silent-card watchdog treats notice_active like a pending Approval ───
# (Pruefbericht B1, high severity: without this, a card silenced today by
# an open review_stuck notice gets LOUDER — stage-1 notify fires anyway —
# once the flag is on.)


@pytest.mark.asyncio
async def test_silent_card_stage1_skips_when_notice_active(
    make_board, make_agent, make_task,
):
    from app.services.watchdog.core import WatchdogService
    from app.services.operator_notices import raise_notice

    now = utcnow()
    board = await make_board(name="B1Guard", slug=f"b1-{uuid.uuid4().hex[:8]}")
    lead = await make_agent(name="Lead", board_id=board.id,
                            is_board_lead=True, role="lead")
    worker = await make_agent(name="WorkerF", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Notice-silenced card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           ack_at=now - timedelta(minutes=90),
                           started_at=now - timedelta(minutes=90),
                           updated_at=now - timedelta(minutes=90))

    with patch("app.services.operator_notices.emit_event", new_callable=AsyncMock), \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(True, [])):
        async with _session() as s:
            await raise_notice(
                s, action_type="review_stuck", task=task,
                title="Review STUCK", body="already noticed",
            )

    with patch("app.services.watchdog.task_monitor.emit_event",
               new_callable=AsyncMock):
        async with _session() as s:
            await WatchdogService()._check_silent_cards(s)

    async with _session() as s:
        from app.models.task import TaskComment
        q = select(TaskComment).where(
            TaskComment.task_id == task.id,
            TaskComment.comment_type == "watchdog_notify",
        )
        notify_comments = list((await s.exec(q)).all())
    assert notify_comments == [], (
        "a card with an active notice must not ALSO get a stage-1 "
        "silent-card notify — that would be louder, not equal"
    )


# ── Retraction in the notice path (Pruefbericht B2 / Punkt 2) ───────────


@pytest.mark.asyncio
async def test_lead_escalation_retraction_when_notice_active(
    make_board, make_agent, make_task,
):
    """No Approval row exists (notice-only path), but the card moves again
    after the stage-2 alert → the operator must get an Entwarnung, not
    silence. Mirrors test_silent_card_retraction.py's stage-2 shape."""
    from app.services.watchdog.core import WatchdogService
    from app.services.operator_notices import notice_active, raise_notice
    from app.models.task import TaskComment

    now = utcnow()
    board = await make_board(name="B2Guard", slug=f"b2-{uuid.uuid4().hex[:8]}")
    lead = await make_agent(name="Lead", board_id=board.id,
                            is_board_lead=True, role="lead")
    worker = await make_agent(name="WorkerG", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Escalated then resumed",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=120))

    # Stage-2 alert marker, same as the real ESKALATION comment.
    async with _session() as s:
        s.add(TaskComment(
            task_id=task.id, author_type="system",
            comment_type="lead_escalated_notify",
            content="ESKALATION STUFE 2",
            created_at=now - timedelta(minutes=20),
        ))
        await s.commit()

    with patch("app.services.operator_notices.emit_event", new_callable=AsyncMock), \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(True, [])):
        async with _session() as s:
            await raise_notice(
                s, action_type="lead_escalation", task=task,
                title="ESKALATION STUFE 2", body="Lead reagiert nicht",
                agent_id=lead.id,
            )

    assert await notice_active(task.id) is True

    # Real activity AFTER the alert → the card moved again.
    async with _session() as s:
        s.add(TaskComment(
            task_id=task.id, author_type="agent", author_agent_id=lead.id,
            comment_type="progress", content="back at it",
            created_at=now - timedelta(minutes=5),
        ))
        await s.commit()

    with patch("app.services.watchdog.task_monitor.emit_event",
               new_callable=AsyncMock), \
         patch("app.services.operator_notices.emit_event",
               new_callable=AsyncMock) as notice_emit, \
         patch("app.services.operator_notices.send_report",
               new_callable=AsyncMock, return_value=(True, [])) as retract_send:
        async with _session() as s:
            await WatchdogService()._check_silent_card_retractions(s)

    retraction_events = [
        c for c in notice_emit.call_args_list
        if len(c.args) > 1 and c.args[1] == "operator.notice_retracted"
    ]
    assert retraction_events, "must emit operator.notice_retracted"
    retract_send.assert_awaited_once()
    retract_text = retract_send.call_args.args[0]
    assert "Entwarnung" in retract_text

    assert await notice_active(task.id) is False, "marker must be deleted"


# ── 5. dispatch_escalation (task_runner._create_dispatch_approval) ──────


@pytest.mark.asyncio
async def test_dispatch_escalation_notice_only_when_flag_on(
    make_board, make_agent, make_task,
):
    from app.services.task_runner import task_runner
    from app.models.task import Task
    from app.config import settings
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    board = await make_board(name="B1", slug=f"dse-{uuid.uuid4().hex[:8]}")
    agent = await make_agent(
        name="WorkerA", board_id=board.id, agent_runtime="cli-bridge",
        scopes=["tasks:read", "tasks:write", "heartbeat"],
    )
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=utcnow() - timedelta(minutes=20),
        dispatch_attempt_id=str(uuid.uuid4()),
    )

    with patch.object(settings, "notice_only_escalations_enabled", True), \
         patch("app.services.task_runner.raise_notice",
               new_callable=AsyncMock) as notice, \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await task_runner._create_dispatch_approval(
                s, await s.get(Task, task.id), agent, 20.0, "kein ACK nach Dispatch",
            )

    assert await _approvals(task.id, "dispatch_escalation") == [], (
        "flag on: no Approval row must be created"
    )
    notice.assert_awaited_once()
    _, kwargs = notice.call_args
    assert kwargs.get("action_type") == "dispatch_escalation"


@pytest.mark.asyncio
async def test_dispatch_escalation_creates_approval_when_flag_off(
    make_board, make_agent, make_task,
):
    from app.services.task_runner import task_runner
    from app.models.task import Task
    from app.config import settings
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    board = await make_board(name="B2", slug=f"dse-off-{uuid.uuid4().hex[:8]}")
    agent = await make_agent(
        name="WorkerA2", board_id=board.id, agent_runtime="cli-bridge",
        scopes=["tasks:read", "tasks:write", "heartbeat"],
    )
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=utcnow() - timedelta(minutes=20),
        dispatch_attempt_id=str(uuid.uuid4()),
    )

    with patch.object(settings, "notice_only_escalations_enabled", False), \
         patch("app.services.operator_approvals.send_approval",
               new_callable=AsyncMock), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await task_runner._create_dispatch_approval(
                s, await s.get(Task, task.id), agent, 20.0, "kein ACK nach Dispatch",
            )

    approvals = await _approvals(task.id, "dispatch_escalation")
    assert len(approvals) == 1, "flag off: today's Approval behaviour unchanged"


# ── 6. review_stuck (WatchdogService._check_review_tasks) ───────────────


async def _age_task(task_id, minutes: int) -> None:
    from app.models.task import Task

    async with _session() as s:
        t = await s.get(Task, task_id)
        t.updated_at = utcnow().replace(tzinfo=None) - timedelta(minutes=minutes)
        s.add(t)
        await s.commit()


@pytest.mark.asyncio
async def test_review_stuck_notice_only_when_flag_on(fake_redis, make_board, make_task):
    from app.services.watchdog.core import WatchdogService
    from app.config import settings

    board = await make_board(name="RS1", slug=f"rs-{uuid.uuid4().hex[:8]}")
    task = await make_task(board_id=board.id, title="Stuck review", status="review")
    await _age_task(task.id, 200)

    with patch.object(settings, "notice_only_escalations_enabled", True), \
         patch("app.services.watchdog.task_monitor.get_redis",
               AsyncMock(return_value=fake_redis)), \
         patch("app.services.watchdog.task_monitor.raise_notice",
               new_callable=AsyncMock) as notice, \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with _session() as s:
            await WatchdogService()._check_review_tasks(s)

    assert await _approvals(task.id, "review_stuck") == [], (
        "flag on: no Approval row must be created"
    )
    notice.assert_awaited_once()
    _, kwargs = notice.call_args
    assert kwargs.get("action_type") == "review_stuck"


@pytest.mark.asyncio
async def test_review_stuck_creates_approval_when_flag_off(fake_redis, make_board, make_task):
    from app.services.watchdog.core import WatchdogService
    from app.config import settings

    board = await make_board(name="RS2", slug=f"rs-off-{uuid.uuid4().hex[:8]}")
    task = await make_task(board_id=board.id, title="Stuck review off", status="review")
    await _age_task(task.id, 200)

    with patch.object(settings, "notice_only_escalations_enabled", False), \
         patch("app.services.watchdog.task_monitor.get_redis",
               AsyncMock(return_value=fake_redis)), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with _session() as s:
            await WatchdogService()._check_review_tasks(s)

    approvals = await _approvals(task.id, "review_stuck")
    assert len(approvals) == 1, "flag off: today's Approval behaviour unchanged"


# ── 7. lead_escalation (WatchdogService._check_lead_notify_escalations) ─


async def _add_stage1(task_id, comment_type: str = "watchdog_notify",
                      age_minutes: int = 45):
    from app.models.task import TaskComment

    async with _session() as s:
        s.add(TaskComment(
            task_id=task_id,
            author_type="system",
            comment_type=comment_type,
            content="STILLE KARTE: stage-1 lead message",
            created_at=utcnow() - timedelta(minutes=age_minutes),
        ))
        await s.commit()


@pytest.mark.asyncio
async def test_lead_escalation_notice_only_when_flag_on(make_board, make_agent, make_task):
    from app.services.watchdog.core import WatchdogService
    from app.config import settings

    now = utcnow()
    board = await make_board(name="LE1", slug=f"le-{uuid.uuid4().hex[:8]}")
    lead = await make_agent(name="Lead", board_id=board.id,
                            is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           ack_at=now - timedelta(minutes=75),
                           started_at=now - timedelta(minutes=75),
                           updated_at=now - timedelta(minutes=75))
    await _add_stage1(task.id, "watchdog_notify", age_minutes=45)

    with patch.object(settings, "notice_only_escalations_enabled", True), \
         patch("app.services.watchdog.task_monitor.raise_notice",
               new_callable=AsyncMock) as notice, \
         patch("app.services.operator_approvals.send_approval",
               new_callable=AsyncMock):
        async with _session() as s:
            await WatchdogService()._check_lead_notify_escalations(s)

    assert await _approvals(task.id, "lead_escalation") == [], (
        "flag on: no Approval row must be created"
    )
    notice.assert_awaited_once()
    _, kwargs = notice.call_args
    assert kwargs.get("action_type") == "lead_escalation"


# ── 8. dependency_zombie (TaskMonitorMixin._check_dependency_zombies) ───


@pytest.mark.asyncio
async def test_dependency_zombie_notice_only_when_flag_on(fake_redis, make_board, make_agent, make_task):
    from app.services.watchdog.task_monitor import TaskMonitorMixin
    from app.config import settings
    from app.models.task import TaskDependency
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    board = await make_board(name="Zombie Board Notice", slug=f"zombie-{uuid.uuid4().hex[:8]}")
    agent = await make_agent(name="ZombieWorker2", board_id=board.id, is_board_lead=False)

    dep_task = await make_task(board_id=board.id, title="Failed Dep2", status="failed")
    main_task = await make_task(
        board_id=board.id, title="Waiting Task2", status="inbox",
        assigned_agent_id=agent.id,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        dep = TaskDependency(
            id=uuid.uuid4(),
            task_id=main_task.id,
            depends_on_task_id=dep_task.id,
        )
        s.add(dep)
        await s.commit()

    mixin = TaskMonitorMixin()

    with patch.object(settings, "notice_only_escalations_enabled", True), \
         patch("app.services.watchdog.task_monitor.get_redis", return_value=fake_redis), \
         patch("app.services.watchdog.task_monitor.raise_notice",
               new_callable=AsyncMock) as notice, \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await mixin._check_dependency_zombies(s)

    assert await _approvals(main_task.id, "dependency_zombie") == [], (
        "flag on: no Approval row must be created"
    )
    notice.assert_awaited_once()
    _, kwargs = notice.call_args
    assert kwargs.get("action_type") == "dependency_zombie"
