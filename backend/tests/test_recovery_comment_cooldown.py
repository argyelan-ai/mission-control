"""Tests for the shared recovery-comment cooldown (Workstream W1-C, G6).

Four independent mechanisms can each post a "continue"-style system
TaskComment on the same task within minutes of each other:
  - Tier-3 recovery_recap        (task_runner._run_tiered_recovery)
  - unblock_notify               (agent_task_status.py, blocked→in_progress)
  - watchdog_notify               (task_runner._check_stuck_in_progress, ADR-046)
  - bootstrap recovery_recap     (routers/internal.py, on container restart)

A shared per-task Redis cooldown (RedisKeys.recovery_comment_cooldown,
TTL 600s) ensures only the first mechanism to fire actually posts — the
others detect the cooldown and skip silently. This does NOT gate
operator-facing Approvals/Telegram, only the TaskComment spam.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.secret import Secret
from app.services.encryption import encrypt
from tests.conftest import test_engine


@asynccontextmanager
async def _patched_test_session():
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


async def _count_recovery_recap_comments(s: AsyncSession, task_id: uuid.UUID) -> int:
    from app.models.task import TaskComment

    result = await s.exec(
        select(TaskComment).where(
            TaskComment.task_id == task_id,
            TaskComment.comment_type == "recovery_recap",
        )
    )
    return len(list(result.all()))


# ── Test 1: unit-level — the claim helper is a one-shot ────────────────


@pytest.mark.asyncio
async def test_try_claim_recovery_comment_cooldown_is_one_shot(fake_redis):
    from app.redis_client import try_claim_recovery_comment_cooldown

    task_id = str(uuid.uuid4())

    first = await try_claim_recovery_comment_cooldown(fake_redis, task_id)
    second = await try_claim_recovery_comment_cooldown(fake_redis, task_id)

    assert first is True, "first caller should win the race and claim the cooldown"
    assert second is False, "second caller within the TTL window must be told to skip"

    # A different task is an independent cooldown.
    other_task_id = str(uuid.uuid4())
    other = await try_claim_recovery_comment_cooldown(fake_redis, other_task_id)
    assert other is True


# ── Test 2: two real mechanisms racing on the same task → ONE comment ──


@pytest.mark.asyncio
async def test_bootstrap_and_tier3_recovery_race_yields_one_comment(client):
    """Bootstrap recap (mechanism A) fires first and claims the cooldown.
    Tier-3 recovery recap (mechanism B) then runs against the SAME task —
    it must find the cooldown already claimed and skip its own comment,
    leaving exactly one recovery_recap TaskComment total."""
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task, TaskComment

    board = Board(id=uuid.uuid4(), name="Cooldown Board", slug=f"cooldown-{uuid.uuid4().hex[:8]}")
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    async with _patched_test_session() as s:
        s.add(board)
        await s.commit()

        task = Task(
            id=task_id,
            board_id=board.id,
            title="Interrupted task",
            status="in_progress",
        )
        s.add(task)
        await s.commit()

        agent = Agent(
            id=agent_id,
            name=f"Worker-{uuid.uuid4().hex[:6]}",
            role="developer",
            agent_runtime="docker",
            current_task_id=task_id,
        )
        s.add(agent)
        s.add(Secret(key="ollama_api_key", encrypted_value=encrypt("k-test"), provider="ollama"))
        # build_recovery_context() needs at least one comment/checklist item
        # to produce a non-empty recap.
        s.add(TaskComment(
            task_id=task_id,
            author_type="agent",
            comment_type="progress",
            content="Halfway through the migration.",
        ))
        await s.commit()
        await s.refresh(agent)
        await s.refresh(task)

    # ── Mechanism A: bootstrap recap (container restart signal) ──
    resp = await client.get(f"/api/v1/internal/bootstrap?agent_name={agent.name}")
    assert resp.status_code == 200, resp.text

    async with _patched_test_session() as s:
        count_after_bootstrap = await _count_recovery_recap_comments(s, task_id)
    assert count_after_bootstrap == 1, "bootstrap should have posted the first recap"

    # ── Mechanism B: Tier-3 tiered recovery on the SAME task ──
    from app.services.task_runner import task_runner

    restart_spy = MagicMock(return_value={"status": "restarted", "container": "mc-agent-test"})
    dispatch_spy = AsyncMock(return_value=None)

    async with _patched_test_session() as s:
        agent_row = await s.get(Agent, agent_id)
        task_row = await s.get(Task, task_id)
        with patch("app.services.docker_agent_sync.restart_docker_agent_container", restart_spy), \
             patch("app.services.task_runner.auto_dispatch_task", dispatch_spy), \
             patch("app.services.task_runner.emit_event", new_callable=AsyncMock), \
             patch("app.services.task_runner.logger"), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            await task_runner._run_tiered_recovery(s, task_row, agent_row)

    async with _patched_test_session() as s:
        count_after_tier3 = await _count_recovery_recap_comments(s, task_id)

    assert count_after_tier3 == 1, (
        "Tier-3 recovery recap must be suppressed by the shared cooldown — "
        "exactly one recovery_recap comment total, not two"
    )
