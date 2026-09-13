"""Tests for C2: mc hold / release / reassign — Board Lead queue control.

Covers the DoD from task 0a415bb5:
- hold stops a not-yet-dispatched card from being claimed via /me/poll
- an answered blocker escalation does NOT clear a hold (different mechanism
  than the blocker-approval unblock path — approvals.py never touches
  run_control)
- release re-opens a held card for the poll-claim path
- reassign hands a card to another agent without the caller supplying an
  attempt id: assigned_agent_id changes, dispatch_attempt_id rotates, and a
  TaskAttemptAudit row is written
- non-leads get 403 on all three verbs
"""
from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select as sa_select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.approval import Approval
from app.models.board import Board
from app.models.task import Task
from app.models.task_attempt_audit import TaskAttemptAudit
from tests.conftest import test_engine


async def _setup(session: AsyncSession, *, task_status: str = "inbox"):
    board = Board(name="Queue Board", slug=f"queue-{uuid.uuid4().hex[:8]}")
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

    worker_raw, worker_hash = generate_agent_token()
    worker = Agent(
        name="Worker",
        role="developer",
        board_id=board.id,
        agent_token_hash=worker_hash,
        is_board_lead=False,
        scopes=["tasks:read", "tasks:write"],
    )
    session.add(worker)

    other_raw, other_hash = generate_agent_token()
    other = Agent(
        name="Rex",
        role="reviewer",
        board_id=board.id,
        agent_token_hash=other_hash,
        is_board_lead=False,
        scopes=["tasks:read", "tasks:write"],
    )
    session.add(other)

    task = Task(
        board_id=board.id,
        assigned_agent_id=worker.id,
        title="Queue-controlled card",
        status=task_status,
    )
    session.add(task)

    await session.commit()
    await session.refresh(lead)
    await session.refresh(worker)
    await session.refresh(other)
    await session.refresh(task)
    return board, lead, lead_raw, worker, worker_raw, other, other_raw, task


