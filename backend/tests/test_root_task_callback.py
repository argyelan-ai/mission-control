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
- Both of the above fire from the SAME real PATCH request when status goes
  to "done" — a second review round caught that they both sent a DM,
  double-delivering. Covered end-to-end: exactly one DM per completion.
- `mc delegate` with no active parent task (root delegation) flushes the
  subtask before it's referenced as a foreign key — the invariant behind
  the FK-violation-on-activity_events bug, checked directly since this
  suite's SQLite has FK enforcement off and can't reproduce the 500 itself.
"""

import asyncio
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


async def _make_root_callback_task(s: AsyncSession, *, status: str, name_suffix: str):
    """Board + Lead (callback_agent_id) + Worker (assigned_agent_id) + a
    genuinely root task (no parent_task_id) between them.
    """
    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    root_task_id = uuid.uuid4()

    s.add(Board(id=board_id, name=f"Root-CB-{name_suffix}", slug=f"rc-{uuid.uuid4().hex[:6]}"))
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
    # assigned_agent_id is the WORKER who did the work; callback_agent_id is
    # the LEAD who must be told.
    s.add(Task(
        id=root_task_id, board_id=board_id, title="Root task",
        status=status, parent_task_id=None,
        assigned_agent_id=worker_id,
        callback_agent_id=lead_id,
    ))
    await s.commit()
    return board_id, lead_id, worker_id, root_task_id


@pytest.mark.asyncio
async def test_handle_callback_resume_delivers_root_callback_on_failed():
    """Root task (no parent_task_id) with callback_agent_id, going `failed` →
    `_handle_callback_resume` is the ONLY path that fires for a failed
    status (`_notify_lead_on_completion` only ever runs on `done`), so it
    must deliver the DM itself here — and must NOT surface anything new on
    the finished worker's poll (that would re-wake an agent whose task is
    done with a message addressed to someone else).
    """
    from app.routers.agent_scoped import _handle_callback_resume

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, lead_id, worker_id, root_task_id = await _make_root_callback_task(
            s, status="failed", name_suffix="failed",
        )

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
async def test_handle_callback_resume_skips_dm_on_done_to_avoid_double_delivery():
    """Root task going `done` → `_handle_callback_resume` alone must NOT send
    a DM: on the real PATCH path, `_notify_lead_on_completion` fires for the
    exact same transition and already covers it. Sending here too was the
    double-delivery bug caught in review (2 DM messages for one completion).
    The audit TaskComment + event still happen — only the DM send is
    deferred.
    """
    from app.routers.agent_scoped import _handle_callback_resume

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, lead_id, worker_id, root_task_id = await _make_root_callback_task(
            s, status="done", name_suffix="done-solo",
        )

        root_task = await s.get(Task, root_task_id)
        with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
            await _handle_callback_resume(s, root_task)

        lead = await s.get(Agent, lead_id)
        lead_messages = await _poll_new_messages(s, lead)
        assert lead_messages == [], (
            "_handle_callback_resume darf bei status=done KEIN DM senden — "
            "das uebernimmt _notify_lead_on_completion auf dem realen PATCH-Pfad"
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
    args, kwargs = notify_mock.call_args
    # (session, task, board_id, actor_name)
    assert args[1].id == task_id
    assert args[2] == board_id
    assert kwargs.get("reviewed") is False, (
        "Direkter PATCH ohne Review-Gate darf _notify_lead_on_completion NICHT "
        "als reviewed=True (Default) aufrufen — sonst behauptet die Meldung "
        "eine Freigabe, die nie stattgefunden hat"
    )


@pytest.mark.asyncio
async def test_root_task_done_via_real_patch_delivers_exactly_one_dm(client):
    """End-to-end regression for the double-delivery bug caught in review:
    on the REAL PATCH request, `_handle_callback_resume` (-> `_deliver_root_callback`)
    and the lead-notify hook (-> `_notify_lead_on_completion`) both run,
    unmocked, for the same root-task in_progress -> done transition. The
    lead must receive exactly one DM, not two.
    """
    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    task_id = uuid.uuid4()

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Root-Done-Once", slug=f"rdo-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=lead_id, name="Lead", role="orchestrator",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            is_board_lead=True, scopes=["tasks:read"],
        ))
        s.add(Agent(
            id=worker_id, name="Worker", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
            current_task_id=task_id,
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Root probe",
            status="in_progress", parent_task_id=None,
            assigned_agent_id=worker_id,
            callback_agent_id=lead_id,
        ))
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=worker_id,
            comment_type="reflection",
            content=(
                "## Was wurde gemacht\n"
                "Root-Task ohne Parent direkt fertiggestellt und getestet.\n\n"
                "## Was hat funktioniert\n"
                "PATCH auf done lief ohne Fehler durch, DM kam genau einmal an.\n\n"
                "## Was war unklar\n"
                "Nichts, der Ablauf war fuer diesen Testfall eindeutig dokumentiert.\n\n"
                "## Lesson für Agent-Memory\n"
                "Doppelte Zustellwege muessen sich gegenseitig kennen und abstimmen."
            ),
        ))
        await s.commit()

    background_tasks = []

    def _run_tracked(coro, name=None):
        t = asyncio.ensure_future(coro)
        background_tasks.append(t)
        return t

    with patch("app.utils.create_tracked_task", side_effect=_run_tracked), \
         patch("app.database.engine", test_engine):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        if background_tasks:
            await asyncio.gather(*background_tasks)

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, lead_id)
        lead_messages = await _poll_new_messages(s, lead)

    assert len(lead_messages) == 1, (
        f"Genau eine DM erwartet, bekommen: {len(lead_messages)} "
        f"({[m['body'][:60] for m in lead_messages]})"
    )


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
            await _notify_lead_on_completion(s, task, board_id, "Reviewer")

        lead = await s.get(Agent, lead_id)
        lead_messages = await _poll_new_messages(s, lead)
        assert len(lead_messages) == 1
        assert "TASK ERLEDIGT" in lead_messages[0]["body"]
        assert "**Review:** Approved von Reviewer" in lead_messages[0]["body"], (
            "Default reviewed=True: eine echte Review-Freigabe darf weiter so heissen"
        )


@pytest.mark.asyncio
async def test_notify_lead_on_completion_reviewed_false_does_not_claim_approval():
    """#312 follow-up: on the direct in_progress -> done PATCH there was no
    review at all, and `reviewer_name` is just the agent who closed their own
    task. `reviewed=False` must NOT claim an "Approved von {agent}" that
    never happened (flagged in review as a non-blocking cleanup while
    already touching this function).
    """
    from app.services.task_lifecycle import _notify_lead_on_completion

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Notify-Lead-Direct", slug=f"nld-{uuid.uuid4().hex[:6]}"))
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
        with patch("app.database.engine", test_engine):
            await _notify_lead_on_completion(s, task, board_id, "Worker", reviewed=False)

        lead = await s.get(Agent, lead_id)
        lead_messages = await _poll_new_messages(s, lead)
        assert len(lead_messages) == 1
        body = lead_messages[0]["body"]
        assert "Approved von" not in body
        assert "kein Review-Gate" in body


@pytest.mark.asyncio
async def test_root_delegation_flushes_subtask_before_emit_event(client, fake_redis):
    """Fix 3 regression guard, the real one: this suite's SQLite has FK
    enforcement OFF (tests/conftest.py) and can't reject a bad insert order
    itself, so a "does /delegate return 500" test never actually exercises
    the bug — confirmed in review by reverting the fix and re-running the
    smoke test below unchanged (still 201/5 green). What #312's missing
    session.flush() in the root branch actually breaks is an invariant:
    emit_event() is called with task_id=subtask.id while subtask is still
    merely session.add()-ed (pending/unflushed) — in Postgres that produces
    the FK violation on activity_events this issue is about. Assert that
    invariant directly: by the time emit_event() runs, the subtask row must
    already be flushed (not in session.new anymore).

    Verified red without the fix: temporarily moving the flush() back inside
    `if with_callback and current_task is not None:` (the pre-#312 code)
    turns this test's assertion False.
    """
    from app.models.task import Task as TaskModel

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    worker_id = uuid.uuid4()

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Root-Delegate-Flush", slug=f"rdf-{uuid.uuid4().hex[:6]}"))
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

    captured = {}

    async def _spy_emit_event(session, *args, **kwargs):
        task_id = kwargs.get("task_id")
        pending_task_ids = {
            o.id for o in session.new
            if isinstance(o, TaskModel) and getattr(o, "id", None) is not None
        }
        captured["called"] = True
        captured["still_pending"] = task_id in pending_task_ids
        return None

    with patch("app.routers.agent_scoped.emit_event", side_effect=_spy_emit_event):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            with patch(
                "app.services.operations.check_dispatch_allowed",
                new_callable=AsyncMock,
                return_value=(True, None),
            ):
                resp = await client.post(
                    f"/api/v1/agent/boards/{board_id}/delegate",
                    json={
                        "title": "Root delegation, flush guard",
                        "description": "Prueft, dass der Subtask VOR emit_event() geflusht ist.",
                        "assigned_agent_id": str(worker_id),
                    },
                    headers={"Authorization": f"Bearer {raw_token}"},
                )

    assert resp.status_code == 201, resp.text
    assert captured.get("called") is True, "emit_event wurde nicht aufgerufen — Spy griff nicht"
    assert captured["still_pending"] is False, (
        "Subtask war beim emit_event()-Aufruf noch nicht geflusht — "
        "genau der FK-Bug aus #312"
    )


@pytest.mark.asyncio
async def test_root_delegation_via_delegate_endpoint_no_500(client, fake_redis):
    """Basic smoke test: mc delegate with NO active parent task (root
    delegation) returns 201, not a 500. Doesn't guard fix 3 by itself (see
    test_root_delegation_flushes_subtask_before_emit_event above for why —
    this suite's SQLite can't reproduce the FK violation the fix addresses),
    kept as an end-to-end sanity check of the response shape.
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


