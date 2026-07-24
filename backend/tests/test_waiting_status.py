"""Task 6: `waiting` task status (answer-wait for `ask --blocking`).

`waiting` sits between in_progress and blocked: the task is paused on an
ANSWER (agent or operator), the worker's session stays alive, and the
watchdog must leave it alone entirely — unlike `blocked` (external
impediment) or `user_test` (Mark's manual test gate).

Three scenarios (per task-6-brief.md), plus a pipeline-visibility fixup
(team-lead follow-up: a waiting task must not silently vanish from the
Pipeline view):
  (a) Transition guards: in_progress -> waiting allowed, inbox -> waiting
      is NOT (VALID_TRANSITIONS in app/task_status.py — the Python-side
      guard exercised by the SQLite test engine; the Postgres trigger in
      migration 0159 mirrors the same matrix for production).
  (b) Watchdog stale/stuck checks skip waiting tasks entirely (they filter
      on Task.status == "in_progress", so a waiting task is never even
      selected).
  (c) waiting -> in_progress does not reset started_at (first-set-wins).
  (d) Both pipeline endpoints (tasks.py::get_pipeline,
      agent_task_status.py::agent_get_pipeline) surface a waiting task under
      its own "waiting" bucket instead of dropping/misfiling it.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.task_status import VALID_TRANSITIONS, is_valid_transition
from tests.conftest import test_engine


# ── (a) Transition guards ───────────────────────────────────────────────


def test_in_progress_to_waiting_allowed():
    assert is_valid_transition("in_progress", "waiting") is True


def test_inbox_to_waiting_not_allowed():
    """Binding semantics: a task must be actively worked before it can wait
    on an answer — inbox -> waiting is deliberately NOT in the matrix."""
    assert is_valid_transition("inbox", "waiting") is False
    assert "waiting" not in VALID_TRANSITIONS["inbox"]


def test_waiting_to_in_progress_allowed():
    assert is_valid_transition("waiting", "in_progress") is True


def test_waiting_to_blocked_allowed():
    assert is_valid_transition("waiting", "blocked") is True


def test_waiting_to_done_not_allowed():
    """waiting has exactly two exits: back to in_progress, or escalate to
    blocked. It can never jump straight to done/review/etc."""
    assert is_valid_transition("waiting", "done") is False
    assert VALID_TRANSITIONS["waiting"] == {"in_progress", "blocked"}


@pytest.mark.asyncio
async def test_inbox_to_waiting_rejected_via_patch_endpoint(auth_client, fake_redis, make_board, make_task):
    """End-to-end: the operator PATCH endpoint enforces the same guard
    (_enforce_board_rules -> VALID_TRANSITIONS) and returns 400, not a raw
    500 from an unhandled DB error."""
    board = await make_board(name="Waiting Guard Board", slug=f"wg-{uuid.uuid4().hex[:6]}")
    task = await make_task(board_id=board.id, title="Fresh inbox task", status="inbox")

    resp = await auth_client.patch(
        f"/api/v1/boards/{board.id}/tasks/{task.id}",
        json={"status": "waiting"},
    )
    assert resp.status_code == 400, resp.text
    assert "Status-" in resp.text or "waiting" in resp.text.lower()


@pytest.mark.asyncio
async def test_in_progress_to_waiting_accepted_via_patch_endpoint(auth_client, fake_redis, make_board, make_agent, make_task):
    """Positive counterpart: in_progress -> waiting is a real, accepted
    transition through the same operator PATCH path."""
    board = await make_board(name="Waiting Accept Board", slug=f"wa-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(name="Worker", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Active task", status="in_progress",
        assigned_agent_id=agent.id,
    )

    resp = await auth_client.patch(
        f"/api/v1/boards/{board.id}/tasks/{task.id}",
        json={"status": "waiting"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "waiting"


# ── (b) Watchdog stale/stuck exemption ──────────────────────────────────


async def _create_waiting_task(session, *, with_stale_comment: bool = False):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task, TaskComment
    from app.utils import utcnow
    from datetime import timedelta

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    board = Board(id=board_id, name="Waiting Watchdog Board", slug=f"wd-{board_id.hex[:8]}")
    session.add(board)

    _raw, token_hash = generate_agent_token()
    agent = Agent(
        id=agent_id,
        name="WaitingWorker",
        board_id=board_id,
        agent_token_hash=token_hash,
        is_board_lead=False,
        role="developer",
        scopes=["tasks:read", "tasks:write", "tasks:create"],
        agent_runtime="cli-bridge",
        last_seen_at=utcnow(),
        last_task_activity_at=utcnow() - timedelta(minutes=60),
    )
    session.add(agent)

    task = Task(
        id=task_id,
        board_id=board_id,
        title="Waiting on an answer",
        status="waiting",
        assigned_agent_id=agent_id,
        ack_at=utcnow() - timedelta(minutes=60),
        started_at=utcnow() - timedelta(minutes=60),
        updated_at=utcnow() - timedelta(minutes=60),
    )
    session.add(task)

    if with_stale_comment:
        session.add(TaskComment(
            task_id=task_id,
            author_type="agent",
            author_agent_id=agent_id,
            content="done, see above",
            comment_type="resolution",
            created_at=utcnow() - timedelta(minutes=60),
        ))

    await session.commit()
    await session.refresh(board)
    await session.refresh(agent)
    await session.refresh(task)
    return board, agent, task


@pytest.mark.asyncio
async def test_check_stale_in_progress_skips_waiting_task(fake_redis):
    """`_check_stale_in_progress` filters on Task.status == 'in_progress' —
    a waiting task (even with a stale agent-resolution comment that would
    normally auto-promote it to review) must be left untouched entirely."""
    from app.services.task_runner import TaskRunnerService

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _board, _agent, task = await _create_waiting_task(s, with_stale_comment=True)

    runner = TaskRunnerService()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_runner.emit_event", new_callable=AsyncMock):
            await runner._check_stale_in_progress(s)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(type(task), task.id)
        assert refreshed.status == "waiting", (
            "waiting task must not be auto-promoted to review by the stale check"
        )


@pytest.mark.asyncio
async def test_check_stuck_in_progress_skips_waiting_task(fake_redis):
    """`_check_stuck_in_progress` (silent-abort auto-block) also filters on
    Task.status == 'in_progress' — a waiting task must never be blocked by
    the watchdog even though its agent-activity timestamps look stale."""
    from app.services.task_runner import TaskRunnerService

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _board, _agent, task = await _create_waiting_task(s)

    runner = TaskRunnerService()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_runner.get_redis", AsyncMock(return_value=fake_redis)), \
             patch("app.services.task_runner.emit_event", new_callable=AsyncMock), \
             patch("app.services.telegram_bot.telegram_bot.send_approval_telegram", new_callable=AsyncMock):
            await runner._check_stuck_in_progress(s)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(type(task), task.id)
        assert refreshed.status == "waiting", (
            "waiting task must not be auto-blocked by the stuck-in-progress watchdog"
        )


# ── (c) started_at preserved across waiting -> in_progress ─────────────


@pytest.mark.asyncio
async def test_waiting_to_in_progress_preserves_started_at(auth_client, fake_redis, make_board, make_agent, make_task):
    """First-set-wins (F2 fix, Plan 26-03): resuming from waiting must NOT
    reset started_at — the same rule already applied to review/blocked ->
    in_progress re-opens, for accurate Cycle Time analytics."""
    from datetime import timedelta
    from app.utils import utcnow

    board = await make_board(name="Waiting Resume Board", slug=f"wr-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(name="Resumer", board_id=board.id)
    original_started = utcnow() - timedelta(hours=2)
    task = await make_task(
        board_id=board.id, title="Resuming task", status="waiting",
        assigned_agent_id=agent.id, started_at=original_started,
    )

    resp = await auth_client.patch(
        f"/api/v1/boards/{board.id}/tasks/{task.id}",
        json={"status": "in_progress"},
    )
    assert resp.status_code == 200, resp.text

    returned_started = resp.json()["started_at"]
    assert returned_started is not None
    # Compare with second precision — JSON round-trips lose sub-second/tz noise.
    from dateutil import parser as _dt
    parsed = _dt.parse(returned_started).replace(tzinfo=None)
    expected = original_started.replace(tzinfo=None)
    assert abs((parsed - expected).total_seconds()) < 2, (
        f"started_at was reset on waiting->in_progress: {returned_started} vs {original_started}"
    )


# ── (d) Pipeline visibility ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_waiting_task_appears_in_operator_pipeline(auth_client, fake_redis, make_board, make_agent, make_task):
    """tasks.py::get_pipeline must not drop waiting tasks — before this fix
    the fixed pipeline dict had no 'waiting' key and `if t.status not in
    pipeline: continue` silently dropped the task from every view."""
    board = await make_board(name="Pipeline Waiting Board", slug=f"pw-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(name="PipelineWorker", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Waiting for an answer", status="waiting",
        assigned_agent_id=agent.id,
    )

    resp = await auth_client.get(f"/api/v1/boards/{board.id}/tasks/pipeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "waiting" in body["pipeline"]
    waiting_ids = {t["id"] for t in body["pipeline"]["waiting"]}
    assert str(task.id) in waiting_ids

    # And it must not also show up misfiled in another bucket.
    for key, bucket in body["pipeline"].items():
        if key == "waiting":
            continue
        assert str(task.id) not in {t["id"] for t in bucket}


@pytest.mark.asyncio
async def test_waiting_task_appears_in_agent_pipeline(client, fake_redis, make_board, make_task):
    """agent_task_status.py::agent_get_pipeline previously had no fallback
    key either — `pipeline.get(t.status, pipeline.get('inbox'))` would have
    silently misfiled a waiting task into 'inbox'."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    board = await make_board(name="Agent Pipeline Waiting Board", slug=f"apw-{uuid.uuid4().hex[:6]}")

    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name="PipelineAgent",
            board_id=board.id,
            agent_token_hash=token_hash,
            is_board_lead=False,
            role="developer",
            scopes=["tasks:read", "tasks:write", "tasks:create"],
            agent_runtime="cli-bridge",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)

    task = await make_task(
        board_id=board.id, title="Agent waiting for an answer", status="waiting",
        assigned_agent_id=agent.id,
    )

    resp = await client.get(
        f"/api/v1/agent/boards/{board.id}/tasks/pipeline",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "waiting" in body["pipeline"]
    waiting_ids = {t["id"] for t in body["pipeline"]["waiting"]}
    assert str(task.id) in waiting_ids
    inbox_ids = {t["id"] for t in body["pipeline"]["inbox"]}
    assert str(task.id) not in inbox_ids, "waiting task must not fall back into the inbox bucket"
