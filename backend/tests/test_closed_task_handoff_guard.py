"""A follow-up order must become a task, not a comment on a closed one.

Live incident 2026-08-06 (Boss's own diagnosis): Boss posted a `handoff`
comment on a DONE Downloader task. The comment was delivered (handoff is a
DELIVERABLE_SYSTEM_TYPE), but a closed task gives it no vessel — no ACK, no
status, no dispatch record, no watchdog. The worker got a wall of text and
no mandate; nothing happened and nobody noticed.

Two structural fixes, pinned here:
  A. The comment endpoint refuses delivered-type comments on closed tasks
     with a 409 (hard, not a hint — the hint pattern of Bug 9 would not have
     been read).
  B. `mc delegate` without an active task no longer dead-ends for Board
     Leads: it creates a ROOT task (no parent, no callback) — the reason
     Boss hand-rolled API calls in the first place.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _fixture(lead_has_task=True, lead=True):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board_id, lead_id, worker_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="GB", slug=f"gb-{uuid.uuid4().hex[:6]}"))
        lead_raw, lead_hash = generate_agent_token()
        s.add(
            Agent(
                id=lead_id, name="Lead", slug=f"lead-{uuid.uuid4().hex[:6]}",
                role="lead", board_id=board_id, agent_token_hash=lead_hash,
                scopes=["tasks:read", "tasks:write", "tasks:create", "chat:write"],
                is_board_lead=lead,
                current_task_id=task_id if lead_has_task else None,
                provision_status="provisioned",
            )
        )
        s.add(
            Agent(
                id=worker_id, name="Worker", slug=f"w-{uuid.uuid4().hex[:6]}",
                role="developer", board_id=board_id,
                agent_token_hash=generate_agent_token()[1],
                provision_status="provisioned",
            )
        )
        s.add(
            Task(
                id=task_id, board_id=board_id, title="Erledigt",
                status="done", assigned_agent_id=worker_id,
                owner_agent_id=lead_id,
            )
        )
        await s.commit()
    return board_id, lead_id, worker_id, task_id, lead_raw


async def _set_task_status(task_id, status_):
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        task.status = status_
        s.add(task)
        await s.commit()


# ── A. The guard ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("ctype", ["handoff", "blocker", "feedback", "resolution"])
@pytest.mark.asyncio
async def test_delivered_comment_on_closed_task_is_refused(
    client: AsyncClient, ctype
):
    board_id, _, _, task_id, token = await _fixture()
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": ctype, "content": "Folgeauftrag: bitte Film X holen"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409, resp.text
    assert "neuen task" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_handoff_on_open_task_still_works(client: AsyncClient):
    board_id, _, _, task_id, token = await _fixture()
    await _set_task_status(task_id, "in_progress")
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": "handoff", "content": "Briefing: bitte zuerst prüfen"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_feedback_on_review_task_still_works(client: AsyncClient):
    """Rex' review feedback must keep flowing — review is not closed."""
    board_id, _, _, task_id, token = await _fixture()
    await _set_task_status(task_id, "review")
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": "feedback", "content": "Bitte Test ergänzen."},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_plain_message_on_closed_task_stays_allowed(client: AsyncClient):
    """`message` is an audit note, not a delivered wake signal — a closed
    task may keep collecting notes."""
    board_id, _, _, task_id, token = await _fixture()
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": "message", "content": "Nachtrag fürs Protokoll."},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 201), resp.text


# ── B. Board-Lead root delegation ────────────────────────────────────────


def _dispatch_patches():
    return (
        patch(
            "app.services.operations.check_dispatch_allowed",
            new_callable=AsyncMock, return_value=(True, None),
        ),
        patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock),
    )


@pytest.mark.asyncio
async def test_board_lead_without_task_delegates_a_root_task(client: AsyncClient):
    from app.models.task import Task

    board_id, lead_id, worker_id, _, token = await _fixture(lead_has_task=False)
    p1, p2 = _dispatch_patches()
    with p1, p2:
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/delegate",
            json={
                "title": "Folgeauftrag Film X",
                "description": "Bitte Film X in Deutsch besorgen und verifizieren.",
                "assigned_agent_id": str(worker_id),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code in (200, 201), resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        created = (
            await s.exec(select(Task).where(Task.title == "Folgeauftrag Film X"))
        ).one()
        assert created.parent_task_id is None
        # Superseded by #312: this used to assert `callback_agent_id is None`.
        # #284 read "no parent to resume" as "no callback at all" — and that is
        # what left the chat-ordered delegation mute. Resuming a blocked parent
        # and telling the requester are different jobs: the first is genuinely
        # meaningless without a parent, the second is the whole point of a
        # follow-up order placed from a conversation. The lead is still NOT
        # blocked (asserted below) — only reachable.
        assert created.callback_agent_id == lead_id
        assert created.assigned_agent_id == worker_id
        assert created.owner_agent_id == lead_id

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        lead = await s.get(Agent, lead_id)
        assert lead.current_task_id is None, (
            "Root-Delegation darf den Lead nicht blockieren — es gibt keinen "
            "Parent-Task, auf den er warten koennte (#284-Garantie bleibt)"
        )


@pytest.mark.asyncio
async def test_worker_without_task_still_gets_409(client: AsyncClient):
    board_id, _, worker_id, _, _ = await _fixture(lead_has_task=False, lead=False)
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    helper_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        raw, hsh = generate_agent_token()
        s.add(
            Agent(
                id=helper_id, name="Helper", slug=f"h-{uuid.uuid4().hex[:6]}",
                role="developer", board_id=board_id, agent_token_hash=hsh,
                scopes=["tasks:read", "tasks:write", "tasks:create"],
                provision_status="provisioned",
            )
        )
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/delegate",
        json={
            "title": "Sollte scheitern",
            "description": "Worker ohne aktiven Task darf nicht delegieren.",
            "assigned_agent_id": str(worker_id),
        },
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_root_delegation_carries_an_explicit_origin_thread(
    client: AsyncClient,
):
    """The chat-order case (#270): no parent to inherit from, so the explicit
    flag must land on the root task."""
    from app.models.task import Task
    from app.models.thread import Thread

    board_id, lead_id, worker_id, _, token = await _fixture(lead_has_task=False)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        origin = Thread(kind="chat", agent_id=lead_id, title="Chat")
        s.add(origin)
        await s.commit()
        origin_id = origin.id

    p1, p2 = _dispatch_patches()
    with p1, p2:
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/delegate",
            json={
                "title": "Root mit Herkunft",
                "description": "Auftrag aus dem Chat, Herkunft verlinkt.",
                "assigned_agent_id": str(worker_id),
                "origin_thread_id": str(origin_id),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code in (200, 201), resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        created = (
            await s.exec(select(Task).where(Task.title == "Root mit Herkunft"))
        ).one()
        assert created.origin_thread_id == origin_id


# ── C. The SOUL teaches it ───────────────────────────────────────────────


def test_soul_teaches_the_closed_task_rule():
    """Both role branches carry the fourth delegation-table row — a future
    SOUL rewrite must not silently lose it (pattern: soul rule pin tests)."""
    from app.models.agent import Agent
    from app.services.template_renderer import build_agent_context, render_agent_file

    # The delegation tables render in the orchestrator branch and in the
    # board-lead/lead branch — exactly the roles that delegate.
    for role in ("orchestrator", "lead"):
        agent = Agent(id=uuid.uuid4(), name="X", role=role, board_id=uuid.uuid4())
        soul = render_agent_file("SOUL.md.j2", build_agent_context(agent, agents_on_board=[]))
        assert "CLOSED task" in soul, role
        assert "dead order" in soul, role
