"""
Task Queue Service — Redis-basierte FIFO-Queue pro Agent.

Wenn ein Agent bereits eine aktive Task bearbeitet, werden neue Tasks
in die Queue eingereiht. Der Watchdog verarbeitet die Queue periodisch.

Key-Schema: mc:agent:{agent_id}:task_queue  (Redis List, RPUSH/LPOP)
"""

import logging
from app.redis_client import RedisKeys, get_redis

logger = logging.getLogger("mc.task_queue")

def _queue_key(agent_id: str) -> str:
    return RedisKeys.agent_task_queue(agent_id)


async def enqueue_task(agent_id: str, task_id: str) -> None:
    """Task ans Ende der Agent-Queue haengen."""
    redis = await get_redis()
    await redis.rpush(_queue_key(agent_id), task_id)
    logger.info("Enqueued task %s for agent %s", task_id, agent_id)


async def dequeue_task(agent_id: str) -> str | None:
    """Naechste Task vom Anfang der Agent-Queue holen (FIFO)."""
    redis = await get_redis()
    result = await redis.lpop(_queue_key(agent_id))
    if result:
        task_id = result.decode() if isinstance(result, bytes) else result
        logger.info("Dequeued task %s for agent %s", task_id, agent_id)
        return task_id
    return None


async def queue_length(agent_id: str) -> int:
    """Anzahl wartender Tasks fuer diesen Agent."""
    redis = await get_redis()
    return await redis.llen(_queue_key(agent_id))


async def peek_queue(agent_id: str) -> list[str]:
    """Alle Tasks in der Queue anzeigen (ohne entfernen)."""
    redis = await get_redis()
    items = await redis.lrange(_queue_key(agent_id), 0, -1)
    return [i.decode() if isinstance(i, bytes) else i for i in items]


# ── Pending Dispatch Queue ──────────────────────────────────────────────
# Tasks die wegen fehlender Agent-Session nicht gepusht werden konnten.
# Watchdog liefert nach, sobald Agent online ist.

def _pending_key(agent_id: str) -> str:
    return RedisKeys.agent_pending_dispatch(agent_id)


async def enqueue_pending_dispatch(agent_id: str, task_id: str) -> None:
    """Task in Pending-Dispatch-Queue legen (Agent hat keine aktive Session)."""
    redis = await get_redis()
    await redis.rpush(_pending_key(agent_id), task_id)
    logger.info("Pending dispatch queued: task %s for agent %s", task_id, agent_id)


async def dequeue_pending_dispatch(agent_id: str) -> str | None:
    """Naechste pending Task holen (FIFO)."""
    redis = await get_redis()
    result = await redis.lpop(_pending_key(agent_id))
    if result:
        task_id = result.decode() if isinstance(result, bytes) else result
        logger.info("Pending dispatch dequeued: task %s for agent %s", task_id, agent_id)
        return task_id
    return None


async def pending_dispatch_length(agent_id: str) -> int:
    """Anzahl wartender Pending-Dispatch Tasks."""
    redis = await get_redis()
    return await redis.llen(_pending_key(agent_id))


# ── Dispatch Lock ───────────────────────────────────────────────────────
# Verhindert Race-Conditions: Nur ein Dispatch-Prozess pro Agent gleichzeitig.

def _lock_key(agent_id: str) -> str:
    return RedisKeys.agent_dispatch_lock(agent_id)


async def acquire_dispatch_lock(agent_id: str, ttl: int = 30) -> bool:
    """Dispatch-Lock fuer einen Agent setzen (SET NX EX). True = Lock erhalten.

    Fail-open: Bei Redis-Fehler wird True zurueckgegeben (Lock durchlassen).
    """
    try:
        redis = await get_redis()
        result = await redis.set(_lock_key(agent_id), "1", nx=True, ex=ttl)
        return result is not None and result is not False
    except Exception:
        return True  # Fail-open: besser doppelt dispatchen als gar nicht


async def release_dispatch_lock(agent_id: str) -> None:
    """Dispatch-Lock freigeben."""
    try:
        redis = await get_redis()
        await redis.delete(_lock_key(agent_id))
    except Exception:
        pass  # Best-effort


# ── Review Rejection Counter ────────────────────────────────────────────
# Zaehlt wie oft ein Task vom Review abgelehnt wurde.

MAX_REJECTIONS = 10


def _rejection_key(task_id: str) -> str:
    return RedisKeys.task_rejection_count(task_id)


async def increment_rejection_count(task_id: str) -> int:
    """Rejection-Counter erhoehen. Returns neuer Zaehlerstand."""
    redis = await get_redis()
    key = _rejection_key(task_id)
    count = await redis.incr(key)
    await redis.expire(key, 7 * 24 * 3600)  # 7 Tage TTL
    return count


async def get_rejection_count(task_id: str) -> int:
    """Aktuellen Rejection-Counter lesen."""
    redis = await get_redis()
    result = await redis.get(_rejection_key(task_id))
    return int(result) if result else 0
