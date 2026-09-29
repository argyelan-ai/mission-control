"""Tests for Workstream W2-B2: liveness- and occupancy-aware unblock.

Audit finding G3: the lead/operator unblock path (blocked→in_progress) only
ever posted an `unblock_notify` TaskComment — no liveness check, no
redispatch. If the blocked agent's process had died in the meantime, nobody
read the comment and the task silently stalled until the 15-45min
stale-recovery ladder caught it.

Fix (`resolve_unblock_action` + `redispatch_unblocked_task` in
task_lifecycle.py, wired into both agent_task_status.py's agent-scoped PATCH
and tasks.py's operator PATCH):
  - assigned agent ALIVE (last_seen_at fresh) + idle → comment only (today's
    behavior), cooldown-gated.
  - assigned agent ALIVE but occupied with a different in_progress task →
    comment only, no interrupt.
  - assigned agent DEAD/stale (last_seen_at NULL or beyond the wrapper
    liveness floor) → dispatched_at/ack_at reset + auto_dispatch_task
    re-dispatch (mocked in tests), no comment (redispatch carries recovery
    context instead).
"""
import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment
from tests.conftest import test_engine


async def _setup(
    session: AsyncSession,
    *,
    target_last_seen: dt.datetime | None,
    target_current_task_id: uuid.UUID | None = None,
    target_heartbeat_interval: str = "5m",
):
    board = Board(name="Unblock Board", slug=f"unblock-{uuid.uuid4().hex[:8]}", blocker_triage_minutes=0)
    session.add(board)
    await session.commit()
    await session.refresh(board)

    lead_raw, lead_hash = generate_agent_token()
    lead = Agent(
        name="Boss",
        role="lead",
        board_id=board.id,
        agent_token_hash=lead_hash,
        is_board_lead=True,
        scopes=["tasks:read", "tasks:write", "tasks:manage"],
    )
    session.add(lead)

    target_raw, target_hash = generate_agent_token()
    target = Agent(
        name="Sparky",
        role="developer",
        board_id=board.id,
        agent_token_hash=target_hash,
        is_board_lead=False,
        scopes=["tasks:read", "tasks:write"],
        last_seen_at=target_last_seen,
        heartbeat_config={"interval": target_heartbeat_interval},
    )
    session.add(target)
    await session.commit()
    await session.refresh(lead)
    await session.refresh(target)

    if target_current_task_id is not None:
        target.current_task_id = target_current_task_id
        session.add(target)
        await session.commit()

    task = Task(
        board_id=board.id,
        assigned_agent_id=target.id,
        title="Blocked probe",
        status="blocked",
        dispatched_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=30),
        ack_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=30),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    return board, lead, target, lead_raw, task


@pytest.mark.asyncio
async def test_unblock_with_stale_agent_resets_dispatch_and_redispatches(client: AsyncClient, async_session):
    """Assigned agent's last_seen_at is way past the liveness floor (dead) →
    dispatched_at/ack_at reset to None + auto_dispatch_task invoked."""
    stale_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=2)
    board, lead, target, lead_token, task = await _setup(async_session, target_last_seen=stale_seen)

    with patch(
        "app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock
    ) as mock_dispatch, patch("app.utils.create_tracked_task") as mock_create_tracked:
        # create_tracked_task just needs to run the coroutine so the mock
        # dispatch call is actually observed.
        def _run_now(coro, name=None):
            import asyncio
            return asyncio.ensure_future(coro)
        mock_create_tracked.side_effect = _run_now

        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
        assert resp.status_code == 200, resp.text
        import asyncio
        await asyncio.sleep(0)  # let the tracked task run

    mock_dispatch.assert_called_once()
    called_task_id, called_board_id = mock_dispatch.call_args[0]
    assert called_task_id == task.id
    assert called_board_id == board.id

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatched_at is None
        assert refreshed.ack_at is None

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        assert not any(c.comment_type == "unblock_notify" for c in comments), (
            "dead-agent redispatch must not also post the unread comment"
        )


@pytest.mark.asyncio
async def test_unblock_with_fresh_idle_agent_posts_comment_only(client: AsyncClient, async_session):
    """Assigned agent's last_seen_at is fresh (alive, idle) → comment-only
    path, no redispatch/no dispatched_at reset."""
    fresh_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=10)
    board, lead, target, lead_token, task = await _setup(async_session, target_last_seen=fresh_seen)
    original_dispatched_at = task.dispatched_at

    with patch(
        "app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock
    ) as mock_dispatch:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
        assert resp.status_code == 200, resp.text

    mock_dispatch.assert_not_called()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        # dispatch handshake untouched — this is not a redispatch.
        assert refreshed.dispatched_at is not None
        assert refreshed.dispatched_at.replace(tzinfo=None) == original_dispatched_at.replace(tzinfo=None)

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        assert any(c.comment_type == "unblock_notify" for c in comments)


