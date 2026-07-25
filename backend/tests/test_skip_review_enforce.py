"""Tests: skip_review actually skips the review handoff (not just the done-gate).

Root cause (2026-07-18): task.skip_review was only honored at ONE point —
the done-transition gate in work_context.py (`if skip_review and new_status
== "done": return`). It is *permissive*: it merely allows an agent to jump
straight to done. It never intercepted the review path. So a developer-role
agent (e.g. Grok on the "Morgenbriefing X-Trends" scheduled job) that finishes
with `status: review` triggered handle_review_handoff → the task landed at the
Board Lead (Boss) for review — exactly what skip_review was meant to prevent.

Fix: on a skip_review task, a `review` transition is auto-corrected to `done`
(both the PATCH-status path and the resolution-comment path), UNLESS
human_review_required is set — that stays a hard gate to Mark.
"""

import uuid

import pytest
from unittest.mock import patch, AsyncMock
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


_REFLECTION_BODY = (
    "## Was wurde gemacht\nX-Trends Briefing erstellt\n\n"
    "## Was hat funktioniert\nRecherche lief sauber\n\n"
    "## Was war unklar\nNichts\n\n"
    "## Lesson fuer Agent-Memory\n"
    "Automation-Tasks mit skip_review brauchen keinen Reviewer."
)


