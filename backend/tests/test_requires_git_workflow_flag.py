"""requires_git_workflow Flag respektieren im Git-Commit-Gate.

Davinci (Designer) bekam "Keine Git-Commits im Workspace gefunden" auf seinem
Design-Task (kein Code-Change, reine Deliverables). Fix: validate_task_completion
ueberspringt Git-Check wenn assigned agent.requires_git_workflow=False.
"""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.models.task import Task
from app.models.board import Project


@pytest.mark.asyncio
async def test_git_check_skipped_for_designer_agent(async_session, board_with_agents):
    """Agent mit requires_git_workflow=False -> Git-Check uebersprungen."""
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
        workspace_path="/tmp/some/path",  # wuerde Git-Check triggern
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    # Trotz workspace_path + project_id + github_repo_url: kein Git-Check
    ok, errors = await validate_task_completion(async_session, task)
    assert "Keine Git-Commits im Workspace gefunden" not in errors


@pytest.mark.asyncio
async def test_git_check_runs_for_developer_agent(async_session, board_with_agents, tmp_path):
    """Agent mit requires_git_workflow=True + echtes Git-Repo -> Git-Check laeuft (mocked)."""
    from app.services.work_context import validate_task_completion
    from app.models.agent import Agent

    # tmp_path muss ein Git-Repo sein, sonst greift der neue Guard und
    # skippet den Check komplett. Nur `.git` zu mkdir'en reicht — der
    # Check prueft nur auf dir-existence, nicht git-repo-validity.
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

    # has_task_commits returns False -> error erwartet
    with patch("app.services.git_service.git_service.has_task_commits",
               new=AsyncMock(return_value=False)):
        ok, errors = await validate_task_completion(async_session, task)

    assert "Keine Git-Commits im Workspace gefunden" in errors


@pytest.mark.asyncio
async def test_git_check_skipped_when_workspace_is_not_git_repo(
    async_session, board_with_agents, tmp_path,
):
    """Fallback-Workspace ohne .git -> Git-Check wird geskippt, nicht als Fehler.

    FreeCode case (2026-04-19): backend fiel beim Dispatch auf eine
    leere Placeholder-Dir zurueck (~/FreeCode/projects/<slug>/), waehrend
    der Agent im Container unter /workspace/... das echte Repo hatte.
    has_task_commits auf die leere Dir haette den Task auf review
    blockiert obwohl der Agent alles korrekt gepushed hat.
    """
    from app.services.work_context import validate_task_completion
    from app.models.agent import Agent

    # Kein `.git` in tmp_path → Workspace ist kein Repo
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

    # Selbst wenn has_task_commits False liefern wuerde — wir kommen gar nicht
    # dorthin, weil der guard vorher greift.
    with patch("app.services.git_service.git_service.has_task_commits",
               new=AsyncMock(return_value=False)) as m:
        ok, errors = await validate_task_completion(async_session, task)

    assert "Keine Git-Commits im Workspace gefunden" not in errors
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_git_check_skipped_when_no_workspace(async_session, board_with_agents):
    """Ohne workspace_path -> kein Git-Check (bestehendes Verhalten)."""
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
