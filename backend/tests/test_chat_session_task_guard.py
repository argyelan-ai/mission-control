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

Round-2 review (23.10.2026, r1 S2/S3/NICE-5) — no gaps, not too wide:

* `mc question` / `mc help` / `mc delegate` from the chat would set the run's
  task to `blocked` (or hang a subtask and a progress note on it) and the
  answer would start a second run — refused like `ask --blocking`;
* a plain reply (`message` comment) is the chat's job, also in the task
  thread — allowed, and it never ACKs or claims a dispatched task;
* the guard covers a run that holds the task now or let go of it within
  30 minutes (the incident's comment came 36 s after the handoff) — not a
  task finished weeks ago, not a task the chat just created for itself
  (inbox, not dispatched);
* one timeline row per task and action per 10 minutes, however often the
  model retries.

All ids are made up.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

CHAT = {"X-MC-Session-Role": "chat"}
ATTEMPT = "11111111-2222-3333-4444-555555555555"


async def _setup(
    *, status: str = "in_progress", assigned: bool = True, worked: bool = True,
    dispatched: bool = False, left_run_ago: timedelta | None = None,
):
    """One board, one omp agent, one task.

    ``left_run_ago``: the run let go of the task that long ago (an
    ``in_progress → status`` event, and ``updated_at`` that old) — the guard
    only covers a run that holds the task or let go of it within
    ``chat_run_context.RECENT_WINDOW``."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task, TaskEvent

    board_id, agent_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    token, token_hash = generate_agent_token()
    left_at = datetime.now(timezone.utc) - left_run_ago if left_run_ago is not None else None
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Chat Guard", slug=f"cg-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=agent_id, name="Probe", role="developer", board_id=board_id,
            agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "tasks:help", "tasks:create"],
            provision_status="provisioned", agent_runtime="cli-bridge", harness="omp",
            current_task_id=task_id if assigned else None,
        ))
        await s.commit()
        s.add(Task(
            id=task_id, board_id=board_id, title="[Probe] Mini page", status=status,
            assigned_agent_id=agent_id if assigned else None,
            dispatch_attempt_id=ATTEMPT if status == "in_progress" else None,
            dispatched_at=datetime.now(timezone.utc) if dispatched else None,
        ))
        await s.commit()
        if worked:
            s.add(TaskEvent(task_id=task_id, from_status="inbox", to_status="in_progress",
                            changed_by="agent", agent_id=agent_id))
            await s.commit()
        if left_at is not None:
            s.add(TaskEvent(task_id=task_id, from_status="in_progress", to_status=status,
                            changed_by="agent", agent_id=agent_id, created_at=left_at))
            await s.commit()
            # updated_at has onupdate=utcnow: set it with a plain UPDATE.
            from sqlalchemy import update

            await s.exec(update(Task).where(Task.id == task_id).values(updated_at=left_at))
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
    board_id, _agent, task_id, token = await _setup(
        status="review", assigned=False, left_run_ago=timedelta(seconds=36),
    )
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


async def _enable_comm_v2(agent_id) -> None:
    """Blocking asks need the messaging-v2 pilot and the chat:write scope."""
    from app.models.agent import Agent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = await s.get(Agent, agent_id)
        agent.comm_v2 = True
        agent.scopes = [*agent.scopes, "chat:write"]
        s.add(agent)
        await s.commit()


async def test_chat_blocking_ask_on_running_task_is_refused(client):
    """`mc ask --blocking` from the chat would park the RUN's task in
    `waiting`; the answer then starts a second run next to the first one —
    the same double work through another endpoint. Refused, status kept."""
    _board, agent_id, task_id, token = await _setup()
    await _enable_comm_v2(agent_id)
    resp = await client.post(
        "/api/v1/agent/tasks/current/ask",
        json={"question": "Which colour do you want?", "blocking": True},
        headers=_auth(token, chat=True),
    )
    assert resp.status_code == 409, resp.text
    assert "chat_session_task_write" in resp.json()["detail"]
    assert (await _task(task_id)).status == "in_progress"


async def test_chat_non_blocking_ask_stays_allowed(client):
    """A plain question leaves the task status alone — the chat may ask."""
    _board, agent_id, task_id, token = await _setup()
    await _enable_comm_v2(agent_id)
    resp = await client.post(
        "/api/v1/agent/tasks/current/ask",
        json={"question": "Shall I tell you more?", "blocking": False},
        headers=_auth(token, chat=True),
    )
    assert resp.status_code == 201, resp.text
    assert (await _task(task_id)).status == "in_progress"


async def test_run_session_blocking_ask_still_parks_the_task(client):
    _board, agent_id, task_id, token = await _setup()
    await _enable_comm_v2(agent_id)
    resp = await client.post(
        "/api/v1/agent/tasks/current/ask",
        json={"question": "Which colour do you want?", "blocking": True},
        headers=_auth(token, chat=False),
    )
    assert resp.status_code == 201, resp.text
    assert (await _task(task_id)).status == "waiting"


async def test_refusal_row_in_the_timeline_shows_no_raw_keys(auth_client):
    """The operator reads the title ("…refused: comment progress"), not the
    machine dict ("action: … · status: …"); the dict stays in meta."""
    board_id, _agent, task_id, token = await _setup()
    await auth_client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": "progress", "content": "x"},
        headers=_auth(token, chat=True),
    )
    resp = await auth_client.get(f"/api/v1/boards/{board_id}/tasks/{task_id}/timeline")
    assert resp.status_code == 200, resp.text
    row = next(e for e in resp.json()["entries"]
               if (e.get("meta") or {}).get("event_type") == "task.chat_write_rejected")
    assert row["title"].endswith("comment progress")
    assert row["detail"] is None
    assert row["meta"]["detail"] == {"action": "comment progress", "status": "in_progress"}


# ── Round 2 (23.10.2026): the three open endpoints ─────────────────────────


async def _approvals(task_id) -> list:
    from app.models.approval import Approval

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return list((await s.exec(select(Approval).where(Approval.task_id == task_id))).all())


async def _subtasks(task_id) -> list:
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return list((await s.exec(select(Task).where(Task.parent_task_id == task_id))).all())


async def test_chat_question_does_not_block_the_running_task(client):
    """`mc question` = POST /clarification: blocks the run's task and opens an
    approval; the answer would start a second run (r1 S2)."""
    board_id, _agent, task_id, token = await _setup()
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/clarification",
        json={"question": "Which colour do you want?"},
        headers=_auth(token, chat=True),
    )
    assert resp.status_code == 409, resp.text
    assert "chat_session_task_write" in resp.json()["detail"]
    assert (await _task(task_id)).status == "in_progress"
    assert await _approvals(task_id) == []


async def test_run_session_question_still_blocks_the_task(client):
    board_id, _agent, task_id, token = await _setup()
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/clarification",
        json={"question": "Which colour do you want?"},
        headers=_auth(token, chat=False),
    )
    assert resp.status_code == 201, resp.text
    assert (await _task(task_id)).status == "blocked"


async def test_chat_help_request_does_not_block_the_running_task(client):
    """`mc help` = POST /help-request: subtask + run's task to blocked."""
    board_id, _agent, task_id, token = await _setup()
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/help-request",
        json={"needed_role": "developer", "title": "Check colours", "context": "need a hand"},
        headers=_auth(token, chat=True),
    )
    assert resp.status_code == 409, resp.text
    assert "chat_session_task_write" in resp.json()["detail"]
    assert (await _task(task_id)).status == "in_progress"
    assert await _subtasks(task_id) == []


