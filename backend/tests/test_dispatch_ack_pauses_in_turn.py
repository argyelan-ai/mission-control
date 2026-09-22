"""Bauplan Lauf 2 Teil 4 (analyse.md, 21.09.2026): the ACK clock pauses
while the agent is genuinely mid-turn.

Covers the 13/80 Hand-Starts (Nachpruefung N.2, bucket A: 'andere Karte
in_progress') left uncovered by Teil 1-3 (those cover the 60 Hand-Starts
with no visible park state). `_check_dispatch_ack`
(app/services/task_runner.py) must skip BOTH the ACK-timeout ladder
(`_handle_ack_timeout`, rotation + escalation) and the pending ladder
(`_handle_dispatch_pending`) whenever `agent.status == "working"` AND
`agent.last_seen_at` is younger than the 90s turn-signal freshness gate
(dispatch.TURN_SIGNAL_HEARTBEAT_MAX_AGE_SECONDS) — the same signal Teil 2
uses to decide whether to queue in the first place. A stale/dead poll (no
heartbeat, or older than 90s) must escalate exactly as today: the pause
must never hide a real hang.

RED (21.09.2026): none of this exists in `_check_dispatch_ack` yet — every
test that expects NO escalation fails because the ladder still runs and
creates an Approval; the two sanity/regression tests (stale heartbeat,
idle agent) should already pass today (they assert existing behaviour)
and serve as the RED-phase control group.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _approval_path_flag_off():
    """Approval-Pfad = Schalter aus (Lauf 4): these tests assert the Approval
    row the ACK/pending ladders create; with notice-only escalations on
    (default since #643) those ladders raise a notice instead."""
    from app.config import settings
    with patch.object(settings, "notice_only_escalations_enabled", False):
        yield


from app.utils import utcnow


@asynccontextmanager
async def _session():
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


async def _make_fixtures(
    make_board, make_agent, make_task, *,
    agent_runtime: str = "host",
    agent_status: str = "working",
    last_seen_ago_seconds: float | None = 10,
    # H1 (Nacharbeit 21.09.2026): default must stay UNDER the pause cap
    # (2x the agent's ack_timeout — host=5min -> 10min, cli-bridge=15min ->
    # 30min) for every test that expects the pause to hold, yet still well
    # past the raw ack_timeout so it actually proves the pause overrides the
    # normal ladder, not just "not enough time has passed yet".
    dispatched_ago_minutes: float | None = 8,
):
    from app.utils import ensure_aware

    board = await make_board(name="ACKPause Board", slug=f"ackpause-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(
        name="Worker-ACKPause", board_id=board.id, is_board_lead=False,
        role="developer", agent_runtime=agent_runtime, status=agent_status,
        last_seen_at=(
            utcnow() - timedelta(seconds=last_seen_ago_seconds)
            if last_seen_ago_seconds is not None else None
        ),
    )
    task = await make_task(
        board_id=board.id, title="ACKPause task", status="inbox",
        assigned_agent_id=agent.id,
    )
    if dispatched_ago_minutes is not None:
        async with _session() as s:
            from app.models.task import Task
            t = await s.get(Task, task.id)
            t.dispatched_at = utcnow() - timedelta(minutes=dispatched_ago_minutes)
            s.add(t)
            await s.commit()
    return board, agent, task


async def _run_check(fake_redis):
    from app.services.task_runner import task_runner

    with patch("app.services.task_runner.get_redis", AsyncMock(return_value=fake_redis)), \
         patch("app.services.task_runner.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_runner.logger"):
        async with _session() as session:
            await task_runner._check_dispatch_ack(session)


async def _approval_count(task_id) -> int:
    from app.models.approval import Approval
    from sqlmodel import select
    async with _session() as s:
        rows = (await s.exec(select(Approval).where(Approval.task_id == task_id))).all()
        return len(rows)


async def _get_task(task_id):
    from app.models.task import Task
    async with _session() as s:
        return await s.get(Task, task_id)


@pytest.mark.asyncio
async def test_ack_clock_pauses_while_agent_in_turn(
    fake_redis, make_board, make_agent, make_task,
):
    """host agent status=working, heartbeat 10s old, task dispatched_at
    8min ago (past host ack_timeout=5min, still under the H1 pause cap of
    2x5=10min) -> no Approval, no dispatch_attempt_id rotation."""
    board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task,
        agent_runtime="host", agent_status="working",
        last_seen_ago_seconds=10, dispatched_ago_minutes=8,
    )
    before = await _get_task(task.id)
    attempt_before = before.dispatch_attempt_id

    await _run_check(fake_redis)

    assert await _approval_count(task.id) == 0, (
        "ACK clock must pause while agent is genuinely mid-turn"
    )
    after = await _get_task(task.id)
    assert after.dispatch_attempt_id == attempt_before, (
        "no silent-retry rotation while paused"
    )


