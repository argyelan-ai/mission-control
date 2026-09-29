"""Tests for help request auto-resume on subtask completion."""

import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession
from unittest.mock import patch, AsyncMock

from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.auth import generate_agent_token

from .conftest import test_engine


# ── Helpers ──────────────────────────────────────────────────────────────────

async def make_board(session: AsyncSession) -> Board:
    board = Board(id=uuid.uuid4(), name="Test Board", slug=f"test-{uuid.uuid4().hex[:6]}")
    session.add(board)
    await session.commit()
    await session.refresh(board)
    return board


async def make_agent(session: AsyncSession, name: str, board_id: uuid.UUID) -> Agent:
    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=uuid.uuid4(),
        name=name,
        role="developer",
        board_id=board_id,
        provision_status="provisioned",
        agent_token_hash=token_hash,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def make_task(session: AsyncSession, board_id: uuid.UUID, **kwargs) -> Task:
    task = Task(
        id=uuid.uuid4(),
        board_id=board_id,
        title=kwargs.pop("title", "Test Task"),
        status=kwargs.pop("status", "in_progress"),
        **kwargs,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestHelpRequestResume:
    """_handle_help_request_resume — direct unit tests."""

    async def test_subtask_done_resumes_blocked_parent(self):
        """Parent goes from blocked → in_progress when a help-request subtask becomes done."""
        from app.routers.agent_scoped import _handle_help_request_resume

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            board = await make_board(session)
            requester = await make_agent(session, "Requester", board.id)
            helper = await make_agent(session, "Helper", board.id)

            parent = await make_task(session, board.id, title="Parent Task", status="blocked")
            subtask = await make_task(
                session,
                board.id,
                title="Help Subtask",
                status="done",
                help_request_from=requester.id,
                assigned_agent_id=helper.id,
                parent_task_id=parent.id,
            )

            # parent.blocked_by_task_id → subtask.id (so resume kicks in)
            parent.blocked_by_task_id = subtask.id
            session.add(parent)
            await session.commit()
            await session.refresh(parent)

            with patch(
                "app.routers.agent_scoped.dispatch_resume_to_agent",
                new_callable=AsyncMock,
            ):
                with patch(
                    "app.services.activity.emit_event",
                    new_callable=AsyncMock,
                ) as mock_emit:
                    await _handle_help_request_resume(session, subtask)

            # Parent must now be in_progress with blocked_by_task_id cleared
            await session.refresh(parent)
            assert parent.status == "in_progress"
            assert parent.blocked_by_task_id is None

            # Resolved event must have been emitted
            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["event_type"] == "task.help_request.resolved"

    async def test_subtask_failed_keeps_parent_blocked(self):
        """Parent stays blocked when a help-request subtask fails."""
        from app.routers.agent_scoped import _handle_help_request_resume

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            board = await make_board(session)
            requester = await make_agent(session, "RequesterB", board.id)

            parent = await make_task(session, board.id, title="Parent Task B", status="blocked")
            subtask = await make_task(
                session,
                board.id,
                title="Help Subtask B",
                status="failed",
                help_request_from=requester.id,
                parent_task_id=parent.id,
            )

            parent.blocked_by_task_id = subtask.id
            session.add(parent)
            await session.commit()
            await session.refresh(parent)

            with patch(
                "app.services.activity.emit_event",
                new_callable=AsyncMock,
            ) as mock_emit:
                await _handle_help_request_resume(session, subtask)

            # Parent must NOT have changed
            await session.refresh(parent)
            assert parent.status == "blocked"
            assert parent.blocked_by_task_id == subtask.id

            # Failed event must have been emitted
            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["event_type"] == "task.help_request.failed"

    async def test_held_parent_stays_held_on_subtask_done(self):
        """C2 (PR #533 Nacharbeit, 7th claim path): a parent under `mc hold`
        (run_control="manual_hold") must NOT be resumed to in_progress just
        because the help-request subtask that blocked it finished — the hold
        is a separate, deliberate lifecycle intent (Board Lead controlling
        its own queue) that this auto-resume carries no signal about.
        `mc release` is the only way out."""
        from app.routers.agent_scoped import _handle_help_request_resume

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            board = await make_board(session)
            requester = await make_agent(session, "RequesterHeld", board.id)
            helper = await make_agent(session, "HelperHeld", board.id)

            parent = await make_task(
                session, board.id, title="Held Parent Task", status="blocked",
                run_control="manual_hold", hold_reason="Deploy-Fenster",
            )
            subtask = await make_task(
                session,
                board.id,
                title="Help Subtask Held",
                status="done",
                help_request_from=requester.id,
                assigned_agent_id=helper.id,
                parent_task_id=parent.id,
            )

            parent.blocked_by_task_id = subtask.id
            session.add(parent)
            await session.commit()
            await session.refresh(parent)

            with patch(
                "app.routers.agent_task_status.dispatch_resume_to_agent",
                new_callable=AsyncMock,
            ) as mock_dispatch:
                with patch(
                    "app.services.activity.emit_event",
                    new_callable=AsyncMock,
                ) as mock_emit:
                    await _handle_help_request_resume(session, subtask)

            # Parent must stay exactly as the hold left it — no resume.
            await session.refresh(parent)
            assert parent.status == "blocked"
            assert parent.run_control == "manual_hold"
            assert parent.blocked_by_task_id == subtask.id

            mock_dispatch.assert_not_called()
            mock_emit.assert_not_called()

    async def test_unheld_parent_still_resumes_normally(self):
        """Counter-probe for the guard above: a parent with run_control=None
        (the normal, non-held case) must still resume exactly as before —
        the new check must not block the case it isn't meant to touch."""
        from app.routers.agent_scoped import _handle_help_request_resume

        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            board = await make_board(session)
            requester = await make_agent(session, "RequesterUnheld", board.id)
            helper = await make_agent(session, "HelperUnheld", board.id)

            parent = await make_task(
                session, board.id, title="Unheld Parent Task", status="blocked",
                run_control=None,
            )
            subtask = await make_task(
                session,
                board.id,
                title="Help Subtask Unheld",
                status="done",
                help_request_from=requester.id,
                assigned_agent_id=helper.id,
                parent_task_id=parent.id,
            )

            parent.blocked_by_task_id = subtask.id
            session.add(parent)
            await session.commit()
            await session.refresh(parent)

            with patch(
                "app.routers.agent_task_status.dispatch_resume_to_agent",
                new_callable=AsyncMock,
            ) as mock_dispatch:
                with patch(
                    "app.services.activity.emit_event",
                    new_callable=AsyncMock,
                ) as mock_emit:
                    await _handle_help_request_resume(session, subtask)

            await session.refresh(parent)
            assert parent.status == "in_progress"
            assert parent.blocked_by_task_id is None

            mock_dispatch.assert_called_once()
            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["event_type"] == "task.help_request.resolved"
