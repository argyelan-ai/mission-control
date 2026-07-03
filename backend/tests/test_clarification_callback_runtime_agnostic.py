"""Tests for clarification callback via TaskComment (runtime-agnostic).

Bug repro 2026-04-24: the operator answered Boss's clarification question about
the stitch-skills install. Approval → approved + resolver_note. Task → in_progress. But:
- rpc.chat_send_isolated checks agent.gateway_agent_id
- Boss is agent_runtime='host' → no gateway_agent_id → callback skipped
- task_comments table stayed empty, Boss never learned the answer

Fix: ALWAYS post a TaskComment with the answer (comment_type=resolution).
poll.sh deliver_comments delivers it to Boss on the next cycle — works
the same for host, cli-bridge, and openclaw.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _make_clarification_setup(*, agent_runtime: str = "host", has_gateway_id: bool = False):
    """Board + agent + task (blocked) + clarification approval."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.approval import Approval
    from app.models.board import Board
    from app.models.task import Task

    _, th = generate_agent_token()
    board_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="TestBoard", slug=f"clar-{uuid.uuid4().hex[:6]}"))
        await s.commit()

        agent = Agent(
            id=uuid.uuid4(),
            name="BossClar", role="orchestrator", is_board_lead=True,
            board_id=board_id, agent_runtime=agent_runtime,
            agent_token_hash=th, model="x", provision_status="provisioned",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)

        task = Task(
            board_id=board_id,
            title="Task mit offener Frage",
            description="Blocked auf clarification",
            status="blocked",
            assigned_agent_id=agent.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)

        approval = Approval(
            board_id=board_id, agent_id=agent.id, task_id=task.id,
            action_type="clarification_question",
            description="Klaerungsfrage von Boss: Wie weiter?",
            payload={
                "question": "Wie soll ich die stitch-skills installieren? Allowlist erweitern?",
                "options": ["A", "B", "C"],
            },
            status="pending",
        )
        s.add(approval)
        await s.commit()
        await s.refresh(approval)

    return agent, task, approval, board_id


@pytest.mark.asyncio
async def test_clarification_resolved_posts_comment_for_host_runtime(
    auth_client: AsyncClient, fake_redis,
):
    """Boss (host runtime, no gateway_agent_id) gets the answer via TaskComment."""
    agent, task, approval, board_id = await _make_clarification_setup(
        agent_runtime="host", has_gateway_id=False,
    )

    resp = await auth_client.patch(
        f"/api/v1/approvals/{approval.id}",
        json={
            "status": "approved",
            "resolver_note": "A) Allowlist erweitern — Sparky macht PR",
        },
    )
    assert resp.status_code == 200, resp.text

    # TaskComment with the answer should exist
    from app.models.task import TaskComment
    from sqlmodel import select
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        resolution_comments = [c for c in comments if c.comment_type == "resolution"]
        assert len(resolution_comments) == 1, (
            f"Expected 1 resolution comment (the operator's answer), got {len(resolution_comments)}"
        )
        c = resolution_comments[0]
        assert "Allowlist erweitern" in c.content
        assert "Wie soll ich die stitch-skills installieren" in c.content  # question quoted
        assert c.author_type == "user"


@pytest.mark.asyncio
async def test_clarification_resolved_also_posts_comment_for_cli_bridge(
    auth_client: AsyncClient, fake_redis,
):
    """cli-bridge worker without gateway_agent_id gets the answer via TaskComment."""
    agent, task, approval, board_id = await _make_clarification_setup(
        agent_runtime="cli-bridge", has_gateway_id=False,
    )

    resp = await auth_client.patch(
        f"/api/v1/approvals/{approval.id}",
        json={"status": "approved", "resolver_note": "Option B"},
    )
    assert resp.status_code == 200

    from app.models.task import TaskComment
    from sqlmodel import select
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (await s.exec(
            select(TaskComment).where(
                TaskComment.task_id == task.id,
                TaskComment.comment_type == "resolution",
            )
        )).all()
        assert len(comments) == 1
        assert "Option B" in comments[0].content


@pytest.mark.asyncio
async def test_clarification_task_goes_to_in_progress(
    auth_client: AsyncClient, fake_redis,
):
    """After approval the task status is in_progress (no longer blocked)."""
    agent, task, approval, board_id = await _make_clarification_setup(
        agent_runtime="host", has_gateway_id=False,
    )

    await auth_client.patch(
        f"/api/v1/approvals/{approval.id}",
        json={"status": "approved", "resolver_note": "ja"},
    )

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
        assert fresh.status == "in_progress"


@pytest.mark.asyncio
async def test_clarification_comment_also_for_openclaw_gateway_agent(
    auth_client: AsyncClient, fake_redis,
):
    """Openclaw gateway agent gets a TaskComment like all other runtimes
    (belt-and-braces). Additionally rpc.chat_send_isolated is triggered via
    fire-and-forget — we don't test that here (asyncio.create_task timing)."""
    agent, task, approval, board_id = await _make_clarification_setup(
        agent_runtime="openclaw", has_gateway_id=True,
    )

    resp = await auth_client.patch(
        f"/api/v1/approvals/{approval.id}",
        json={"status": "approved", "resolver_note": "live-antwort"},
    )
    assert resp.status_code == 200

    # Comment should also exist for openclaw agents (the guaranteed path,
    # RPC is only an additional best-effort live delivery)
    from app.models.task import TaskComment
    from sqlmodel import select
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (await s.exec(
            select(TaskComment).where(
                TaskComment.task_id == task.id,
                TaskComment.comment_type == "resolution",
            )
        )).all()
        assert len(comments) == 1
        assert "live-antwort" in comments[0].content


@pytest.mark.asyncio
async def test_clarification_rejected_no_comment(auth_client: AsyncClient, fake_redis):
    """With status=rejected → no resolution comment (the operator didn't answer,
    just rejected). Task may remain blocked (different logic)."""
    agent, task, approval, board_id = await _make_clarification_setup(
        agent_runtime="host", has_gateway_id=False,
    )

    resp = await auth_client.patch(
        f"/api/v1/approvals/{approval.id}",
        json={"status": "rejected", "resolver_note": "nope"},
    )
    assert resp.status_code == 200

    from app.models.task import TaskComment
    from sqlmodel import select
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (await s.exec(
            select(TaskComment).where(
                TaskComment.task_id == task.id,
                TaskComment.comment_type == "resolution",
            )
        )).all()
        assert len(comments) == 0, "Kein Comment bei rejected"
