"""Tests for redispatch_after_blocker_answer (dispatch.py).

Incident 2026-09-13 (card 4c9bb492 / G5): a blocker_decision approval was
resolved, which synchronously set the task to "inbox" and scheduled a
background auto_dispatch_task call. By the time that background task
actually ran, a different agent had already picked the card up, worked it,
and moved it to "review" (PR #554) — auto_dispatch_task never re-checks the
task's current status/run_control, so the stale redispatch fired anyway and
shoved the card back to "inbox", reassigned to yet another agent.

redispatch_after_blocker_answer wraps auto_dispatch_task with a fresh
status/run_control re-check immediately before dispatching.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import test_engine


@pytest.mark.asyncio
async def test_stale_redispatch_skips_when_task_moved_to_review(
    make_board, make_agent, make_task,
):
    """Task was 'inbox' when the blocker answer scheduled the redispatch, but
    by the time the guarded wrapper actually runs the card is 'review' (an
    agent picked it up and finished real work in the gap) — must NOT
    dispatch, must emit a visible skip event."""
    from app.services.dispatch import redispatch_after_blocker_answer

    board = await make_board(name="Redispatch Race", slug="redispatch-race")
    agent = await make_agent(name="OtherAgent", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Moved on while answer was in flight",
        status="review", assigned_agent_id=agent.id,
    )

    with patch("app.services.dispatch.engine", test_engine), \
         patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch, \
         patch("app.services.dispatch.emit_event", new_callable=AsyncMock) as mock_emit:
        await redispatch_after_blocker_answer(task.id, board.id)

    mock_dispatch.assert_not_called()
    mock_emit.assert_called_once()
    call_kwargs = mock_emit.call_args.kwargs
    assert call_kwargs.get("detail", {}).get("current_status") == "review"


@pytest.mark.parametrize("stale_status", ["done", "waiting", "in_progress", "blocked"])
@pytest.mark.asyncio
async def test_stale_redispatch_skips_for_any_non_inbox_status(
    make_board, make_agent, make_task, stale_status,
):
    """Explicit, not accidental: EVERY status other than the expected 'inbox'
    (done, waiting, in_progress, blocked-again) skips the redispatch the same
    way — no special-casing, no silent default."""
    from app.services.dispatch import redispatch_after_blocker_answer

    board = await make_board(name=f"Redispatch {stale_status}", slug=f"redispatch-{stale_status}")
    agent = await make_agent(name="SomeAgent", board_id=board.id)
    task = await make_task(
        board_id=board.id, title=f"Task at {stale_status}",
        status=stale_status, assigned_agent_id=agent.id,
    )

    with patch("app.services.dispatch.engine", test_engine), \
         patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch, \
         patch("app.services.dispatch.emit_event", new_callable=AsyncMock):
        await redispatch_after_blocker_answer(task.id, board.id)

    mock_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_stale_redispatch_skips_when_held_in_the_gap(
    make_board, make_agent, make_task,
):
    """Task is still 'inbox' (the expected status) but run_control got set
    (e.g. `mc hold`) in the gap between the blocker answer and this call —
    must also skip. The blocker answer does not override a hold placed by
    someone else after it."""
    from app.services.dispatch import redispatch_after_blocker_answer

    board = await make_board(name="Redispatch Held", slug="redispatch-held")
    agent = await make_agent(name="HeldAgent", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Held right after the blocker answer",
        status="inbox", assigned_agent_id=agent.id, run_control="manual_hold",
    )

    with patch("app.services.dispatch.engine", test_engine), \
         patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch, \
         patch("app.services.dispatch.emit_event", new_callable=AsyncMock) as mock_emit:
        await redispatch_after_blocker_answer(task.id, board.id)

    mock_dispatch.assert_not_called()
    mock_emit.assert_called_once()


@pytest.mark.asyncio
async def test_redispatch_proceeds_when_task_still_at_expected_status(
    make_board, make_agent, make_task,
):
    """Counter-probe: the task is still exactly where the blocker answer left
    it (status='inbox', run_control=None) — the guard must NOT overreach and
    must let the redispatch through unchanged."""
    from app.services.dispatch import redispatch_after_blocker_answer

    board = await make_board(name="Redispatch OK", slug="redispatch-ok")
    agent = await make_agent(name="ReadyAgent", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Still exactly where the answer left it",
        status="inbox", assigned_agent_id=agent.id, run_control=None,
    )

    with patch("app.services.dispatch.engine", test_engine), \
         patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch, \
         patch("app.services.dispatch.emit_event", new_callable=AsyncMock) as mock_emit:
        await redispatch_after_blocker_answer(task.id, board.id)

    mock_dispatch.assert_called_once_with(task.id, board.id)
    mock_emit.assert_not_called()


@pytest.mark.asyncio
async def test_missing_task_is_a_silent_noop(make_board):
    """A deleted/never-existed task must not raise — best-effort background
    path, mirrors auto_dispatch_task's own `if not task: return`."""
    from app.services.dispatch import redispatch_after_blocker_answer

    board = await make_board(name="Redispatch Gone", slug="redispatch-gone")

    with patch("app.services.dispatch.engine", test_engine), \
         patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch:
        await redispatch_after_blocker_answer(uuid.uuid4(), board.id)

    mock_dispatch.assert_not_called()
