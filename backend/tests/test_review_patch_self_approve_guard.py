"""M3 (Fix 2, W2-A): close the review→done self-approve bypass.

execute_review_decision() (task_lifecycle.py, called from POST
.../tasks/{id}/review) has a self-review guard: the agent that did the
IMPLEMENTATION work on a task may not approve its own review. But the
generic PATCH .../tasks/{id} endpoint (agent_task_status.py:
agent_update_task) has a "Fallback: automatically set review_decision"
block that sets review_decision=approved on ANY review→done PATCH,
completely bypassing that guard — an agent could self-approve its own
work just by using PATCH instead of the dedicated review endpoint.

These tests exercise the guard through the real router (client.patch),
not execute_review_decision directly (that's already covered by
test_review_decision.py / test_self_review_fallback.py).
"""
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _make_agent_with_token(
    *, name: str, board_id, is_board_lead: bool = False, role: str = "developer",
):
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=uuid.uuid4(),
        name=name,
        role=role,
        board_id=board_id,
        agent_token_hash=token_hash,
        is_board_lead=is_board_lead,
        scopes=["tasks:read", "tasks:write"],
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
    return agent, raw_token


def _agent_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _record_worker_transition(task_id, agent_id):
    """Simulate the agent having moved the task in_progress → review itself
    (i.e. it did the implementation work), the same TaskEvent shape
    record_task_event() would have written."""
    from app.models.task import TaskEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        event = TaskEvent(
            id=uuid.uuid4(),
            task_id=task_id,
            from_status="in_progress",
            to_status="review",
            changed_by="agent",
            agent_id=agent_id,
            created_at=datetime.utcnow(),
        )
        s.add(event)
        await s.commit()


@pytest.mark.asyncio
async def test_assignee_cannot_patch_own_review_to_done(
    client, fake_redis, make_board, make_task,
):
    """The assignee that did the work PATCHes its own task review→done
    directly (not via POST /review) → must be rejected, not silently
    self-approved."""
    board = await make_board(slug="mc-dev-self-approve")
    cody, cody_token = await _make_agent_with_token(
        name="Cody", board_id=board.id, is_board_lead=False,
    )
    task = await make_task(
        board_id=board.id, status="review", assigned_agent_id=cody.id,
    )
    await _record_worker_transition(task.id, cody.id)

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        response = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "done"},
            headers=_agent_headers(cody_token),
        )
    assert response.status_code in (403, 409), (
        f"Self-approve via generic PATCH must be blocked. "
        f"Got: {response.status_code} {response.text[:300]}"
    )
    detail = response.json().get("detail", "")
    assert "review" in detail.lower() or "approve" in detail.lower(), (
        f"Expected a message pointing to the review flow. Got: {detail[:300]}"
    )

    # Confirm the task was NOT silently approved.
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
    assert refreshed.status == "review", "Task must remain in review, not silently done"
    assert refreshed.review_decision is None, "review_decision must not be set by the blocked PATCH"


@pytest.mark.asyncio
async def test_different_reviewer_can_still_patch_review_to_done(
    client, fake_redis, make_board, make_task,
):
    """A genuinely different reviewer agent (didn't do the implementation
    work) PATCHing someone else's task review→done via the generic PATCH
    endpoint must still work — the guard must not overreach."""
    board = await make_board(slug="mc-dev-reviewer-ok")
    cody, _ = await _make_agent_with_token(
        name="Cody", board_id=board.id, is_board_lead=False,
    )
    rex, rex_token = await _make_agent_with_token(
        name="Rex", board_id=board.id, is_board_lead=False, role="reviewer",
    )
    task = await make_task(
        board_id=board.id, status="review", assigned_agent_id=cody.id,
    )
    # Cody did the work (worker transition), Rex never touched the task
    # before reviewing it — a clean, legitimate review.
    await _record_worker_transition(task.id, cody.id)

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        response = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "done"},
            headers=_agent_headers(rex_token),
        )
    assert response.status_code in (200, 201), (
        f"A different reviewer must still be able to PATCH review→done. "
        f"Got: {response.status_code} {response.text[:300]}"
    )


# ── A-2 (adversarial review): role-independent review-cycle exemption ───


async def _record_transitions(task_id, transitions):
    """Insert a chronological TaskEvent chain: [(agent_id, from_s, to_s), ...]."""
    from app.models.task import TaskEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for agent_id, from_s, to_s in transitions:
            s.add(TaskEvent(
                id=uuid.uuid4(),
                task_id=task_id,
                from_status=from_s,
                to_status=to_s,
                changed_by="agent",
                agent_id=agent_id,
                created_at=datetime.utcnow(),
            ))
        await s.commit()


@pytest.mark.asyncio
async def test_developer_role_pure_reviewer_can_approve(
    client, fake_redis, make_board, make_task,
):
    """A-2: an agent with role='developer' handed a task PURELY to review
    (its only transitions are the review cycle: review→in_progress ACK,
    then in_progress→review hand-back) must be able to approve via the
    generic PATCH. The old classifier exempted review-shaped transitions
    only for role=='reviewer', so this legitimate reviewer was
    misclassified as worker and 409-blocked."""
    board = await make_board(slug="mc-dev-devrole-reviewer")
    cody, _ = await _make_agent_with_token(
        name="Cody", board_id=board.id, is_board_lead=False,
    )
    dana, dana_token = await _make_agent_with_token(
        name="Dana", board_id=board.id, is_board_lead=False, role="developer",
    )
    task = await make_task(
        board_id=board.id, status="review", assigned_agent_id=dana.id,
    )
    # Cody implemented; Dana (developer role!) only ever did the review
    # cycle: entered FROM review (ACK), handed back TO review.
    await _record_transitions(task.id, [
        (cody.id, "inbox", "in_progress"),
        (cody.id, "in_progress", "review"),
        (dana.id, "review", "in_progress"),
        (dana.id, "in_progress", "review"),
    ])

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        response = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "done"},
            headers=_agent_headers(dana_token),
        )
    assert response.status_code in (200, 201), (
        f"A developer-role agent that only did review work must be able to "
        f"approve. Got: {response.status_code} {response.text[:300]}"
    )


@pytest.mark.asyncio
async def test_worker_with_review_reject_reentry_still_blocked(
    client, fake_redis, make_board, make_task,
):
    """A-2 regression guard (the tricky case): a WORKER re-entering after a
    review-reject transitions review→in_progress exactly like a reviewer
    ACK. But because it ALSO has non-review-shaped work transitions
    (inbox→in_progress ACK) it must stay classified as worker — the
    review-shaped re-entry must not launder its worker status."""
    board = await make_board(slug="mc-dev-reject-reentry")
    cody, cody_token = await _make_agent_with_token(
        name="Cody", board_id=board.id, is_board_lead=False,
    )
    task = await make_task(
        board_id=board.id, status="review", assigned_agent_id=cody.id,
    )
    # Full worker lifecycle incl. review-reject re-entry:
    # ACK, handoff, reject re-entry (review-shaped!), second handoff.
    await _record_transitions(task.id, [
        (cody.id, "inbox", "in_progress"),
        (cody.id, "in_progress", "review"),
        (cody.id, "review", "in_progress"),
        (cody.id, "in_progress", "review"),
    ])

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        response = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "done"},
            headers=_agent_headers(cody_token),
        )
    assert response.status_code in (403, 409), (
        f"Worker with reject re-entry must still be blocked from "
        f"self-approving. Got: {response.status_code} {response.text[:300]}"
    )
