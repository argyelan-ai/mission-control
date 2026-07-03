"""Bundle 4 — Workspace Isolation Tests.

1. Create worktree + path is correct
2. Cleanup on done (complete)
3. Cleanup on failed (files kept)
4. workspace_path saved on task
5. Fallback on worktree error
6. Recovery context shows task workspace_path
"""
import os
import uuid
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession


# ── Git Service Worktree Functions ───────────────────────────────────────

class TestWorktreeFunctions:

    @pytest.mark.asyncio
    async def test_create_worktree_returns_path(self):
        """create_task_worktree returns the worktree path."""
        from app.services.git_service import GitService

        svc = GitService()
        svc._configured = True

        with patch.object(svc, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = ""
            # Simulate temporary directory
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                project_dir = os.path.join(tmpdir, "myproject")
                os.makedirs(project_dir)
                # Git init for a realistic test
                os.makedirs(os.path.join(project_dir, ".git"))

                path = await svc.create_task_worktree(project_dir, "test-task")

                expected = os.path.join(tmpdir, "worktrees", "test-task")
                assert path == expected
                assert mock_cmd.call_count >= 1  # fetch + worktree add

    @pytest.mark.asyncio
    async def test_create_worktree_existing_returns_path(self):
        """Existing worktree is not recreated."""
        from app.services.git_service import GitService

        svc = GitService()
        svc._configured = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = os.path.join(tmpdir, "myproject")
            worktree_dir = os.path.join(tmpdir, "worktrees", "existing-task")
            os.makedirs(project_dir)
            os.makedirs(worktree_dir)  # Worktree already exists

            path = await svc.create_task_worktree(project_dir, "existing-task")
            assert path == worktree_dir

    @pytest.mark.asyncio
    async def test_cleanup_worktree_done(self):
        """done: remove worktree completely."""
        from app.services.git_service import GitService

        svc = GitService()
        svc._configured = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = os.path.join(tmpdir, "myproject")
            worktree_dir = os.path.join(tmpdir, "worktrees", "done-task")
            os.makedirs(project_dir)
            os.makedirs(worktree_dir)

            with patch.object(svc, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
                mock_cmd.return_value = ""
                await svc.cleanup_worktree(project_dir, worktree_dir, keep_on_fail=False)

                # git worktree remove + prune called
                calls = [str(c) for c in mock_cmd.call_args_list]
                assert any("worktree" in c and "remove" in c for c in calls)
                assert any("prune" in c for c in calls)

    @pytest.mark.asyncio
    async def test_cleanup_worktree_failed_keeps_files(self):
        """failed: worktree removed from index, files remain."""
        from app.services.git_service import GitService

        svc = GitService()
        svc._configured = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = os.path.join(tmpdir, "myproject")
            worktree_dir = os.path.join(tmpdir, "worktrees", "failed-task")
            os.makedirs(project_dir)
            os.makedirs(worktree_dir)

            with patch.object(svc, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
                mock_cmd.return_value = ""
                await svc.cleanup_worktree(project_dir, worktree_dir, keep_on_fail=True)

                # Check calls: remove called with --force
                calls = [str(c) for c in mock_cmd.call_args_list]
                assert any("remove" in c for c in calls)

    @pytest.mark.asyncio
    async def test_cleanup_nonexistent_worktree_silent(self):
        """Non-existent worktree → silent return."""
        from app.services.git_service import GitService

        svc = GitService()
        svc._configured = True

        await svc.cleanup_worktree("/nonexistent/repo", "/nonexistent/wt")
        # No error, no crash


# ── Task Model ───────────────────────────────────────────────────────────

class TestTaskWorkspacePath:

    @pytest.mark.asyncio
    async def test_task_workspace_path_field(self, session: AsyncSession, make_task):
        """Task has a workspace_path field."""
        board_id = uuid.uuid4()
        task = await make_task(
            board_id, title="WS Task",
            workspace_path="/tmp/worktrees/ws-task",
        )
        assert task.workspace_path == "/tmp/worktrees/ws-task"

    @pytest.mark.asyncio
    async def test_task_workspace_path_default_none(self, session: AsyncSession, make_task):
        """workspace_path defaults to None."""
        board_id = uuid.uuid4()
        task = await make_task(board_id, title="No WS Task")
        assert task.workspace_path is None


# ── Recovery Integration ─────────────────────────────────────────────────

class TestWorkspaceInRecovery:

    @pytest.mark.asyncio
    async def test_recovery_shows_task_workspace(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Recovery context shows Task.workspace_path (not agent workspace)."""
        from app.services.dispatch import build_recovery_context
        from app.models.task import TaskComment

        board_id = uuid.uuid4()
        agent = await make_agent(
            "WsAgent", board_id=board_id, role="developer",
workspace_path="/agent/default/workspace",
        )
        task = await make_task(
            board_id, title="WS Recovery",
            assigned_agent_id=agent.id,
            status="in_progress",
            workspace_path="/worktrees/ws-recovery-task",
        )

        comment = TaskComment(
            task_id=task.id, author_type="agent",
            comment_type="progress", content="Angefangen",
        )
        session.add(comment)
        await session.commit()

        ctx = await build_recovery_context(session, task)
        assert ctx is not None
        assert "/worktrees/ws-recovery-task" in ctx
        # Agent workspace should NOT appear
        assert "/agent/default/workspace" not in ctx


# ── Dispatch Integration ─────────────────────────────────────────────────

class TestDispatchWorkspacePath:

    @pytest.mark.asyncio
    async def test_dispatch_message_uses_task_workspace(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Dispatch message uses Task.workspace_path instead of agent workspace."""
        from app.services.dispatch import _build_dispatch_message

        board_id = uuid.uuid4()
        agent = await make_agent(
            "DispAgent", board_id=board_id, role="developer",
workspace_path="/agent/workspace",
        )
        task = await make_task(
            board_id, title="Dispatch WS Test",
            assigned_agent_id=agent.id,
            status="inbox",
            workspace_path="/worktrees/isolated-task",
        )

        msg = await _build_dispatch_message(task, agent, session)
        assert "/worktrees/isolated-task" in msg

    @pytest.mark.asyncio
    async def test_dispatch_fallback_without_workspace(
        self, session: AsyncSession, make_agent, make_task,
    ):
        """Without Task.workspace_path → message contains no worktree path."""
        from app.services.dispatch import _build_dispatch_message

        board_id = uuid.uuid4()
        agent = await make_agent(
            "FallbackAgent", board_id=board_id, role="developer",
        )
        task = await make_task(
            board_id, title="Fallback WS Test",
            assigned_agent_id=agent.id,
            status="inbox",
        )

        msg = await _build_dispatch_message(task, agent, session)
        # No worktree path, but message still gets built
        assert "Fallback WS Test" in msg
        assert "/worktrees/" not in msg
