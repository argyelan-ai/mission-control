"""Workspace setup must never silently fall back on project-with-repo tasks.

Regression guard for the 2026-04-19 incident: FreeCode's Phase 1b task
had project.github_repo_url=mc-demo-site, but git clone failed
(destination already exists). The old cli_bridge_runner silently fell
back to an empty placeholder directory. FreeCode's container mounts
~/Workspace as /workspace read-write, so the agent found another repo
there (demo-website) and committed to the wrong GitHub repo.

The fix: git-clone failure on a project-with-repo task blocks the
task instead of dispatching it with an empty workspace.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board, Project
from app.models.task import Task, TaskComment
from tests.conftest import test_engine


async def _seed_project_task():
    """Returns (agent, project, task) with github_repo_url set."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        project = Project(
            id=uuid.uuid4(),
            board_id=board.id,
            name="AI Project",
            github_repo_url="https://github.com/test-owner/ai-project.git",
        )
        s.add(project)
        agent = Agent(
            id=uuid.uuid4(),
            name=f"FreeCode-{uuid.uuid4().hex[:6]}",
            role="developer",
            agent_runtime="cli-bridge",
            workspace_path="/tmp/nonexistent-workspace",
        )
        s.add(agent)
        task = Task(
            id=uuid.uuid4(),
            board_id=board.id,
            title="Build the Hero",
            status="in_progress",
            project_id=project.id,
            assigned_agent_id=agent.id,
        )
        s.add(task)
        await s.commit()
        return agent.id, project.id, task.id


@pytest.mark.asyncio
async def test_cli_bridge_blocks_task_when_clone_fails():
    """ensure_workspace failure must block the task, not fall back to empty dir."""
    from app.services.cli_bridge_runner import dispatch_to_cli_bridge

    agent_id, project_id, task_id = await _seed_project_task()

    with patch("app.services.git_service.git_service.ensure_workspace",
               new_callable=AsyncMock,
               side_effect=RuntimeError("git clone failed: destination exists")), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            agent = await s.get(Agent, agent_id)
            task = await s.get(Task, task_id)
            result = await dispatch_to_cli_bridge(agent, task, "prompt", s)

    assert result is False, "dispatch_to_cli_bridge must return False on clone failure"

    # Task must be blocked, NOT silently dispatched with an empty workspace
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.status == "blocked"
        assert task.workspace_path != "/Users/testuser/FreeCode/projects", (
            "must not land on placeholder workspace"
        )

        # Blocker comment must exist so the operator sees the problem
        from sqlmodel import select
        comments = list((await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all())
        blockers = [c for c in comments if c.comment_type == "blocker"]
        assert len(blockers) == 1
        assert "Workspace-Setup fehlgeschlagen" in blockers[0].content
        assert "git clone failed" in blockers[0].content


@pytest.mark.asyncio
async def test_cli_bridge_worktree_failure_still_uses_main_repo():
    """Worktree is a nice-to-have. If create_task_worktree fails but
    ensure_workspace succeeded, fall back to the main repo (same remote,
    same safety). Only ensure_workspace failure must block.
    """
    from app.services.cli_bridge_runner import dispatch_to_cli_bridge

    agent_id, project_id, task_id = await _seed_project_task()

    with patch("app.services.git_service.git_service.ensure_workspace",
               new_callable=AsyncMock,
               return_value="/tmp/main-repo"), \
         patch("app.services.git_service.git_service.create_task_worktree",
               new_callable=AsyncMock,
               side_effect=RuntimeError("worktree unsupported on this fs")), \
         patch("app.services.git_service.git_service.setup_git_identity",
               new_callable=AsyncMock), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            agent = await s.get(Agent, agent_id)
            task = await s.get(Task, task_id)
            result = await dispatch_to_cli_bridge(agent, task, "prompt", s)

    assert result is True

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.status != "blocked"
        # workspace_path falls back to main repo (still inside the correct clone)
        assert "main-repo" in (task.workspace_path or "")


@pytest.mark.asyncio
async def test_cli_bridge_plain_workspace_for_project_without_repo():
    """A project configured WITHOUT github_repo_url is a design/docs-only
    project. Plain workspace is intended, not a failure mode.
    """
    from app.services.cli_bridge_runner import dispatch_to_cli_bridge

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        project = Project(
            id=uuid.uuid4(),
            board_id=board.id,
            name="Design Only Project",
            # No github_repo_url
        )
        s.add(project)
        agent = Agent(
            id=uuid.uuid4(),
            name=f"Davinci-{uuid.uuid4().hex[:6]}",
            role="designer",
            agent_runtime="cli-bridge",
            workspace_path="/tmp/ws",
        )
        s.add(agent)
        task = Task(
            id=uuid.uuid4(),
            board_id=board.id,
            title="Design mockup",
            status="in_progress",
            project_id=project.id,
            assigned_agent_id=agent.id,
        )
        s.add(task)
        await s.commit()
        agent_id, task_id = agent.id, task.id

    with patch("app.services.activity.broadcast", new_callable=AsyncMock), \
         patch("app.services.cli_bridge_runner._create_plain_workspace",
               return_value="/tmp/plain-ws"):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            agent = await s.get(Agent, agent_id)
            task = await s.get(Task, task_id)
            result = await dispatch_to_cli_bridge(agent, task, "prompt", s)

    assert result is True

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.status != "blocked"  # plain workspace is OK, not a failure