@pytest.mark.asyncio
async def test_unblock_respects_recovery_comment_cooldown(client: AsyncClient, async_session, fake_redis):
    """Fresh/alive agent path is still gated by the shared recovery-comment
    cooldown (G6) — a second unblock notify within the TTL is skipped."""
    from app.redis_client import try_claim_recovery_comment_cooldown

    fresh_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=10)
    board, lead, target, lead_token, task = await _setup(async_session, target_last_seen=fresh_seen)

    # Pre-claim the cooldown for this task, simulating another mechanism
    # (Tier-3 recap etc.) having already fired.
    claimed = await try_claim_recovery_comment_cooldown(fake_redis, str(task.id))
    assert claimed is True

    resp = await client.patch(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
        json={"status": "in_progress"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        assert not any(c.comment_type == "unblock_notify" for c in comments), (
            "cooldown already claimed by another mechanism — unblock_notify must be skipped"
        )


@pytest.mark.asyncio
async def test_unblock_with_busy_agent_requeues_without_interrupt(client: AsyncClient, async_session):
    """Review fix B-2: assigned agent is alive but occupied with a DIFFERENT
    in_progress task → the unblocked task must NOT stay in_progress (two
    in_progress tasks corrupt poll's active-task resolution). It goes back
    to inbox with dispatched_at/ack_at reset, so the normal claim flow
    re-delivers it after the current work. The other task is untouched — no
    interrupt, no immediate redispatch."""
    fresh_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=10)
    board, lead, target, lead_token, task = await _setup(async_session, target_last_seen=fresh_seen)

    # Give the target agent a different, currently in_progress task.
    other_task = Task(
        board_id=board.id,
        assigned_agent_id=target.id,
        title="Other active work",
        status="in_progress",
    )
    async_session.add(other_task)
    await async_session.commit()
    await async_session.refresh(other_task)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Agent, target.id)
        t.current_task_id = other_task.id
        s.add(t)
        await s.commit()

    with patch(
        "app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock
    ) as mock_dispatch:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
        assert resp.status_code == 200, resp.text

    mock_dispatch.assert_not_called()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # The other task must be untouched — no interrupt.
        other = await s.get(Task, other_task.id)
        assert other.status == "in_progress"
        busy_agent = await s.get(Agent, target.id)
        assert busy_agent.current_task_id == other_task.id, "active-task lock untouched"

        refreshed = await s.get(Task, task.id)
        assert refreshed.status == "inbox", (
            "unblocked task must be requeued to inbox, not left as a second in_progress"
        )
        assert refreshed.dispatched_at is None
        assert refreshed.ack_at is None


@pytest.mark.asyncio
async def test_unblock_notify_resets_ack_and_rotates_attempt_id_so_poll_redelivers(
    client: AsyncClient, async_session
):
    """Incident 2026-09-14 (61 min Stillstand): a failed ACP run leaves the
    task `blocked` with its old ack_at/dispatch_attempt_id still on the row.
    The fresh/idle-agent ("notify") unblock used to only post a TaskComment —
    poll's own liveness gate (`ack_at is not None`) then reported `working`
    with no task for a full `poll_orphan_run_threshold_seconds` window, and
    even once that window lapsed the redelivered card still carried the OLD
    attempt_id, which the bridge's own dispatch-dedup (last_attempt_id)
    treats as already handled.

    Fix: the notify branch now resets ack_at to None and rotates
    dispatch_attempt_id for a `blocked` origin, exactly like the already
    accepted "parked" fix in messaging.resolve_waiting_answer. Proven
    end-to-end here: right after unblock, the assigned agent's very next
    `/me/poll` must deliver `state: new_task` with a FRESH attempt_id — the
    actual repro of "Lauf scheitert -> Unblock -> neuer Lauf startet"."""
    fresh_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=10)
    board, lead, target, lead_token, task = await _setup(async_session, target_last_seen=fresh_seen)
    # `_setup` leaves dispatch_attempt_id unset (None) — same as every other
    # test in this file. Seeding a non-None "stale" id here would trip the
    # agent-scoped PATCH's own X-Dispatch-Attempt-Id staleness guard for the
    # Lead's unrelated request (a separate, pre-existing contract, out of
    # scope for this fix) — None -> non-None already proves the rotation the
    # fix adds; the operator-path test below additionally proves an already
    # non-None stale id gets replaced, not just first-assigned.
    old_attempt_id = task.dispatch_attempt_id
    assert old_attempt_id is None

    with patch(
        "app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock
    ) as mock_dispatch:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
        assert resp.status_code == 200, resp.text
    mock_dispatch.assert_not_called()  # notify path — no full redispatch

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.ack_at is None, "ack_at must be cleared so poll re-delivers the prompt"
        assert refreshed.dispatch_attempt_id != old_attempt_id, (
            "dispatch_attempt_id must rotate — else the redelivered prompt is deduped as "
            "'already handled' by the bridge's own last_attempt_id guard"
        )

    target_raw_token = None
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # _setup only returns the lead's raw token; mint a fresh one for the
        # assigned target agent (Sparky) so we can poll as the actual worker.
        t = await s.get(Agent, target.id)
        raw, token_hash = generate_agent_token()
        t.agent_token_hash = token_hash
        s.add(t)
        await s.commit()
        target_raw_token = raw

    with patch(
        "app.services.dispatch.build_agent_task_prompt",
        new_callable=AsyncMock,
        return_value="prompt text",
    ):
        poll_resp = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {target_raw_token}"},
        )
    assert poll_resp.status_code == 200, poll_resp.text
    poll_body = poll_resp.json()
    assert poll_body["state"] == "new_task", (
        f"unblock must cause the very next poll to redeliver the task, got: {poll_body}"
    )
    assert poll_body["task"]["id"] == str(task.id)
    delivered_attempt_id = poll_body["task"]["dispatch_attempt_id"]
    assert delivered_attempt_id != old_attempt_id, (
        "redelivered task must carry a NEW attempt_id — a bridge deduping on "
        "the old one would silently swallow this redispatch"
    )


