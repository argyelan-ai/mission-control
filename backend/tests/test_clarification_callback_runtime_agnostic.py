"""Tests fuer Clarification-Callback via TaskComment (runtime-agnostic).

Bug-Repro 2026-04-24: Der Operator beantwortete Boss's Klaerungsfrage zum stitch-skills
Install. Approval → approved + resolver_note. Task → in_progress. Aber:
- rpc.chat_send_isolated checkt agent.gateway_agent_id
- Boss ist agent_runtime='host' → keine gateway_agent_id → Callback skipped
- task_comments Tabelle blieb leer, Boss erfuhr die Antwort nie

Fix: IMMER TaskComment mit der Antwort posten (comment_type=resolution).
poll.sh deliver_comments liefert das im naechsten Cycle an Boss — funktioniert
fuer host, cli-bridge und openclaw gleichermassen.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _make_clarification_setup(*, agent_runtime: str = "host", has_gateway_id: bool = False):
    """Board + Agent + Task (blocked) + Clarification-Approval."""
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
    """Boss (host-runtime, keine gateway_agent_id) bekommt Antwort via TaskComment."""
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

    # TaskComment mit der Antwort sollte existieren
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
        assert "Wie soll ich die stitch-skills installieren" in c.content  # Frage zitiert
        assert c.author_type == "user"


@pytest.mark.asyncio
async def test_clarification_resolved_also_posts_comment_for_cli_bridge(
    auth_client: AsyncClient, fake_redis,
):
    """cli-bridge Worker ohne gateway_agent_id bekommt Antwort via TaskComment."""
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
    """Nach Approval ist der Task status=in_progress (nicht mehr blocked)."""
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
    """Openclaw-Gateway-Agent bekommt TaskComment wie alle anderen Runtimes
    (belt-and-braces). Zusaetzlich wird rpc.chat_send_isolated via fire-and-
    forget getriggert — das testen wir hier nicht (asyncio.create_task Timing)."""
    agent, task, approval, board_id = await _make_clarification_setup(
        agent_runtime="openclaw", has_gateway_id=True,
    )

    resp = await auth_client.patch(
        f"/api/v1/approvals/{approval.id}",
        json={"status": "approved", "resolver_note": "live-antwort"},
    )
    assert resp.status_code == 200

    # Comment sollte auch fuer openclaw-Agents da sein (der garantierte Pfad,
    # RPC ist nur best-effort live-delivery zusaetzlich)
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
    """Bei status=rejected → kein Resolution-Comment (der Operator hat nicht geantwortet,
    nur abgelehnt). Task bleibt moeglicherweise blocked (andere Logik)."""
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
