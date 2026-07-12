"""Regression: vertical task_done hooks must fire on the review-approve path.

The PATCH routers (tasks.py, agent_task_status.py) call run_task_done_hooks
on status=done — but execute_review_decision (task_lifecycle.py) is a third
way a task reaches done (`mc approve`) and skipped the hooks. 2026-07-12
incident: bench_studio entry stuck in 'generating' after the lead approved
the bench task.
"""

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.task import Task, TaskEvent
from app.verticals import hooks as vertical_hooks
from tests.conftest import test_engine


@pytest.mark.asyncio
async def test_review_approve_fires_task_done_hooks(make_board, make_agent, make_task):
    board = await make_board(name="Hook Board", slug="hook-board")
    lead = await make_agent(name="Lead", is_board_lead=True, board_id=board.id)
    worker = await make_agent(name="Worker", role="developer", board_id=board.id)
    task_obj = await make_task(
        board_id=board.id, title="Hooked Task",
        status="review", assigned_agent_id=lead.id,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskEvent(
            id=uuid.uuid4(), task_id=task_obj.id,
            from_status="inbox", to_status="in_progress",
            changed_by="agent", agent_id=worker.id,
            created_at=datetime.utcnow(),
        ))
        await s.commit()

    seen: list[uuid.UUID] = []

    async def probe(session, task):
        seen.append(task.id)

    vertical_hooks.task_done_hooks.append(probe)
    try:
        with (
            patch("app.services.activity.broadcast", new_callable=AsyncMock),
            patch("app.services.operations.get_system_mode",
                  new_callable=AsyncMock, return_value="active"),
        ):
            async with AsyncSession(test_engine, expire_on_commit=False) as s:
                task = await s.get(Task, task_obj.id)
                lead_agent = await s.get(Agent, lead.id)

                from app.services.task_lifecycle import execute_review_decision
                await execute_review_decision(
                    session=s,
                    task=task,
                    board_id=board.id,
                    decision="approve",
                    comment_text="Approved — looks good",
                    actor_agent=lead_agent,
                )
    finally:
        vertical_hooks.task_done_hooks.remove(probe)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_obj.id)
    assert task.status == "done"
    assert seen == [task_obj.id], "task_done hook did not fire on review-approve"
