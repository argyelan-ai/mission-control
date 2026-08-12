"""Tests for Issue #312 — root-task callbacks are silently dropped.

A root task (no parent_task_id) with an explicit callback_agent_id used to
vanish into silence on completion: `_handle_callback_resume` only knows how
to resume a *parent*, and `_notify_lead_on_completion` only ever ran from
the review-approve path. Covers:

- `_handle_callback_resume` genuinely delivers a callback to the
  callback_agent_id via that agent's DM thread — verified end-to-end through
  the SAME poll helpers `/agent/me/poll` uses, not just "a comment exists
  somewhere". A first version of this fix wrote a deliverable TaskComment on
  the finished task itself; that never reaches the callback agent because
  `/me/poll` scopes comment delivery to `Task.assigned_agent_id`, which on a
  root task is the WORKER — caught in review, see `_deliver_root_callback`'s
  docstring in agent_task_status.py.
- A direct in_progress -> done PATCH (no review involved) triggers
  `_notify_lead_on_completion` when callback_agent_id is set explicitly, and
  that function's message genuinely reaches the lead's DM thread too.
- `mc delegate` with no active parent task (root delegation) no longer 500s
  with a foreign-key violation on activity_events.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.comment_types import DELIVERABLE_SYSTEM_TYPES
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment

from .conftest import test_engine


async def _poll_deliverable_comments(session: AsyncSession, agent: Agent) -> list[dict]:
    """Exercise the real `/agent/me/poll` comment-delivery path for `agent`."""
    from app.routers.agents import _collect_and_ack_new_comments
    return await _collect_and_ack_new_comments(agent, session)


async def _poll_new_messages(session: AsyncSession, agent: Agent) -> list[dict]:
    """Exercise the real `/agent/me/poll` message-delivery path (DM threads
    included) for `agent` — the same helper `_collect_new_messages` that
    backs `/agent/me/poll` and `/agent/me/inbox`.
    """
    from app.routers.agents import _collect_new_messages
    return await _collect_new_messages(session, agent, acked={})


@pytest.mark.asyncio
async def test_handle_callback_resume_delivers_root_callback_with_no_parent():
    """Root task (no parent_task_id) with callback_agent_id, going `done` →
    _handle_callback_resume must deliver a callback that the callback agent's
    OWN poll actually surfaces — and must NOT surface anything new on the
    finished worker's poll (that would re-wake an agent whose task is done
    with a message addressed to someone else).
    """
    from app.routers.agent_scoped import _handle_callback_resume

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    root_task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Root-Callback", slug=f"rc-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=lead_id, name="Lead", role="orchestrator",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            is_board_lead=True, scopes=["tasks:read"],
        ))
        s.add(Agent(
            id=worker_id, name="Worker", role="developer",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read"],
        ))
        # Genuinely root: no parent_task_id at all. Created e.g. via
        # POST /boards/{board_id}/tasks (not `mc delegate`), callback_agent_id
        # set explicitly by the caller — repro from #312. assigned_agent_id
        # is the WORKER who did the work; callback_agent_id is the LEAD who
        # must be told.
        s.add(Task(
            id=root_task_id, board_id=board_id, title="Root task",
            status="done", parent_task_id=None,
            assigned_agent_id=worker_id,
            callback_agent_id=lead_id,
        ))
        await s.commit()

        root_task = await s.get(Task, root_task_id)
        with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock) as mock_emit:
            await _handle_callback_resume(s, root_task)

        # Audit-trail TaskComment exists on the task, but is deliberately
        # NOT a deliverable type — it must not auto-push to anyone's poll.
        comment_result = await s.exec(
            select(TaskComment).where(TaskComment.task_id == root_task_id)
        )
        comments = list(comment_result.all())
        assert len(comments) == 1, "Genau ein Audit-Kommentar erwartet"
        assert comments[0].comment_type not in DELIVERABLE_SYSTEM_TYPES, (
            "Audit-Kommentar auf dem Task selbst darf NICHT deliverable sein — "
            "sonst sieht der (fertige) Worker eine an den Lead adressierte Nachricht"
        )

        # task.callback_received fired, addressed to the callback_agent_id.
        mock_emit.assert_awaited_once()
        _, kwargs = mock_emit.call_args
        assert kwargs["event_type"] == "task.callback_received"
        assert kwargs["agent_id"] == lead_id
        assert kwargs["task_id"] == root_task_id

        # Real delivery check, same helpers /agent/me/poll uses:
        lead = await s.get(Agent, lead_id)
        worker = await s.get(Agent, worker_id)
        lead_messages = await _poll_new_messages(s, lead)
        worker_comments = await _poll_deliverable_comments(s, worker)

        assert len(lead_messages) == 1, (
            "Der Lead (callback_agent_id) muss die Meldung ueber seinen "
            "eigenen DM-Thread bekommen"
        )
        assert "Root-Task" in lead_messages[0]["body"]
        assert worker_comments == [], (
            "Der Worker (assigned_agent_id, Task bereits done) darf KEINE "
            "neue deliverable Nachricht bekommen, die eigentlich an den Lead ging"
        )


@pytest.mark.asyncio
async def test_handle_callback_resume_noop_without_callback_agent_id():
    """Root task with NO callback_agent_id → still a clean no-op (regression guard)."""
    from app.routers.agent_scoped import _handle_callback_resume

    board_id = uuid.uuid4()
    root_task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Root-No-Callback", slug=f"rnc-{uuid.uuid4().hex[:6]}"))
        s.add(Task(
            id=root_task_id, board_id=board_id, title="Root task, no callback",
            status="done", parent_task_id=None, callback_agent_id=None,
        ))
        await s.commit()

        root_task = await s.get(Task, root_task_id)
        with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock) as mock_emit:
            await _handle_callback_resume(s, root_task)

        mock_emit.assert_not_awaited()
        comment_result = await s.exec(
            select(TaskComment).where(TaskComment.task_id == root_task_id)
        )
        assert comment_result.first() is None


@pytest.mark.asyncio
async def test_direct_done_patch_notifies_lead_when_callback_agent_set(client):
    """Trust-by-default board (require_review_before_done=False): a worker
    PATCHes their task straight in_progress -> done. Previously
    `_notify_lead_on_completion` only ever ran from the review-approve path,
    so this transition never told anyone. Now it must fire whenever
    callback_agent_id is explicitly set (#312, fix 2).
    """
    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    task_id = uuid.uuid4()

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Direct-Done", slug=f"dd-{uuid.uuid4().hex[:6]}")
        s.add(board)
        s.add(Agent(
            id=lead_id, name="Lead", role="orchestrator",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            is_board_lead=True, scopes=["tasks:read"],
        ))
        worker = Agent(
            id=worker_id, name="Worker", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
            current_task_id=task_id,
        )
        s.add(worker)
        s.add(Task(
            id=task_id, board_id=board_id, title="Direct-done task",
            status="in_progress", assigned_agent_id=worker_id,
            callback_agent_id=lead_id,
        ))
        # Mandatory-reflection gate (ADR-023) — needs a reflection comment
        # on record before a direct PATCH to done is allowed.
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=worker_id,
            comment_type="reflection",
            content=(
                "## Was wurde gemacht\n"
                "Den direkten in_progress-zu-done Uebergang fuer den Test implementiert und verifiziert.\n\n"
                "## Was hat funktioniert\n"
                "Der komplette Ablauf inklusive PATCH-Endpoint lief ohne Fehler durch.\n\n"
                "## Was war unklar\n"
                "Nichts, der Ablauf war fuer diesen Testfall eindeutig dokumentiert.\n\n"
                "## Lesson für Agent-Memory\n"
                "Reflexions-Kommentare brauchen mindestens 80 Zeichen Inhalt pro Feld."
            ),
        ))
        await s.commit()

    assert board.require_review_before_done is False

    notify_mock = AsyncMock()
    with patch("app.services.task_lifecycle._notify_lead_on_completion", notify_mock), \
         patch("app.utils.create_tracked_task", side_effect=lambda coro, name=None: coro.close()), \
         patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock), \
         patch("app.services.auto_memory.create_tracked_task", create=True):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )

    assert resp.status_code == 200, resp.text
    notify_mock.assert_called_once()
    args, _ = notify_mock.call_args
    # (session, task, board_id, actor_name)
    assert args[1].id == task_id
    assert args[2] == board_id


@pytest.mark.asyncio
async def test_notify_lead_on_completion_reaches_lead_dm_thread():
    """`_notify_lead_on_completion` itself (not mocked) must land a message
    in the lead's DM thread — its TaskComment (comment_type="system_notify")
    is deliberately not a deliverable type (same reasoning as
    _deliver_root_callback: task.id's assigned_agent_id is the worker, not
    the lead), so the DM-thread post is what actually closes the loop.
    """
    from app.services.task_lifecycle import _notify_lead_on_completion

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Notify-Lead", slug=f"nl-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=lead_id, name="Lead", role="orchestrator",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            is_board_lead=True, scopes=["tasks:read"],
        ))
        s.add(Agent(
            id=worker_id, name="Worker", role="developer",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read"],
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Directly-done task",
            status="done", assigned_agent_id=worker_id,
            callback_agent_id=lead_id,
        ))
        await s.commit()

        task = await s.get(Task, task_id)
        # _notify_lead_on_completion opens its OWN session via
        # `from app.database import engine` (fire-and-forget background task
        # in production) — point that at the sqlite test engine, same
        # precedent as test_mc_henry_sunset_script.py.
        with patch("app.database.engine", test_engine):
            await _notify_lead_on_completion(s, task, board_id, "Worker")

        lead = await s.get(Agent, lead_id)
        lead_messages = await _poll_new_messages(s, lead)
        assert len(lead_messages) == 1
        assert "TASK ERLEDIGT" in lead_messages[0]["body"]


@pytest.mark.asyncio
async def test_root_delegation_via_delegate_endpoint_no_500(client, fake_redis):
    """mc delegate with NO active parent task (root delegation) must not 500
    with a FK violation on activity_events — the missing flush before
    emit_event() in the root branch of agent_delegate_task (#312, fix 3).
    """
    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    worker_id = uuid.uuid4()

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Root-Delegate", slug=f"rd-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=lead_id, name="Lead", role="orchestrator",
            board_id=board_id, agent_token_hash=token_hash,
            is_board_lead=True, scopes=["tasks:read", "tasks:write", "tasks:create"],
            current_task_id=None,
        ))
        s.add(Agent(
            id=worker_id, name="Worker", role="developer",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
        ))
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            with patch(
                "app.services.operations.check_dispatch_allowed",
                new_callable=AsyncMock,
                return_value=(True, None),
            ):
                resp = await client.post(
                    f"/api/v1/agent/boards/{board_id}/delegate",
                    json={
                        "title": "Root delegation, no active parent",
                        "description": "Repro fuer #312 — kein current_task beim delegierenden Lead.",
                        "assigned_agent_id": str(worker_id),
                    },
                    headers={"Authorization": f"Bearer {raw_token}"},
                )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    subtask_id = uuid.UUID(body["subtask_id"])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        subtask = await s.get(Task, subtask_id)
        assert subtask is not None
        assert subtask.parent_task_id is None
        assert subtask.assigned_agent_id == worker_id