async def test_chat_delegate_does_not_block_the_running_task(client):
    """`mc delegate` = POST /delegate (callback on by default): subtask under
    the run's task + run's task to blocked."""
    board_id, _agent, task_id, token = await _setup()
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/delegate",
        json={"title": "Check colours", "description": "Check the three colours please",
              "assigned_agent_id": str(uuid.uuid4())},
        headers=_auth(token, chat=True),
    )
    assert resp.status_code == 409, resp.text
    assert "chat_session_task_write" in resp.json()["detail"]
    assert (await _task(task_id)).status == "in_progress"
    assert await _subtasks(task_id) == []


# ── Round 2: not too wide ──────────────────────────────────────────────────


async def test_chat_reply_comment_on_running_task_is_allowed(client):
    """A `message` is an answer, not run state: the chat may reply in the
    task thread (r1 S3)."""
    board_id, _agent, task_id, token = await _setup()
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": "message", "content": "Blue, because the brief says so."},
        headers=_auth(token, chat=True),
    )
    assert resp.status_code == 201, resp.text
    assert [c.comment_type for c in await _comments(task_id)] == ["message"]
    assert (await _task(task_id)).status == "in_progress"


async def test_chat_reply_never_claims_a_dispatched_task(client):
    """The first comment of the assignee ACKs a dispatched task (inbox →
    in_progress, active-task lock). From the chat that would start the run's
    task by the back door: the reply lands, the task stays as it was."""
    board_id, _agent, task_id, token = await _setup(
        status="inbox", worked=False, dispatched=True,
    )
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": "message", "content": "It starts in a moment."},
        headers=_auth(token, chat=True, attempt=False),
    )
    assert resp.status_code == 201, resp.text
    task = await _task(task_id)
    assert task.status == "inbox" and task.ack_at is None


