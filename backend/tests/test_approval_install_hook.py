"""
Tests for the InstallExecutor hook in resolve_approval().

Covers:
- Resolving an install_skill approval with status=approved triggers InstallExecutor.execute()
- Resolving with status=rejected does NOT trigger InstallExecutor.execute()
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Helpers ────────────────────────────────────────────────────────────────


async def _make_install_approval(
    *,
    action_type: str = "install_skill",
    status: str = "pending",
):
    """Create Board + Agent + Approval for install tests. Return (approval, board, agent)."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.approval import Approval

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="MC Dev", slug=f"mc-dev-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        target = Agent(
            name="Spark",
            role="developer",
            scopes=[],
            cli_skills=[],
            board_id=board.id,
        )
        s.add(target)
        await s.commit()
        await s.refresh(target)

        approval = Approval(
            board_id=board.id,
            agent_id=target.id,
            action_type=action_type,
            description="Install web-perf",
            payload={
                "name": "web-performance",
                "source": "github:anthropic/skill-web-performance",
                "target_agent_id": str(target.id),
                "requester_agent_id": str(target.id),
            },
            status=status,
        )
        s.add(approval)
        await s.commit()
        await s.refresh(approval)

    return approval, board, target


def _make_mock_install_result(result: str = "success"):
    install_result = MagicMock()
    install_result.result = result
    install_result.error = None
    install_result.installed_version = "1.0"
    install_result.install_log_id = uuid.uuid4()
    return install_result


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approval_resolve_triggers_install_executor(auth_client, fake_redis):
    """Resolving an install_skill approval with status=approved triggers InstallExecutor.execute()."""
    approval, board, agent = await _make_install_approval(action_type="install_skill")

    install_result = _make_mock_install_result("success")

    with patch("app.routers.approvals.InstallExecutor") as mock_executor_cls:
        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value=install_result)
        mock_executor_cls.return_value = mock_executor

        with patch("app.routers.approvals.emit_event", new_callable=AsyncMock):
            resp = await auth_client.patch(
                f"/api/v1/approvals/{approval.id}",
                json={"status": "approved", "resolver_note": "looks good"},
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    mock_executor.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_approval_rejected_does_not_trigger_install(auth_client, fake_redis):
    """Resolving an install_skill approval with status=rejected must NOT trigger InstallExecutor."""
    approval, board, agent = await _make_install_approval(action_type="install_skill")

    with patch("app.routers.approvals.InstallExecutor") as mock_executor_cls:
        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock()
        mock_executor_cls.return_value = mock_executor

        with patch("app.routers.approvals.emit_event", new_callable=AsyncMock):
            resp = await auth_client.patch(
                f"/api/v1/approvals/{approval.id}",
                json={"status": "rejected", "resolver_note": "not needed"},
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    mock_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_install_plugin_approval_triggers_executor(auth_client, fake_redis):
    """install_plugin action_type also triggers InstallExecutor when approved."""
    approval, board, agent = await _make_install_approval(action_type="install_plugin")
    # Patch payload to use cli_plugins instead of cli_skills
    from app.models.approval import Approval
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = await s.get(Approval, approval.id)
        a.payload = {
            "name": "superpowers",
            "source": "claude-plugins-official",
            "target_agent_id": str(agent.id),
            "requester_agent_id": str(agent.id),
        }
        s.add(a)
        await s.commit()

    install_result = _make_mock_install_result("success")

    with patch("app.routers.approvals.InstallExecutor") as mock_executor_cls:
        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value=install_result)
        mock_executor_cls.return_value = mock_executor

        with patch("app.routers.approvals.emit_event", new_callable=AsyncMock):
            resp = await auth_client.patch(
                f"/api/v1/approvals/{approval.id}",
                json={"status": "approved"},
            )

    assert resp.status_code == 200, resp.text
    mock_executor.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_executor_failure_sets_failure_reason(auth_client, fake_redis):
    """When InstallExecutor returns result='failed', approval.failure_reason is set."""
    approval, board, agent = await _make_install_approval(action_type="install_skill")

    failed_result = _make_mock_install_result("failed")
    failed_result.error = "git clone failed: repo not found"

    with patch("app.routers.approvals.InstallExecutor") as mock_executor_cls:
        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value=failed_result)
        mock_executor_cls.return_value = mock_executor

        with patch("app.routers.approvals.emit_event", new_callable=AsyncMock):
            resp = await auth_client.patch(
                f"/api/v1/approvals/{approval.id}",
                json={"status": "approved"},
            )

    assert resp.status_code == 200, resp.text

    # Verify failure_reason is persisted
    from app.models.approval import Approval
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = await s.get(Approval, approval.id)
        assert a.failure_reason == "git clone failed: repo not found"


# ── Install-Callback Tests (mirror subtask_completed pattern) ─────────────