async def _setup(*, skip_review: bool, human_review_required: bool = False):
    """Create board + developer agent + in_progress root task. Returns ids dict."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="SR Enforce Board", slug=f"sre-{board_id.hex[:8]}"))
        raw_token, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id,
            name="Grok",
            role="developer",
            board_id=board_id,
            agent_token_hash=token_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write"],
        ))
        s.add(Task(
            id=task_id,
            board_id=board_id,
            title="Morgenbriefing X-Trends",
            status="in_progress",
            assigned_agent_id=agent_id,
            skip_review=skip_review,
            human_review_required=human_review_required,
        ))
        await s.commit()

    return {"board_id": board_id, "agent_id": agent_id, "task_id": task_id, "token": raw_token}


async def _post_reflection(client, headers, board_id, task_id):
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        headers=headers,
        json={"content": _REFLECTION_BODY, "comment_type": "reflection"},
    )
    assert resp.status_code in (200, 201), resp.text


# ── PATCH status path ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skip_review_patch_review_becomes_done(client, fake_redis):
    """skip_review task: PATCH status=review → auto-corrected to done, no handoff."""
    ids = await _setup(skip_review=True)
    headers = {"Authorization": f"Bearer {ids['token']}"}
    await _post_reflection(client, headers, ids["board_id"], ids["task_id"])

    with (
        patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_handoff,
        patch("app.routers.agent_task_status.handle_review_pr_creation", new_callable=AsyncMock),
        patch("app.verticals.hooks.run_task_done_hooks", new_callable=AsyncMock),
    ):
        resp = await client.patch(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}",
            headers=headers,
            json={"status": "review"},
        )

    assert resp.status_code in (200, 201), resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        task = await s.get(Task, ids["task_id"])
        assert task.status == "done", f"Expected done, got {task.status}"
        assert task.completed_at is not None

    mock_handoff.assert_not_called()


@pytest.mark.asyncio
async def test_no_skip_review_patch_still_goes_to_review(client, fake_redis):
    """Regression: normal root task (skip_review=False) still → review + handoff."""
    ids = await _setup(skip_review=False)
    headers = {"Authorization": f"Bearer {ids['token']}"}
    await _post_reflection(client, headers, ids["board_id"], ids["task_id"])

    with (
        patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_handoff,
        patch("app.routers.agent_task_status.handle_review_pr_creation", new_callable=AsyncMock),
    ):
        resp = await client.patch(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}",
            headers=headers,
            json={"status": "review"},
        )

    assert resp.status_code in (200, 201), resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        task = await s.get(Task, ids["task_id"])
        assert task.status == "review", f"Expected review, got {task.status}"

    mock_handoff.assert_called_once()


@pytest.mark.asyncio
async def test_human_review_required_beats_skip_review_patch(client, fake_redis):
    """Hard gate: human_review_required=True keeps the task in review even with
    skip_review=True — skip_review must never route around Mark."""
    ids = await _setup(skip_review=True, human_review_required=True)
    headers = {"Authorization": f"Bearer {ids['token']}"}
    await _post_reflection(client, headers, ids["board_id"], ids["task_id"])

    with (
        patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_handoff,
        patch("app.services.task_lifecycle.handle_human_review_handoff", new_callable=AsyncMock) as mock_human,
        patch("app.routers.agent_task_status.handle_review_pr_creation", new_callable=AsyncMock),
    ):
        resp = await client.patch(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}",
            headers=headers,
            json={"status": "review"},
        )

    assert resp.status_code in (200, 201), resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        task = await s.get(Task, ids["task_id"])
        assert task.status == "review", f"Expected review (human gate), got {task.status}"

    mock_handoff.assert_not_called()
    mock_human.assert_called_once()


# ── Dispatch message hint (Ebene 2) ───────────────────────────────────────

_HINT = "Automation task — no review needed"


@pytest.mark.asyncio
async def test_dispatch_message_has_skip_review_hint(session, make_agent, make_task):
    """skip_review task → dispatch message tells the worker to close via `mc done`."""
    from app.services.dispatch import _build_dispatch_message

    board_id = uuid.uuid4()
    agent = await make_agent("Grok", board_id=board_id, role="developer")
    task = await make_task(
        board_id, title="X-Trends", assigned_agent_id=agent.id,
        status="inbox", skip_review=True,
    )

    msg = await _build_dispatch_message(task, agent, session)

    assert _HINT in msg
    assert "mc done" in msg


@pytest.mark.asyncio
async def test_dispatch_message_no_hint_without_skip_review(session, make_agent, make_task):
    """Normal task → no automation hint (regression)."""
    from app.services.dispatch import _build_dispatch_message

    board_id = uuid.uuid4()
    agent = await make_agent("Grok", board_id=board_id, role="developer")
    task = await make_task(
        board_id, title="Normal", assigned_agent_id=agent.id,
        status="inbox", skip_review=False,
    )

    msg = await _build_dispatch_message(task, agent, session)

    assert _HINT not in msg


@pytest.mark.asyncio
async def test_dispatch_message_no_hint_when_human_review_required(session, make_agent, make_task):
    """skip_review + human_review_required → still routed to Mark, no auto-done hint."""
    from app.services.dispatch import _build_dispatch_message

    board_id = uuid.uuid4()
    agent = await make_agent("Grok", board_id=board_id, role="developer")
    task = await make_task(
        board_id, title="Sensitive", assigned_agent_id=agent.id,
        status="inbox", skip_review=True, human_review_required=True,
    )

    msg = await _build_dispatch_message(task, agent, session)

    assert _HINT not in msg


# ── Resolution-comment path ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skip_review_resolution_comment_becomes_done(client, fake_redis):
    """skip_review task: resolution comment auto-promote → done, no handoff."""
    ids = await _setup(skip_review=True)
    headers = {"Authorization": f"Bearer {ids['token']}"}

    with (
        patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_handoff,
    ):
        resp = await client.post(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/comments",
            headers=headers,
            json={"content": "X-Trends fertig", "comment_type": "resolution"},
        )

    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        task = await s.get(Task, ids["task_id"])
        assert task.status == "done", f"Expected done, got {task.status}"
        assert task.completed_at is not None

    mock_handoff.assert_not_called()


@pytest.mark.asyncio
async def test_human_review_required_beats_skip_review_resolution(client, fake_redis):
    """Hard gate on the resolution path too: human_review_required keeps review."""
    ids = await _setup(skip_review=True, human_review_required=True)
    headers = {"Authorization": f"Bearer {ids['token']}"}

    with (
        patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_handoff,
        patch("app.services.task_lifecycle.handle_human_review_handoff", new_callable=AsyncMock) as mock_human,
    ):
        resp = await client.post(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/comments",
            headers=headers,
            json={"content": "X-Trends fertig", "comment_type": "resolution"},
        )

    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        task = await s.get(Task, ids["task_id"])
        assert task.status == "review", f"Expected review (human gate), got {task.status}"

    mock_handoff.assert_not_called()
    mock_human.assert_called_once()
