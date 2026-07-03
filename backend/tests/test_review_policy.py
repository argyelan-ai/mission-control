"""Per-project review policy tests (T-1 Phase H)."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


_REFLECTION_BODY = (
    "## Was wurde gemacht\nReview-Policy Bypass-Test\n\n"
    "## Was hat funktioniert\nPolicy override greift\n\n"
    "## Was war unklar\nNichts\n\n"
    "## Lesson fuer Agent-Memory\n"
    "ADR-023: Reflexion ist unabhaengig vom Review-Policy-Override."
)


async def _post_reflection(client, agent_headers, board_id, task_id):
    """ADR-023: reflection is independent of the review policy override."""
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        headers=agent_headers,
        json={"content": _REFLECTION_BODY, "comment_type": "reflection"},
    )
    assert resp.status_code in (200, 201), resp.json()


async def _setup_review_policy_scenario(
    require_review: bool = True,
    review_policy=None,
):
    """Create board + agent + project + task."""
    from app.models.board import Board, Project
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    project_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(
            id=board_id,
            name=f"Review Board {uuid.uuid4().hex[:4]}",
            slug=f"rb-{uuid.uuid4().hex[:6]}",
            require_review_before_done=require_review,
        )
        s.add(board)

        project = Project(
            id=project_id,
            board_id=board_id,
            name="Test Project",
            project_config={"review_policy": review_policy} if review_policy else None,
        )
        s.add(project)

        raw_token, token_hash = generate_agent_token()
        agent = Agent(
            id=agent_id,
            name="Cody",
            role="developer",
            board_id=board_id,
            agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(agent)

        task = Task(
            id=task_id,
            board_id=board_id,
            project_id=project_id,
            title="Test Task",
            status="in_progress",
            assigned_agent_id=agent_id,
        )
        s.add(task)
        await s.commit()

    return {
        "board_id": board_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "project_id": project_id,
        "agent_token": raw_token,
    }


@pytest.mark.asyncio
async def test_project_review_policy_never_bypasses_board_review_rule(client):
    """If project review_policy=never → done is possible directly, even if board require_review=True."""
    with patch(
        "app.services.work_context.validate_task_completion",
        new=AsyncMock(return_value=(True, [])),
    ), patch("app.services.activity.broadcast", new_callable=AsyncMock):
        ids = await _setup_review_policy_scenario(
            require_review=True,
            review_policy="never",
        )
        agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}
        board_id = ids["board_id"]
        task_id = ids["task_id"]

        # Post reflection (ADR-023 — independent of the review policy override)
        await _post_reflection(client, agent_headers, board_id, task_id)
        # Set directly to done (no review needed thanks to policy=never)
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            headers=agent_headers,
            json={"status": "done"},
        )
        assert resp.status_code == 200, resp.json()


@pytest.mark.asyncio
async def test_project_without_review_policy_uses_board_default(client):
    """Without project_config.review_policy, the board default applies."""
    with patch(
        "app.services.work_context.validate_task_completion",
        new=AsyncMock(return_value=(True, [])),
    ), patch("app.services.activity.broadcast", new_callable=AsyncMock):
        ids = await _setup_review_policy_scenario(
            require_review=True,
            review_policy=None,  # no override
        )
        agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}
        board_id = ids["board_id"]
        task_id = ids["task_id"]

        # done without review → should be blocked (board default applies)
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            headers=agent_headers,
            json={"status": "done"},
        )
        # Should be 400 (review required)
        assert resp.status_code == 400, resp.json()


@pytest.mark.asyncio
async def test_task_skip_review_bypasses_board_review_rule(client):
    """Task with skip_review=True can be set directly to done, even if board require_review=True."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(
            id=board_id,
            name=f"Skip Review Board {uuid.uuid4().hex[:4]}",
            slug=f"srb-{uuid.uuid4().hex[:6]}",
            require_review_before_done=True,
        )
        s.add(board)

        raw_token, token_hash = generate_agent_token()
        agent = Agent(
            id=agent_id,
            name="Sparky",
            role="developer",
            board_id=board_id,
            agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(agent)

        task = Task(
            id=task_id,
            board_id=board_id,
            title="AI Tech Digest",
            status="in_progress",
            assigned_agent_id=agent_id,
            skip_review=True,
        )
        s.add(task)
        await s.commit()

    with patch(
        "app.services.work_context.validate_task_completion",
        new=AsyncMock(return_value=(True, [])),
    ), patch("app.services.activity.broadcast", new_callable=AsyncMock):
        # Post reflection (ADR-023 — independent of skip_review)
        await _post_reflection(
            client,
            {"Authorization": f"Bearer {raw_token}"},
            board_id,
            task_id,
        )
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {raw_token}"},
            json={"status": "done"},
        )
        assert resp.status_code == 200, resp.json()
