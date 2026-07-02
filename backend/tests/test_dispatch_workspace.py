"""Tests for Non-Code-Task workspace creation (T-1 Phase C)."""
import os
import uuid
import pytest


@pytest.mark.asyncio
async def test_non_code_task_gets_workspace_directory(tmp_path):
    """Non-Code-Task bekommt ein _tasks/{id}/ Verzeichnis."""
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
    """Gleicher Task = gleicher Workspace-Pfad, auch wenn zweimal aufgerufen."""
    from app.services.dispatch import _ensure_task_workspace

    task_id = uuid.uuid4()
    ws1 = await _ensure_task_workspace(task_id, project=None, agent_workspace=str(tmp_path))
    ws2 = await _ensure_task_workspace(task_id, project=None, agent_workspace=str(tmp_path))

    assert ws1 == ws2


@pytest.mark.asyncio
async def test_git_project_skips_workspace_creation(tmp_path):
    """Wenn Projekt ein GitHub-Repo hat, wird kein _tasks/ Verzeichnis erstellt."""
    from unittest.mock import MagicMock
    from app.services.dispatch import _ensure_task_workspace

    project = MagicMock()
    project.github_repo_url = "https://github.com/example/repo"

    task_id = uuid.uuid4()
    result = await _ensure_task_workspace(task_id, project=project, agent_workspace=str(tmp_path))

    assert result is None


@pytest.mark.asyncio
async def test_fallback_to_tmp_when_no_agent_workspace():
    """Wenn kein agent_workspace, Fallback auf /tmp/mc_tasks/{id}/."""
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
    """Backend-Container hat oft kein Mount auf den Host-Workspace-Pfad
    (z.B. /Users/testuser/Workspace). Wenn os.makedirs PermissionError wirft,
    soll _ensure_task_workspace None zurückgeben, damit auto_dispatch_task
    weiter läuft — nicht crashen."""
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
    """Generelle OSError (z.B. ReadOnly Filesystem) soll auch geschluckt werden —
    Workspace ist Nice-to-Have, kein Hard-Requirement."""
    from app.services import dispatch

    def _readonly(*args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(dispatch.os, "makedirs", _readonly)

    task_id = uuid.uuid4()
    workspace = await dispatch._ensure_task_workspace(
        task_id, project=None, agent_workspace="/some/readonly/path"
    )

    assert workspace is None
