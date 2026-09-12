"""Ownership on the review verbs: you don't get to decide your own card.

Two incidents, one missing check.

  E-review — a reviewer pointed `mc reject` at its OWN round-1 review card
  (the only card sitting in `review`) and it went through: the card left
  `review` for `inbox` carrying review_decision=changes_requested, and the
  correction re-dispatch that followed was pure noise. execute_review_decision
  ran its ownership guard for `approve` only; request_changes and hold had
  none at all.

  D — a reviewer could not file its request_changes on PR #500 because both
  author cards were already `done`, and the review verbs need a card in
  `review`. The verdict survived as a feedback comment: correct in substance,
  with no recorded decision. record_late_review_note closes that.

The counter-check matters as much as the guards: in the everyday flow
handle_review_handoff assigns the AUTHOR's card to the reviewer, so a guard
keyed on assigned_agent_id would reject exactly the case that must keep
working. Ownership is derived from who did the implementation work
(get_review_worker_agent_ids), not from who the card is assigned to.
"""
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _make_agent_with_token(
    *, name: str, board_id, is_board_lead: bool = False, role: str = "developer",
):
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=uuid.uuid4(),
        name=name,
        role=role,
        board_id=board_id,
        agent_token_hash=token_hash,
        is_board_lead=is_board_lead,
        scopes=["tasks:read", "tasks:write"],
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
    return agent, raw_token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _event(task_id, agent_id, *, frm: str, to: str, at: datetime):
    from app.models.task import TaskEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskEvent(
            id=uuid.uuid4(), task_id=task_id,
            from_status=frm, to_status=to,
            changed_by="agent", agent_id=agent_id, created_at=at,
        ))
        await s.commit()


def _quiet():
    return (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    )


async def _refresh(task_id):
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(Task, task_id)


# ── E-review: reject/hold on your own card ────────────────────────────────

@pytest.mark.asyncio
async def test_reject_on_own_card_is_blocked(client, fake_redis, make_board, make_task):
    """The exact E-review shape: the agent ACKed the card itself (inbox →
    in_progress) and later put it in review, then aims `mc reject` at it."""
    board = await make_board(slug="mc-own-reject")
    agent, token = await _make_agent_with_token(
        name="Solo", board_id=board.id, role="reviewer",
    )
    task = await make_task(
        board_id=board.id, status="review", assigned_agent_id=agent.id,
    )
    t0 = datetime.utcnow()
    await _event(task.id, agent.id, frm="inbox", to="in_progress", at=t0)
    await _event(task.id, agent.id, frm="in_progress", to="review", at=t0 + timedelta(minutes=5))

    b, m = _quiet()
    with b, m:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/review",
            json={"decision": "request_changes", "comment": "Blocker in der Migration."},
            headers=_headers(token),
        )

    assert resp.status_code == 409, resp.text[:400]
    detail = resp.json().get("detail", "")
    assert "eigene karte" in detail.lower()
    assert "review-note" in detail.lower(), "Der 409 muss den Ausweg fuer Fall D nennen"

    refreshed = await _refresh(task.id)
    assert refreshed.status == "review", "Die Karte darf nicht nach inbox/in_progress rutschen"
    assert refreshed.review_decision is None, "changes_requested darf nicht gesetzt werden"


@pytest.mark.asyncio
async def test_hold_on_own_card_is_blocked(client, fake_redis, make_board, make_task):
    board = await make_board(slug="mc-own-hold")
    agent, token = await _make_agent_with_token(name="Solo", board_id=board.id)
    task = await make_task(
        board_id=board.id, status="review", assigned_agent_id=agent.id,
    )
    t0 = datetime.utcnow()
    await _event(task.id, agent.id, frm="inbox", to="in_progress", at=t0)

    b, m = _quiet()
    with b, m:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/review",
            json={"decision": "hold", "comment": "Warte auf Entscheidung."},
            headers=_headers(token),
        )
    assert resp.status_code == 409, resp.text[:400]
    assert (await _refresh(task.id)).review_decision is None


# ── Gegenprobe: the everyday case must be untouched ───────────────────────

@pytest.mark.asyncio
async def test_reviewer_can_still_reject_a_foreign_card(
    client, fake_redis, make_board, make_task,
):
    """The case the guardrail protects: the author did the work, the reviewer
    entered via the review handoff. Note the card is ASSIGNED TO THE REVIEWER
    (handle_review_handoff does exactly that) — a guard keyed on
    assigned_agent_id would wrongly fire here."""
    board = await make_board(slug="mc-foreign-reject")
    author, _ = await _make_agent_with_token(name="Author", board_id=board.id)
    reviewer, reviewer_token = await _make_agent_with_token(
        name="Checker", board_id=board.id, role="reviewer",
    )
    task = await make_task(
        board_id=board.id, status="review", assigned_agent_id=reviewer.id,
    )
    t0 = datetime.utcnow()
    await _event(task.id, author.id, frm="inbox", to="in_progress", at=t0)
    await _event(task.id, author.id, frm="in_progress", to="review", at=t0 + timedelta(minutes=5))
    # Reviewer ACK on the handed-over card — review work, not implementation.
    await _event(task.id, reviewer.id, frm="review", to="in_progress", at=t0 + timedelta(minutes=6))

    b, m = _quiet()
    with b, m:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/review",
            json={"decision": "request_changes", "comment": "Tests fehlen, bitte nachziehen."},
            headers=_headers(reviewer_token),
        )
    assert resp.status_code == 200, f"Normalfall muss unveraendert laufen: {resp.text[:400]}"
    assert (await _refresh(task.id)).review_decision == "changes_requested"


