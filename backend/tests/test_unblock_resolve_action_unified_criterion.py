"""W1/W2/W3 (Rex' review of PR #570): one criterion for all three
`resolve_unblock_action` branches, instead of patching the next stalling
site one card at a time.

Card source: Rex' ship-ready verdict on #570 named three loose ends —

  W1 — the notify branch's comment claimed to "mirror" the parked branch of
       `messaging.resolve_waiting_answer`. It didn't: the twin discriminates
       by *liveness* (`current_task_id`), the notify branch discriminated by
       *old_status*. Fixed by folding the twin's liveness check into the
       notify branch's own criterion (this file's W2 tests) and correcting
       the claim (task_lifecycle.apply_unblock_notify_reset's docstring).

  W2 — Sonde P-B: a `waiting` card whose agent is alive but holds NO lock on
       it (`current_task_id != task.id` — released, not paused) got the same
       "never reset" protection as a genuinely live, paused `mc ask
       --blocking` session. Both this file's tests below pin the fix in both
       directions (P4's concern: the truly live case must still survive).

  W3 — Sonde P-D: the dead-agent `redispatch` branch never touched
       `dispatch_attempt_id` at all. Its own immediate `auto_dispatch_task`
       call only sets a fresh id with `only_if_null=True` — a no-op against
       an already-non-None stale id, so the OLD id (and the bridge's dedup
       keyed on it) survived the whole path.
"""
from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token, generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.models.user import User
from tests.conftest import test_engine


async def _setup(
    session: AsyncSession,
    *,
    target_last_seen: dt.datetime | None,
    target_heartbeat_interval: str = "5m",
):
    board = Board(name="Unify Board", slug=f"unify-{uuid.uuid4().hex[:8]}", blocker_triage_minutes=0)
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

    task = Task(
        board_id=board.id,
        assigned_agent_id=target.id,
        title="Unify probe",
        status="blocked",
        dispatched_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=30),
        ack_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=30),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    return board, lead, target, lead_raw, target_raw, task


# ─────────────────────────────────────────────────────────────────────────
# W2 — Sonde P-B: waiting + released lock (current_task_id != task.id)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unblock_notify_on_waiting_with_released_lock_resets_and_redelivers(
    client: AsyncClient, async_session,
):
    """P-B repro, now fixed: a `waiting` card whose assigned agent is alive
    but no longer holds the lock on it (current_task_id is None — the
    "parked" case of the messaging.py twin, reached here via `mc
    park`/Deploy-Fenster instead of `mc ask --blocking`) must get the SAME
    reset as a `blocked` origin. Before the W2 fix, the guard only checked
    `old_status == "blocked"`, so this exact stall signature (ack_at fresh,
    attempt_id stale, poll reports `working` with no card) survived
    unchanged."""
    fresh_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=10)
    board, lead, target, lead_token, target_token, task = await _setup(
        async_session, target_last_seen=fresh_seen,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        pre = await s.get(Task, task.id)
        pre.status = "waiting"
        pre.dispatch_attempt_id = str(uuid.uuid4())
        s.add(pre)
        await s.commit()
        old_attempt_id = pre.dispatch_attempt_id
        # Agent released the lock (current_task_id stays None) — this is
        # the P-B case, distinct from a genuinely live paused session.

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
    mock_dispatch.assert_not_called()  # notify path, not a full redispatch

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.ack_at is None, (
            "released-lock waiting card must get the same reset as blocked — "
            "otherwise poll's liveness gate keeps reporting 'working' with no card"
        )
        assert refreshed.dispatch_attempt_id != old_attempt_id, (
            "dispatch_attempt_id must rotate — else the bridge's own dedup "
            "(last_attempt_id) swallows the redelivery"
        )

    # End-to-end: the assigned agent's very next poll must redeliver.
    with patch(
        "app.services.dispatch.build_agent_task_prompt",
        new_callable=AsyncMock,
        return_value="prompt text",
    ):
        poll_resp = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {target_token}"},
        )
    assert poll_resp.status_code == 200, poll_resp.text
    poll_body = poll_resp.json()
    assert poll_body["state"] == "new_task", (
        f"released-lock waiting unblock must redeliver on the very next poll, got: {poll_body}"
    )
    assert poll_body["task"]["id"] == str(task.id)


@pytest.mark.asyncio
async def test_unblock_notify_on_waiting_with_live_lock_still_untouched(
    client: AsyncClient, async_session,
):
    """P4 Gegenprobe (must NOT regress): a `waiting` card whose agent is
    alive AND still holds the lock on it (current_task_id == task.id — a
    genuinely paused `mc ask --blocking` session) must stay completely
    untouched. This is the exact case the card's Definition of Done calls
    out as "die Nebenbedingung, die hier am leichtesten kippt" — widening
    the W2 guard to catch the released-lock case must not also catch the
    live one."""
    fresh_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=10)
    board, lead, target, lead_token, target_token, task = await _setup(
        async_session, target_last_seen=fresh_seen,
    )

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
            "a genuinely live paused session must keep its ack_at untouched"
        )
        assert refreshed.dispatch_attempt_id == old_attempt_id, (
            "a genuinely live paused session must NOT be redispatched a second time"
        )


