"""Tests: Self-Review Fallback an Board Lead."""
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine
from app.models.task import Task, TaskEvent
from app.models.agent import Agent


# ── Test 1: Self-Review eskaliert an Board Lead ─────────────────────


@pytest.mark.asyncio
async def test_self_review_escalates_to_board_lead(make_board, make_agent, make_task):
    """Wenn Worker versucht zu approven → Task an Board Lead re-assigned."""
    board = await make_board(name="Escalation Board", slug="escalate-board")
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)
    henry = await make_agent(name="Henry", is_board_lead=True, board_id=board.id)
    task_obj = await make_task(
        board_id=board.id, title="Fact-Check",
        status="review",
        assigned_agent_id=rex.id,
    )

    # Rex war Worker (task_event zeigt in_progress Transition)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        event = TaskEvent(
            id=uuid.uuid4(),
            task_id=task_obj.id,
            from_status="inbox",
            to_status="in_progress",
            changed_by="agent",
            agent_id=rex.id,
            created_at=datetime.utcnow(),
        )
        s.add(event)
        await s.commit()

    # Rex versucht approve
    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            task = await s.get(Task, task_obj.id)
            rex_agent = await s.get(Agent, rex.id)

            from app.services.task_lifecycle import execute_review_decision
            # Sollte NICHT 409 werfen — eskaliert statt blockiert
            await execute_review_decision(
                session=s,
                task=task,
                board_id=board.id,
                decision="approve",
                comment_text="Alles OK",
                actor_agent=rex_agent,
            )

        # Task sollte an Henry re-assigned sein
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            task = await s.get(Task, task_obj.id)
            assert task.assigned_agent_id == henry.id, \
                f"Task sollte an Henry assigned sein, ist aber {task.assigned_agent_id}"
            assert task.status == "review", "Task bleibt in review (Board Lead muss entscheiden)"


# ── Test 2: Ohne Board Lead → harter Block (409) ────────────────────


@pytest.mark.asyncio
async def test_self_review_without_board_lead_raises(make_board, make_agent, make_task):
    """Ohne Board Lead → harter Block (409) wie bisher."""
    board = await make_board(name="No-Lead Board", slug="no-lead-board")
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)
    # KEIN Board Lead erstellt!
    task_obj = await make_task(
        board_id=board.id, title="Lonely Review",
        status="review",
        assigned_agent_id=rex.id,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        event = TaskEvent(
            id=uuid.uuid4(),
            task_id=task_obj.id,
            from_status="inbox",
            to_status="in_progress",
            changed_by="agent",
            agent_id=rex.id,
            created_at=datetime.utcnow(),
        )
        s.add(event)
        await s.commit()

    from fastapi import HTTPException

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            task = await s.get(Task, task_obj.id)
            rex_agent = await s.get(Agent, rex.id)

            from app.services.task_lifecycle import execute_review_decision
            with pytest.raises(HTTPException) as exc_info:
                await execute_review_decision(
                    session=s,
                    task=task,
                    board_id=board.id,
                    decision="approve",
                    comment_text="Alles OK",
                    actor_agent=rex_agent,
                )

            assert exc_info.value.status_code == 409
            assert "Self-review not allowed" in str(exc_info.value.detail)


# ── Test 3: Board Lead darf weiterhin selbst approven ────────────────


@pytest.mark.asyncio
async def test_board_lead_can_still_self_review(make_board, make_agent, make_task):
    """Board Lead darf weiterhin selbst approven (keine Regression)."""
    board = await make_board(name="Lead Board", slug="lead-board")
    henry = await make_agent(name="Henry", is_board_lead=True, board_id=board.id)
    task_obj = await make_task(
        board_id=board.id, title="Henry's Task",
        status="review",
        assigned_agent_id=henry.id,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        event = TaskEvent(
            id=uuid.uuid4(),
            task_id=task_obj.id,
            from_status="inbox",
            to_status="in_progress",
            changed_by="agent",
            agent_id=henry.id,
            created_at=datetime.utcnow(),
        )
        s.add(event)
        await s.commit()

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            task = await s.get(Task, task_obj.id)
            henry_agent = await s.get(Agent, henry.id)

            from app.services.task_lifecycle import execute_review_decision
            # Board Lead darf — sollte KEINE Exception werfen
            await execute_review_decision(
                session=s,
                task=task,
                board_id=board.id,
                decision="approve",
                comment_text="Approved by Lead",
                actor_agent=henry_agent,
            )

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            task = await s.get(Task, task_obj.id)
            assert task.status in ("done", "user_test"), \
                f"Board Lead Approve sollte done/user_test ergeben, ist {task.status}"
