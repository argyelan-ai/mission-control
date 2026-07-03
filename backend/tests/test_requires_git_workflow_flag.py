"""Respect the requires_git_workflow flag in the git commit gate.

Davinci (Designer) got "Keine Git-Commits im Workspace gefunden" on his
design task (no code change, deliverables only). Fix: validate_task_completion
skips the git check when the assigned agent.requires_git_workflow=False.
"""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.models.task import Task
from app.models.board import Project


@pytest.mark.asyncio
async def test_git_check_skipped_for_designer_agent(async_session, board_with_agents):
    """Agent with requires_git_workflow=False -> git check skipped."""
    from app.services.work_context import validate_task_completion
    from app.models.agent import Agent

    board = board_with_agents["board"]
    designer = Agent(
        id=uuid4(), name="Designer", board_id=board.id,
        role="designer", requires_git_workflow=False, scopes=["tasks:write"],
    )
    async_session.add(designer)
    project = Project(
        id=uuid4(), board_id=board.id, name="Web Project",
        github_repo_url="https://github.com/foo/bar",
    )
    async_session.add(project)
    await async_session.commit()

    task = Task(
        board_id=board.id, title="Design Arbeit", status="in_progress",
        assigned_agent_id=designer.id, project_id=project.id,
        workspace_path="/tmp/some/path",  # would trigger the git check
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    # Despite workspace_path + project_id + github_repo_url: no git check
    ok, errors = await validate_task_completion(async_session, task)
    assert "Keine Git-Commits im Workspace gefunden" not in errors


@pytest.mark.asyncio
async def test_git_check_runs_for_developer_agent(async_session, board_with_agents, tmp_path):
    """Agent with requires_git_workflow=True + real git repo -> git check runs (mocked)."""
    from app.services.work_context import validate_task_completion
    from app.models.agent import Agent

    # tmp_path must be a git repo, otherwise the new guard kicks in and
    # skips the check entirely. Just mkdir'ing `.git` is enough — the
    # check only tests for dir existence, not git-repo validity.
    (tmp_path / ".git").mkdir()

    board = board_with_agents["board"]
    dev = Agent(
        id=uuid4(), name="Dev", board_id=board.id,
        role="developer", requires_git_workflow=True, scopes=["tasks:write"],
    )
    async_session.add(dev)
    project = Project(
        id=uuid4(), board_id=board.id, name="Code Project",
        github_repo_url="https://github.com/foo/bar",
    )
    async_session.add(project)
    await async_session.commit()

    task = Task(
        board_id=board.id, title="Code Arbeit", status="in_progress",
        assigned_agent_id=dev.id, project_id=project.id,
        workspace_path=str(tmp_path),
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    # has_task_commits returns False -> error expected
    with patch("app.services.git_service.git_service.has_task_commits",
               new=AsyncMock(return_value=False)):
        ok, errors = await validate_task_completion(async_session, task)

    assert "Keine Git-Commits im Workspace gefunden" in errors


@pytest.mark.asyncio
async def test_git_check_skipped_when_workspace_is_not_git_repo(
    async_session, board_with_agents, tmp_path,
):
    """Fallback workspace without .git -> git check is skipped, not treated as an error.

    FreeCode case (2026-04-19): backend fell back during dispatch to an
    empty placeholder dir (~/FreeCode/projects/<slug>/), while the
    agent in the container had the real repo under /workspace/....
    has_task_commits on the empty dir would have blocked the task on
    review even though the agent had pushed everything correctly.
    """
    from app.services.work_context import validate_task_completion
    from app.models.agent import Agent

    # No `.git` in tmp_path → workspace is not a repo
    assert not (tmp_path / ".git").exists()

    board = board_with_agents["board"]
    dev = Agent(
        id=uuid4(), name="DevNoRepo", board_id=board.id,
        role="developer", requires_git_workflow=True, scopes=["tasks:write"],
    )
    async_session.add(dev)
    project = Project(
        id=uuid4(), board_id=board.id, name="Code Project No Repo",
        github_repo_url="https://github.com/foo/bar",
    )
    async_session.add(project)
    await async_session.commit()

    task = Task(
        board_id=board.id, title="Code Task", status="in_progress",
        assigned_agent_id=dev.id, project_id=project.id,
        workspace_path=str(tmp_path),
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    # Even if has_task_commits would return False — we never get there,
    # because the guard kicks in first.
    with patch("app.services.git_service.git_service.has_task_commits",
               new=AsyncMock(return_value=False)) as m:
        ok, errors = await validate_task_completion(async_session, task)

    assert "Keine Git-Commits im Workspace gefunden" not in errors
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_git_check_skipped_when_no_workspace(async_session, board_with_agents):
    """Without workspace_path -> no git check (existing behavior)."""
    from app.services.work_context import validate_task_completion

    board = board_with_agents["board"]
    dev = board_with_agents["developer"]
    task = Task(
        board_id=board.id, title="No workspace", status="in_progress",
        assigned_agent_id=dev.id, workspace_path=None,
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    ok, errors = await validate_task_completion(async_session, task)
    assert "Keine Git-Commits im Workspace gefunden" not in errors
