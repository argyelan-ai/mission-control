"""`mc comment --task-id` (W5-C) — a Board Lead without an active task is
otherwise mute: `POST /boards/{board_id}/tasks/{task_id}/comments` needs a
board_id the CLI no longer has once poll.sh clears TASK_ID/BOARD_ID from the
env (docker/shared/poll.sh cancel/stop handlers). This pins the new
board-agnostic route `POST /api/v1/agent/tasks/{task_id}/comments`, gated on
Scope.TASKS_MANAGE so a plain worker without the scope gets a clear 403
instead of silently writing somewhere else.
"""
from __future__ import annotations

import datetime as _dt
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment
from tests.conftest import test_engine


async def _fixture(*, lead_scopes: list[str] | None = None):
    board_id, lead_id, worker_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    task_id = uuid.uuid4()
    worker_raw, worker_hash = generate_agent_token()
    lead_raw, lead_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="B", slug=f"b-{uuid.uuid4().hex[:6]}"))
        s.add(
            Agent(
                id=lead_id, name="Lead", slug=f"lead-{uuid.uuid4().hex[:6]}",
                role="lead", board_id=board_id, agent_token_hash=lead_hash,
                is_board_lead=True,
                scopes=lead_scopes
                if lead_scopes is not None
                else ["tasks:read", "tasks:write", "tasks:manage", "chat:write"],
                current_task_id=None,
                provision_status="provisioned",
            )
        )
        s.add(
            Agent(
                id=worker_id, name="Worker", slug=f"w-{uuid.uuid4().hex[:6]}",
                role="developer", board_id=board_id,
                agent_token_hash=worker_hash,
                agent_runtime="cli-bridge",
                current_task_id=task_id,
                scopes=["tasks:read", "tasks:write", "heartbeat"],
                provision_status="provisioned",
            )
        )
        _now = _dt.datetime.now(tz=_dt.timezone.utc)
        s.add(
            Task(
                id=task_id, board_id=board_id, title="Fremde Karte",
                status="in_progress", assigned_agent_id=worker_id,
                dispatched_at=_now, ack_at=_now,
            )
        )
        await s.commit()
    return board_id, lead_id, worker_id, task_id, lead_raw, worker_raw


@pytest.mark.asyncio
async def test_lead_without_active_task_can_comment_via_task_id(client: AsyncClient):
    """(1) Board Lead ohne aktiven Task setzt POST /tasks/{task_id}/comments
    (kein board_id in der URL noetig) — Kommentar landet auf der richtigen
    Karte."""
    board_id, lead_id, worker_id, task_id, lead_token, worker_token = await _fixture()

    resp = await client.post(
        f"/api/v1/agent/tasks/{task_id}/comments",
        json={"comment_type": "handoff", "content": "Bitte weitermachen — Kontext X"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (
            await s.exec(select(TaskComment).where(TaskComment.task_id == task_id))
        ).all()
        assert len(comments) == 1
        assert comments[0].comment_type == "handoff"
        assert comments[0].author_agent_id == lead_id


@pytest.mark.asyncio
async def test_handoff_via_task_id_wakes_the_assigned_worker(client: AsyncClient):
    """(2) A `handoff` posted through the task-id-only route must reach the
    assigned worker's poll — same delivery guarantee as the board-scoped
    comment endpoint (see test_comment_delivery_via_poll.py)."""
    board_id, lead_id, worker_id, task_id, lead_token, worker_token = await _fixture()

    resp = await client.post(
        f"/api/v1/agent/tasks/{task_id}/comments",
        json={"comment_type": "handoff", "content": "Briefing fuer dich"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 201, resp.text

    poll = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert poll.status_code == 200, poll.text
    data = poll.json()
    contents = [c["content"] for c in (data.get("new_comments") or [])]
    assert "Briefing fuer dich" in contents, (
        f"handoff via /tasks/{{task_id}}/comments did not reach the worker's "
        f"poll: {data}"
    )


@pytest.mark.asyncio
async def test_agent_without_tasks_manage_gets_403_not_silent_fallback(
    client: AsyncClient,
):
    """(3) An agent lacking tasks:manage must be refused clearly — never
    silently redirected to its own active task."""
    board_id, lead_id, worker_id, task_id, _, _ = await _fixture(
        lead_scopes=["tasks:read", "tasks:write", "chat:write"],  # no tasks:manage
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, lead_id)
        # own an unrelated task to prove a rejected --task-id write never
        # silently lands there instead.
        own_task_id = uuid.uuid4()
        s.add(
            Task(
                id=own_task_id, board_id=lead.board_id, title="Eigener Task",
                status="in_progress", assigned_agent_id=lead_id,
            )
        )
        lead.current_task_id = own_task_id
        s.add(lead)
        await s.commit()
        raw = None
        # re-derive a usable raw token for this scoped-down agent
        from app.auth import generate_agent_token as _gen
        raw, token_hash = _gen()
        lead.agent_token_hash = token_hash
        s.add(lead)
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/tasks/{task_id}/comments",
        json={"comment_type": "handoff", "content": "sollte scheitern"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 403, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        foreign_comments = (
            await s.exec(select(TaskComment).where(TaskComment.task_id == task_id))
        ).all()
        assert foreign_comments == [], "refused write must not land on the foreign card"

        own_comments = (
            await s.exec(select(TaskComment).where(TaskComment.task_id == own_task_id))
        ).all()
        assert own_comments == [], (
            "refused write must not silently fall back to the agent's own active task"
        )


@pytest.mark.asyncio
async def test_task_id_route_refuses_cross_board_task(client: AsyncClient):
    """A Lead may only touch cards on its OWN board."""
    board_id, lead_id, worker_id, task_id, lead_token, worker_token = await _fixture()

    other_board_id = uuid.uuid4()
    other_task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=other_board_id, name="Other", slug=f"other-{uuid.uuid4().hex[:6]}"))
        s.add(
            Task(
                id=other_task_id, board_id=other_board_id, title="Fremdes Board",
                status="in_progress",
            )
        )
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/tasks/{other_task_id}/comments",
        json={"comment_type": "message", "content": "sollte scheitern"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 403, resp.text
