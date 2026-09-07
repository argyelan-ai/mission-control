"""
Task Queue Service — Redis-based FIFO queue per agent.

If an agent is already working on an active task, new tasks are
enqueued. When the agent frees up (task done/failed/aborted/review,
review-approve, or heartbeat self-heal), `drain_agent_task_queue`
dequeues tasks in FIFO order and dispatches them — the queue is no
longer a write-only grave. The watchdog `_check_undispatched_tasks`
stays as the coarse safety net.

Key schema: mc:agent:{agent_id}:task_queue  (Redis List, RPUSH/LPOP)
"""

import logging
import uuid

from typing import TYPE_CHECKING

from app.redis_client import RedisKeys, get_redis

if TYPE_CHECKING:
    from app.models.task import Task

logger = logging.getLogger("mc.task_queue")

def _queue_key(agent_id: str) -> str:
    return RedisKeys.agent_task_queue(agent_id)


async def enqueue_task(agent_id: str, task_id: str) -> None:
    """Append a task to the end of the agent's queue."""
    redis = await get_redis()
    await redis.rpush(_queue_key(agent_id), task_id)
    logger.info("Enqueued task %s for agent %s", task_id, agent_id)


async def dequeue_task(agent_id: str) -> str | None:
    """Get the next task from the front of the agent's queue (FIFO)."""
    redis = await get_redis()
    result = await redis.lpop(_queue_key(agent_id))
    if result:
        task_id = result.decode() if isinstance(result, bytes) else result
        logger.info("Dequeued task %s for agent %s", task_id, agent_id)
        return task_id
    return None


def _inflight_key(agent_id: str) -> str:
    """Redis key marking the task_id currently being drained/dispatched."""
    return f"mc:agent:{agent_id}:drain_inflight"


def _task_done_or_gone(task: "Task | None") -> bool:
    """True if a queued task no longer needs dispatching."""
    if task is None:
        return True  # deleted → drop
    return task.status in ("done", "aborted", "failed")


async def drain_agent_task_queue(agent_id: str) -> str | None:
    """Drain: dispatch the next dispatchable task from the agent's queue.

    Called when the agent frees up (task completion, review-approve) and
    by the startup cleanup for legacy stale entries. FIFO order via peek;
    entries whose task is done/aborted/failed/deleted are skipped and
    removed (LREM) without dispatching.

    Returns the dispatched task_id, or None if the queue is empty or
    nothing was dispatchable (agent busy / dispatch failed — the watchdog
    retry covers those).

    Double-dispatch guard: auto_dispatch_task → set_dispatch_attempt_id
    (only_if_null=True) is race-free, and the busy-checks (Guards 1-3)
    re-queue instead of double-pasting. `drain_inflight` additionally
    deduplicates concurrent drain triggers (done-hook + watchdog firing
    together) for the same agent.
    """
    redis = await get_redis()
    queue = await peek_queue(agent_id)

    if not queue:
        return None

    # Reentrancy guard: another drain (or dispatch) is already working on
    # this agent's queue — don't double-dispatch the head task.
    lock_acquired = await redis.set(_inflight_key(agent_id), "1", nx=True, ex=60)
    if not lock_acquired:
        return None

    try:
        from app.database import engine
        from app.models.task import Task as TaskModel
        from sqlmodel.ext.asyncio.session import AsyncSession

        async with AsyncSession(engine, expire_on_commit=False) as session:
            skipped = 0
            for idx, queued_task_id in enumerate(queue):
                try:
                    queued_uuid = uuid.UUID(queued_task_id)
                except (ValueError, AttributeError):
                    logger.warning(
                        "Drain: malformed queue entry %r for agent %s — removing",
                        queued_task_id, agent_id,
                    )
                    await redis.lrem(_queue_key(agent_id), 0, queued_task_id)
                    skipped += 1
                    continue

                task = await session.get(TaskModel, queued_uuid)
                if _task_done_or_gone(task):
                    # Stale entry: done/aborted/failed or deleted — skip, don't
                    # dispatch. Remove from the queue.
                    await redis.lrem(_queue_key(agent_id), 0, queued_task_id)
                    skipped += 1
                    logger.info(
                        "Drain: skipped stale queue entry %s for agent %s (task=%s)",
                        queued_task_id, agent_id,
                        task.status if task else "deleted",
                    )
                    continue
                # First dispatchable task found. Remove it from the queue
                # FIRST (LREM), then dispatch: if the agent is still busy,
                # auto_dispatch_task's guards re-enqueue it at the tail —
                # no loss, no double entry.
                await redis.lrem(_queue_key(agent_id), 0, queued_task_id)
                from app.services.dispatch import auto_dispatch_task
                from app.services.activity import emit_event

                # Event BEFORE the dispatch: task.dequeued marks the queue
                # exit (queue_length drops), the subsequent task.auto_dispatched
                # / task.cli_bridge_ready events mark the actual dispatch.
                await emit_event(
                    session, "task.dequeued",
                    f"Queue-Drain: Task {queued_task_id} aus Warteschlange genommen",
                    task_id=task.id,
                    agent_id=task.assigned_agent_id,
                    board_id=task.board_id,
                    detail={
                        "agent_id": agent_id,
                        "task_id": str(task.id),
                        "queue_position": idx,
                        "stale_skipped": skipped,
                    },
                )
                await auto_dispatch_task(task.id, task.board_id)
                logger.info(
                    "Drain: dispatched queued task %s for agent %s",
                    queued_task_id, agent_id,
                )
                return queued_task_id
    except Exception:
        logger.exception("Drain failed for agent %s", agent_id)
    finally:
        try:
            await redis.delete(_inflight_key(agent_id))
        except Exception:
            pass  # Best-effort
    return None


