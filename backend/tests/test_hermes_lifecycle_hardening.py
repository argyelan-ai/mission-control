"""Phase 26 / Plan 26-01: RED regression tests for Hermes lifecycle hardening.

Pins the current broken behavior of three convergent bugs that all live in
backend/app/routers/agents.py:2944-2948 (the poll-claim atomic write):

- F1 (HERM-10): Poll-claim flips status=inbox -> in_progress before the
  agent CLI has even seen the prompt. Status MUST stay 'inbox' until the
  agent itself PATCHes status=in_progress (= explicit ACK handshake per
  Migration 0018).
- F2 (HERM-10): started_at is NEVER set in the poll-claim path. The
  subsequent admin PATCH to in_progress is a no-op (status didn't change),
  so started_at stays NULL forever -> bad analytics, bad lifecycle audit.
- F3 (HERM-10): dispatched_at == ack_at exactly (same `now` literal at
  lines 2947+2948). The two timestamps must have a measurable spread so
  that the dispatch-vs-ack latency is observable.

These tests are EXPECTED TO FAIL today. They turn GREEN once Plans 26-02
(F1+F3 split) and 26-03 (F2 deterministic started_at) land.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _seed_agent_with_inbox_task(session: AsyncSession):
    """Board + Agent (with poll/PATCH scopes) + inbox Task pre-dispatched.

    Mirrors the Hermes shape: dispatched_at is already set (the original push
    dispatch put it there), ack_at is NULL, status is 'inbox' awaiting the
    agent's poll + ACK.
    """
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    board = Board(id=board_id, name="Hermes Board", slug="hermes")
    session.add(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=agent_id,
        name="Hermes",
        board_id=board_id,
        agent_token_hash=token_hash,
        scopes=["tasks:read", "tasks:write", "knowledge:read"],
    )
    session.add(agent)

    pre_dispatched = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=2)
    task = Task(
        id=task_id,
        board_id=board_id,
        title="Smoke Hermes Lifecycle",
        description="Test task pinning F1+F2+F3",
        status="inbox",
        assigned_agent_id=agent_id,
        dispatched_at=pre_dispatched,  # already dispatched once via push path
        ack_at=None,
        started_at=None,
    )
    session.add(task)
    await session.commit()
    await session.refresh(board)
    await session.refresh(agent)
    await session.refresh(task)
    return board, agent, task, raw_token


async def _reload_task(task_id: uuid.UUID):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(select(__import__("app.models.task", fromlist=["Task"]).Task).where(
            __import__("app.models.task", fromlist=["Task"]).Task.id == task_id
        ))
        return result.first()


# ── F1: status must NOT flip on poll-claim ────────────────────────────


@pytest.mark.asyncio
async def test_poll_does_not_flip_status_until_explicit_ack(client: AsyncClient):
    """F1 (HERM-10): Poll-claim is a RESERVATION, not a status transition.

    GIVEN: An inbox task assigned to an agent (already dispatched_at set).
    WHEN:  Agent calls GET /api/v1/agent/me/poll.
    THEN:  Response state == 'new_task' (prompt delivered) AND
           DB-reload(task).status STAYS 'inbox' AND
           DB-reload(task).ack_at IS NULL (only set after agent PATCHes
           status=in_progress as the real ACK).

    RED: pinned by Plan 26-01, expected GREEN after Plan 26-02.
    Current bug surface: agents.py:2945-2948 atomically sets status +
    ack_at on poll, conflating reservation with explicit-ACK handshake.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _seed_agent_with_inbox_task(s)

    with patch("app.services.dispatch.build_agent_task_prompt", new_callable=AsyncMock) as mock_prompt:
        mock_prompt.return_value = "DISPATCH-PROMPT"
        resp = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "new_task", f"expected new_task, got {body}"

    reloaded = await _reload_task(task.id)
    assert reloaded is not None

    # F1 assertion — the bug is HERE today.
    assert reloaded.status == "inbox", (
        f"F1: status leaked to {reloaded.status!r} before agent ACK "
        f"(agents.py:2946 sets status=in_progress in same atomic write as poll-claim)"
    )
    # ack_at must remain NULL until agent PATCHes status=in_progress itself.
    assert reloaded.ack_at is None, (
        f"F1: ack_at={reloaded.ack_at} set on poll-claim — must wait for "
        f"explicit agent PATCH (Migration 0018 ACK handshake contract)"
    )


# ── F2: started_at must be set deterministically ──────────────────────


