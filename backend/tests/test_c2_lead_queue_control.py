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

PR #533 Nacharbeit (task 00241bad, Rex review 09a860f6) additionally covers:
- GET /boards/{id}/tasks/next (pull-claim) respected the hold (Blocker)
- hold_reason no longer survives a user PATCH back to inbox (Warning 1)
- reassign clears the old agent's current_task_id lock (Warning 2)
- reassign --to no longer treats the target name as an ilike wildcard
  pattern (Warning 3)
- two further claim paths found while searching for a third: phase
  auto-advance (tasks.py + watchdog task_monitor.py) and the Board-Lead
  implicit-ACK-on-subtask-creation path also now respect the hold
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


# ── Blocker: GET /boards/{id}/tasks/next also respects the hold ────────────


@pytest.mark.asyncio
async def test_hold_prevents_tasks_next_claim(client: AsyncClient, async_session):
    """Blocker (Rex review 09a860f6, PR #533 Nacharbeit): the pull-claim
    endpoint `/tasks/next` calls lock_and_set() directly and bypasses
    check_dispatch_allowed entirely — a second, undocumented door around
    `mc hold`, found in addition to the /me/poll path already covered
    above. A subtask (not a root task) is used deliberately: the root guard
    at agent_task_status.py:754 already 204s a non-lead's root-task pull for
    an unrelated reason, which would mask whether the hold filter is doing
    anything — with a subtask that guard doesn't fire.

    Sabotage-Probe: removing the `Task.run_control.is_(None)` filter from
    the candidates query (agent_task_status.py, get_next_task) turns this
    red — the endpoint then returns 200 with the held subtask, ack_at set,
    status flipped to in_progress, while run_control stays manual_hold
    (exactly the deadlock Rex's probe demonstrated: review/blocked PATCHes
    afterwards both 409 on run_control).
    """
    # parent status="done" (not "in_progress"): the default fixture task is
    # only used here as an FK anchor for parent_task_id — assigned to worker
    # AND "in_progress" would trip the endpoint's own "agent already busy"
    # check (Step 1) before the candidates query is even reached, masking
    # whether the run_control filter does anything.
    board, lead, lead_token, worker, worker_token, _o, _ot, parent = await _setup(
        async_session, task_status="done"
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        subtask = Task(
            board_id=board.id,
            parent_task_id=parent.id,
            assigned_agent_id=worker.id,
            title="Held subtask",
            status="inbox",
        )
        s.add(subtask)
        await s.commit()
        await s.refresh(subtask)

    hold = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{subtask.id}/hold",
        json={"reason": "Deploy-Fenster — Subtask soll warten"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert hold.status_code == 200, hold.text

    next_resp = await client.get(
        f"/api/v1/agent/boards/{board.id}/tasks/next",
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert next_resp.status_code == 204, next_resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, subtask.id)
        assert refreshed.status == "inbox"
        assert refreshed.ack_at is None
        assert refreshed.run_control == "manual_hold"


@pytest.mark.asyncio
async def test_tasks_next_still_delivers_unheld_subtask(client: AsyncClient, async_session):
    """Control test: an un-held subtask is still claimable via /tasks/next —
    the new filter doesn't just block everything."""
    board, lead, lead_token, worker, worker_token, _o, _ot, parent = await _setup(
        async_session, task_status="done"
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        subtask = Task(
            board_id=board.id,
            parent_task_id=parent.id,
            assigned_agent_id=worker.id,
            title="Free subtask",
            status="inbox",
        )
        s.add(subtask)
        await s.commit()
        await s.refresh(subtask)

    next_resp = await client.get(
        f"/api/v1/agent/boards/{board.id}/tasks/next",
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert next_resp.status_code == 200, next_resp.text
    assert next_resp.json()["task"]["id"] == str(subtask.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, subtask.id)
        assert refreshed.status == "in_progress"
        assert refreshed.ack_at is not None


# ── Warning 1: hold_reason must not survive an inbox reset ─────────────────


@pytest.mark.asyncio
async def test_inbox_reset_clears_hold_reason(client: AsyncClient, async_session, auth_client):
    """Warning 1 (Rex review 09a860f6): a direct user PATCH back to inbox
    resets run_control but used to leave hold_reason as a phantom
    justification for a hold that no longer exists.

    Sabotage-Probe: deleting the `task.hold_reason = None` line added next
    to the existing `task.run_control = None` in tasks.py's inbox-reset
    branch turns this red (hold_reason would still read the old text).
    """
    board, lead, lead_token, worker, worker_token, _o, _ot, task = await _setup(async_session)

    hold = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/hold",
        json={"reason": "Warten auf Freigabe"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert hold.status_code == 200, hold.text

    # inbox -> blocked (valid transition, untouched by the run_control reset
    # branch) so we can PATCH blocked -> inbox next without re-using /hold.
    blocked = await auth_client.patch(
        f"/api/v1/boards/{board.id}/tasks/{task.id}",
        json={"status": "blocked"},
    )
    assert blocked.status_code == 200, blocked.text

    reset = await auth_client.patch(
        f"/api/v1/boards/{board.id}/tasks/{task.id}",
        json={"status": "inbox"},
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["run_control"] is None
    assert reset.json()["hold_reason"] is None, (
        "hold_reason ueberlebt den inbox-Reset als Phantom-Begruendung"
    )


# ── Warning (Runde 3, Rex review W1): operator PATCH overriding a held
# card must clear the hold, not leave it running under a lock nothing can
# lift ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_operator_override_of_held_card_clears_run_control(
    client: AsyncClient, async_session, auth_client
):
    """Warning (Rex review, Runde 3 W1): an operator PATCH that overrides a
    held card by moving it straight from inbox to a running status (e.g.
    in_progress) used to leave run_control=manual_hold and hold_reason in
    place — only the inbox-reset branch cleared them. The card then ran,
    but the assigned agent's very next status update hit the run_control
    409 guard (agent_task_status.py) for a hold the operator had just
    overridden and could no longer see or release (`mc release` only
    accepts status=inbox) — a deadlock with no exit.

    Overriding the lead's hold is fine — the operator is allowed to. It
    must just not leave the card running under a lock nothing can lift.

    Sabotage-Probe: removing the `elif old_status == "inbox" and
    task.run_control == "manual_hold": ...` branch (routers/tasks.py)
    turns this red — run_control survives the override and the agent's
    follow-up PATCH gets 409 instead of 200.
    """
    board, lead, lead_token, worker, worker_token, _o, _ot, task = await _setup(async_session)

    hold = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/hold",
        json={"reason": "Deploy-Fenster"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert hold.status_code == 200, hold.text

    override = await auth_client.patch(
        f"/api/v1/boards/{board.id}/tasks/{task.id}",
        json={"status": "in_progress"},
    )
    assert override.status_code == 200, override.text
    assert override.json()["run_control"] is None, (
        "Operator-Override liess run_control=manual_hold stehen"
    )
    assert override.json()["hold_reason"] is None, (
        "Operator-Override liess hold_reason als Phantom-Begruendung stehen"
    )

    # The deadlock Rex found: with run_control still stuck, ANY subsequent
    # agent PATCH on this card — even one that touches no status at all —
    # hits the run_control 409 guard before it can do anything else.
    followup = await client.patch(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
        json={"description": "Weiterarbeit nach Operator-Override"},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert followup.status_code == 200, followup.text


# ── Warning 2: reassign must release the old agent's active-task lock ──────


@pytest.mark.asyncio
async def test_reassign_clears_old_agents_current_task_lock(client: AsyncClient, async_session):
    """Warning 2 (Rex review 09a860f6): reassign never cleared the old
    agent's `current_task_id`. Left dangling, dispatch.py's Guard 1
    (`best_agent.current_task_id`) queues every future push for the old
    agent behind a card it no longer owns — silently, forever, since
    nothing else clears that field.

    Sabotage-Probe: removing the `old_agent.current_task_id = None` write
    added in the reassign handler turns this red.
    """
    board, lead, lead_token, worker, worker_token, other, other_token, task = await _setup(
        async_session, task_status="in_progress"
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        w = await s.get(Agent, worker.id)
        w.current_task_id = task.id
        s.add(w)
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
        json={"to": "Rex"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed_worker = await s.get(Agent, worker.id)
        assert refreshed_worker.current_task_id is None, (
            "Alt-Agent behaelt current_task_id auf eine Karte, die ihm nicht mehr gehoert"
        )


@pytest.mark.asyncio
async def test_reassign_leaves_unrelated_current_task_lock_alone(client: AsyncClient, async_session):
    """Control test: reassign must only clear current_task_id when it
    actually points at the reassigned card — not blindly null it out."""
    board, lead, lead_token, worker, worker_token, other, other_token, task = await _setup(
        async_session, task_status="in_progress"
    )
    other_task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        unrelated = Task(
            id=other_task_id, board_id=board.id, assigned_agent_id=worker.id,
            title="Unrelated active card", status="in_progress",
        )
        s.add(unrelated)
        w = await s.get(Agent, worker.id)
        w.current_task_id = other_task_id
        s.add(w)
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
        json={"to": "Rex"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed_worker = await s.get(Agent, worker.id)
        assert refreshed_worker.current_task_id == other_task_id


# ── Warning 3: reassign --to must not be an ilike wildcard pattern ─────────


@pytest.mark.asyncio
async def test_reassign_to_wildcard_does_not_match_arbitrary_agent(client: AsyncClient, async_session):
    """Warning 3 (Rex review 09a860f6): `Agent.name.ilike(payload.to)`
    without escaping treated '%' and '_' as SQL wildcards — `--to "%"`
    matched the first agent row on the board (200) instead of 404, handing
    the card to whichever agent the query happened to return first.

    Sabotage-Probe: reverting the func.lower()-equality fix back to
    `Agent.name.ilike(payload.to)` turns this red (200 instead of 404).
    """
    board, lead, lead_token, worker, worker_token, other, other_token, task = await _setup(async_session)
    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
        json={"to": "%"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_reassign_to_is_still_case_insensitive(client: AsyncClient, async_session):
    """Control test: the exact-match rewrite must not break the documented
    case-insensitive name lookup."""
    board, lead, lead_token, worker, worker_token, other, other_token, task = await _setup(async_session)
    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
        json={"to": "rEx"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["assigned_agent_id"] == str(other.id)


# ── Further claim paths found while searching for a third ──────────────────


@pytest.mark.asyncio
@patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock)
async def test_watchdog_phase_auto_advance_skips_held_next_phase(mock_emit, session, make_board):
    """Third claim path found (DoD: 'weitere Claim-Pfade gesucht'): the
    watchdog's phase auto-advance (`_auto_advance_next_phase`) selected the
    next inbox phase root task and called lock_and_set(..., 'in_progress')
    directly — same pattern as the /tasks/next blocker, same bypass of
    check_dispatch_allowed. A held phase root would be force-started the
    moment its predecessor phase finished.

    Sabotage-Probe: removing the `Task.run_control.is_(None)` filter from
    this candidate query (watchdog/task_monitor.py) turns this red — phase2
    flips to in_progress despite the hold.
    """
    from app.services.watchdog.task_monitor import TaskMonitorMixin
    from tests.test_auto_advance_phase import _create_project, _create_phase

    board = await make_board(name="Held Phase Board", slug=f"held-phase-{uuid.uuid4().hex[:8]}")
    project = await _create_project(session, board.id)

    phase1 = await _create_phase(session, board.id, project.id, "Phase 1", sort_order=1, status="done")
    phase2 = await _create_phase(session, board.id, project.id, "Phase 2", sort_order=2, status="inbox")
    phase2.run_control = "manual_hold"
    phase2.hold_reason = "Deploy-Fenster"
    session.add(phase2)
    await session.commit()

    monitor = TaskMonitorMixin()
    await monitor._auto_advance_next_phase(session, phase1)

    await session.refresh(phase2)
    assert phase2.status == "inbox"
    assert phase2.run_control == "manual_hold"


@pytest.mark.asyncio
async def test_router_phase_auto_advance_skips_held_next_phase(auth_client, async_session):
    """Same third claim path as above, but the duplicate inline
    implementation in routers/tasks.py (triggered synchronously by the PATCH
    that sets a phase's status to 'done', not by the watchdog sweep).

    Sabotage-Probe: removing the `Task.run_control.is_(None)` filter from
    this candidate query (routers/tasks.py, update_task) turns this red.
    """
    from app.models.board import Project

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="Inline Phase Board", slug=f"inline-phase-{uuid.uuid4().hex[:8]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        project = Project(board_id=board.id, name="Inline Project", status="active", project_type="feature")
        s.add(project)
        await s.commit()
        await s.refresh(project)

        phase1 = Task(
            board_id=board.id, project_id=project.id, title="Phase 1",
            sort_order=1, status="review", parent_task_id=None,
        )
        s.add(phase1)
        phase2 = Task(
            board_id=board.id, project_id=project.id, title="Phase 2",
            sort_order=2, status="inbox", parent_task_id=None,
            run_control="manual_hold", hold_reason="Deploy-Fenster",
        )
        s.add(phase2)
        await s.commit()
        await s.refresh(phase1)
        await s.refresh(phase2)

    resp = await auth_client.patch(
        f"/api/v1/boards/{board.id}/tasks/{phase1.id}",
        json={"status": "done"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, phase2.id)
        assert refreshed.status == "inbox"
        assert refreshed.run_control == "manual_hold"


# ── Runde 3 (Rex review B1): the fix above only proved the held phase
# doesn't force-start — with two phases there is nothing behind the hold to
# leapfrog to, so the underlying bug (a FILTER, not a stop-check) stayed
# invisible. These two tests add a third phase to prove the walk actually
# halts at the hold instead of skipping past it. ──────────────────────────


@pytest.mark.asyncio
@patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock)
async def test_watchdog_phase_auto_advance_does_not_leapfrog_held_phase(mock_emit, session, make_board):
    """Three phases: 1=done, 2=held, 3=inbox. Before the fix,
    `Task.run_control.is_(None)` sat as a FILTER on the candidate query — a
    filter removes the held row from the result set, it doesn't stop the
    walk, so the query fell through to phase 3 and started it while phase 2
    stayed held. The fix selects the immediate next phase by sort_order
    first and only THEN checks run_control, so a hold stops the advance
    instead of being skipped over.

    Sabotage-Probe: reverting `if not next_phase or next_phase.run_control
    is not None: return` back to a `Task.run_control.is_(None)` query filter
    (watchdog/task_monitor.py, `_auto_advance_next_phase`) turns this red —
    phase 3 flips to in_progress despite phase 2's hold.
    """
    from app.services.watchdog.task_monitor import TaskMonitorMixin
    from tests.test_auto_advance_phase import _create_project, _create_phase

    board = await make_board(name="Leapfrog Board", slug=f"leapfrog-{uuid.uuid4().hex[:8]}")
    project = await _create_project(session, board.id)

    phase1 = await _create_phase(session, board.id, project.id, "Phase 1", sort_order=1, status="done")
    phase2 = await _create_phase(session, board.id, project.id, "Phase 2", sort_order=2, status="inbox")
    phase2.run_control = "manual_hold"
    phase2.hold_reason = "Deploy-Fenster"
    session.add(phase2)
    phase3 = await _create_phase(session, board.id, project.id, "Phase 3", sort_order=3, status="inbox")
    await session.commit()

    monitor = TaskMonitorMixin()
    await monitor._auto_advance_next_phase(session, phase1)

    await session.refresh(phase2)
    await session.refresh(phase3)
    assert phase2.status == "inbox"
    assert phase2.run_control == "manual_hold"
    assert phase3.status == "inbox", (
        "Leapfrog: Phase 3 wurde gestartet, obwohl Phase 2 gehalten ist"
    )


@pytest.mark.asyncio
async def test_router_phase_auto_advance_does_not_leapfrog_held_phase(auth_client, async_session):
    """Same three-phase leapfrog as above, but the duplicate inline
    implementation in routers/tasks.py (triggered synchronously by the PATCH
    that sets phase 1's status to 'done').

    Sabotage-Probe: reverting the post-selection check back to a
    `Task.run_control.is_(None)` query filter (routers/tasks.py,
    update_task) turns this red — phase 3 flips to in_progress despite
    phase 2's hold.
    """
    from app.models.board import Project

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="Leapfrog Inline Board", slug=f"leapfrog-inline-{uuid.uuid4().hex[:8]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        project = Project(board_id=board.id, name="Leapfrog Inline Project", status="active", project_type="feature")
        s.add(project)
        await s.commit()
        await s.refresh(project)

        phase1 = Task(
            board_id=board.id, project_id=project.id, title="Phase 1",
            sort_order=1, status="review", parent_task_id=None,
        )
        s.add(phase1)
        phase2 = Task(
            board_id=board.id, project_id=project.id, title="Phase 2",
            sort_order=2, status="inbox", parent_task_id=None,
            run_control="manual_hold", hold_reason="Deploy-Fenster",
        )
        s.add(phase2)
        phase3 = Task(
            board_id=board.id, project_id=project.id, title="Phase 3",
            sort_order=3, status="inbox", parent_task_id=None,
        )
        s.add(phase3)
        await s.commit()
        await s.refresh(phase1)
        await s.refresh(phase2)
        await s.refresh(phase3)

    resp = await auth_client.patch(
        f"/api/v1/boards/{board.id}/tasks/{phase1.id}",
        json={"status": "done"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed2 = await s.get(Task, phase2.id)
        refreshed3 = await s.get(Task, phase3.id)
        assert refreshed2.status == "inbox"
        assert refreshed2.run_control == "manual_hold"
        assert refreshed3.status == "inbox", (
            "Leapfrog: Phase 3 wurde gestartet, obwohl Phase 2 gehalten ist"
        )


@pytest.mark.asyncio
async def test_subtask_creation_does_not_ack_a_held_parent(client: AsyncClient, async_session):
    """Fourth path checked while searching for a third: creating a subtask
    under a held parent implicitly ACKed the parent (inbox -> in_progress)
    via the same direct lock_and_set() pattern, letting the Board Lead's own
    subtask creation silently work around its own `mc hold`.

    Sabotage-Probe: removing the `parent.run_control is None` condition
    added to the implicit-ACK guard (agent_task_status.py, agent_create_task)
    turns this red — the held parent flips to in_progress.
    """
    from unittest.mock import AsyncMock, patch as _patch

    board, lead, lead_token, worker, worker_token, _o, _ot, _task = await _setup(async_session)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # _setup()'s lead has no tasks:create scope (not needed by the
        # hold/release/reassign verbs it was built for) — grant it here.
        l = await s.get(Agent, lead.id)
        l.scopes = ["tasks:read", "tasks:write", "tasks:manage", "tasks:create"]
        s.add(l)

        parent = Task(
            board_id=board.id, assigned_agent_id=lead.id,
            title="Held parent", status="inbox",
        )
        s.add(parent)
        await s.commit()
        await s.refresh(parent)

    hold = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{parent.id}/hold",
        json={"reason": "Lead haelt die eigene Root-Karte"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert hold.status_code == 200, hold.text

    with _patch("app.services.activity.broadcast", new_callable=AsyncMock), \
         _patch("app.routers.agent_scoped.rpc", AsyncMock(connected=True), create=True):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={
                "title": "Subtask under held parent",
                "description": (
                    "Ziel: Testabdeckung fuer den Hold-Bypass pruefen. "
                    "Kontext: Parent-Task ist gehalten (mc hold). "
                    "Guardrails: keine echten Seiteneffekte. "
                    "Erwarteter Output: Subtask wird angelegt. "
                    "Definition of Done: Response 201."
                ),
                "parent_task_id": str(parent.id),
                "assigned_agent_id": str(worker.id),
            },
        )
    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed_parent = await s.get(Task, parent.id)
        assert refreshed_parent.status == "inbox", (
            "Held parent wurde ueber die Subtask-Erstellung implizit ge-ACKt"
        )
        assert refreshed_parent.run_control == "manual_hold"


# ── Sechster Claim-Pfad (Runde 3, Rex review B2): impliziter ACK ueber den
# Kommentar-Kanal umgeht den Hold ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_comment_does_not_implicitly_ack_a_held_dispatched_card(client: AsyncClient, async_session):
    """Sixth claim path (Rex review, Runde 3 B2): a card pushed to a worker
    but not yet ACKed sits at status="inbox" with dispatched_at already
    set — the exact same shape `mc hold` accepts, since hold only checks
    `status == "inbox"`, never `dispatched_at`. If the Board Lead holds such
    a card and the worker then posts its first comment, `apply_ack_handshake`
    used to see `assigned_agent_id == agent.id and ack_at is None and
    dispatched_at is not None` — all true — and silently ACKed it: ack_at
    set, status inbox -> in_progress, active-task lock taken. The hold that
    was JUST applied gets undone through a channel that carries no lifecycle
    intent at all.

    Rex's Sonde: hold -> 200, Kommentar -> 201, danach status='in_progress'.

    Sabotage-Probe: removing `and task.run_control is None` from the guard
    in `apply_ack_handshake` (task_lifecycle.py) turns this red — the
    comment flips the held card to in_progress and sets ack_at.
    """
    board, lead, lead_token, worker, worker_token, _o, _ot, task = await _setup(async_session)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        db_task = await s.get(Task, task.id)
        db_task.dispatched_at = dt.datetime.now(tz=dt.timezone.utc)
        s.add(db_task)
        await s.commit()

    hold = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/hold",
        json={"reason": "Deploy-Fenster — Karte soll trotz laufendem Dispatch warten"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert hold.status_code == 200, hold.text

    comment = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
        json={"content": "Fange an.", "comment_type": "progress"},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert comment.status_code == 201, comment.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        refreshed_worker = await s.get(Agent, worker.id)
        assert refreshed.status == "inbox", (
            "Kommentar hat die gehaltene Karte implizit ge-ACKt (inbox -> in_progress)"
        )
        assert refreshed.run_control == "manual_hold"
        assert refreshed.ack_at is None
        assert refreshed_worker.current_task_id is None, (
            "Kommentar hat trotz Hold das Aktiv-Task-Lock genommen"
        )
