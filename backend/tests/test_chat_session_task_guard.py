"""A chat session never works a task of its own agent (round-1 user test t1,
MUST-1, 22.10.2026).

The live case: the operator asked "how far are you?" in an omp agent's chat while
task 15cc7c63 ran in its own session. The chat session did the task again —
a progress comment at 22:22:22, AFTER the review handoff at 22:21:46, and a
second ``mc finish``. Contract:

* a call with ``X-MC-Session-Role: chat`` that writes the run state of a task
  of its own agent (comment, status, deliverable, checklist, recover) is
  refused with 409 and changes nothing;
* "its own" includes a task handed to human review (assignee cleared) that
  this agent worked;
* the run session (no role header) keeps working exactly as before;
* tasks of other agents are not touched by this guard.

All ids are made up.
"""
from __future__ import annotations

import uuid

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

CHAT = {"X-MC-Session-Role": "chat"}
ATTEMPT = "11111111-2222-3333-4444-555555555555"


async def _setup(*, status: str = "in_progress", assigned: bool = True, worked: bool = True):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task, TaskEvent

    board_id, agent_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Chat Guard", slug=f"cg-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=agent_id, name="Probe", role="developer", board_id=board_id,
            agent_token_hash=token_hash, scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned", agent_runtime="cli-bridge", harness="omp",
            current_task_id=task_id if assigned else None,
        ))
        await s.commit()
        s.add(Task(
            id=task_id, board_id=board_id, title="[Probe] Mini page", status=status,
            assigned_agent_id=agent_id if assigned else None,
            dispatch_attempt_id=ATTEMPT if status == "in_progress" else None,
        ))
        await s.commit()
        if worked:
            s.add(TaskEvent(task_id=task_id, from_status="inbox", to_status="in_progress",
                            changed_by="agent", agent_id=agent_id))
            await s.commit()
    return board_id, agent_id, task_id, token


def _auth(token: str, *, chat: bool, attempt: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if attempt:
        headers["X-Dispatch-Attempt-Id"] = ATTEMPT
    if chat:
        headers.update(CHAT)
    return headers


async def _comments(task_id) -> list:
    from app.models.task import TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return list((await s.exec(select(TaskComment).where(TaskComment.task_id == task_id))).all())


async def _task(task_id):
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(Task, task_id)


async def test_chat_progress_comment_on_running_task_is_refused(client):
    board_id, _agent, task_id, token = await _setup()
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": "progress", "content": "Schritt 1-3 erledigt"},
        headers=_auth(token, chat=True),
    )
    assert resp.status_code == 409, resp.text
    assert "chat_session_task_write" in resp.json()["detail"]
    assert f"/deliverables/{task_id}/" in resp.json()["detail"]
    assert await _comments(task_id) == []


async def test_run_session_progress_comment_still_accepted(client):
    board_id, _agent, task_id, token = await _setup()
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": "progress", "content": "Update: created index.html"},
        headers=_auth(token, chat=False),
    )
    assert resp.status_code == 201, resp.text
    assert len(await _comments(task_id)) == 1


async def test_chat_finish_on_running_task_is_refused(client):
    board_id, _agent, task_id, token = await _setup()
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        json={"status": "review"},
        headers=_auth(token, chat=True),
    )
    assert resp.status_code == 409, resp.text
    assert "chat_session_task_write" in resp.json()["detail"]
    assert (await _task(task_id)).status == "in_progress"


async def test_chat_comment_after_review_handoff_is_refused(client):
    """The exact 22:22:22 comment: review, assignee cleared by the human
    review handoff, the chat agent worked the task."""
    board_id, _agent, task_id, token = await _setup(status="review", assigned=False)
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": "progress", "content": "Schritt 1-3 erledigt"},
        headers=_auth(token, chat=True, attempt=False),
    )
    assert resp.status_code == 409, resp.text
    assert await _comments(task_id) == []


async def test_chat_deliverable_on_running_task_is_refused(client):
    from app.models.deliverable import TaskDeliverable

    _board, _agent, task_id, token = await _setup()
    resp = await client.post(
        "/api/v1/agent/me/deliverable",
        json={"title": "index.html", "deliverable_type": "file",
              "path": f"/deliverables/{task_id}/index.html", "task_id": str(task_id)},
        headers=_auth(token, chat=True),
    )
    assert resp.status_code == 409, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(select(TaskDeliverable).where(TaskDeliverable.task_id == task_id))).all()
    assert list(rows) == []


async def test_chat_checklist_write_on_running_task_is_refused(client):
    board_id, _agent, task_id, token = await _setup()
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/checklist",
        json={"items": [{"title": "Verify file"}]},
        headers=_auth(token, chat=True),
    )
    assert resp.status_code == 409, resp.text


async def test_chat_recover_does_not_rotate_the_runs_attempt(client):
    """`mc recover` from the chat would hand the chat a fresh attempt id and
    lock the real run out (409 stale) — refused, attempt unchanged."""
    _board, _agent, task_id, token = await _setup()
    resp = await client.get(
        "/api/v1/agent/me/active-task-recovery", headers=_auth(token, chat=True),
    )
    assert resp.status_code == 409, resp.text
    assert (await _task(task_id)).dispatch_attempt_id == ATTEMPT


async def test_chat_reading_the_task_stays_allowed(client):
    """Information is the chat's job: reading is never refused."""
    board_id, _agent, task_id, token = await _setup()
    resp = await client.get(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        headers=_auth(token, chat=True),
    )
    assert resp.status_code == 200, resp.text


async def test_guard_ignores_tasks_of_other_agents(client):
    """A task that is not this agent's run: the guard says nothing (other
    rules may still apply, but never this refusal)."""
    board_id, _agent, task_id, token = await _setup(assigned=False, worked=False)
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": "message", "content": "note"},
        headers=_auth(token, chat=True, attempt=False),
    )
    assert "chat_session_task_write" not in resp.text


async def test_a_refusal_lands_in_the_task_timeline(client):
    """The operator sees that the chat tried (task timeline, kind "stuck")."""
    from app.models.activity import ActivityEvent
    from app.routers.tasks import _ACTIVITY_KIND_MAP

    board_id, agent_id, task_id, token = await _setup()
    await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": "progress", "content": "x"},
        headers=_auth(token, chat=True),
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        events = (await s.exec(select(ActivityEvent).where(
            ActivityEvent.task_id == task_id,
            ActivityEvent.event_type == "task.chat_write_rejected",
        ))).all()
    assert len(events) == 1 and events[0].agent_id == agent_id
    assert events[0].detail == {"action": "comment progress", "status": "in_progress"}
    assert _ACTIVITY_KIND_MAP["task.chat_write_rejected"] == "stuck"