async def _make_install_approval_with_task(
    *,
    action_type: str = "install_plugin",
    status: str = "pending",
    include_task_id: bool = True,
):
    """Create Board + Agent + Task + Approval with requester_task_id in payload."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.approval import Approval
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="MC Dev Callback", slug=f"mc-cb-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        requester = Agent(
            name="Boss_CB", role="orchestrator", scopes=[],
            cli_plugins=[], board_id=board.id, is_board_lead=True,
        )
        target = Agent(
            name="Davinci_CB", role="developer", scopes=[],
            cli_plugins=[], board_id=board.id,
        )
        s.add_all([requester, target])
        await s.commit()
        await s.refresh(requester)
        await s.refresh(target)

        task = Task(
            board_id=board.id, title="Video-Task fuer Davinci",
            description="Needs higgsfield-mcp plugin", status="in_progress",
            assigned_agent_id=requester.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)

        payload = {
            "name": "higgsfield-mcp",
            "source": "anthropic-agent-skills",
            "target_agent_id": str(target.id),
            "target_agent_name": target.name,
            "requester_agent_id": str(requester.id),
            "requester_agent_name": requester.name,
        }
        if include_task_id:
            payload["requester_task_id"] = str(task.id)

        approval = Approval(
            board_id=board.id, agent_id=requester.id,
            action_type=action_type, description="Install higgsfield-mcp for Davinci",
            payload=payload, status=status,
        )
        s.add(approval)
        await s.commit()
        await s.refresh(approval)

    return approval, board, requester, target, task


@pytest.mark.asyncio
async def test_install_callback_success_creates_comment(auth_client, fake_redis):
    """Successful install with requester_task_id → install_completed comment on task."""
    approval, board, requester, target, task = await _make_install_approval_with_task(
        action_type="install_plugin", include_task_id=True,
    )
    success_result = _make_mock_install_result("success")
    success_result.installed_version = "0.2.0"

    with patch("app.routers.approvals.InstallExecutor") as mock_executor_cls:
        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value=success_result)
        mock_executor_cls.return_value = mock_executor

        resp = await auth_client.patch(
            f"/api/v1/approvals/{approval.id}",
            json={"status": "approved"},
        )

    assert resp.status_code == 200, resp.text

    # Verify comment was posted on the requester's task
    from app.models.task import TaskComment
    from sqlmodel import select
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        install_comments = [c for c in comments if c.comment_type == "install_completed"]
        assert len(install_comments) == 1, f"Expected 1 install_completed comment, got {len(comments)}"
        c = install_comments[0]
        assert "higgsfield-mcp" in c.content
        assert "Davinci_CB" in c.content
        assert "0.2.0" in c.content
        assert c.author_type == "system"


@pytest.mark.asyncio
async def test_install_callback_no_task_id_no_comment(auth_client, fake_redis):
    """Install without requester_task_id → activity_event emitted, no comment."""
    approval, board, requester, target, task = await _make_install_approval_with_task(
        action_type="install_plugin", include_task_id=False,
    )
    success_result = _make_mock_install_result("success")

    with patch("app.routers.approvals.InstallExecutor") as mock_executor_cls:
        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value=success_result)
        mock_executor_cls.return_value = mock_executor

        resp = await auth_client.patch(
            f"/api/v1/approvals/{approval.id}",
            json={"status": "approved"},
        )

    assert resp.status_code == 200, resp.text

    from app.models.task import TaskComment
    from sqlmodel import select
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # There may be other comments on the task (e.g. from approval.resolved event
        # not creating comments — but verify no install_completed/install_failed)
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        install_comments = [
            c for c in comments
            if c.comment_type in ("install_completed", "install_failed")
        ]
        assert len(install_comments) == 0


@pytest.mark.asyncio
async def test_install_callback_failure_creates_failed_comment(auth_client, fake_redis):
    """Failed install with requester_task_id → install_failed comment with error."""
    approval, board, requester, target, task = await _make_install_approval_with_task(
        action_type="install_plugin", include_task_id=True,
    )
    failed_result = _make_mock_install_result("failed")
    failed_result.error = "pip install failed: no matching distribution"

    with patch("app.routers.approvals.InstallExecutor") as mock_executor_cls:
        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value=failed_result)
        mock_executor_cls.return_value = mock_executor

        resp = await auth_client.patch(
            f"/api/v1/approvals/{approval.id}",
            json={"status": "approved"},
        )

    assert resp.status_code == 200, resp.text

    from app.models.task import TaskComment
    from sqlmodel import select
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        failed_comments = [c for c in comments if c.comment_type == "install_failed"]
        assert len(failed_comments) == 1
        c = failed_comments[0]
        assert "fehlgeschlagen" in c.content.lower() or "failed" in c.content.lower()
        assert "pip install failed" in c.content
