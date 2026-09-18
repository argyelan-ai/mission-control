"""`is_board_lead` in EVERY /agent/me/poll response (incident 2026-09-18).

The Board Lead's poll.sh cleared its own context on every self-created card:
a Lead opens cards while orchestrating, each one looks like a "new task" to
the bridge, and the `/clear` fired into the running orchestration turn. The
client-side gate needs the role, and the poll body is the only authoritative
source — agent-name matching is explicitly not a contract.

So the contract under test is: whatever branch answers the poll (idle,
working, cancelled, new_task), the body carries the agent's role. A missing
key would make poll.sh fall back to "not a lead" and the incident returns.
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


async def _make_agent(*, is_board_lead: bool):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board

    async with _session() as s:
        board = Board(name="Lead Poll Board", slug=f"lead-poll-{uuid.uuid4().hex[:8]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        raw_token, token_hash = generate_agent_token()
        agent = Agent(
            name=f"PollAgent-{uuid.uuid4().hex[:6]}",
            agent_runtime="cli-bridge",
            agent_token_hash=token_hash,
            board_id=board.id,
            is_board_lead=is_board_lead,
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return agent.id, board.id, raw_token


async def _make_task(board_id, agent_id, *, status: str, ack_at=None):
    from app.models.task import Task

    async with _session() as s:
        task = Task(
            board_id=board_id,
            assigned_agent_id=agent_id,
            title="Lead poll probe",
            status=status,
            dispatched_at=dt.datetime.now(tz=dt.timezone.utc),
            ack_at=ack_at,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        return task.id


async def _poll(client: AsyncClient, token: str) -> dict:
    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_idle_poll_reports_lead_role(client: AsyncClient):
    _, _, token = await _make_agent(is_board_lead=True)

    body = await _poll(client, token)

    assert body["state"] == "idle", body
    assert body["is_board_lead"] is True, body


@pytest.mark.asyncio
async def test_idle_poll_reports_worker_role_as_false(client: AsyncClient):
    _, _, token = await _make_agent(is_board_lead=False)

    body = await _poll(client, token)

    assert body["state"] == "idle", body
    assert body["is_board_lead"] is False, body


@pytest.mark.asyncio
async def test_working_poll_reports_lead_role(client: AsyncClient):
    agent_id, board_id, token = await _make_agent(is_board_lead=True)
    await _make_task(
        board_id, agent_id, status="in_progress",
        ack_at=dt.datetime.now(tz=dt.timezone.utc),
    )

    body = await _poll(client, token)

    assert body["state"] == "working", body
    assert body["is_board_lead"] is True, body


@pytest.mark.asyncio
async def test_cancelled_poll_reports_lead_role(client: AsyncClient):
    agent_id, board_id, token = await _make_agent(is_board_lead=True)
    await _make_task(board_id, agent_id, status="failed")

    body = await _poll(client, token)

    assert body["state"] == "cancelled", body
    assert body["is_board_lead"] is True, body


@pytest.mark.asyncio
async def test_new_task_poll_reports_lead_role(client: AsyncClient):
    agent_id, board_id, token = await _make_agent(is_board_lead=True)
    await _make_task(board_id, agent_id, status="inbox")

    with patch("app.services.dispatch.build_agent_task_prompt", return_value="P"):
        body = await _poll(client, token)

    assert body["state"] == "new_task", body
    assert body["is_board_lead"] is True, body