@pytest.mark.asyncio
async def test_direct_done_patch_skips_lead_notice_for_delegated_subtask(client):
    """Review-Fix #313: der Lead-Completion-Hook darf fuer delegierte
    Subtasks NICHT feuern. `mc delegate --callback` setzt parent_task_id UND
    callback_agent_id zusammen — der Delegierende bekommt beim Abschluss
    bereits den dispatch_callback_to_parent-Resume-Nudge aus
    _handle_callback_resume. Ohne das parent_task_id-Gate wuerde er pro
    fertigem Subtask ZWEI Nachrichten bekommen (Nudge + TASK-ERLEDIGT-DM).
    """
    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    subtask_id = uuid.uuid4()

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Subtask-No-Notice", slug=f"snn-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=lead_id, name="Lead", role="orchestrator",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            is_board_lead=True, scopes=["tasks:read"],
        ))
        s.add(Agent(
            id=worker_id, name="Worker", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
            current_task_id=subtask_id,
        ))
        # Delegate-Form: Parent blockiert auf den Subtask, Subtask traegt
        # parent_task_id UND callback_agent_id (wie agent_delegate_task).
        s.add(Task(
            id=parent_id, board_id=board_id, title="Parent (wartet)",
            status="blocked", assigned_agent_id=lead_id,
            blocked_by_task_id=subtask_id, callback_agent_id=lead_id,
        ))
        s.add(Task(
            id=subtask_id, board_id=board_id, title="Delegierter Subtask",
            status="in_progress", parent_task_id=parent_id,
            assigned_agent_id=worker_id, callback_agent_id=lead_id,
        ))
        s.add(TaskComment(
            task_id=subtask_id, author_type="agent", author_agent_id=worker_id,
            comment_type="reflection",
            content=(
                "## Was wurde gemacht\n"
                "Delegierten Subtask fuer den Regressionstest abgeschlossen und verifiziert.\n\n"
                "## Was hat funktioniert\n"
                "Der PATCH auf done lief durch, der Resume-Nudge an den Parent reicht aus.\n\n"
                "## Was war unklar\n"
                "Nichts, der Ablauf war fuer diesen Testfall eindeutig dokumentiert.\n\n"
                "## Lesson für Agent-Memory\n"
                "Subtasks brauchen keine zweite Erledigt-DM an den Delegierenden."
            ),
        ))
        await s.commit()

    notify_mock = AsyncMock()
    with patch("app.services.task_lifecycle._notify_lead_on_completion", notify_mock), \
         patch("app.utils.create_tracked_task", side_effect=lambda coro, name=None: coro.close()), \
         patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock), \
         patch("app.services.auto_memory.create_tracked_task", create=True):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{subtask_id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )

    assert resp.status_code == 200, resp.text
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_review_done_patch_notifies_lead_as_reviewed(client):
    """Review-Fix #313: PATCH review → done ist der legitime
    Reviewer-Approve-Shortcut (review_decision wird im selben Request auf
    'approved' gesetzt). Der Hook muss dann reviewed=True uebergeben — sonst
    behauptet die DM 'kein Review-Gate', obwohl eine echte Freigabe stattfand.
    """
    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()
    task_id = uuid.uuid4()

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Review-Done", slug=f"rvd-{uuid.uuid4().hex[:6]}"))
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
        s.add(Agent(
            id=reviewer_id, name="Reviewer", role="reviewer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
        ))
        # Root-Task in review; Worker hat gearbeitet, Reviewer approved per PATCH.
        s.add(Task(
            id=task_id, board_id=board_id, title="Review-approve task",
            status="review", parent_task_id=None,
            assigned_agent_id=worker_id,
            callback_agent_id=lead_id,
        ))
        await s.commit()

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
    _, kwargs = notify_mock.call_args
    assert kwargs.get("reviewed") is True, (
        "PATCH review → done ist eine echte Freigabe (review_decision=approved "
        "im selben Request) — der Hook darf sie nicht als reviewed=False melden"
    )
