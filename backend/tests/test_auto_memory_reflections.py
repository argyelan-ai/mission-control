"""Reflexions-Triage (2026-09-11) — record_task_completion no longer folds
reflections into BoardMemory.

Was hier vorher stand (Phase 5 MSY-01, D-01..D-04): `record_task_completion`
folded EVERY reflection comment of a completed task into a
BoardMemory(memory_type='journal') row, unconditionally, via
`_fold_reflections_into_memory`. That is exactly the automatic,
ungated board_memory write Mark's 11.09.2026 decision closes ("81 Lessons
in 2 Tagen, unbelegt, doppelt, unbewertet"). `_load_reflections_for_task`,
`_reflection_dedup_key` and `_fold_reflections_into_memory` were deleted
from `app/services/auto_memory.py`; the RedisKeys.auto_memory_reflection_fold
dedup-key helper was deleted from `app/redis_client.py`.

The only reflection→BoardMemory write left in the system is Lead-gated:
`app.routers.agent_comments._handle_reflection_verdict`, exercised by
`backend/tests/test_reflection_enforcement.py`
(`test_reflection_verdict_uebernehmen_creates_lesson_with_quelle` et al.).

This file keeps a narrow regression test: a completed task with reflections
must NOT produce any BoardMemory row via `record_task_completion`, no matter
how many times it's called (mutation check for a resurrected auto-fold).
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.memory import BoardMemory
from app.models.task import Task, TaskComment
from app.services.auto_memory import record_task_completion
from app.utils import utcnow
from tests.conftest import test_engine


async def _seed_task_with_reflection(
    reflection_text: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a board + agent + task + 1 reflection comment.

    Returns (board_id, agent_id, task_id).
    """
    bid = uuid.uuid4()
    aid = uuid.uuid4()
    tid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(
            Board(
                id=bid,
                name="Triage-Test",
                slug=f"triage-test-{bid.hex[:8]}",
                require_review_before_done=False,
            )
        )
        await s.commit()
        s.add(
            Agent(
                id=aid,
                board_id=bid,
                name="ReflAgent",
                role="researcher",
                scopes=["chat:write"],
                provision_status="provisioned",
            )
        )
        await s.commit()
        s.add(
            Task(
                id=tid,
                board_id=bid,
                title="Test task",
                description="x",
                status="done",
                priority="medium",
                assigned_agent_id=aid,
                started_at=utcnow(),
                completed_at=utcnow(),
            )
        )
        await s.commit()
        s.add(
            TaskComment(
                id=uuid.uuid4(),
                task_id=tid,
                author_type="agent",
                author_agent_id=aid,
                content=reflection_text,
                comment_type="reflection",
            )
        )
        await s.commit()
    return bid, aid, tid


async def _count_memory(session: AsyncSession, board_id: uuid.UUID) -> int:
    res = await session.exec(
        select(func.count())
        .select_from(BoardMemory)
        .where(BoardMemory.board_id == board_id)
    )
    return res.one()


@pytest.mark.asyncio
async def test_record_task_completion_creates_no_board_memory_from_reflection(fake_redis):
    """Mutation check: an un-triaged reflection must produce ZERO BoardMemory
    rows when the task completes — not even once, not even the old
    'journal' fold. Only a Lead's `reflection_verdict` (verdict=uebernehmen)
    may write to board_memory (see test_reflection_enforcement.py)."""
    reflection = (
        "## Was wurde gemacht\n- x\n## Was hat funktioniert\n- x\n"
        "## Was war unklar\n- x\n## Lesson für Agent-Memory\nSollte NICHT automatisch landen."
    )
    bid, aid, tid = await _seed_task_with_reflection(reflection)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        before = await _count_memory(s, bid)

    with patch("app.services.auto_memory.engine", test_engine), \
         patch("app.services.auto_memory.get_redis", AsyncMock(return_value=fake_redis)), \
         patch(
            "app.services.memory_indexing.index_memory",
            new=AsyncMock(return_value=None),
         ):
        await record_task_completion(tid, aid)
        # Second call (task re-saved as done, e.g. retry) must stay at zero too.
        await record_task_completion(tid, aid)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        after = await _count_memory(s, bid)
    assert after == before == 0, (
        f"record_task_completion darf keine BoardMemory mehr aus Reflexionen "
        f"anlegen (Reflexions-Triage, 2026-09-11). before={before} after={after}"
    )


@pytest.mark.asyncio
async def test_record_task_completion_still_writes_audit_comment(fake_redis):
    """The task-done audit trail (W4.2 TaskComment redirect) is unaffected
    by the reflection-fold removal — it's a separate write path."""
    reflection = "## Lesson für Agent-Memory\nAudit-Trail bleibt bestehen."
    bid, aid, tid = await _seed_task_with_reflection(reflection)

    with patch("app.services.auto_memory.engine", test_engine), \
         patch("app.services.auto_memory.get_redis", AsyncMock(return_value=fake_redis)), \
         patch(
            "app.services.memory_indexing.index_memory",
            new=AsyncMock(return_value=None),
         ):
        await record_task_completion(tid, aid)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(TaskComment)
            .where(TaskComment.task_id == tid)
            .where(TaskComment.comment_type == "reflection")
            .where(TaskComment.author_type == "system")
        )
        audit_comments = result.all()
    assert len(audit_comments) == 1, (
        f"Expected 1 system audit-trail TaskComment, got {len(audit_comments)}"
    )
