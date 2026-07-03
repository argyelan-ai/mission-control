"""Tests for create_task_internal — shared internal task creation helper."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.board import Board
from app.models.task import Task


class TestCreateTaskInternal:
    """Unit tests for services/task_create.py."""

    async def test_creates_task_with_minimal_args(self, session):
        """Helper creates task with required fields and defaults."""
        board = Board(name="Test Board", slug="test-board")
        session.add(board)
        await session.commit()
        await session.refresh(board)

        with (
            patch("app.services.task_create.emit_event", new_callable=AsyncMock) as mock_emit,
            patch("app.services.task_create.create_tracked_task"),
        ):
            from app.services.task_create import create_task_internal

            task = await create_task_internal(
                session,
                board_id=board.id,
                title="Scheduled daily briefing",
                dispatch=False,  # no auto-dispatch in unit test
            )

        assert task.id is not None
        assert task.title == "Scheduled daily briefing"
        assert task.board_id == board.id
        assert task.status == "inbox"
        assert task.priority == "medium"
        assert task.task_type == "story"
        assert task.is_auto_created is False

        # Activity event must be emitted
        mock_emit.assert_called_once()
        call_kwargs = mock_emit.call_args
        assert call_kwargs.args[1] == "task.created"

        # Task is persisted in DB
        from_db = await session.get(Task, task.id)
        assert from_db is not None
        assert from_db.title == "Scheduled daily briefing"

    async def test_emits_task_created_event(self, session):
        """emit_event is called with 'task.created' and the correct task_id."""
        board = Board(name="Event Board", slug="event-board")
        session.add(board)
        await session.commit()
        await session.refresh(board)

        with (
            patch("app.services.task_create.emit_event", new_callable=AsyncMock) as mock_emit,
            patch("app.services.task_create.create_tracked_task"),
        ):
            from app.services.task_create import create_task_internal

            task = await create_task_internal(
                session,
                board_id=board.id,
                title="Event test task",
                is_auto_created=True,
                auto_reason="scheduler",
                dispatch=False,
            )

        mock_emit.assert_called_once()
        _, kwargs = mock_emit.call_args.args, mock_emit.call_args.kwargs
        assert kwargs.get("task_id") == task.id
        assert kwargs.get("board_id") == board.id
        assert task.is_auto_created is True
        assert task.auto_reason == "scheduler"

    async def test_resolves_project_id_from_board_default(self, session):
        """When project_id is missing and there's no parent: Board.default_project_id is used."""
        from app.models.board import Project

        board = Board(name="Project Board", slug="project-board")
        session.add(board)
        await session.commit()
        await session.refresh(board)

        project = Project(
            name="Default Project",
            board_id=board.id,
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)

        # Set board's default_project_id
        board.default_project_id = project.id
        session.add(board)
        await session.commit()

        with (
            patch("app.services.task_create.emit_event", new_callable=AsyncMock),
            patch("app.services.task_create.create_tracked_task"),
        ):
            from app.services.task_create import create_task_internal

            task = await create_task_internal(
                session,
                board_id=board.id,
                title="Auto-project task",
                dispatch=False,
            )

        assert task.project_id == project.id

    async def test_resolves_project_id_from_parent_task(self, session):
        """When parent_task_id is set: project_id is inherited from the parent."""
        from app.models.board import Project

        board = Board(name="Parent Board", slug="parent-board")
        session.add(board)
        await session.commit()
        await session.refresh(board)

        project = Project(name="Parent Project", board_id=board.id)
        session.add(project)
        await session.commit()
        await session.refresh(project)

        parent_task = Task(
            board_id=board.id,
            title="Parent Task",
            project_id=project.id,
        )
        session.add(parent_task)
        await session.commit()
        await session.refresh(parent_task)

        with (
            patch("app.services.task_create.emit_event", new_callable=AsyncMock),
            patch("app.services.task_create.create_tracked_task"),
        ):
            from app.services.task_create import create_task_internal

            subtask = await create_task_internal(
                session,
                board_id=board.id,
                title="Child task",
                parent_task_id=parent_task.id,
                dispatch=False,
            )

        # Subtask inherits project_id from the parent
        assert subtask.project_id == project.id
        assert subtask.parent_task_id == parent_task.id

    async def test_dispatch_triggered_when_auto_dispatch_enabled(self, session):
        """create_tracked_task is called when Board.auto_dispatch_enabled=True."""
        board = Board(name="Dispatch Board", slug="dispatch-board", auto_dispatch_enabled=True)
        session.add(board)
        await session.commit()
        await session.refresh(board)

        # auto_dispatch_task is a lazy import inside the function body → patch at the source module.
        # Use MagicMock (not AsyncMock) since create_tracked_task only receives the coroutine
        # as an argument — it is not awaited in the test.
        with (
            patch("app.services.task_create.emit_event", new_callable=AsyncMock),
            patch("app.services.task_create.create_tracked_task") as mock_tracked,
            patch("app.services.dispatch.auto_dispatch_task"),
        ):
            from app.services.task_create import create_task_internal

            await create_task_internal(
                session,
                board_id=board.id,
                title="Auto-dispatched task",
                dispatch=True,
            )

        mock_tracked.assert_called_once()

    async def test_dispatch_skipped_when_disabled(self, session):
        """create_tracked_task is NOT called when dispatch=False."""
        board = Board(name="No Dispatch Board", slug="no-dispatch-board", auto_dispatch_enabled=True)
        session.add(board)
        await session.commit()
        await session.refresh(board)

        with (
            patch("app.services.task_create.emit_event", new_callable=AsyncMock),
            patch("app.services.task_create.create_tracked_task") as mock_tracked,
        ):
            from app.services.task_create import create_task_internal

            await create_task_internal(
                session,
                board_id=board.id,
                title="No dispatch task",
                dispatch=False,
            )

        mock_tracked.assert_not_called()

    async def test_extra_fields_applied(self, session):
        """extra_fields are applied to the task via setattr."""
        board = Board(name="Extra Board", slug="extra-board")
        session.add(board)
        await session.commit()
        await session.refresh(board)

        with (
            patch("app.services.task_create.emit_event", new_callable=AsyncMock),
            patch("app.services.task_create.create_tracked_task"),
        ):
            from app.services.task_create import create_task_internal

            task = await create_task_internal(
                session,
                board_id=board.id,
                title="Extra fields task",
                extra_fields={"skip_review": True, "dispatch_intent": "subtask"},
                dispatch=False,
            )

        assert task.skip_review is True
        assert task.dispatch_intent == "subtask"

    async def test_report_back_fields_mapped(self, session):
        """report_back_enabled → report_back_required, format → report_back_requirements."""
        board = Board(name="Report Board", slug="report-board")
        session.add(board)
        await session.commit()
        await session.refresh(board)

        with (
            patch("app.services.task_create.emit_event", new_callable=AsyncMock),
            patch("app.services.task_create.create_tracked_task"),
        ):
            from app.services.task_create import create_task_internal

            task = await create_task_internal(
                session,
                board_id=board.id,
                title="Report back task",
                report_back_enabled=True,
                report_back_channel="telegram",
                report_back_format=["summary", "screenshot"],
                dispatch=False,
            )

        assert task.report_back_required is True
        assert task.report_back_channel == "telegram"
        assert task.report_back_requirements == "summary,screenshot"
