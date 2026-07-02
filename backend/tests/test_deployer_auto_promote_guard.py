"""Phase 8 — Deployer Resolution Auto-Promote Guard (BUG-01).

3 xfail stubs reserving the test surface for Plan 08-01:

- Path A guard (agent_comments.py:287)
- Path B guard (task_runner.py:771)
- Preservation: Cody (auto_promote_on_resolution=True) still gets the safety-net
  promote — guards against an inverted-boolean regression.

Plan 08-01 flips all 3 from xfail -> PASS by adding the
`agent.auto_promote_on_resolution` check in both auto-promote paths.

Pattern follows Plan 04-00 (Wave-0 scaffolding for Phase 4 TST-04 reflection
enforcement) — bodies are realistic enough to flip without a rewrite, but the
last assertion is wrapped in pytest.xfail() so the file collects today.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Helpers ──────────────────────────────────────────────────────────────


async def _create_deployer_test_data(
    session,
    *,
    auto_promote_on_resolution: bool,
    role: str = "deployer",
):
    """Board + Agent (with explicit auto_promote_on_resolution flag) + Task in in_progress.

    The flag is set on the freshly-created Agent row; this is what Plan 08-01's
    guards read to decide whether to fire the auto-promote.
    """
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    board = Board(id=board_id, name="Test Board", slug=f"test-{board_id.hex[:8]}")
    session.add(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=agent_id,
        name="TestDeployer" if role == "deployer" else "TestCody",
        board_id=board_id,
        agent_token_hash=token_hash,
        is_board_lead=False,
        role=None,  # role validator requires AgentRole enum value; keep None to avoid coupling
        scopes=["tasks:read", "tasks:write", "tasks:create"],
        auto_promote_on_resolution=auto_promote_on_resolution,
    )
    session.add(agent)

    task = Task(
        id=task_id,
        board_id=board_id,
        title="Deploy to staging",
        status="in_progress",
        assigned_agent_id=agent_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(board)
    await session.refresh(agent)
    await session.refresh(task)

    return board, agent, task, raw_token


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deployer_resolution_does_not_auto_promote_path_a(client, fake_redis):
    """Path A guard: deployer (auto_promote_on_resolution=False) posts a
    resolution comment via POST /comments -> task MUST stay in_progress."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_deployer_test_data(
            s, auto_promote_on_resolution=False, role="deployer"
        )

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_handoff:
                resp = await client.post(
                    f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
                    json={
                        "content": "Deploy auf staging fertig, starte Verifikation",
                        "comment_type": "resolution",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )

    assert resp.status_code == 201, resp.text

    # Task MUST stay in_progress — the auto_promote_on_resolution=False guard fired
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task

        updated_task = await s.get(Task, task.id)
        assert updated_task.status == "in_progress", (
            f"Deployer with auto_promote_on_resolution=False got promoted to "
            f"{updated_task.status}; expected in_progress"
        )

    # Review-handoff must NOT have been triggered
    mock_handoff.assert_not_called()


@pytest.mark.asyncio
async def test_deployer_resolution_does_not_auto_promote_path_b(fake_redis):
    """Path B guard: deployer (auto_promote_on_resolution=False) has last
    comment of type 'resolution' on an in_progress task; running the
    stale-check loop MUST NOT promote -- the guard fires before the resolution
    branch in task_runner.py:771."""
    from app.models.task import Task, TaskComment
    from app.services.task_runner import TaskRunnerService as TaskRunner
    from app.utils import utcnow

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, _ = await _create_deployer_test_data(
            s, auto_promote_on_resolution=False, role="deployer"
        )
        # Last comment = resolution (the trigger condition for Path B)
        comment = TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=agent.id,
            comment_type="resolution",
            content="Deploy fertig, jetzt Verifikation",
            created_at=utcnow(),
        )
        s.add(comment)
        await s.commit()

    # Run a single stale-check pass
    runner = TaskRunner()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_runner.emit_event", new_callable=AsyncMock):
            await runner._check_stale_in_progress(s)

    # Task MUST stay in_progress
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated_task = await s.get(Task, task.id)
        assert updated_task.status == "in_progress", (
            f"Stale-check Path B promoted deployer task to "
            f"{updated_task.status}; expected in_progress"
        )


@pytest.mark.asyncio
async def test_cody_resolution_still_auto_promotes(client, fake_redis):
    """Preservation guard: Cody (auto_promote_on_resolution=True -- the default)
    posts a resolution comment -> task MUST be promoted to review.

    This test exists so an inverted-boolean regression in Plan 08-01
    (`if not agent.auto_promote_on_resolution:` instead of `if agent.auto_promote_on_resolution:`)
    is caught immediately. Mirrors test_resolution_auto_promote.py:65 but
    explicitly asserts the True-branch with the new flag."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_deployer_test_data(
            s, auto_promote_on_resolution=True, role="developer"
        )

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_handoff:
                resp = await client.post(
                    f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
                    json={"content": "Done", "comment_type": "resolution"},
                    headers={"Authorization": f"Bearer {token}"},
                )

    assert resp.status_code == 201, resp.text

    # Task MUST be promoted to review (auto_promote_on_resolution=True branch)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task

        updated_task = await s.get(Task, task.id)
        assert updated_task.status == "review", (
            f"Cody with auto_promote_on_resolution=True did NOT get promoted; "
            f"got {updated_task.status}; expected review (preservation regression!)"
        )

    mock_handoff.assert_called_once()
