"""W4.2 — record_task_completion writes TaskComment, not BoardMemory.

After the redirect in auto_memory.py, calling record_task_completion() must:
  - Create exactly one TaskComment(comment_type='reflection') for the task
  - NOT create any BoardMemory row with tags=['auto', 'task_done']

Redis dedup is bypassed by using fakeredis with a fresh server per test
(the dedup key is never set, so _dedup_check returns True).
"""

import uuid

import fakeredis
import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _make_board_and_task(session: AsyncSession) -> tuple:
    """Create a minimal Board + Task and return (board, task)."""
    from app.models.board import Board
    from app.models.task import Task

    board = Board(id=uuid.uuid4(), name="W4 Test Board", slug=f"w4-test-{uuid.uuid4().hex[:6]}")
    session.add(board)
    await session.commit()
    await session.refresh(board)

    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Implement the auth layer",
        status="done",
        priority="high",
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    return board, task


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_task_completion_writes_task_comment(fake_redis):
    """W4.2: record_task_completion() must create a TaskComment, not a BoardMemory."""
    import app.redis_client as rc_mod
    from app.models.memory import BoardMemory
    from app.models.task import Task, TaskComment
    from app.services import auto_memory as am_mod

    # Patch redis so _dedup_check always says "not seen yet" (returns True)
    original_get_redis = rc_mod.get_redis

    async def _fake_get_redis():
        return fake_redis

    rc_mod.get_redis = _fake_get_redis
    am_mod.get_redis = _fake_get_redis  # auto_memory imports get_redis at module level

    try:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board, task = await _make_board_and_task(s)

        # Run the function under test — uses its own internal AsyncSession
        # but that connects to the same test_engine via the patched engine in
        # auto_memory (see Note below).
        #
        # Note: auto_memory.py uses `engine` (the real app engine) directly.
        # We patch it here so it uses test_engine instead.
        import app.services.auto_memory as auto_memory_module
        original_engine = auto_memory_module.engine
        auto_memory_module.engine = test_engine

        try:
            await am_mod.record_task_completion(task.id, agent_id=None)
        finally:
            auto_memory_module.engine = original_engine

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            # Must have created a TaskComment with comment_type='reflection'
            result = await s.exec(
                select(TaskComment)
                .where(TaskComment.task_id == task.id)
                .where(TaskComment.comment_type == "reflection")
            )
            comments = list(result.all())
            assert len(comments) == 1, (
                f"Expected 1 reflection TaskComment, got {len(comments)}"
            )
            assert comments[0].author_type == "system"
            assert "Task erledigt" in comments[0].content

            # Must NOT have created a BoardMemory with tags including 'task_done'
            mem_result = await s.exec(
                select(BoardMemory).where(BoardMemory.board_id == board.id)
            )
            auto_memories = [
                m for m in mem_result.all()
                if isinstance(m.tags, list) and "task_done" in m.tags
            ]
            assert len(auto_memories) == 0, (
                f"Expected 0 BoardMemory task_done rows, got {len(auto_memories)}: "
                f"{[m.title for m in auto_memories]}"
            )
    finally:
        rc_mod.get_redis = original_get_redis
        am_mod.get_redis = original_get_redis


@pytest.mark.asyncio
async def test_record_task_completion_comment_contains_task_title(fake_redis):
    """W4.2: reflection comment body includes the task title."""
    import app.redis_client as rc_mod
    from app.models.task import TaskComment
    from app.services import auto_memory as am_mod

    async def _fake_get_redis():
        return fake_redis

    original_get_redis = rc_mod.get_redis
    original_am_get_redis = am_mod.get_redis
    rc_mod.get_redis = _fake_get_redis
    am_mod.get_redis = _fake_get_redis

    import app.services.auto_memory as auto_memory_module
    original_engine = auto_memory_module.engine
    auto_memory_module.engine = test_engine

    try:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            _, task = await _make_board_and_task(s)

        await am_mod.record_task_completion(task.id, agent_id=None)

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            result = await s.exec(
                select(TaskComment)
                .where(TaskComment.task_id == task.id)
                .where(TaskComment.comment_type == "reflection")
            )
            comment = result.first()
            assert comment is not None
            assert task.title in comment.content
    finally:
        auto_memory_module.engine = original_engine
        # Restore the TRUE originals. The old `rc_mod.get_redis =
        # am_mod.get_redis` "restore" leaked this test's (closed) fake_redis
        # into both modules and broke every later test that calls
        # get_redis() directly (e.g. the recovery-comment cooldown claim).
        rc_mod.get_redis = original_get_redis
        am_mod.get_redis = original_am_get_redis