@pytest.mark.asyncio
async def test_started_at_set_after_poll_then_agent_patches_in_progress(
    client: AsyncClient,
):
    """F2 (HERM-10): started_at must be set when the lifecycle reaches in_progress.

    GIVEN: Inbox task, started_at=NULL.
    WHEN:  Agent polls (claims) AND THEN PATCHes status=in_progress as the real ACK.
    THEN:  DB-reload(task).status == 'in_progress' AND
           DB-reload(task).started_at IS NOT NULL AND fresh (< 5s).

    RED: pinned by Plan 26-01, expected GREEN after Plan 26-02/03.
    Current bug surface: poll-claim already flipped status to in_progress
    (F1), so the subsequent agent PATCH is a no-op — old_status ==
    new_status — and tasks.py:1239 condition `old_status != "in_progress"`
    is False, started_at NEVER gets set.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _seed_agent_with_inbox_task(s)

    # Step 1: poll (claim).
    with patch("app.services.dispatch.build_agent_task_prompt", new_callable=AsyncMock) as mp:
        mp.return_value = "PROMPT"
        poll_resp = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert poll_resp.status_code == 200

    # Step 2: agent ACKs by patching status -> in_progress (board-scoped admin path).
    # This mirrors what mc-mcp.py does via mc_patch_task (Phase 25-08).
    await asyncio.sleep(0.05)  # simulate sub-second think time
    patch_resp = await client.patch(
        f"/api/v1/boards/{board.id}/tasks/{task.id}",
        json={"status": "in_progress"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Agent token can write its own task; if 401 happens, fall back to admin JWT shape.
    # If the path requires admin, we still want to capture started_at semantics.
    if patch_resp.status_code in (401, 403):
        # Retry via JWT admin if scope-gated.
        from app.auth import create_access_token
        from app.models.user import User
        admin_id = uuid.UUID("00000000-0000-0000-0000-000000000077")
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            s.add(User(id=admin_id, email="admin77@mc.local", name="A77",
                       role="admin", is_active=True))
            await s.commit()
        admin_token = create_access_token(str(admin_id), "admin")
        patch_resp = await client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    # If the PATCH was rejected as "in_progress -> in_progress" no-op, that
    # IS the F2 bug surface: poll-claim already flipped status, so the
    # ACK PATCH cannot run -> started_at can never be set.
    if patch_resp.status_code == 400 and "in progress" in patch_resp.text.lower():
        assert False, (
            "F2: PATCH status=in_progress rejected as no-op transition — "
            "poll-claim already flipped status (F1 bug), so the agent ACK "
            "cannot transition status and tasks.py:1239 elif-branch never "
            f"sets started_at. PATCH response: {patch_resp.text}"
        )
    assert patch_resp.status_code in (200, 204), patch_resp.text

    reloaded = await _reload_task(task.id)
    assert reloaded.status == "in_progress", (
        f"F2 prerequisite: status not in_progress after PATCH (got {reloaded.status})"
    )

    # F2 assertion — the bug is HERE today.
    assert reloaded.started_at is not None, (
        "F2: started_at NULL after in_progress transition — poll-claim "
        "already flipped status, so the admin PATCH was a status no-op "
        "and tasks.py:1239 elif-branch never set started_at"
    )
    delta = dt.datetime.now(tz=dt.timezone.utc) - reloaded.started_at.replace(
        tzinfo=dt.timezone.utc
    ) if reloaded.started_at.tzinfo is None else dt.datetime.now(tz=dt.timezone.utc) - reloaded.started_at
    assert delta.total_seconds() < 5, (
        f"F2: started_at stale ({delta.total_seconds()}s old) — must be set "
        f"NOW on the in_progress transition"
    )


# ── F3: dispatched_at strictly < ack_at ───────────────────────────────


@pytest.mark.asyncio
async def test_dispatched_at_strictly_before_ack_at(client: AsyncClient):
    """F3 (HERM-10): dispatched_at < ack_at with measurable spread.

    GIVEN: Inbox task with dispatched_at=NULL, ack_at=NULL.
    WHEN:  Poll claims AND agent later PATCHes in_progress (real ACK).
    THEN:  DB-reload(task).dispatched_at < DB-reload(task).ack_at AND
           the spread is >= 1 millisecond (any positive observable spread).

    RED: pinned by Plan 26-01, expected GREEN after Plan 26-02.
    Current bug surface: agents.py:2947 + 2948 set both fields to the same
    `now` literal, producing identical timestamps with zero spread.
    """
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Hermes F3", slug="hermes-f3"))
        raw_token, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="HermesF3", board_id=board_id,
            agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="F3 spread",
            status="inbox", assigned_agent_id=agent_id,
            dispatched_at=None, ack_at=None,
        ))
        await s.commit()

    # Poll claim.
    with patch("app.services.dispatch.build_agent_task_prompt", new_callable=AsyncMock) as mp:
        mp.return_value = "PROMPT"
        poll_resp = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert poll_resp.status_code == 200

    await asyncio.sleep(0.05)

    # Agent ACK via PATCH.
    patch_resp = await client.patch(
        f"/api/v1/boards/{board_id}/tasks/{task_id}",
        json={"status": "in_progress"},
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    if patch_resp.status_code in (401, 403):
        from app.auth import create_access_token
        from app.models.user import User
        admin_id = uuid.UUID("00000000-0000-0000-0000-000000000088")
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            s.add(User(id=admin_id, email="admin88@mc.local", name="A88",
                       role="admin", is_active=True))
            await s.commit()
        admin_token = create_access_token(str(admin_id), "admin")
        patch_resp = await client.patch(
            f"/api/v1/boards/{board_id}/tasks/{task_id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    # If PATCH rejected as no-op transition, the bug already cascaded —
    # we still want to assert F3 contract on the post-poll DB state.
    reloaded = await _reload_task(task_id)
    assert reloaded is not None
    if patch_resp.status_code == 400 and "in progress" in patch_resp.text.lower():
        # Poll already set BOTH timestamps to identical `now` literal —
        # this IS the F3 surface in its purest form (no PATCH happened, but
        # dispatched_at + ack_at are equal because of agents.py:2947-2948).
        assert reloaded.dispatched_at is not None, "F3 prereq: dispatched_at must be set"
        assert reloaded.ack_at is not None, "F3 prereq: ack_at set on poll (F1 bug)"
    else:
        assert patch_resp.status_code in (200, 204), patch_resp.text
        assert reloaded.dispatched_at is not None, "F3 prereq: dispatched_at must be set"
        assert reloaded.ack_at is not None, "F3 prereq: ack_at must be set"

    # Normalize tz for comparison.
    da = reloaded.dispatched_at
    aa = reloaded.ack_at
    if da.tzinfo is None:
        da = da.replace(tzinfo=dt.timezone.utc)
    if aa.tzinfo is None:
        aa = aa.replace(tzinfo=dt.timezone.utc)

    # F3 assertion — the bug is HERE today.
    assert da < aa, (
        f"F3: dispatched_at={da.isoformat()} == ack_at={aa.isoformat()} "
        f"(no spread) — both set to identical `now` literal in agents.py:2947-2948"
    )
    spread_ms = (aa - da).total_seconds() * 1000
    assert spread_ms >= 1.0, (
        f"F3: spread between dispatched_at and ack_at is {spread_ms:.3f}ms "
        f"— must be >= 1ms to be observable"
    )


# ── F2 regression: re-open MUST NOT reset started_at ──────────────────


@pytest.mark.asyncio
async def test_reopen_does_not_reset_started_at(client: AsyncClient):
    """F2 regression (Plan 26-03): first-set-wins on started_at.

    GIVEN: Task with status=review and started_at set 2 hours ago.
    WHEN:  Admin PATCHes status=in_progress (re-open).
    THEN:  started_at unchanged (still 2 hours ago, NOT now).

    Audit-trail integrity: original "work began" timestamp must survive
    re-opens (review→in_progress, blocked→in_progress).
    """
    from app.models.board import Board
    from app.models.task import Task
    from app.models.user import User
    from app.auth import create_access_token

    board_id = uuid.uuid4()
    task_id = uuid.uuid4()
    admin_id = uuid.UUID("00000000-0000-0000-0000-000000000099")

    original_started_at = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=2)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Reopen Board", slug="reopen-board"))
        s.add(User(id=admin_id, email="admin99@mc.local", name="A99",
                   role="admin", is_active=True))
        s.add(Task(
            id=task_id, board_id=board_id, title="Reopen me",
            status="review", started_at=original_started_at,
            ack_at=original_started_at,
        ))
        await s.commit()

    admin_token = create_access_token(str(admin_id), "admin")
    patch_resp = await client.patch(
        f"/api/v1/boards/{board_id}/tasks/{task_id}",
        json={"status": "in_progress"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert patch_resp.status_code in (200, 204), patch_resp.text

    reloaded = await _reload_task(task_id)
    # PR #109: review->in_progress with no reconstructable developer resolves
    # to inbox (no_developer outcome) instead of a ghost in_progress. This
    # test's subject is started_at preservation, which must hold either way.
    assert reloaded.status == "inbox"
    assert reloaded.started_at is not None

    sa = reloaded.started_at
    if sa.tzinfo is None:
        sa = sa.replace(tzinfo=dt.timezone.utc)
    drift = abs((sa - original_started_at).total_seconds())
    assert drift < 1.0, (
        f"F2 regression: started_at drifted {drift}s from original on re-open "
        f"(was {original_started_at.isoformat()}, now {sa.isoformat()}) — "
        f"first-set-wins violated, Cycle Time analytics broken"
    )
