"""Tests for the Lifecycle Safety Watchdog — silent-abort auto-block (ADR-046).

`task_runner._check_stuck_in_progress` blocks a task that was ACKed then went
SILENT (agent never sent a terminal PATCH to review/blocked/failed) — but ONLY
for cli-bridge agents, only past a conservative runtime-aware threshold,
corroborated by no agent TaskComment, and only after the condition persists
across ≥2 ticks (tick 1 nudges, tick 2+ blocks).

PRIME DIRECTIVE coverage: a genuinely-working / just-acked / in-review / host /
slow-local / dead-process agent is NEVER blocked. A false block of a healthy
agent is worse than the bug being fixed.

Conventions (tests/conftest.py): in-memory SQLite `test_engine`, `fake_redis`
fixture, `make_board`/`make_agent`/`make_task` factories. No freezegun exists —
past timestamps are injected into DB rows so elapsed-time math trips the
threshold. The async check is called directly (never the loop).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.utils import utcnow


# ── Session helper bound to the test engine ────────────────────────────


@asynccontextmanager
async def _session():
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


async def _run_check(fake_redis, session):
    """Invoke the leaf check with all fan-out side-effects patched."""
    from app.services.task_runner import TaskRunnerService

    with patch("app.services.task_runner.get_redis", AsyncMock(return_value=fake_redis)), \
         patch("app.services.task_runner.emit_event", new_callable=AsyncMock) as emit, \
         patch("app.services.telegram_bot.telegram_bot.send_approval_telegram",
               new_callable=AsyncMock):
        runner = TaskRunnerService()
        await runner._check_stuck_in_progress(session)
    return emit


async def _make_stuck_setup(
    make_board, make_agent, make_task, *,
    agent_runtime="cli-bridge",
    role="developer",
    is_board_lead=False,
    run_state="idle",
    operational_mode="active",
    seen_age_seconds=10,
    activity_age_minutes=30,
    last_activity_none=False,
    runtime_id=None,
    dispatch_config=None,
    review_decision=None,
    run_control=None,
    blocked_by_task_id=None,
    ack=True,
    bind_current_task=True,
):
    """Create board + agent + task wired for the stuck-in-progress predicate.

    Defaults describe the canonical silent-abort: cli-bridge developer, wrapper
    alive (last_seen_at fresh), turn dead (last_task_activity_at 30min stale).
    """
    now = utcnow()
    board = await make_board(name="LW Board", slug=f"lw-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(
        name=f"Worker-{uuid.uuid4().hex[:4]}",
        board_id=board.id,
        is_board_lead=is_board_lead,
        role=role,
        agent_runtime=agent_runtime,
        run_state=run_state,
        operational_mode=operational_mode,
        last_seen_at=now - timedelta(seconds=seen_age_seconds),
        last_task_activity_at=(None if last_activity_none
                               else now - timedelta(minutes=activity_age_minutes)),
        runtime_id=runtime_id,
        dispatch_config=dispatch_config or {},
    )
    task = await make_task(
        board_id=board.id,
        title="Silent abort task",
        status="in_progress",
        assigned_agent_id=agent.id,
        ack_at=(now - timedelta(minutes=activity_age_minutes + 5)) if ack else None,
        started_at=now - timedelta(minutes=activity_age_minutes + 5),
        review_decision=review_decision,
        run_control=run_control,
        blocked_by_task_id=blocked_by_task_id,
    )
    if bind_current_task:
        async with _session() as s:
            from app.models.agent import Agent
            a = await s.get(Agent, agent.id)
            a.current_task_id = task.id
            s.add(a)
            await s.commit()
    return board, agent, task


async def _reload_task(task_id):
    async with _session() as s:
        from app.models.task import Task
        return await s.get(Task, task_id)


async def _reload_agent(agent_id):
    async with _session() as s:
        from app.models.agent import Agent
        return await s.get(Agent, agent_id)


async def _pending_blocker_approvals(task_id):
    from sqlmodel import select
    from app.models.approval import Approval
    async with _session() as s:
        res = await s.exec(
            select(Approval).where(
                Approval.task_id == task_id,
                Approval.action_type == "blocker_decision",
            )
        )
        return res.all()


# ════════════════════════════════════════════════════════════════════════
# Pure threshold-helper tests (no clock)
# ════════════════════════════════════════════════════════════════════════


def _fake_agent(role="developer", dispatch_config=None, is_board_lead=False):
    return SimpleNamespace(
        role=role,
        dispatch_config=dispatch_config or {},
        is_board_lead=is_board_lead,
    )


def test_stuck_block_threshold_dispatch_config_override():
    """dispatch_config override wins WHEN above the floor (40 → 40)."""
    from app.services.task_runner import _stuck_block_threshold_for
    agent = _fake_agent(dispatch_config={"stuck_block_minutes": 40})
    assert _stuck_block_threshold_for(agent) == 40


def test_stuck_block_threshold_override_is_floored():
    """PRIME DIRECTIVE: a mis-set override of 5 can NEVER block below the floor."""
    from app.services.task_runner import _stuck_block_threshold_for, MIN_STUCK_BLOCK_FLOOR
    agent = _fake_agent(role="developer", dispatch_config={"stuck_block_minutes": 5})
    resolved = _stuck_block_threshold_for(agent)
    assert resolved >= MIN_STUCK_BLOCK_FLOOR
    assert resolved >= 20
    assert resolved != 5


def test_stuck_block_threshold_never_below_role_idle():
    """Invariant: resolved threshold ≥ role idle threshold (recovery ran first)."""
    from app.services.task_runner import _stuck_block_threshold_for, _idle_threshold_for
    agent = _fake_agent(role="developer")
    assert _stuck_block_threshold_for(agent) >= _idle_threshold_for(agent)


def test_stuck_block_default_slow_runtime_is_higher():
    """A slow/local runtime (lmstudio) defaults ≥45; a claude cli-bridge to 25."""
    from app.services.task_runner import _stuck_block_default_for
    agent = _fake_agent(role="developer")
    slow_rt = SimpleNamespace(runtime_type="lmstudio")
    fast = _stuck_block_default_for(agent, runtime=None)
    slow = _stuck_block_default_for(agent, runtime=slow_rt)
    assert fast == 25
    assert slow >= 45


def test_liveness_floor_from_heartbeat_config():
    """Liveness floor = 2× heartbeat interval, min 120s."""
    from app.services.task_runner import _liveness_floor_seconds
    assert _liveness_floor_seconds(SimpleNamespace(heartbeat_config={"interval": "5m"})) == 600.0
    assert _liveness_floor_seconds(SimpleNamespace(heartbeat_config={"interval": "30s"})) == 120.0  # min floor
    assert _liveness_floor_seconds(SimpleNamespace(heartbeat_config={})) == 600.0


# ════════════════════════════════════════════════════════════════════════
# Behavioral tests
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_blocks_silent_abort(fake_redis, make_board, make_agent, make_task):
    """Canonical silent abort → 2nd tick blocks + blocker_decision Approval.

    Proves apply_terminal_unassign was used (agent.current_task_id released,
    run_state='blocked', assigned_agent_id KEPT so the agent can resume).
    """
    _b, agent, task = await _make_stuck_setup(make_board, make_agent, make_task)

    async with _session() as s:
        await _run_check(fake_redis, s)  # tick 1 = nudge
    t1 = await _reload_task(task.id)
    assert t1.status == "in_progress", "tick 1 must NOT block (nudge only)"

    async with _session() as s:
        await _run_check(fake_redis, s)  # tick 2 = block

    blocked = await _reload_task(task.id)
    assert blocked.status == "blocked"
    assert blocked.assigned_agent_id == agent.id, "assignment kept (resumable)"

    a = await _reload_agent(agent.id)
    assert a.current_task_id is None, "lock released (human-wait)"
    assert a.run_state == "blocked"

    approvals = await _pending_blocker_approvals(task.id)
    assert len(approvals) == 1
    assert approvals[0].payload["blocker_type"] == "technical_problem"
    assert approvals[0].payload["source"] == "lifecycle_watchdog"


@pytest.mark.asyncio
async def test_first_tick_nudges_not_blocks(fake_redis, make_board, make_agent, make_task):
    """Tick 1 posts a nudge comment and leaves the task in_progress."""
    from sqlmodel import select
    from app.models.task import TaskComment

    _b, agent, task = await _make_stuck_setup(make_board, make_agent, make_task)
    async with _session() as s:
        await _run_check(fake_redis, s)

    t = await _reload_task(task.id)
    assert t.status == "in_progress"
    assert not await _pending_blocker_approvals(task.id)

    async with _session() as s:
        res = await s.exec(select(TaskComment).where(TaskComment.task_id == task.id))
        comments = res.all()
    assert any(c.comment_type == "watchdog_notify" and c.author_type == "system"
               for c in comments), "a nudge comment must be posted on tick 1"


@pytest.mark.asyncio
async def test_never_blocks_healthy_long_turn(fake_redis, make_board, make_agent, make_task):
    """PRIME DIRECTIVE: both timestamps fresh (long tool call) → NEVER blocked."""
    _b, agent, task = await _make_stuck_setup(
        make_board, make_agent, make_task,
        seen_age_seconds=10, activity_age_minutes=1,  # activity fresh
    )
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "in_progress"
    assert not await _pending_blocker_approvals(task.id)


@pytest.mark.asyncio
async def test_never_blocks_host_agent(fake_redis, make_board, make_agent, make_task):
    """PRIME DIRECTIVE (the traced FP hole): host agents are hard-skipped (guard 0).

    A host agent freezes last_task_activity_at at ack for the whole turn while
    last_seen_at stays fresh — guard 13 WOULD fire, so guard 0 must veto it.
    """
    _b, agent, task = await _make_stuck_setup(
        make_board, make_agent, make_task,
        agent_runtime="host", role="developer", is_board_lead=False,
        activity_age_minutes=40,
    )
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "in_progress"
    assert not await _pending_blocker_approvals(task.id)


@pytest.mark.asyncio
async def test_never_blocks_manual_runtime(fake_redis, make_board, make_agent, make_task):
    """PRIME DIRECTIVE: manual runtime has no working-heartbeat → guard 0 skips."""
    _b, agent, task = await _make_stuck_setup(
        make_board, make_agent, make_task,
        agent_runtime="manual", activity_age_minutes=40,
    )
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "in_progress"


@pytest.mark.asyncio
async def test_never_blocks_slow_runtime_under_45min(fake_redis, make_board, make_agent, make_task):
    """PRIME DIRECTIVE (Sparky class): slow/local cli-bridge at 30min stale is a
    legit long local cook (past the 25-min claude default but under its own
    45-min floor) → NEVER blocked."""
    from app.models.runtime import Runtime
    now = utcnow()
    board = await make_board(name="Slow Board", slug=f"slow-{uuid.uuid4().hex[:6]}")
    async with _session() as s:
        rt = Runtime(
            id=uuid.uuid4(), slug=f"lms-{uuid.uuid4().hex[:6]}",
            display_name="LM Studio", runtime_type="lmstudio",
            endpoint="http://localhost:1234",
        )
        s.add(rt)
        await s.commit()
        await s.refresh(rt)

    _b, agent, task = await _make_stuck_setup(
        make_board, make_agent, make_task,
        agent_runtime="cli-bridge", role="developer",
        activity_age_minutes=30,  # over 25 (claude) but under 45 (slow floor)
        runtime_id=rt.id,
    )
    # override board to reuse rt board not required; setup made its own board.
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "in_progress", "slow runtime under 45min must not block"
    assert not await _pending_blocker_approvals(task.id)


@pytest.mark.asyncio
async def test_blocks_zombie_despite_run_state_running(fake_redis, make_board, make_agent, make_task):
    """Regression (Incident 2026-07-02, omp): run_state='running' ist nur ein
    Dispatch-Latch. Task 40min stumm + Latch haengt → tick 1 nudged,
    tick 2 blockt. Der fruehere opportunistic skip liess genau diesen
    Zombie ewig laufen."""
    _b, agent, task = await _make_stuck_setup(
        make_board, make_agent, make_task,
        run_state="running", activity_age_minutes=40,
    )
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "blocked", "silent zombie with stale activity must be blocked"


@pytest.mark.asyncio
async def test_never_blocks_dead_process(fake_redis, make_board, make_agent, make_task):
    """last_seen_at ALSO stale → process/container dead → orphan path owns it,
    NOT this check (guard 12 bails)."""
    _b, agent, task = await _make_stuck_setup(
        make_board, make_agent, make_task,
        seen_age_seconds=3600,  # 60min — wrapper dead
        activity_age_minutes=60,
    )
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "in_progress"
    assert not await _pending_blocker_approvals(task.id)


@pytest.mark.asyncio
async def test_never_blocks_unacked_task(fake_redis, make_board, make_agent, make_task):
    """ack_at IS NULL → the ACK-timeout path owns it, not this check."""
    _b, agent, task = await _make_stuck_setup(
        make_board, make_agent, make_task,
        ack=False, activity_age_minutes=40,
    )
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "in_progress"


@pytest.mark.asyncio
async def test_blocks_adhoc_task_no_parent(fake_redis, make_board, make_agent, make_task):
    """COVERAGE: a leaf ad-hoc task with parent_task_id IS NULL and no children
    is still evaluated and blocked on silent abort (prefilter doesn't need a
    parent)."""
    _b, agent, task = await _make_stuck_setup(make_board, make_agent, make_task)
    assert task.parent_task_id is None
    async with _session() as s:
        await _run_check(fake_redis, s)  # nudge
    async with _session() as s:
        await _run_check(fake_redis, s)  # block
    t = await _reload_task(task.id)
    assert t.status == "blocked"


@pytest.mark.asyncio
async def test_skips_board_lead(fake_redis, make_board, make_agent, make_task):
    """By-design waiter: board lead orchestrates → never its own worker turn."""
    _b, agent, task = await _make_stuck_setup(
        make_board, make_agent, make_task,
        is_board_lead=True, role="orchestrator", activity_age_minutes=60,
    )
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "in_progress"


@pytest.mark.asyncio
async def test_skips_parent_with_children(fake_redis, make_board, make_agent, make_task):
    """Parent-with-subtasks legitimately waits → skipped."""
    _b, agent, task = await _make_stuck_setup(make_board, make_agent, make_task)
    # Add a child subtask.
    await make_task(
        board_id=task.board_id, title="child", status="in_progress",
        parent_task_id=task.id,
    )
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "in_progress"


@pytest.mark.asyncio
async def test_skips_review_hold(fake_redis, make_board, make_agent, make_task):
    """review_decision='hold' is an intentional pause → skipped."""
    _b, agent, task = await _make_stuck_setup(
        make_board, make_agent, make_task, review_decision="hold",
        activity_age_minutes=40,
    )
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "in_progress"


@pytest.mark.asyncio
async def test_skips_run_control_stopped(fake_redis, make_board, make_agent, make_task):
    """run_control='stopped' operator hold → skipped."""
    _b, agent, task = await _make_stuck_setup(
        make_board, make_agent, make_task, run_control="stopped",
        activity_age_minutes=40,
    )
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "in_progress"


@pytest.mark.asyncio
async def test_skips_callback_wait(fake_redis, make_board, make_agent, make_task):
    """blocked_by_task_id set (callback-wait) → paused by design, not stuck."""
    # A dependency task to point blocked_by at.
    board = await make_board(name="CB", slug=f"cb-{uuid.uuid4().hex[:6]}")
    dep = await make_task(board_id=board.id, title="dep", status="in_progress")
    _b, agent, task = await _make_stuck_setup(
        make_board, make_agent, make_task,
        blocked_by_task_id=dep.id, activity_age_minutes=40,
    )
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "in_progress"


@pytest.mark.asyncio
async def test_idempotent_no_double_block(fake_redis, make_board, make_agent, make_task):
    """After a block, the Redis dedup key + pending Approval prevent a 2nd block."""
    _b, agent, task = await _make_stuck_setup(make_board, make_agent, make_task)
    async with _session() as s:
        await _run_check(fake_redis, s)  # nudge
    async with _session() as s:
        await _run_check(fake_redis, s)  # block
    async with _session() as s:
        await _run_check(fake_redis, s)  # must be no-op
    approvals = await _pending_blocker_approvals(task.id)
    assert len(approvals) == 1, "must not create a second Approval"


@pytest.mark.asyncio
async def test_skips_when_pollsh_already_blocked(fake_redis, make_board, make_agent, make_task):
    """Last comment is a poll.sh agent-authored blocker → already handled, skip."""
    from app.models.task import TaskComment
    _b, agent, task = await _make_stuck_setup(make_board, make_agent, make_task)
    async with _session() as s:
        s.add(TaskComment(
            task_id=task.id, author_type="agent", comment_type="blocker",
            content="poll.sh auto-blocker",
        ))
        await s.commit()
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "in_progress"
    assert not await _pending_blocker_approvals(task.id)


@pytest.mark.asyncio
async def test_skips_recent_agent_progress_comment(fake_redis, make_board, make_agent, make_task):
    """Fresh agent-authored progress comment inside the window → not silent."""
    from app.models.task import TaskComment
    _b, agent, task = await _make_stuck_setup(make_board, make_agent, make_task)
    async with _session() as s:
        s.add(TaskComment(
            task_id=task.id, author_type="agent", comment_type="progress",
            content="still working on it",  # created_at defaults to now → fresh
        ))
        await s.commit()
    for _ in range(3):
        async with _session() as s:
            await _run_check(fake_redis, s)
    t = await _reload_task(task.id)
    assert t.status == "in_progress"


@pytest.mark.asyncio
async def test_kill_switch_disables_check(fake_redis, make_board, make_agent, make_task):
    """lifecycle_watchdog_enabled=False → the whole check is a no-op."""
    import app.config
    _b, agent, task = await _make_stuck_setup(make_board, make_agent, make_task)
    original = app.config.settings.lifecycle_watchdog_enabled
    app.config.settings.lifecycle_watchdog_enabled = False
    try:
        for _ in range(3):
            async with _session() as s:
                await _run_check(fake_redis, s)
    finally:
        app.config.settings.lifecycle_watchdog_enabled = original
    t = await _reload_task(task.id)
    assert t.status == "in_progress"
