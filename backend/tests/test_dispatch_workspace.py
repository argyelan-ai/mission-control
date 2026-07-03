"""Tests for Non-Code-Task workspace creation (T-1 Phase C)."""
import os
import uuid
import pytest


@pytest.mark.asyncio
async def test_non_code_task_gets_workspace_directory(tmp_path):
    """A non-code task gets a _tasks/{id}/ directory."""
    from app.services.dispatch import _ensure_task_workspace

    task_id = uuid.uuid4()
    workspace = await _ensure_task_workspace(
        task_id, project=None, agent_workspace=str(tmp_path)
    )

    assert workspace is not None
    assert os.path.isdir(workspace)
    assert str(task_id) in workspace
    assert os.path.isdir(os.path.join(workspace, "output"))


@pytest.mark.asyncio
async def test_workspace_path_idempotent(tmp_path):
    """Same task = same workspace path, even when called twice."""
    from app.services.dispatch import _ensure_task_workspace

    task_id = uuid.uuid4()
    ws1 = await _ensure_task_workspace(task_id, project=None, agent_workspace=str(tmp_path))
    ws2 = await _ensure_task_workspace(task_id, project=None, agent_workspace=str(tmp_path))

    assert ws1 == ws2


@pytest.mark.asyncio
async def test_git_project_skips_workspace_creation(tmp_path):
    """If the project has a GitHub repo, no _tasks/ directory is created."""
    from unittest.mock import MagicMock
    from app.services.dispatch import _ensure_task_workspace

    project = MagicMock()
    project.github_repo_url = "https://github.com/example/repo"

    task_id = uuid.uuid4()
    result = await _ensure_task_workspace(task_id, project=project, agent_workspace=str(tmp_path))

    assert result is None


@pytest.mark.asyncio
async def test_fallback_to_tmp_when_no_agent_workspace():
    """If there's no agent_workspace, fall back to /tmp/mc_tasks/{id}/."""
    from app.services.dispatch import _ensure_task_workspace
    import shutil

    task_id = uuid.uuid4()
    workspace = None
    try:
        workspace = await _ensure_task_workspace(task_id, project=None, agent_workspace=None)
        assert workspace is not None
        assert str(task_id) in workspace
        assert os.path.isdir(workspace)
    finally:
        # Cleanup
        if workspace:
            shutil.rmtree(workspace, ignore_errors=True)


@pytest.mark.asyncio
async def test_permission_error_returns_none_instead_of_raising(monkeypatch):
    """The backend container often has no mount to the host workspace path
    (e.g. /Users/testuser/Workspace). If os.makedirs raises PermissionError,
    _ensure_task_workspace should return None so auto_dispatch_task
    keeps running — not crash."""
    from app.services import dispatch

    def _denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(dispatch.os, "makedirs", _denied)

    task_id = uuid.uuid4()
    workspace = await dispatch._ensure_task_workspace(
        task_id, project=None, agent_workspace="/Users/testuser/Workspace"
    )

    assert workspace is None


@pytest.mark.asyncio
async def test_oserror_returns_none_instead_of_raising(monkeypatch):
    """A general OSError (e.g. read-only filesystem) should also be swallowed —
    the workspace is a nice-to-have, not a hard requirement."""
    from app.services import dispatch

    def _readonly(*args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(dispatch.os, "makedirs", _readonly)

    task_id = uuid.uuid4()
    workspace = await dispatch._ensure_task_workspace(
        task_id, project=None, agent_workspace="/some/readonly/path"
    )

    assert workspace is None