async def test_chat_may_write_a_task_whose_run_ended_long_ago(client):
    """Weeks-old tasks are not "the run" any more (r1 S3: no time limit)."""
    board_id, _agent, task_id, token = await _setup(
        status="review", assigned=False, left_run_ago=timedelta(hours=2),
    )
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": "progress", "content": "Answered the operator's question."},
        headers=_auth(token, chat=True, attempt=False),
    )
    assert "chat_session_task_write" not in resp.text
    assert resp.status_code == 201, resp.text


async def test_chat_may_fill_a_task_it_just_created(client):
    """"Create me a task with a checklist": the chat's own new task is assigned
    to its agent but no run holds it (inbox, not dispatched) — r1 NICE-5."""
    board_id, _agent, task_id, token = await _setup(status="inbox", worked=False)
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/checklist",
        json={"items": [{"title": "Collect the colours"}]},
        headers=_auth(token, chat=True, attempt=False),
    )
    assert "chat_session_task_write" not in resp.text
    assert resp.status_code in (200, 201), resp.text


async def test_chat_progress_on_a_dispatched_task_is_refused(client):
    """Delivered but not yet started: the run is about to take it."""
    board_id, _agent, task_id, token = await _setup(
        status="inbox", worked=False, dispatched=True,
    )
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"comment_type": "progress", "content": "x"},
        headers=_auth(token, chat=True, attempt=False),
    )
    assert resp.status_code == 409, resp.text


async def test_repeated_refusals_leave_one_timeline_row(client):
    """A model that retries must not fill the timeline (r1 S3): one row per
    task and action per 10 minutes; every call is still refused."""
    from app.models.activity import ActivityEvent

    board_id, _agent, task_id, token = await _setup()
    for _ in range(3):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
            json={"comment_type": "progress", "content": "x"},
            headers=_auth(token, chat=True),
        )
        assert resp.status_code == 409, resp.text
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        json={"status": "review"},
        headers=_auth(token, chat=True),
    )
    assert resp.status_code == 409, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        events = (await s.exec(select(ActivityEvent).where(
            ActivityEvent.task_id == task_id,
            ActivityEvent.event_type == "task.chat_write_rejected",
        ))).all()
    assert sorted(e.detail["action"] for e in events) == ["comment progress", "status review"]