# ─────────────────────────────────────────────────────────────────────────
# W3 — Sonde P-D: redispatch branch never touched dispatch_attempt_id
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_redispatch_clears_stale_dispatch_attempt_id(
    client: AsyncClient, async_session,
):
    """P-D repro, now fixed: the dead-agent redispatch branch left
    dispatch_attempt_id completely untouched. Its own auto_dispatch_task
    call (mocked here, but real in production) only sets a fresh id with
    only_if_null=True — against an already-non-None stale id that's a
    no-op, so the OLD id survived the whole path and a bridge deduping on
    it would silently swallow the redelivery. Fix: clear_dispatch_attempt_id
    before the auto_dispatch_task call, mirroring requeue_unblocked_task's
    existing (already-correct) pattern."""
    stale_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=2)
    board, lead, target, lead_token, target_token, task = await _setup(
        async_session, target_last_seen=stale_seen,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        pre = await s.get(Task, task.id)
        pre.dispatch_attempt_id = str(uuid.uuid4())
        s.add(pre)
        await s.commit()
        old_attempt_id = pre.dispatch_attempt_id
    assert old_attempt_id is not None

    with patch(
        "app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock
    ) as mock_dispatch, patch("app.utils.create_tracked_task") as mock_create_tracked:
        def _run_now(coro, name=None):
            import asyncio
            return asyncio.ensure_future(coro)
        mock_create_tracked.side_effect = _run_now

        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={
                "Authorization": f"Bearer {lead_token}",
                "X-Dispatch-Attempt-Id": old_attempt_id,
            },
        )
        assert resp.status_code == 200, resp.text
        import asyncio
        await asyncio.sleep(0)  # let the tracked task run

    mock_dispatch.assert_called_once()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatch_attempt_id != old_attempt_id, (
            "redispatch must clear the stale attempt_id — otherwise the "
            "re-delivered card is deduped by the bridge as 'already handled'"
        )
        assert refreshed.dispatch_attempt_id is None, (
            "redispatch clears (not rotates) the id, mirroring "
            "requeue_unblocked_task — auto_dispatch_task's own "
            "only_if_null=True write then sets the genuinely fresh one"
        )


# ─────────────────────────────────────────────────────────────────────────
# W2 on the operator path (routers/tasks.py) — symmetry check
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_operator_unblock_from_waiting_with_released_lock_resets_and_redelivers(
    client: AsyncClient, async_session,
):
    """Same P-B case as the agent-scoped test above, exercised via the
    OPERATOR PATCH (`/api/v1/boards/.../tasks/{id}`, admin auth). This path
    has no upstream `update_agent_active_task` call, so
    `target.current_task_id` read at the notify call site in tasks.py is
    already the pre-transition value — confirming the fix works there
    without needing the agent-scoped path's extra snapshot-before-mutation
    step (see apply_unblock_notify_reset's docstring)."""
    board = Board(name="Unify Op Board", slug=f"unify-op-{uuid.uuid4().hex[:8]}", blocker_triage_minutes=0)
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    _, target_hash = generate_agent_token()
    target = Agent(
        name="Sparky",
        role="developer",
        board_id=board.id,
        agent_token_hash=target_hash,
        is_board_lead=False,
        scopes=["tasks:read", "tasks:write"],
        last_seen_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=10),
        heartbeat_config={"interval": "5m"},
    )
    async_session.add(target)
    await async_session.commit()
    await async_session.refresh(target)
    # current_task_id stays None — the agent released the lock, or was
    # never on this task in the first place (the parked/P-B case).

    old_attempt_id = str(uuid.uuid4())
    task = Task(
        board_id=board.id,
        assigned_agent_id=target.id,
        title="Operator-path unify probe",
        status="waiting",
        dispatched_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=30),
        ack_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=30),
        dispatch_attempt_id=old_attempt_id,
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    admin_id = uuid.uuid4()
    async_session.add(User(id=admin_id, email=f"op-{admin_id.hex[:6]}@mc.local", name="Op", role="admin", is_active=True))
    await async_session.commit()
    admin_token = create_access_token(str(admin_id), "admin")

    with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch:
        resp = await client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200, resp.text
    mock_dispatch.assert_not_called()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.ack_at is None, (
            "released-lock waiting card must get the reset via the operator path too"
        )
        assert refreshed.dispatch_attempt_id != old_attempt_id, (
            "dispatch_attempt_id must rotate on the operator path as well"
        )
