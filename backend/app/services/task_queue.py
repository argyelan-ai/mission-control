"""
Task Queue Service — Redis-based FIFO queue per agent.

If an agent is already working on an active task, new tasks are
enqueued. The watchdog processes the queue periodically.

Key schema: mc:agent:{agent_id}:task_queue  (Redis List, RPUSH/LPOP)
"""

import logging
from app.redis_client import RedisKeys, get_redis

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
