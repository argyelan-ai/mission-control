"""A stopped card must not starve the agent's queue (live incident 28.10.2026).

A bench time-limit stop (`stop_task_run`) leaves the card `status=blocked,
run_control=stopped` for good: nobody resumes a finished bench run. /me/poll
answered `state=stopped` for that old card on every poll, so a newly
dispatched inbox card for the same agent was never handed out — the bridge
treats `stopped` as "nothing to do" and sat idle until the operator cleared
run_control by hand.

Contract under test: `stopped` / `cancelled` is a one-shot instruction to end
the session working on THAT card. It wins while the agent is still bound to
the card (pointer on it, or a live turn with no other card). Otherwise it must
not shadow deliverable work — and the stopped card itself is never
re-delivered. With nothing else to do the old answer stays (idempotent on the
client side), so the plain stop -> resume flow is unchanged.
"""
from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


def _session() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


async def _make_agent(**fields):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board

    async with _session() as s:
        board = Board(name="Stop Poll Board", slug=f"stop-poll-{uuid.uuid4().hex[:8]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        raw_token, token_hash = generate_agent_token()
        agent = Agent(
            name=f"StopPoll-{uuid.uuid4().hex[:6]}",
            agent_runtime="cli-bridge",
            agent_token_hash=token_hash,
            board_id=board.id,
            **fields,
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return agent.id, board.id, raw_token


async def _make_task(board_id, agent_id, **fields):
    from app.models.task import Task

    async with _session() as s:
        task = Task(
            board_id=board_id,
            assigned_agent_id=agent_id,
            title=fields.pop("title", "Stop poll probe"),
            **fields,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        return task.id


async def _set_agent(agent_id, **fields):
    from app.models.agent import Agent

    async with _session() as s:
        agent = await s.get(Agent, agent_id)
        for k, v in fields.items():
            setattr(agent, k, v)
        s.add(agent)
        await s.commit()


async def _stopped_bench_card(board_id, agent_id, *, age_minutes: float = 30):
    """The card a bench time-limit stop leaves behind (stop_task_run)."""
    stopped_at = _now() - dt.timedelta(minutes=age_minutes)
    return await _make_task(
        board_id, agent_id,
        title="Old bench run",
        status="blocked",
        run_control="stopped",
        blocked_at=stopped_at,
        dispatched_at=None,
        ack_at=None,
        created_at=stopped_at - dt.timedelta(minutes=60),
        updated_at=stopped_at,
    )


async def _fresh_dispatch(board_id, agent_id):
    return await _make_task(
        board_id, agent_id,
        title="New dispatched card",
        status="inbox",
        dispatched_at=_now(),
        dispatch_attempt_id=str(uuid.uuid4()),
    )


async def _poll(client: AsyncClient, token: str) -> dict:
    with patch("app.services.dispatch.build_agent_task_prompt", return_value="P"):
        resp = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _card(body: dict) -> str | None:
    """Card id of a poll answer: `task.id` for new_task, `task_id` otherwise."""
    if body.get("state") == "new_task":
        return (body.get("task") or {}).get("id")
    return body.get("task_id")


# ── the live incident ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_old_stopped_card_does_not_shadow_new_dispatched_card(client: AsyncClient):
    agent_id, board_id, token = await _make_agent(status="idle")
    await _stopped_bench_card(board_id, agent_id)
    new_id = await _fresh_dispatch(board_id, agent_id)

    body = await _poll(client, token)

    assert body["state"] == "new_task", body
    assert _card(body) == str(new_id), body


@pytest.mark.asyncio
async def test_freshly_stopped_card_is_never_redelivered_as_new_task(client: AsyncClient):
    """Inside the blocked grace window the stopped card (ack_at cleared by the
    stop) must not be picked up as an 'undelivered active card' once the stop
    answer falls through — that would undo the stop."""
    agent_id, board_id, token = await _make_agent(status="idle")
    stopped_id = await _stopped_bench_card(board_id, agent_id, age_minutes=1)
    new_id = await _fresh_dispatch(board_id, agent_id)

    body = await _poll(client, token)

    assert body["state"] == "new_task", body
    assert _card(body) == str(new_id), body
    assert _card(body) != str(stopped_id)


# ── the stop still reaches the session that runs the card ──────────────────


@pytest.mark.asyncio
async def test_stop_wins_while_agent_is_in_turn_on_the_card(client: AsyncClient):
    """Stop pressed mid-turn: the heartbeat still reports a live turn (status
    working, fresh beat) and the agent holds no other card — the session must
    be ended first, the queued card waits for the next poll."""
    agent_id, board_id, token = await _make_agent(status="working", last_task_activity_at=_now())
    stopped_id = await _stopped_bench_card(board_id, agent_id, age_minutes=0.1)
    await _fresh_dispatch(board_id, agent_id)

    body = await _poll(client, token)

    assert body["state"] == "stopped", body
    assert _card(body) == str(stopped_id), body


@pytest.mark.asyncio
async def test_stop_wins_while_pointer_names_the_card(client: AsyncClient):
    agent_id, board_id, token = await _make_agent(status="idle")
    stopped_id = await _stopped_bench_card(board_id, agent_id, age_minutes=0.1)
    await _set_agent(agent_id, current_task_id=stopped_id)
    await _fresh_dispatch(board_id, agent_id)

    body = await _poll(client, token)

    assert body["state"] == "stopped", body
    assert _card(body) == str(stopped_id), body


@pytest.mark.asyncio
async def test_stale_working_heartbeat_does_not_hold_the_stop(client: AsyncClient):
    """'working' with a dead heartbeat proves nothing (dispatch Guard 3 rule)."""
    agent_id, board_id, token = await _make_agent(
        status="working", last_task_activity_at=_now() - dt.timedelta(minutes=10),
    )
    await _stopped_bench_card(board_id, agent_id)
    new_id = await _fresh_dispatch(board_id, agent_id)

    body = await _poll(client, token)

    assert body["state"] == "new_task", body
    assert _card(body) == str(new_id), body


@pytest.mark.asyncio
async def test_agent_working_on_another_card_reports_working(client: AsyncClient):
    """The agent already runs card B: an old stopped card A must not answer
    'stopped' (poll.sh would ESC + /clear B's session)."""
    agent_id, board_id, token = await _make_agent(status="working", last_task_activity_at=_now())
    await _stopped_bench_card(board_id, agent_id)
    running_id = await _make_task(
        board_id, agent_id, title="Running card", status="in_progress",
        dispatched_at=_now(), ack_at=_now(),
    )
    await _set_agent(agent_id, current_task_id=running_id)

    body = await _poll(client, token)

    assert body["state"] == "working", body
    assert _card(body) == str(running_id), body


# ── unchanged: nothing else to do -> the stop answer stays ─────────────────


@pytest.mark.asyncio
async def test_stopped_card_alone_still_answers_stopped(client: AsyncClient):
    agent_id, board_id, token = await _make_agent(status="idle")
    stopped_id = await _stopped_bench_card(board_id, agent_id)

    body = await _poll(client, token)

    assert body["state"] == "stopped", body
    assert _card(body) == str(stopped_id), body


@pytest.mark.asyncio
async def test_resume_delivers_the_stopped_card_again(client: AsyncClient):
    """Stop -> Resume: the resumed card is claimable through the normal path."""
    from app.services.operations import resume_task_run

    agent_id, board_id, token = await _make_agent(status="idle")
    stopped_id = await _stopped_bench_card(board_id, agent_id)
    async with _session() as s:
        with patch("app.services.dispatch.auto_dispatch_task", return_value=None):
            await resume_task_run(s, stopped_id, "user")
        await s.commit()

    body = await _poll(client, token)

    assert body["state"] == "new_task", body
    assert _card(body) == str(stopped_id), body


# ── same shape for a failed card that kept its assignment ──────────────────


@pytest.mark.asyncio
async def test_old_failed_card_does_not_shadow_new_dispatched_card(client: AsyncClient):
    """Normally every →failed path unassigns (apply_terminal_unassign), so a
    failed card never lingers. A path that misses it must still not starve
    the agent."""
    agent_id, board_id, token = await _make_agent(status="idle")
    await _make_task(board_id, agent_id, title="Old failed", status="failed")
    new_id = await _fresh_dispatch(board_id, agent_id)

    body = await _poll(client, token)

    assert body["state"] == "new_task", body
    assert _card(body) == str(new_id), body
