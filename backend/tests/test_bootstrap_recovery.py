"""Bootstrap-triggered recovery recap.

Regression guard: the container entrypoint hits GET /api/v1/internal/bootstrap
on EVERY process start (crash-restart or manual `docker compose up`). If the
agent still "owns" an in_progress task when it comes back up, MC must
auto-post a recovery_recap TaskComment so the agent's poll-loop picks up
where it left off — instead of relying on a human to manually nudge it, or
waiting for the 60-min-staleness-gated task_runner Tier-3 recovery.

See app.routers.internal._maybe_post_bootstrap_recovery_recap.
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.secret import Secret
from app.models.task import Task, TaskComment
from app.services.encryption import encrypt
from tests.conftest import test_engine


async def _seed_agent_with_task(
    s: AsyncSession,
    *,
    task_status: str = "in_progress",
    with_progress_comment: bool = True,
) -> tuple[Agent, Task]:
    """Board + task + agent wired via agent.current_task_id, plus a vault
    secret so bootstrap doesn't 404 on an empty tokens dict."""
    board = Board(id=uuid.uuid4(), name="Test Board", slug=f"test-{uuid.uuid4().hex[:8]}")
    s.add(board)
    await s.commit()

    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Interrupted Task",
        status=task_status,
    )
    s.add(task)
    await s.commit()
    await s.refresh(task)

    agent = Agent(
        id=uuid.uuid4(),
        name=f"Freecode-{uuid.uuid4().hex[:6]}",
        role="developer",
        agent_runtime="cli-bridge",
        current_task_id=task.id,
    )
    s.add(agent)
    s.add(Secret(
        key="ollama_api_key",
        encrypted_value=encrypt("k-test"),
        provider="ollama",
    ))

    if with_progress_comment:
        # build_recovery_context() returns None without comments/checklist
        # items — seed one so the recap has content to post.
        s.add(TaskComment(
            task_id=task.id,
            author_type="agent",
            comment_type="progress",
            content="Finished the schema migration, about to wire the endpoint.",
        ))

    await s.commit()
    await s.refresh(agent)
    await s.refresh(task)
    return agent, task


async def _count_recovery_recap_comments(s: AsyncSession, task_id: uuid.UUID) -> int:
    from sqlmodel import select
    result = await s.exec(
        select(TaskComment).where(
            TaskComment.task_id == task_id,
            TaskComment.comment_type == "recovery_recap",
        )
    )
    return len(list(result.all()))


@pytest.mark.asyncio
async def test_bootstrap_posts_recovery_recap_for_in_progress_task(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task = await _seed_agent_with_task(s)

    resp = await client.get(f"/api/v1/internal/bootstrap?agent_name={agent.name}")
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        count = await _count_recovery_recap_comments(s, task.id)
    assert count == 1


@pytest.mark.asyncio
async def test_bootstrap_recovery_recap_is_idempotent(client: AsyncClient):
    """Repeated bootstraps (crash-loop) only create ONE recovery_recap comment."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task = await _seed_agent_with_task(s)

    resp1 = await client.get(f"/api/v1/internal/bootstrap?agent_name={agent.name}")
    assert resp1.status_code == 200
    resp2 = await client.get(f"/api/v1/internal/bootstrap?agent_name={agent.name}")
    assert resp2.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        count = await _count_recovery_recap_comments(s, task.id)
    assert count == 1


@pytest.mark.asyncio
async def test_bootstrap_noop_when_no_current_task(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name=f"Freecode-{uuid.uuid4().hex[:6]}",
            role="developer",
            agent_runtime="cli-bridge",
            current_task_id=None,
        )
        s.add(agent)
        s.add(Secret(
            key="ollama_api_key",
            encrypted_value=encrypt("k-test"),
            provider="ollama",
        ))
        await s.commit()

    resp = await client.get(f"/api/v1/internal/bootstrap?agent_name={agent.name}")
    assert resp.status_code == 200, resp.text
    # Nothing to assert against (no task) — just confirms no crash/side effect.


@pytest.mark.asyncio
async def test_bootstrap_noop_when_task_not_in_progress(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task = await _seed_agent_with_task(s, task_status="review")

    resp = await client.get(f"/api/v1/internal/bootstrap?agent_name={agent.name}")
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        count = await _count_recovery_recap_comments(s, task.id)
    assert count == 0


@pytest.mark.asyncio
async def test_bootstrap_noop_when_recap_would_be_empty(client: AsyncClient):
    """in_progress task but build_recovery_context() has nothing to say
    (no comments, no checklist items) → no comment posted."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task = await _seed_agent_with_task(s, with_progress_comment=False)

    resp = await client.get(f"/api/v1/internal/bootstrap?agent_name={agent.name}")
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        count = await _count_recovery_recap_comments(s, task.id)
    assert count == 0


@pytest.mark.asyncio
async def test_bootstrap_token_response_unchanged_by_recovery_step(client: AsyncClient):
    """Regression: normal token-delivery response shape is untouched by the
    recovery-recap addition — GH_TOKEN still delivered as before."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, task = await _seed_agent_with_task(s)
        s.add(Secret(
            key="github_token",
            encrypted_value=encrypt("gho_test_token_abcdef"),
            provider="github",
        ))
        await s.commit()

    resp = await client.get(f"/api/v1/internal/bootstrap?agent_name={agent.name}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("GH_TOKEN") == "gho_test_token_abcdef"
    assert "CONTEXT_MAX" in body
