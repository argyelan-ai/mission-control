"""Tests: Auto-Promote bei resolution-Kommentar respektiert Subtask-Exemption.

Subtasks gehen bei resolution direkt auf done (Review laeuft auf Phase-Ebene).
Root-Tasks gehen weiterhin auf review.
dispatch_attempt_id wird in beiden Faellen resettet.
"""

import uuid

import pytest
from unittest.mock import patch, AsyncMock
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Helpers ──────────────────────────────────────────────────────────────

async def _create_test_data(
    session,
    *,
    task_status="in_progress",
    parent_task_id=None,
    dispatch_attempt_id=None,
):
    """Board + Agent + Task erstellen. Gibt (board, agent, task, token) zurueck."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    board = Board(id=board_id, name="Test Board", slug=f"test-{uuid.uuid4().hex[:8]}")
    session.add(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=agent_id,
        name="Cody",
        board_id=board_id,
        agent_token_hash=token_hash,
        is_board_lead=False,
        scopes=["tasks:read", "tasks:write", "tasks:create"],
    )
    session.add(agent)

    # Parent-Task erstellen falls parent_task_id gewuenscht
    parent = None
    if parent_task_id is True:
        parent = Task(
            id=uuid.uuid4(),
            board_id=board_id,
            title="Phase 1",
            status="in_progress",
        )
        session.add(parent)
        await session.flush()
        parent_task_id = parent.id

    task = Task(
        id=uuid.uuid4(),
        board_id=board_id,
        title="Implement feature X",
        status=task_status,
        assigned_agent_id=agent_id,
        parent_task_id=parent_task_id,
        dispatch_attempt_id=dispatch_attempt_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(board)
    await session.refresh(agent)
    await session.refresh(task)

    return board, agent, task, raw_token


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subtask_resolution_promotes_to_done(client, fake_redis):
    """Subtask mit resolution-Kommentar → status wird done (nicht review)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(
            s, task_status="in_progress", parent_task_id=True,
        )

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_handoff:
                resp = await client.post(
                    f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
                    json={"content": "Task abgeschlossen", "comment_type": "resolution"},
                    headers={"Authorization": f"Bearer {token}"},
                )

    assert resp.status_code == 201, resp.text

    # Task muss auf done stehen (Subtask-Exemption)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, task.id)
        assert updated.status == "done", f"Expected done, got {updated.status}"
        assert updated.completed_at is not None, "completed_at muss gesetzt sein"

    # Review-Handoff darf NICHT aufgerufen worden sein
    mock_handoff.assert_not_called()


@pytest.mark.asyncio
async def test_root_task_resolution_promotes_to_review(client, fake_redis):
    """Root-Task mit resolution-Kommentar → status wird review (wie bisher)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(
            s, task_status="in_progress", parent_task_id=None,
        )

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_handoff:
                resp = await client.post(
                    f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
                    json={"content": "Feature fertig", "comment_type": "resolution"},
                    headers={"Authorization": f"Bearer {token}"},
                )

    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, task.id)
        assert updated.status == "review", f"Expected review, got {updated.status}"

    # Review-Handoff muss aufgerufen worden sein
    mock_handoff.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_attempt_id_reset_on_subtask_promote(client, fake_redis):
    """dispatch_attempt_id wird bei Subtask Auto-Promote resettet."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(
            s,
            task_status="in_progress",
            parent_task_id=True,
            dispatch_attempt_id="old-attempt-123",
        )
        assert task.dispatch_attempt_id == "old-attempt-123"

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock):
                resp = await client.post(
                    f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
                    json={"content": "Erledigt", "comment_type": "resolution"},
                    headers={"Authorization": f"Bearer {token}"},
                )

    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, task.id)
        assert updated.dispatch_attempt_id is None, "dispatch_attempt_id muss resettet sein"
        assert updated.status == "done"


@pytest.mark.asyncio
async def test_dispatch_attempt_id_reset_on_root_promote(client, fake_redis):
    """dispatch_attempt_id wird bei Root-Task Auto-Promote resettet."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(
            s,
            task_status="in_progress",
            parent_task_id=None,
            dispatch_attempt_id="old-attempt-456",
        )

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock):
                resp = await client.post(
                    f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
                    json={"content": "Fertig", "comment_type": "resolution"},
                    headers={"Authorization": f"Bearer {token}"},
                )

    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, task.id)
        assert updated.dispatch_attempt_id is None, "dispatch_attempt_id muss resettet sein"
        assert updated.status == "review"
