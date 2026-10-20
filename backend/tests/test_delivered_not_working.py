"""Delivered is not working (operator finding 19.10.2026).

The omp-bridge (ACP driver) no longer claims a task right after the poll
delivered it: the card stays ``inbox`` with ``dispatched_at`` set until the
model shows its first sign of life (docker/omp-bridge FirstActivityAck). Two
backend paths must accept that "delivered, not yet claimed" shape:

1. Heartbeat control channel: the bridge reports the running turn's
   task_id/attempt_id from the very first second. ``_withdrawn_task_reason``
   used to read EVERY inbox card as "taken away from you" and answered with a
   hard interrupt — it would kill each run before the model's first token.
   A delivered card (dispatched_at set, not acked, SAME attempt) is live work.
   A real requeue still interrupts: every requeue path clears dispatched_at
   and the attempt id, and an ACK-timeout rotation changes the attempt.

2. Agent PATCH ``blocked`` straight from inbox (the model never answered):
   the agent did answer the delivery, so ``ack_at`` is stamped — without it
   the poll treats a blocked card with ``ack_at`` NULL as "prompt never
   delivered" and hands the dead task out again. ``started_at`` stays empty:
   no work ever began.
"""
import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from tests.conftest import test_engine


async def _delivered_card(
    session: AsyncSession,
    *,
    dispatched: bool = True,
    acked: bool = False,
    attempt: str | None = "att-1",
):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    session.add(board)
    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Worker-{uuid.uuid4().hex[:6]}",
        agent_runtime="cli-bridge",
        agent_token_hash=token_hash,
        board_id=board.id,
        scopes=["heartbeat", "tasks:read", "tasks:write"],
    )
    session.add(agent)
    now = dt.datetime.now(tz=dt.timezone.utc)
    task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title="Delivered probe",
        status="inbox",
        dispatched_at=now if dispatched else None,
        ack_at=now if acked else None,
        dispatch_attempt_id=attempt,
    )
    session.add(task)
    await session.commit()
    await session.refresh(agent)
    await session.refresh(task)
    return board, agent, task, raw_token


async def _beat(client: AsyncClient, token: str, task: Task, attempt: str | None):
    payload = {"status": "working", "task_id": str(task.id)}
    if attempt is not None:
        payload["attempt_id"] = attempt
    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── 1. heartbeat control channel ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_delivered_unclaimed_card_is_live_work_not_withdrawn(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, _, task, token = await _delivered_card(s)

    body = await _beat(client, token, task, "att-1")
    assert "control" not in body, body


@pytest.mark.asyncio
async def test_rotated_attempt_still_interrupts(client: AsyncClient):
    """ACK-timeout rotation = MC re-dispatched; the old turn must stop."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, _, task, token = await _delivered_card(s, attempt="att-NEW")

    body = await _beat(client, token, task, "att-1")
    assert body["control"]["interrupt"] == "hard", body
    assert "entzogen" in body["control"]["reason"]


@pytest.mark.asyncio
async def test_requeued_card_still_interrupts(client: AsyncClient):
    """Every requeue clears dispatched_at + the attempt id."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, _, task, token = await _delivered_card(s, dispatched=False, attempt=None)

    body = await _beat(client, token, task, "att-1")
    assert body["control"]["interrupt"] == "hard", body


@pytest.mark.asyncio
async def test_bridge_without_attempt_id_keeps_the_old_strict_answer(client: AsyncClient):
    """No attempt in the payload = nothing to match against: stay strict."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, _, task, token = await _delivered_card(s)

    body = await _beat(client, token, task, None)
    assert body["control"]["interrupt"] == "hard", body


# ── 2. agent PATCH blocked straight from inbox ─────────────────────────────

@pytest.mark.asyncio
async def test_blocked_from_inbox_stamps_ack_but_not_started(client: AsyncClient, fake_redis):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _delivered_card(s)

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={
                "status": "blocked",
                "blocker_type": "technical_problem",
                "blocker_question": "Das Modell hat nicht geantwortet",
            },
            headers={"Authorization": f"Bearer {token}",
                     "X-Dispatch-Attempt-Id": "att-1"},
        )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        assert t.status == "blocked"
        assert t.ack_at is not None, "the agent answered the delivery"
        assert t.started_at is None, "no work ever began"


@pytest.mark.asyncio
async def test_blocked_from_inbox_is_not_handed_out_again(client: AsyncClient, fake_redis):
    """Without the ack stamp the poll read the card as 'prompt never
    delivered' and returned it as new_task — the dead run would start over."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _delivered_card(s)

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={
                "status": "blocked",
                "blocker_type": "technical_problem",
                "blocker_question": "Das Modell hat nicht geantwortet",
            },
            headers={"Authorization": f"Bearer {token}",
                     "X-Dispatch-Attempt-Id": "att-1"},
        )
    assert resp.status_code == 200, resp.text

    with patch("app.services.dispatch.build_agent_task_prompt", return_value="x"):
        poll = await client.get(
            "/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"},
        )
    assert poll.status_code == 200, poll.text
    assert poll.json()["state"] != "new_task", poll.json()
