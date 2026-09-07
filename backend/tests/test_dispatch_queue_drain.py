"""Queue Drain: the dispatch queue is emptied, not just filled.

Incident (Fix 4, 07.09.2026): `enqueue_task` is fed by dispatch Guards 1-3
(#452) and task_lifecycle review-rejection, but `dequeue_task` had no
caller — tasks marked `task.dispatch_queued` sat in the Redis queue
forever; 11 stale entries (6 done since April) clogged the boss queue.
Only the watchdog's coarse `_check_undispatched_tasks` pass (30 s tick,
no FIFO guarantee) rescued them.

Drain contract tested here:
1. enqueue → agent frees up (task done) → drain dispatches the next entry.
2. Stale entries (task done/aborted/deleted) are skipped and removed,
   never dispatched.
3. No double dispatch: dispatch_attempt_id stays unique per task
   (set_dispatch_attempt_id only_if_null) and the drain-inflight lock
   dedupes concurrent drains.
4. Startup purge removes done/aborted queue entries.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _seed_agent_with_queue(
    make_board, make_agent, make_task, fake_redis, *,
    queue_task_ids: list[str],
    agent_status: str = "idle",
):
    """Board + cli-bridge agent + queue entries pre-filled in Redis."""
    board = await make_board(
        name=f"Drain-{uuid.uuid4().hex[:8]}",
        slug=f"drain-{uuid.uuid4().hex[:8]}",
        auto_dispatch_enabled=True,
    )
    agent = await make_agent(
        name="drained-dev",
        role="developer",
        board_id=board.id,
        agent_runtime="cli-bridge",
        status=agent_status,
    )
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        from app.services.task_queue import enqueue_task
        for tid in queue_task_ids:
            await enqueue_task(str(agent.id), tid)
    return board, agent


async def _drain(agent_id: str, fake_redis) -> str | None:
    with patch("app.services.task_queue.get_redis", return_value=fake_redis), \
         patch("app.database.engine", test_engine), \
         patch("app.services.dispatch.engine", test_engine), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        from app.services.task_queue import drain_agent_task_queue
        return await drain_agent_task_queue(agent_id)


async def _get_task(task_id) -> "Task":
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(Task, task_id)


@pytest.mark.asyncio
async def test_drain_dispatches_next_entry_after_completion(
    make_board, make_agent, make_task, fake_redis,
):
    """enqueue → first task done → drain dispatches the queued one:
    dispatched_at set, attempt_id allocated, queue empty afterwards."""
    board, agent = await _seed_agent_with_queue(
        make_board, make_agent, make_task, fake_redis,
        queue_task_ids=[],  # fill below — need the task id first
    )
    queued = await make_task(
        board_id=board.id, title="Queued follower", status="inbox",
        assigned_agent_id=agent.id,
    )
    finished = await make_task(
        board_id=board.id, title="Just finished", status="done",
        assigned_agent_id=agent.id,
    )
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        from app.services.task_queue import enqueue_task
        await enqueue_task(str(agent.id), str(queued.id))

    drained = await _drain(str(agent.id), fake_redis)

    assert drained == str(queued.id), "drain must return the dispatched task id"

    refreshed = await _get_task(queued.id)
    assert refreshed.dispatched_at is not None, (
        "drain must start the queued task (dispatched_at stamped)"
    )
    assert refreshed.dispatch_attempt_id, (
        "dispatch must have allocated a dispatch_attempt_id (attempt proof)"
    )

    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        from app.services.task_queue import queue_length
        assert await queue_length(str(agent.id)) == 0, (
            "dispatched entry must be out of the queue"
        )
    # The finished task is untouched by the drain.
    assert (await _get_task(finished.id)).status == "done"


@pytest.mark.asyncio
async def test_drain_skips_stale_done_entry(
    make_board, make_agent, make_task, fake_redis,
):
    """Queue = [done-task, live-task]: drain skips the done entry (removes
    it, no dispatch) and dispatches the live one."""
    board, agent = await _seed_agent_with_queue(
        make_board, make_agent, make_task, fake_redis, queue_task_ids=[],
    )
    stale = await make_task(
        board_id=board.id, title="Already done", status="done",
        assigned_agent_id=agent.id,
    )
    live = await make_task(
        board_id=board.id, title="Still inbox", status="inbox",
        assigned_agent_id=agent.id,
    )
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        from app.services.task_queue import enqueue_task
        await enqueue_task(str(agent.id), str(stale.id))
        await enqueue_task(str(agent.id), str(live.id))

    drained = await _drain(str(agent.id), fake_redis)

    assert drained == str(live.id)
    assert (await _get_task(stale.id)).dispatched_at is None, (
        "stale done task must NOT be dispatched"
    )
    assert (await _get_task(live.id)).dispatched_at is not None

    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        from app.services.task_queue import peek_queue
        assert str(stale.id) not in await peek_queue(str(agent.id)), (
            "stale entry must be removed from the queue"
        )


@pytest.mark.asyncio
async def test_drain_skips_deleted_task_entry(
    make_board, make_agent, make_task, fake_redis,
):
    """A queue entry whose task row no longer exists is dropped, not dispatched."""
    board, agent = await _seed_agent_with_queue(
        make_board, make_agent, make_task, fake_redis, queue_task_ids=[],
    )
    live = await make_task(
        board_id=board.id, title="Survivor", status="inbox",
        assigned_agent_id=agent.id,
    )
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        from app.services.task_queue import enqueue_task
        await enqueue_task(str(agent.id), str(uuid.uuid4()))  # never existed
        await enqueue_task(str(agent.id), str(live.id))

    drained = await _drain(str(agent.id), fake_redis)
    assert drained == str(live.id)
    assert (await _get_task(live.id)).dispatched_at is not None


@pytest.mark.asyncio
async def test_drain_no_double_dispatch_attempt_id(
    make_board, make_agent, make_task, fake_redis,
):
    """Two concurrent drains on the same agent queue → exactly one dispatch.
    Second drain sees the inflight lock and bails; the task gets exactly ONE
    dispatch_attempt_id (only_if_null conditional update)."""
    board, agent = await _seed_agent_with_queue(
        make_board, make_agent, make_task, fake_redis, queue_task_ids=[],
    )
    queued = await make_task(
        board_id=board.id, title="Once only", status="inbox",
        assigned_agent_id=agent.id,
    )
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        from app.services.task_queue import enqueue_task
        await enqueue_task(str(agent.id), str(queued.id))

    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        # Simulate a concurrent drain already holding the inflight lock.
        redis = fake_redis
        from app.services.task_queue import _inflight_key
        await redis.set(_inflight_key(str(agent.id)), "1", ex=60)
        from app.services.task_queue import drain_agent_task_queue
        second = await drain_agent_task_queue(str(agent.id))
        await redis.delete(_inflight_key(str(agent.id)))

    assert second is None, "drain holding the inflight lock must not re-dispatch"

    # Now the real drain runs and dispatches once.
    first = await _drain(str(agent.id), fake_redis)
    assert first == str(queued.id)

    refreshed = await _get_task(queued.id)
    assert refreshed.dispatch_attempt_id, "attempt id must exist"
    # Attempt id allocated exactly once — re-running the drain cannot rotate it
    # because auto_dispatch_task uses set_dispatch_attempt_id(only_if_null=True).
    attempt_id = refreshed.dispatch_attempt_id
    with patch("app.services.task_queue.get_redis", return_value=fake_redis), \
         patch("app.database.engine", test_engine), \
         patch("app.services.dispatch.engine", test_engine), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        from app.services.task_queue import drain_agent_task_queue as drain
        # Queue is empty now → drain is a no-op.
        assert await drain(str(agent.id)) is None
    assert (await _get_task(queued.id)).dispatch_attempt_id == attempt_id


@pytest.mark.asyncio
async def test_drain_emits_task_dequeued_event(
    make_board, make_agent, make_task, fake_redis,
):
    """Drain emits `task.dequeued` for the dispatched task."""
    board, agent = await _seed_agent_with_queue(
        make_board, make_agent, make_task, fake_redis, queue_task_ids=[],
    )
    queued = await make_task(
        board_id=board.id, title="Event probe", status="inbox",
        assigned_agent_id=agent.id,
    )
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        from app.services.task_queue import enqueue_task
        await enqueue_task(str(agent.id), str(queued.id))

    with patch("app.services.task_queue.get_redis", return_value=fake_redis), \
         patch("app.database.engine", test_engine), \
         patch("app.services.dispatch.engine", test_engine), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        from sqlmodel import select
        from app.models.activity import ActivityEvent
        from app.services.task_queue import drain_agent_task_queue
        await drain_agent_task_queue(str(agent.id))
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            events = (await s.exec(
                select(ActivityEvent).where(
                    ActivityEvent.task_id == queued.id,
                    ActivityEvent.event_type == "task.dequeued",
                )
            )).all()
    assert events, "task.dequeued event required"
    detail = events[0].detail or {}
    assert detail.get("task_id") == str(queued.id)
    assert "agent_id" in detail and "queue_position" in detail


@pytest.mark.asyncio
async def test_drain_empty_queue_is_noop(make_board, make_agent, fake_redis):
    """Drain on an agent with an empty queue returns None without dispatching."""
    board = await make_board(name="Empty", slug="empty-drain")
    agent = await make_agent(name="idle-dev", board_id=board.id)
    assert await _drain(str(agent.id), fake_redis) is None


@pytest.mark.asyncio
async def test_purge_removes_done_and_aborted_entries(
    make_board, make_agent, make_task, fake_redis,
):
    """Startup purge: done/aborted entries removed, inbox entries kept."""
    board = await make_board(name="Purge", slug="purge-board")
    agent = await make_agent(name="purge-dev", board_id=board.id)
    done_task = await make_task(
        board_id=board.id, title="Done since April", status="done",
    )
    aborted_task = await make_task(
        board_id=board.id, title="Aborted", status="aborted",
    )
    live_task = await make_task(
        board_id=board.id, title="Still relevant", status="inbox",
    )
    with patch("app.services.task_queue.get_redis", return_value=fake_redis), \
         patch("app.database.engine", test_engine):
        from app.services.task_queue import (
            enqueue_task, purge_finished_queue_entries, peek_queue,
        )
        await enqueue_task(str(agent.id), str(done_task.id))
        await enqueue_task(str(agent.id), str(aborted_task.id))
        await enqueue_task(str(agent.id), str(live_task.id))
        # The 11-legacy-entry motivating case: done entries pile up.
        for _ in range(5):
            t = await make_task(board_id=board.id, title="Old done", status="done")
            await enqueue_task(str(agent.id), str(t.id))

        removed = await purge_finished_queue_entries()
        remaining = await peek_queue(str(agent.id))

    assert removed == 7  # 2 done/aborted + 5 loop-done; live entry survives
    assert remaining == [str(live_task.id)], (
        "only the live inbox entry survives the purge"
    )


@pytest.mark.asyncio
async def test_purge_removes_entries_of_deleted_tasks(
    make_board, make_agent, make_task, fake_redis,
):
    """Purge also drops entries whose task row was deleted (lrem path in
    routers/tasks.py can miss; the purge is the safety net)."""
    board = await make_board(name="Gone", slug="gone-board")
    agent = await make_agent(name="ghost-dev", board_id=board.id)
    with patch("app.services.task_queue.get_redis", return_value=fake_redis), \
         patch("app.database.engine", test_engine):
        from app.services.task_queue import (
            enqueue_task, purge_finished_queue_entries, peek_queue,
        )
        await enqueue_task(str(agent.id), str(uuid.uuid4()))  # deleted/never existed

        removed = await purge_finished_queue_entries()

    assert removed == 1
    assert await peek_queue(str(agent.id)) == []
