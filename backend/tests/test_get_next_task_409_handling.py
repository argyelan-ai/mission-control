"""409-Handling in get_next_task's candidate loop (PR #478 review, M6).

``get_next_task`` (pull-based dispatch, ``mc``'s "give me work" path) picks
the first eligible inbox candidate and calls ``lock_and_set()`` to claim it.
Before this fix, a lost race there (a second poller claiming the same
candidate between this endpoint's own SELECT and the locked re-read — the
exact race this endpoint exists to arbitrate) propagated the 409 straight
out of the endpoint. The agent got a 409 instead of a task, even though a
second, perfectly good candidate was sitting right behind it in the same
list.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from app.models.task import Task
from app.services.task_state import lock_and_set as real_lock_and_set


def _flaky_lock_and_set(fail_task_id: uuid.UUID):
    """Real lock_and_set, but 409s for one specific task_id — simulates a
    second poller having already claimed that exact candidate."""

    async def _inner(session, task_id, to, *, actor):
        if task_id == fail_task_id:
            raise HTTPException(status_code=409, detail="Ungueltiger Statuswechsel (simuliert)")
        return await real_lock_and_set(session, task_id, to, actor=actor)

    return _inner


@pytest.mark.asyncio
@patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock)
async def test_get_next_task_409_on_first_candidate_tries_next(
    mock_emit, session, make_board, make_agent, make_task,
):
    """Two inbox candidates assigned to the same board-lead agent. The
    first (older, so first in the created_at-ordered candidate list) loses
    the lock_and_set() race — the endpoint must still dispatch the second
    instead of aborting with a bare 409."""
    from app.routers.agent_task_status import get_next_task

    board = await make_board(name="M6 Board", slug=f"m6-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(
        name="Lead", board_id=board.id, is_board_lead=True,
    )
    candidate_a = await make_task(
        board_id=board.id, title="Candidate A (races)",
        status="inbox", assigned_agent_id=agent.id, parent_task_id=None,
    )
    candidate_b = await make_task(
        board_id=board.id, title="Candidate B (fine)",
        status="inbox", assigned_agent_id=agent.id, parent_task_id=None,
    )

    with patch(
        "app.routers.agent_task_status.lock_and_set",
        side_effect=_flaky_lock_and_set(candidate_a.id),
    ), patch(
        "app.services.dispatch._build_dispatch_message",
        new_callable=AsyncMock, return_value={},
    ):
        result = await get_next_task(board.id, session=session, agent=agent)

    assert not isinstance(result, Response), (
        f"expected a dispatched task, got {result!r} -- the 409 on "
        "candidate A must not abort the whole endpoint"
    )
    assert result["task"].id == candidate_b.id, (
        "must skip the raced candidate A and dispatch candidate B instead"
    )

    # candidate_a/candidate_b were created via make_task's own throwaway
    # session -- re-fetch through the endpoint's `session` fixture instead
    # of session.refresh() (that requires the same session's identity map).
    reloaded_a = await session.get(Task, candidate_a.id)
    reloaded_b = await session.get(Task, candidate_b.id)
    assert reloaded_a.status == "inbox", "A lost the race -- untouched, retried next poll"
    assert reloaded_b.status == "in_progress", "B must have been claimed"


@pytest.mark.asyncio
@patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock)
async def test_get_next_task_409_on_only_candidate_returns_204(
    mock_emit, session, make_board, make_agent, make_task,
):
    """A single candidate that loses the race must fall through to the
    "no task available" 204, not raise."""
    from app.routers.agent_task_status import get_next_task

    board = await make_board(name="M6 Board Solo", slug=f"m6-solo-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(
        name="Lead Solo", board_id=board.id, is_board_lead=True,
    )
    candidate = await make_task(
        board_id=board.id, title="Only candidate (races)",
        status="inbox", assigned_agent_id=agent.id, parent_task_id=None,
    )

    with patch(
        "app.routers.agent_task_status.lock_and_set",
        side_effect=_flaky_lock_and_set(candidate.id),
    ):
        result = await get_next_task(board.id, session=session, agent=agent)

    assert isinstance(result, Response) and result.status_code == 204

    reloaded = await session.get(Task, candidate.id)
    assert reloaded.status == "inbox"