@pytest.mark.asyncio
async def test_unblock_notify_on_waiting_task_does_not_touch_ack_or_attempt_id(
    client: AsyncClient, async_session
):
    """Gegenprobe (guardrail, sharpened by W2 — see
    test_unblock_resolve_action_unified_criterion.py): `waiting` (e.g. a
    live `mc ask --blocking` session, "Session bleibt bestehen — same
    session, no re-dispatch") must NOT get the ack/attempt reset — that
    session may still be genuinely alive, and resetting
    ack_at/dispatch_attempt_id here would risk a second, competing dispatch
    racing the one already in flight.

    The W2 criterion (task_lifecycle.apply_unblock_notify_reset) narrows
    "genuinely alive" to: the agent's own lock (`current_task_id`) still
    names THIS task — so this Gegenprobe must set that lock explicitly.
    Before W2 this test passed even without it (the old criterion only
    checked `old_status`), which is exactly the gap Sonde P-B in Rex' review
    of #570 found: a `waiting` card whose agent released the lock got the
    same false protection this test used to grant unconditionally."""
    fresh_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=10)
    board, lead, target, lead_token, task = await _setup(async_session, target_last_seen=fresh_seen)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        pre = await s.get(Task, task.id)
        pre.status = "waiting"
        pre.dispatch_attempt_id = str(uuid.uuid4())
        s.add(pre)
        t = await s.get(Agent, target.id)
        t.current_task_id = task.id  # genuinely live session holds the lock
        s.add(t)
        await s.commit()
        old_ack_at = pre.ack_at
        old_attempt_id = pre.dispatch_attempt_id

    with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={
                "Authorization": f"Bearer {lead_token}",
                "X-Dispatch-Attempt-Id": old_attempt_id,
            },
        )
        assert resp.status_code == 200, resp.text
    mock_dispatch.assert_not_called()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.ack_at is not None
        assert refreshed.ack_at.replace(tzinfo=None) == old_ack_at.replace(tzinfo=None), (
            "waiting must keep its ack_at untouched (no reset)"
        )
        assert refreshed.dispatch_attempt_id == old_attempt_id, (
            "waiting must NOT rotate dispatch_attempt_id — a genuinely live paused "
            "session must not be redispatched a second time"
        )


@pytest.mark.asyncio
async def test_redispatch_clears_stale_current_task_pointer(client: AsyncClient, async_session):
    """Review fix B-3: when the dead-agent redispatch path fires and the
    agent's current_task_id still points at the task being redispatched,
    the pointer is cleared before auto_dispatch_task — nothing may read a
    stale active-task lock in the re-dispatch window."""
    stale_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=2)
    board, lead, target, lead_token, task = await _setup(async_session, target_last_seen=stale_seen)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Agent, target.id)
        t.current_task_id = task.id
        s.add(t)
        await s.commit()

    with patch(
        "app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock
    ), patch("app.utils.create_tracked_task"):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
        assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        dead_agent = await s.get(Agent, target.id)
        assert dead_agent.current_task_id is None, (
            "stale current_task_id must be cleared before the re-dispatch"
        )
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatched_at is None
        assert refreshed.ack_at is None
