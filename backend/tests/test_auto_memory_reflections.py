"""Phase 5 MSY-01 tests — reflection-fold into Auto-Memory.

Plan 05-04 (D-01..D-04). Bodies replace the Wave-0 xfail stubs.

Coverage:
- D-02: a finished task with ≥1 reflection produces ≥1 new BoardMemory
  journal entry (in addition to the existing task_done summary)
- D-03: re-running record_task_completion with the same reflection is
  idempotent — per-reflection dedup key blocks the fold; top-level
  dedup blocks the journal-summary INSERT
- D-04: legacy reflections (predating MSY-01) get back-filled lazily
  on the first post-MSY-01 invocation. The fold runs OUTSIDE the
  top-level auto_memory_task_done short-circuit so a previously-set
  top-level key cannot suppress the fold.

Patches `app.services.auto_memory.engine` and `.get_redis` to wire the
in-memory SQLite engine + fakeredis into the production code path
(canonical pattern from `tests/test_memory_indexing_gaps.py:154-159`).
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
from app.redis_client import RedisKeys
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
                name="MSY01",
                slug=f"msy01-{bid.hex[:8]}",
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
async def test_reflection_produces_journal_entry(fake_redis):
    """MSY-01 D-02: task with reflection produces ≥ 1 new BoardMemory journal entry.

    W4.2 update: the top-level task_done summary is now a TaskComment (not a
    BoardMemory), so only the reflection_fold BoardMemory is expected. The count
    assertion is updated from ≥2 to ≥1.
    """
    reflection = (
        "## Was lief gut\n- klar\n## Was war schwierig\n- nichts\n"
        "## Naechstes Mal\n- weitermachen\n## Lesson\nReflection-fold sollte funktionieren."
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

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        after = await _count_memory(s, bid)
        rows = (
            await s.exec(select(BoardMemory).where(BoardMemory.board_id == bid))
        ).all()
    # W4.2: task_done is now a TaskComment — only reflection_fold produces a BoardMemory
    assert after >= before + 1, (
        f"Expected at least 1 new memory (reflection_fold), got {after - before}"
    )
    refl_rows = [r for r in rows if r.tags and "reflection_fold" in r.tags]
    assert len(refl_rows) >= 1, "Expected at least one reflection_fold BoardMemory"
    assert refl_rows[0].memory_type == "journal"
    assert refl_rows[0].source == "system"
    assert refl_rows[0].auto_generated is True


@pytest.mark.asyncio
async def test_reflection_fold_idempotent(fake_redis):
    """MSY-01 D-03: re-running record_task_completion produces no duplicates.

    Each folded reflection (gated by auto_memory_reflection_fold:{task_id}:{sha16})
    is idempotent on re-run with identical input.

    W4.2 update: the top-level journal-summary INSERT is now a TaskComment write
    (gated by auto_memory_task_done). BoardMemory idempotency is verified via
    _count_memory, which only counts BoardMemory rows (not TaskComments).
    W4.2 also added author_type != 'system' filter in _load_reflections_for_task
    so the auto-generated TaskComment is never re-folded into a BoardMemory.
    """
    reflection = "## Lesson\nIdempotency-Test"
    bid, aid, tid = await _seed_task_with_reflection(reflection)

    with patch("app.services.auto_memory.engine", test_engine), \
         patch("app.services.auto_memory.get_redis", AsyncMock(return_value=fake_redis)), \
         patch(
            "app.services.memory_indexing.index_memory",
            new=AsyncMock(return_value=None),
         ):
        await record_task_completion(tid, aid)
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            after_first = await _count_memory(s, bid)
        # Second call — per-reflection dedup blocks the fold even though
        # reflection-fold runs OUTSIDE the top-level short-circuit (D-04).
        # Top-level short-circuit blocks the TaskComment write (W4.2).
        # author_type='system' filter prevents the TaskComment from being re-folded.
        await record_task_completion(tid, aid)
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            after_second = await _count_memory(s, bid)

    assert after_second == after_first, (
        f"Idempotency broken: {after_first} → {after_second}"
    )


@pytest.mark.asyncio
async def test_legacy_reflection_backfill(fake_redis):
    """MSY-01 D-04: legacy reflections (predating this plan) get folded on next call.

    Simulates the lazy-backfill scenario: a task whose reflection was created
    BEFORE MSY-01 shipped, and whose top-level auto_memory_task_done key was
    already set at original completion time. The FIRST post-MSY-01
    record_task_completion call must still pick up the reflection — because
    the fold runs OUTSIDE the top-level short-circuit (D-04).
    """
    reflection = "## Lesson\nLegacy-backfill"
    bid, aid, tid = await _seed_task_with_reflection(reflection)

    # Pre-set the top-level dedup key to simulate "already processed before
    # MSY-01 shipped". If the fold were inside the short-circuit, it would be
    # skipped entirely and refl_rows would be empty.
    await fake_redis.set(
        RedisKeys.auto_memory_task_done(str(tid)), "1", ex=86400
    )

    with patch("app.services.auto_memory.engine", test_engine), \
         patch("app.services.auto_memory.get_redis", AsyncMock(return_value=fake_redis)), \
         patch(
            "app.services.memory_indexing.index_memory",
            new=AsyncMock(return_value=None),
         ):
        await record_task_completion(tid, aid)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (
            await s.exec(select(BoardMemory).where(BoardMemory.board_id == bid))
        ).all()
    refl_rows = [r for r in rows if r.tags and "reflection_fold" in r.tags]
    assert len(refl_rows) >= 1, (
        "Legacy reflection should have been folded on first post-MSY-01 call "
        "(fold must run OUTSIDE the auto_memory_task_done short-circuit)"
    )
    # And the per-reflection dedup key should now exist (so future re-runs
    # are idempotent).
    keys = await fake_redis.keys(f"mc:auto_memory:reflection_fold:{tid}:*")
    assert len(keys) >= 1, (
        "Per-reflection dedup key should be set after the fold runs"
    )