@pytest.mark.asyncio
async def test_pending_clock_pauses_while_agent_in_turn(
    fake_redis, make_board, make_agent, make_task,
):
    """Same in-turn signal, but dispatched_at NULL (Guard-3 queue scenario,
    Teil 2) — the pending ladder must pause too, no Approval. updated_at
    8min ago: past DISPATCH_PENDING_WARN_MINUTES=5 (would normally warn/
    escalate) but under the Runde-2 pending cap (2x host ack_timeout=5min
    = 10min)."""
    board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task,
        agent_runtime="host", agent_status="working",
        last_seen_ago_seconds=10, dispatched_ago_minutes=None,
    )
    # updated_at old enough that _handle_dispatch_pending would normally
    # act (warn or escalate) if the pause did not intervene.
    async with _session() as s:
        from app.models.task import Task
        t = await s.get(Task, task.id)
        t.updated_at = utcnow() - timedelta(minutes=8)
        s.add(t)
        await s.commit()

    await _run_check(fake_redis)

    assert await _approval_count(task.id) == 0, (
        "pending clock must pause while agent is in-turn"
    )


@pytest.mark.asyncio
async def test_pending_clock_resumes_after_pause_cap(
    fake_redis, make_board, make_agent, make_task,
):
    """Runde 2 (21.09.2026, Pruefbericht): the pending pause ALSO has an
    upper bound, anchored on task.updated_at (the clock
    _handle_dispatch_pending itself uses). host agent status=working,
    heartbeat fresh, dispatched_at NULL (Guard-3 queue scenario), but
    updated_at is 16min ago — past both the pause cap (2x5=10min) AND
    DISPATCH_PENDING_TIMEOUT_MINUTES=15 — must escalate exactly like the
    pending ladder does today, pause or no pause."""
    board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task,
        agent_runtime="host", agent_status="working",
        last_seen_ago_seconds=10, dispatched_ago_minutes=None,
    )
    async with _session() as s:
        from app.models.task import Task
        t = await s.get(Task, task.id)
        t.updated_at = utcnow() - timedelta(minutes=16)
        s.add(t)
        await s.commit()

    await _run_check(fake_redis)

    assert await _approval_count(task.id) == 1, (
        "pending pause must have an upper bound too — a permanently "
        "'working' agent must not suppress the pending ladder forever"
    )


@pytest.mark.asyncio
async def test_stale_heartbeat_still_escalates(
    fake_redis, make_board, make_agent, make_task,
):
    """Sabotage probe: status=working but heartbeat 200s old (> 90s gate)
    -> the pause must NOT apply, Approval must still be created."""
    board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task,
        agent_runtime="host", agent_status="working",
        last_seen_ago_seconds=200, dispatched_ago_minutes=20,
    )

    await _run_check(fake_redis)

    assert await _approval_count(task.id) == 1, (
        "dead/stale poll must escalate exactly like today"
    )


@pytest.mark.asyncio
async def test_idle_agent_still_escalates(
    fake_redis, make_board, make_agent, make_task,
):
    """Regression guard: status=idle -> behaviour unchanged, Approval
    still created for a genuinely-unacked card."""
    board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task,
        agent_runtime="host", agent_status="idle",
        last_seen_ago_seconds=10, dispatched_ago_minutes=20,
    )

    await _run_check(fake_redis)

    assert await _approval_count(task.id) == 1, (
        "idle agent's unacked card must still escalate (no regression)"
    )


