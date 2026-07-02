"""Tests fuer Resolution Auto-Promote: Wenn Agent resolution-Kommentar schreibt
und Task noch in_progress → automatisch auf review setzen.

Repro-Case fuer Bug: Agent schreibt "Task abgeschlossen" aber vergisst PATCH status: review.
Task bleibt auf in_progress haengen, obwohl die Arbeit fertig ist.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Helpers ──────────────────────────────────────────────────────────────

async def _create_test_data(session, *, task_status="in_progress", agent_is_lead=False):
    """Board + Agent + Task erstellen. Gibt (board, agent, task, token) zurueck."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    board = Board(id=board_id, name="Test Board", slug="test")
    session.add(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=agent_id,
        name="Cody",
        board_id=board_id,
        agent_token_hash=token_hash,
        is_board_lead=agent_is_lead,
        scopes=["tasks:read", "tasks:write", "tasks:create"],
    )
    session.add(agent)

    task = Task(
        id=task_id,
        board_id=board_id,
        title="Implement feature X",
        status=task_status,
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
async def test_resolution_comment_promotes_in_progress_to_review(client, fake_redis):
    """Repro-Case: Agent schreibt resolution-Kommentar auf in_progress Task → review."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s, task_status="in_progress")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_handoff:
                resp = await client.post(
                    f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
                    json={"content": "**Update** — Task abgeschlossen", "comment_type": "resolution"},
                    headers={"Authorization": f"Bearer {token}"},
                )

    assert resp.status_code == 201, resp.text

    # Task muss jetzt auf review stehen
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated_task = await s.get(Task, task.id)
        assert updated_task.status == "review", f"Expected review, got {updated_task.status}"

    # Review-Handoff muss ausgeloest worden sein
    mock_handoff.assert_called_once()


@pytest.mark.asyncio
async def test_resolution_comment_does_not_affect_review_status(client, fake_redis):
    """Task auf review bleibt review — kein Doppel-Promote."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s, task_status="review")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
            json={"content": "Review-Notiz", "comment_type": "resolution"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated_task = await s.get(Task, task.id)
        assert updated_task.status == "review"


@pytest.mark.asyncio
async def test_resolution_comment_does_not_affect_done_status(client, fake_redis):
    """Task auf done bleibt done."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s, task_status="done")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
            json={"content": "Nachtrag", "comment_type": "resolution"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated_task = await s.get(Task, task.id)
        assert updated_task.status == "done"


@pytest.mark.asyncio
async def test_resolution_comment_does_not_affect_blocked_status(client, fake_redis):
    """Task auf blocked bleibt blocked."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s, task_status="blocked")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
            json={"content": "Blocked resolution", "comment_type": "resolution"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated_task = await s.get(Task, task.id)
        assert updated_task.status == "blocked"


@pytest.mark.asyncio
async def test_non_resolution_comment_does_not_promote(client, fake_redis):
    """Normaler progress-Kommentar aendert Status nicht."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s, task_status="in_progress")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
            json={"content": "Fortschritt: 50% fertig", "comment_type": "progress"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated_task = await s.get(Task, task.id)
        assert updated_task.status == "in_progress"


@pytest.mark.asyncio
async def test_resolution_from_unassigned_agent_does_not_promote(client, fake_redis):
    """Agent der nicht zugewiesen und nicht Board-Lead ist → kein Promote."""
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, assigned_agent, task, _ = await _create_test_data(s, task_status="in_progress")

        # Zweiten Agent erstellen (nicht zugewiesen, nicht Lead)
        raw_token2, token_hash2 = generate_agent_token()
        other_agent = Agent(
            id=uuid.uuid4(),
            name="Rex",
            board_id=board.id,
            agent_token_hash=token_hash2,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(other_agent)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
            json={"content": "Resolution von anderem Agent", "comment_type": "resolution"},
            headers={"Authorization": f"Bearer {raw_token2}"},
        )

    assert resp.status_code == 201

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated_task = await s.get(Task, task.id)
        assert updated_task.status == "in_progress", "Unassigned agent should not promote"


@pytest.mark.asyncio
async def test_resolution_from_board_lead_promotes(client, fake_redis):
    """Board Lead darf fremde Tasks auto-promoten via resolution-Kommentar."""
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, assigned_agent, task, _ = await _create_test_data(s, task_status="in_progress")

        # Board Lead erstellen
        raw_token_lead, token_hash_lead = generate_agent_token()
        lead = Agent(
            id=uuid.uuid4(),
            name="Henry",
            board_id=board.id,
            agent_token_hash=token_hash_lead,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(lead)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock):
                resp = await client.post(
                    f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
                    json={"content": "Task erledigt, promote", "comment_type": "resolution"},
                    headers={"Authorization": f"Bearer {raw_token_lead}"},
                )

    assert resp.status_code == 201

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated_task = await s.get(Task, task.id)
        assert updated_task.status == "review", "Board Lead should be able to promote"