async def purge_finished_queue_entries() -> int:
    """One-time cleanup: remove done/aborted queue entries across ALL agents.

    Called from the FastAPI lifespan (startup). Scans mc:agent:*:task_queue
    keys, drops entries whose task is done/aborted/failed or no longer
    exists. Returns the number of removed entries. Idempotent — safe to
    run on every startup.
    """
    from app.database import engine
    from app.models.task import Task as TaskModel
    from sqlmodel.ext.asyncio.session import AsyncSession

    redis = await get_redis()
    removed = 0
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async for key in redis.scan_iter(match="mc:agent:*:task_queue"):
            key_str = key.decode() if isinstance(key, bytes) else key
            agent_id = key_str.removeprefix("mc:agent:").removesuffix(":task_queue")
            entries = await peek_queue(agent_id)
            for entry in entries:
                try:
                    task = await session.get(TaskModel, uuid.UUID(entry))
                except (ValueError, AttributeError):
                    task = None
                if _task_done_or_gone(task):
                    await redis.lrem(key_str, 0, entry)
                    removed += 1
                    logger.info(
                        "Purge: removed stale queue entry %s (agent %s, task=%s)",
                        entry, agent_id,
                        task.status if task else "deleted",
                    )
    if removed:
        logger.info("Purge: %d stale dispatch-queue entries removed", removed)
    return removed


async def queue_length(agent_id: str) -> int:
    """Number of tasks waiting for this agent."""
    redis = await get_redis()
    return await redis.llen(_queue_key(agent_id))


async def peek_queue(agent_id: str) -> list[str]:
    """Show all tasks in the queue (without removing them)."""
    redis = await get_redis()
    items = await redis.lrange(_queue_key(agent_id), 0, -1)
    return [i.decode() if isinstance(i, bytes) else i for i in items]


# ── Pending Dispatch Queue ──────────────────────────────────────────────
# Tasks that couldn't be pushed because the agent had no active session.
# The watchdog delivers them once the agent is online.

def _pending_key(agent_id: str) -> str:
    return RedisKeys.agent_pending_dispatch(agent_id)


async def enqueue_pending_dispatch(agent_id: str, task_id: str) -> None:
    """Put a task in the pending-dispatch queue (agent has no active session)."""
    redis = await get_redis()
    await redis.rpush(_pending_key(agent_id), task_id)
    logger.info("Pending dispatch queued: task %s for agent %s", task_id, agent_id)


async def dequeue_pending_dispatch(agent_id: str) -> str | None:
    """Get the next pending task (FIFO)."""
    redis = await get_redis()
    result = await redis.lpop(_pending_key(agent_id))
    if result:
        task_id = result.decode() if isinstance(result, bytes) else result
        logger.info("Pending dispatch dequeued: task %s for agent %s", task_id, agent_id)
        return task_id
    return None


async def pending_dispatch_length(agent_id: str) -> int:
    """Number of waiting pending-dispatch tasks."""
    redis = await get_redis()
    return await redis.llen(_pending_key(agent_id))


# ── Dispatch Lock ───────────────────────────────────────────────────────
# Prevents race conditions: only one dispatch process per agent at a time.

def _lock_key(agent_id: str) -> str:
    return RedisKeys.agent_dispatch_lock(agent_id)


async def acquire_dispatch_lock(agent_id: str, ttl: int = 30) -> bool:
    """Set a dispatch lock for an agent (SET NX EX). True = lock acquired.

    Fail-open: on a Redis error, True is returned (let the lock pass through).
    """
    try:
        redis = await get_redis()
        result = await redis.set(_lock_key(agent_id), "1", nx=True, ex=ttl)
        return result is not None and result is not False
    except Exception:
        return True  # Fail-open: better to double-dispatch than not at all


async def release_dispatch_lock(agent_id: str) -> None:
    """Release the dispatch lock."""
    try:
        redis = await get_redis()
        await redis.delete(_lock_key(agent_id))
    except Exception:
        pass  # Best-effort


# ── Review Rejection Counter ────────────────────────────────────────────
# Counts how often a task was rejected by review.

MAX_REJECTIONS = 10


def _rejection_key(task_id: str) -> str:
    return RedisKeys.task_rejection_count(task_id)


async def increment_rejection_count(task_id: str) -> int:
    """Increment the rejection counter. Returns the new count."""
    redis = await get_redis()
    key = _rejection_key(task_id)
    count = await redis.incr(key)
    await redis.expire(key, 7 * 24 * 3600)  # 7-day TTL
    return count


async def get_rejection_count(task_id: str) -> int:
    """Read the current rejection counter."""
    redis = await get_redis()
    result = await redis.get(_rejection_key(task_id))
    return int(result) if result else 0