@pytest.mark.asyncio
async def test_turn_wait_event_emitted_once(
    fake_redis, make_board, make_agent, make_task,
):
    """Two `_check_dispatch_ack` passes in the same Redis world -> exactly
    ONE `task.dispatch_queued_behind_active` event (Redis dedup, TTL
    900s per Bauplan)."""
    board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task,
        agent_runtime="host", agent_status="working",
        last_seen_ago_seconds=10, dispatched_ago_minutes=8,
    )

    from app.services.task_runner import task_runner

    with patch("app.services.task_runner.get_redis", AsyncMock(return_value=fake_redis)), \
         patch("app.services.task_runner.logger"):
        async with _session() as session:
            await task_runner._check_dispatch_ack(session)
        async with _session() as session:
            await task_runner._check_dispatch_ack(session)

    from app.models.activity import ActivityEvent
    from sqlmodel import select
    async with _session() as s:
        events = (await s.exec(
            select(ActivityEvent).where(
                ActivityEvent.task_id == task.id,
                ActivityEvent.event_type == "task.dispatch_queued_behind_active",
            )
        )).all()
    assert len(events) == 1, (
        f"expected exactly one dedup'd turn-wait event, got {len(events)}"
    )


@pytest.mark.asyncio
async def test_cli_bridge_agent_in_turn_also_pauses(
    fake_redis, make_board, make_agent, make_task,
):
    """The pause rule is runtime-free, same as Guard 3 (Teil 2) — a
    cli-bridge agent mid-turn must pause exactly like host."""
    board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task,
        agent_runtime="cli-bridge", agent_status="working",
        last_seen_ago_seconds=10, dispatched_ago_minutes=20,
    )

    await _run_check(fake_redis)

    assert await _approval_count(task.id) == 0, (
        "cli-bridge in-turn must pause exactly like host"
    )


@pytest.mark.asyncio
async def test_ack_clock_resumes_after_pause_cap(
    fake_redis, make_board, make_agent, make_task,
):
    """H1 (Nacharbeit 21.09.2026, Pruefbericht): the pause has an upper
    bound. host agent status=working, heartbeat FRESH (10s old — the pause
    condition itself still holds), but dispatched_at is 11min ago > the H1
    cap (2x host ack_timeout=5min = 10min) -> the ladder runs anyway,
    Approval created. Guards against a daemon permanently stuck
    busy()=True (docker/omp-bridge acp_chat.py: an exception in
    _restart_child() before `self._busy = False` at the end of _run_turn
    leaves busy True forever, while the bridge keeps heartbeating fine)."""
    board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task,
        agent_runtime="host", agent_status="working",
        last_seen_ago_seconds=10, dispatched_ago_minutes=11,
    )

    await _run_check(fake_redis)

    assert await _approval_count(task.id) == 1, (
        "pause must have an upper bound — a permanently 'working' agent "
        "must still escalate once dispatched_at exceeds 2x ack_timeout"
    )


@pytest.mark.asyncio
async def test_ack_clock_pause_switch_off_keeps_legacy_escalation(
    fake_redis, make_board, make_agent, make_task, monkeypatch,
):
    """Schalter (Bauplan 'Schalter', shared by Teil 2-4): host agent
    status=working + fresh heartbeat + dispatched_at well within the H1
    cap -> WOULD pause with the switch on (see
    test_ack_clock_pauses_while_agent_in_turn), but with
    host_turn_signal_enabled=False the ladder must run exactly like before
    Lauf 2 -> Approval created.

    Runde 2 (Pruefbericht Punkt 4): monkeypatch.setattr instead of a hard
    `= True` in `finally` — restores the pre-test value, not an assumption."""
    from app.config import settings

    board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task,
        agent_runtime="host", agent_status="working",
        last_seen_ago_seconds=10, dispatched_ago_minutes=8,
    )

    monkeypatch.setattr(settings, "host_turn_signal_enabled", False)
    await _run_check(fake_redis)

    assert await _approval_count(task.id) == 1, (
        "switch off must restore legacy escalation (no pause at all)"
    )