# ── hold ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hold_prevents_poll_claim(client: AsyncClient, async_session):
    """DoD 1: `mc hold` haelt eine noch nicht dispatchte Karte an; der
    Dispatcher (hier: /me/poll, der Claim-Pfad fuer HOST_POLL-Agenten)
    holt sie NICHT ab."""
    board, lead, lead_token, worker, worker_token, _o, _ot, task = await _setup(async_session)

    hold = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/hold",
        json={"reason": "Deploy-Fenster morgen — Karte soll warten"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert hold.status_code == 200, hold.text
    assert hold.json()["run_control"] == "manual_hold"
    assert hold.json()["hold_reason"] == "Deploy-Fenster morgen — Karte soll warten"

    poll = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert poll.status_code == 200
    body = poll.json()
    assert body["state"] != "new_task", body
    assert body.get("task_id") != str(task.id), body

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.run_control == "manual_hold"
        assert refreshed.dispatched_at is None


@pytest.mark.asyncio
async def test_hold_rejects_already_dispatched_card(client: AsyncClient, async_session):
    board, lead, lead_token, worker, worker_token, _o, _ot, task = await _setup(
        async_session, task_status="in_progress"
    )
    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/hold",
        json={"reason": "zu spaet"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_hold_requires_reason(client: AsyncClient, async_session):
    board, lead, lead_token, worker, worker_token, _o, _ot, task = await _setup(async_session)
    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/hold",
        json={"reason": "   "},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 422, resp.text


# ── hold survives an answered blocker escalation (DoD 2) ───────────────


@pytest.mark.asyncio
async def test_hold_survives_answered_blocker_escalation(client: AsyncClient, async_session, auth_client):
    """DoD 2: Eine beantwortete Blocker-Eskalation hebt einen Hold NICHT auf.

    Setup: card is on hold (run_control=manual_hold) AND separately blocked
    with a pending blocker_decision approval (both can coexist — hold is an
    orthogonal lead-side control). The operator approves the blocker via the
    REAL /api/v1/approvals/{id} endpoint (approvals.py resolve_approval).
    That path resets status blocked -> inbox and clears dispatch_attempt_id,
    but must NEVER touch run_control. Sabotage probe run manually (see PR
    description): removing the `Task.run_control.is_(None)` filter added to
    the /me/poll inbox-candidates query turns this test red because the
    poll assertion below starts returning new_task for the held card.
    """
    board, lead, lead_token, worker, worker_token, _o, _ot, task = await _setup(
        async_session, task_status="blocked"
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        t.run_control = "manual_hold"
        t.hold_reason = "Lead haelt die Karte waehrend des Deploy-Fensters"
        s.add(t)
        await s.commit()

    approval_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            id=approval_id,
            board_id=board.id,
            task_id=task.id,
            agent_id=worker.id,
            action_type="blocker_decision",
            description="Test blocker waehrend Hold",
            status="pending",
        )
        s.add(approval)
        await s.commit()

    with patch("app.utils.create_tracked_task"):
        resolve = await auth_client.patch(
            f"/api/v1/approvals/{approval_id}",
            json={"status": "approved", "resolver_note": "Loesung ist klar, weiter."},
        )
    assert resolve.status_code == 200, resolve.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        # The approval path did its normal job...
        assert refreshed.status == "inbox"
        assert refreshed.dispatch_attempt_id is None
        # ...but the hold survives it.
        assert refreshed.run_control == "manual_hold", (
            "Hold wurde von der Blocker-Eskalation faelschlich aufgehoben"
        )
        assert refreshed.hold_reason == "Lead haelt die Karte waehrend des Deploy-Fensters"

    # And the practical consequence: poll still must not deliver the card.
    poll = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert poll.status_code == 200
    assert poll.json()["state"] != "new_task", poll.json()


# ── release ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_reopens_card_for_poll_claim(client: AsyncClient, async_session):
    """DoD 3: `mc release` gibt die Karte wieder frei."""
    board, lead, lead_token, worker, worker_token, _o, _ot, task = await _setup(async_session)

    hold = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/hold",
        json={"reason": "kurz warten"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert hold.status_code == 200

    with patch("app.utils.create_tracked_task"):
        release = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/release",
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert release.status_code == 200, release.text
    assert release.json()["run_control"] is None
    assert release.json()["hold_reason"] is None

    with patch("app.services.dispatch.build_agent_task_prompt", return_value="x"):
        poll = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {worker_token}"},
        )
    assert poll.status_code == 200
    body = poll.json()
    assert body["state"] == "new_task", body
    assert body["task"]["id"] == str(task.id)


@pytest.mark.asyncio
async def test_release_without_hold_returns_409(client: AsyncClient, async_session):
    board, lead, lead_token, worker, worker_token, _o, _ot, task = await _setup(async_session)
    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/release",
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_release_does_not_clear_admin_stop(client: AsyncClient, async_session):
    """Release only owns run_control=manual_hold — an admin `stopped` run
    (operations.py stop_task_run) must go through /resume, not mc release."""
    board, lead, lead_token, worker, worker_token, _o, _ot, task = await _setup(async_session)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        t.run_control = "stopped"
        s.add(t)
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/release",
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 409, resp.text


# ── reassign ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reassign_rotates_attempt_id_and_audits_without_caller_needing_it(
    client: AsyncClient, async_session,
):
    """DoD 4: `mc reassign <task> --to <agent>` haengt eine fremde Karte um,
    ohne dass der Aufrufer eine Attempt-ID beschafft; Attempt-ID rotiert,
    Audit-Eintrag vorhanden."""
    board, lead, lead_token, worker, worker_token, other, other_token, task = await _setup(
        async_session, task_status="review"
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        t.dispatch_attempt_id = str(uuid.uuid4())
        old_attempt = t.dispatch_attempt_id
        t.ack_at = dt.datetime.now(tz=dt.timezone.utc)
        t.dispatched_at = dt.datetime.now(tz=dt.timezone.utc)
        s.add(t)
        await s.commit()

    # Lead calls reassign WITHOUT any X-Dispatch-Attempt-Id header — that's
    # the whole point, the caller (lead) is not the running worker and has
    # no legitimate attempt id to send.
    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
        json={"to": "Rex"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assigned_agent_id"] == str(other.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.assigned_agent_id == other.id
        assert refreshed.dispatch_attempt_id is not None
        assert refreshed.dispatch_attempt_id != old_attempt
        assert refreshed.ack_at is None
        assert refreshed.dispatched_at is None

        audit_result = await s.exec(
            sa_select(TaskAttemptAudit)
            .where(TaskAttemptAudit.task_id == task.id)
            .order_by(TaskAttemptAudit.created_at)
        )
        audit_rows = audit_result.scalars().all()
        assert len(audit_rows) == 1
        row = audit_rows[0]
        assert str(row.old_attempt) == old_attempt
        assert str(row.new_attempt) == refreshed.dispatch_attempt_id
        assert row.caller == "board_lead_reassign"

    # New agent's poll now sees it as active work (review status, ack_at
    # None -> the prompt-delivery fallthrough in the poll handler).
    with patch("app.services.dispatch.build_agent_task_prompt", return_value="x"):
        poll = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {other_token}"},
        )
    assert poll.status_code == 200
    assert poll.json().get("task_id") == str(task.id) or poll.json().get("task", {}).get("id") == str(task.id)


@pytest.mark.asyncio
async def test_reassign_by_uuid_also_works(client: AsyncClient, async_session):
    board, lead, lead_token, worker, worker_token, other, other_token, task = await _setup(async_session)
    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
        json={"to": str(other.id)},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["assigned_agent_id"] == str(other.id)


@pytest.mark.asyncio
async def test_reassign_unknown_agent_404(client: AsyncClient, async_session):
    board, lead, lead_token, worker, worker_token, other, other_token, task = await _setup(async_session)
    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
        json={"to": "GhostAgent"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_reassign_done_task_rejected(client: AsyncClient, async_session):
    board, lead, lead_token, worker, worker_token, other, other_token, task = await _setup(
        async_session, task_status="done"
    )
    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
        json={"to": "Rex"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 409, resp.text


# ── non-lead gets 403 on all three verbs ────────────────────────────────


@pytest.mark.asyncio
async def test_non_lead_gets_403_on_hold_release_reassign(client: AsyncClient, async_session):
    board, lead, lead_token, worker, worker_token, other, other_token, task = await _setup(async_session)

    hold = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/hold",
        json={"reason": "ich bin kein Lead"},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert hold.status_code == 403, hold.text

    # Set up a held state directly so /release has something to reject on
    # authorization grounds (not on state grounds).
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        t.run_control = "manual_hold"
        t.hold_reason = "x"
        s.add(t)
        await s.commit()

    release = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/release",
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert release.status_code == 403, release.text

    reassign = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
        json={"to": "Rex"},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert reassign.status_code == 403, reassign.text