@pytest.mark.asyncio
async def test_board_lead_may_still_reject_a_card_it_worked_on(
    client, fake_redis, make_board, make_task,
):
    """Parity with the approve branch: the board lead is exempt."""
    board = await make_board(slug="mc-lead-reject")
    lead, lead_token = await _make_agent_with_token(
        name="Lead", board_id=board.id, is_board_lead=True,
    )
    task = await make_task(
        board_id=board.id, status="review", assigned_agent_id=lead.id,
    )
    await _event(task.id, lead.id, frm="inbox", to="in_progress", at=datetime.utcnow())

    b, m = _quiet()
    with b, m:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/review",
            json={"decision": "request_changes", "comment": "Noch nicht rund."},
            headers=_headers(lead_token),
        )
    assert resp.status_code == 200, resp.text[:400]


# ── D: the late review note ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_late_review_note_records_verdict_without_moving_the_card(
    client, fake_redis, make_board, make_task,
):
    """PR #500 shape: the author's card is already `done`, so POST /review is
    closed. The note records the verdict and leaves the status alone."""
    board = await make_board(slug="mc-late-note")
    author, _ = await _make_agent_with_token(name="Author", board_id=board.id)
    reviewer, reviewer_token = await _make_agent_with_token(
        name="Checker", board_id=board.id, role="reviewer",
    )
    task = await make_task(
        board_id=board.id, status="done", assigned_agent_id=author.id,
    )
    await _event(task.id, author.id, frm="inbox", to="in_progress", at=datetime.utcnow())

    b, m = _quiet()
    with b, m:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/review-note",
            json={"decision": "request_changes", "comment": "Rollback-Pfad fehlt."},
            headers=_headers(reviewer_token),
        )
    assert resp.status_code == 200, resp.text[:400]
    body = resp.json()
    assert body["decision"] == "changes_requested"
    assert body["status_changed"] is False

    refreshed = await _refresh(task.id)
    assert refreshed.status == "done", "Eine Notiz darf die Karte nicht wieder aufreissen"
    assert refreshed.review_decision == "changes_requested"
    assert refreshed.review_decided_at is not None

    # The verdict is filed as a review comment, not as loose prose.
    from app.models.task import TaskComment
    from sqlmodel import select
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
    assert any(
        c.comment_type == "review" and "Rollback-Pfad fehlt." in c.content
        for c in comments
    ), [(c.comment_type, c.content[:80]) for c in comments]


@pytest.mark.asyncio
async def test_late_review_note_refuses_a_card_still_in_review(
    client, fake_redis, make_board, make_task,
):
    """An open review has a real decision available — the note must not become
    a way to sidestep it."""
    board = await make_board(slug="mc-note-open")
    author, _ = await _make_agent_with_token(name="Author", board_id=board.id)
    reviewer, reviewer_token = await _make_agent_with_token(
        name="Checker", board_id=board.id, role="reviewer",
    )
    task = await make_task(
        board_id=board.id, status="review", assigned_agent_id=reviewer.id,
    )
    await _event(task.id, author.id, frm="inbox", to="in_progress", at=datetime.utcnow())

    b, m = _quiet()
    with b, m:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/review-note",
            json={"decision": "approve", "comment": "Passt."},
            headers=_headers(reviewer_token),
        )
    assert resp.status_code == 409, resp.text[:400]
    assert (await _refresh(task.id)).review_decision is None


@pytest.mark.asyncio
async def test_late_review_note_on_own_card_is_blocked(
    client, fake_redis, make_board, make_task,
):
    """Filing a late verdict on your own work is self-review with extra steps."""
    board = await make_board(slug="mc-note-own")
    agent, token = await _make_agent_with_token(name="Solo", board_id=board.id)
    task = await make_task(
        board_id=board.id, status="done", assigned_agent_id=agent.id,
    )
    await _event(task.id, agent.id, frm="inbox", to="in_progress", at=datetime.utcnow())

    b, m = _quiet()
    with b, m:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/review-note",
            json={"decision": "approve", "comment": "Von mir aus passt das."},
            headers=_headers(token),
        )
    assert resp.status_code == 409, resp.text[:400]
    assert (await _refresh(task.id)).review_decision is None
