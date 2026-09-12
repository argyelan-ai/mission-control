"""`mc ask` without an active task (W5-C) — a Board Lead between cards has
no `agent.current_task_id`, and `POST /tasks/current/ask` hard-refused that
with a 409 for everyone alike (see test_mc_ask.py::test_no_current_task_
returns_409). That left a Lead as mute as a worker with nothing to ask
about — exactly when an orchestrator most needs to escalate.

Scoped to Scope.TASKS_MANAGE (Leads/orchestrators): such an agent without a
current task now gets its DM thread (kind="dm", agent_id=self) — the Mark
<-> agent conversation (see app/models/thread.py's uq_threads_dm_per_agent:
"Ein Agent hat hoechstens EINEN DM-Thread mit dem Operator") — created on
demand if needed. Posting there IS reaching Mark, the same way PR #496 used
thread participation as proof of delivery instead of a fabricated assertion.
A plain worker without the scope keeps the original 409 (regression-pinned
in test_mc_ask.py, untouched by this change).
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.thread import Message, Thread
from tests.conftest import test_engine


async def _lead(*, scopes: list[str] | None = None, comm_v2: bool = True):
    raw, token_hash = generate_agent_token()
    board_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="B", slug=f"b-{uuid.uuid4().hex[:6]}"))
        agent = Agent(
            id=uuid.uuid4(), name="Lead", slug=f"lead-{uuid.uuid4().hex[:6]}",
            role="lead", board_id=board_id, agent_token_hash=token_hash,
            is_board_lead=True,
            scopes=scopes
            if scopes is not None
            else ["tasks:read", "tasks:write", "tasks:manage", "chat:write"],
            current_task_id=None,
            comm_v2=comm_v2,
            provision_status="provisioned",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
    return agent, raw


@pytest.mark.asyncio
async def test_lead_without_active_task_ask_lands_on_dm_thread_with_mark(
    client: AsyncClient,
):
    """The question must be provably reachable by Mark: it lands on the
    Lead's DM thread (kind='dm') — the one thread the model defines as the
    Mark<->agent conversation — with awaiting=True, not lost or silently
    dropped."""
    agent, token = await _lead()

    resp = await client.post(
        "/api/v1/agent/tasks/current/ask",
        json={"question": "Karte X haengt seit 2h in review — eskalieren?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["your_status"] == "no_task"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        dm = (
            await s.exec(
                select(Thread).where(Thread.kind == "dm", Thread.agent_id == agent.id)
            )
        ).one()
        assert str(dm.id) == data["thread_id"]

        message = (
            await s.exec(select(Message).where(Message.id == uuid.UUID(data["message_id"])))
        ).one()
        assert message.thread_id == dm.id
        assert message.message_type == "question"
        assert message.body == "Karte X haengt seit 2h in review — eskalieren?"
        assert message.question_meta["awaiting"] is True


@pytest.mark.asyncio
async def test_worker_without_tasks_manage_still_gets_409(client: AsyncClient):
    """Regression guard: an agent without TASKS_MANAGE keeps the original
    refusal — this fix must not open the DM fallback for every agent."""
    agent, token = await _lead(scopes=["tasks:read", "tasks:write", "chat:write"])

    resp = await client.post(
        "/api/v1/agent/tasks/current/ask",
        json={"question": "Darf ich das ohne Task?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_blocking_ask_without_active_task_is_rejected(client: AsyncClient):
    """Blocking parks a TASK in `waiting` — with no task there is nothing to
    pause, so this stays a clear 409 rather than silently ignoring
    `--blocking`."""
    agent, token = await _lead()

    resp = await client.post(
        "/api/v1/agent/tasks/current/ask",
        json={"question": "Deploy jetzt?", "blocking": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409, resp.text
