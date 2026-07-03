"""Embedding retry loop — background singleton for MSY-04 (Phase 5).

When ``embedding_service.embed(...)`` fails (Spark / LM Studio offline),
the ``BoardMemory`` row still lands in Postgres (fail-soft). The corresponding
embedding job is pushed onto a Redis LIST
(``mc:embeddings:retry``); this class drains the queue in the background
once the embedding service is reachable again.

Mirrors the singleton pattern from ``intelligence.py`` (used 3x in the
codebase: intelligence, watchdog, task_runner). Lifespan registration
happens in ``main.py`` analogous to ``intelligence.start()`` /
``intelligence.stop()``.

Backoff: 60s, 5min, 15min, 1h, 6h × 4 (sum ~24h, max 8 attempts).
Queue cap: 1000 entries (Pitfall 3, RESEARCH.md). At cap → WARN +
skip enqueue (the BoardMemory row stays intact — it just won't be
retried further).

Acceptance contract (Plan 05-02):
- Dispatch never blocks on embedding (D-17). ``index_memory`` fail-soft +
  retry enqueue is the entry-point that satisfies this — see
  ``test_dispatch_unaffected_by_outage``.
- Recovery on service return is automatic (D-20). The drain probes
  ``embedding_service.is_available()`` before each tick.
- Drop after 8 attempts is loud (ERROR log + dropped counter increments)
  so the operator notices in production logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from app.config import settings
from app.redis_client import RedisKeys, get_redis

logger = logging.getLogger("mc.embedding_retry")

# Backoff schedule per attempt counter (1-based).
# Index 0 = after the 1st failed attempt.  Tuple sums to ~24h cumulative.
RETRY_BACKOFFS_SEC: tuple[int, ...] = (60, 300, 900, 3600, 21600, 21600, 21600, 21600)
MAX_ATTEMPTS = len(RETRY_BACKOFFS_SEC)  # 8
MAX_QUEUE_LEN = 1000  # Pitfall 3: hard cap against unbounded growth
DRAIN_BATCH_SIZE = 50  # max items per tick — bounded recovery work per tick

# In-process drop counter (for observability — D-21).
# Read via get_dropped_total() — incremented after every max-attempts drop.
_dropped_total = 0


def get_dropped_total() -> int:
    """Returns the cumulative count of retry payloads dropped after MAX_ATTEMPTS.

    Resettable only by process restart (per D-21: in-process counter; long-term
    metric is the WARN/ERROR log line). Production observability via the same
    pattern intelligence.py uses for `_cycles_total`.
    """
    return _dropped_total


async def enqueue(memory_id: uuid.UUID, attempt: int = 1) -> bool:
    """Push a retry payload onto ``mc:embeddings:retry``.

    Returns ``True`` if enqueued, ``False`` if the queue cap is full or a Redis
    error occurred. Caller (memory_indexing._enqueue_embedding_retry) wraps this and
    treats False as "BoardMemory still lands; just no retry tracking".

    Pitfall 3 (RESEARCH.md): when ``LLEN >= MAX_QUEUE_LEN`` the enqueue is
    rejected with WARN log; the row stays in DB without an embedding. Better
    than unbounded Redis growth during a 24h+ outage.
    """
    try:
        redis = await get_redis()
        qlen = await redis.llen(RedisKeys.embedding_retry())
        if qlen >= MAX_QUEUE_LEN:
            logger.warning(
                "Embedding retry queue full (%d) — dropping enqueue for %s",
                qlen, memory_id,
            )
            return False
        delay = RETRY_BACKOFFS_SEC[min(attempt - 1, MAX_ATTEMPTS - 1)]
        next_at = int(time.time()) + delay
        payload = json.dumps({
            "memory_id": str(memory_id),
            "attempt": attempt,
            "next_at": next_at,
        })
        await redis.rpush(RedisKeys.embedding_retry(), payload)
        logger.info(
            "embedding_retry: enqueued memory %s (attempt=%d, next_at=%d, delay=%ds)",
            memory_id, attempt, next_at, delay,
        )
        return True
    except Exception as e:
        logger.warning("embedding_retry enqueue failed: %s", e)
        return False


class EmbeddingRetryLoop:
    """Singleton background loop. Mirror of ``IntelligenceService``.

    Lifecycle::

        # main.py lifespan
        await embedding_retry.start()        # schedules _run_loop as Task
        ...
        await embedding_retry.stop()         # cancels + awaits Task

    Tests bypass the loop entirely::

        loop = EmbeddingRetryLoop(interval=99999)  # never auto-fires
        processed = await loop._drain_once()       # direct call
    """

    def __init__(self, interval: Optional[int] = None):
        self._interval = interval or settings.embedding_retry_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("EmbeddingRetry started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("EmbeddingRetry stopped")

    async def _run_loop(self) -> None:
        # Grace period — lifespan is still attaching to Qdrant + DB
        # (mirrors intelligence.py:100 — same 20s window).
        await asyncio.sleep(20)
        while self._running:
            try:
                await self._drain_once()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("EmbeddingRetry tick error: %s", e)
            await asyncio.sleep(self._interval)

    async def _drain_once(self) -> int:
        """One drain iteration. Returns the number of successfully processed items.

        Directly callable from tests (bypasses ``_run_loop`` + grace period).

        Strategy:
        1. Probe ``embedding_service.is_available()`` (2s timeout). If down:
           skip — no point draining the queue if every embed() will fail again.
        2. Pop up to DRAIN_BATCH_SIZE items. For each:
           - next_at still in the future → push back to the TAIL and break (items
             in the LIST are FIFO + monotonically increasing in practice).
           - otherwise: process_one() → success → done; failure → re-enqueue with
             attempt+1, or drop if attempt >= MAX_ATTEMPTS.
        """
        global _dropped_total

        try:
            redis = await get_redis()
        except Exception as e:
            logger.warning("EmbeddingRetry: Redis unavailable: %s", e)
            return 0

        # Probe before draining — no point draining if the service is down.
        from app.services.embedding_service import embedding_service
        if not await embedding_service.is_available():
            logger.debug("EmbeddingRetry: embedding service still unavailable, skipping cycle")
            return 0

        processed = 0
        for _ in range(DRAIN_BATCH_SIZE):
            raw = await redis.lpop(RedisKeys.embedding_retry())
            if raw is None:
                break
            try:
                item = json.loads(raw)
            except Exception:
                logger.warning("EmbeddingRetry: malformed payload dropped: %r", raw)
                continue

            now = int(time.time())
            if item.get("next_at", 0) > now:
                # not ready yet — push back to the TAIL of the LIST
                await redis.rpush(RedisKeys.embedding_retry(), raw)
                # Heuristic: all further items likely carry similar or
                # later next_at values — break instead of popping+pushing
                # each one individually.
                break

            success = await self._process_one(item)
            if success:
                processed += 1
            else:
                # re-enqueue with the next attempt
                attempt = int(item.get("attempt", 1))
                if attempt >= MAX_ATTEMPTS:
                    _dropped_total += 1
                    logger.error(
                        "EmbeddingRetry: dropping memory %s after %d attempts (total dropped=%d)",
                        item.get("memory_id"), attempt, _dropped_total,
                    )
                else:
                    await enqueue(uuid.UUID(item["memory_id"]), attempt=attempt + 1)
        return processed

    async def _process_one(self, item: dict) -> bool:
        """Attempts to generate the embedding for a memory entry +
        Qdrant upsert.

        Returns ``True`` on success (item stays removed), ``False`` on
        transient errors (item gets re-enqueued or dropped by the caller).

        "Success without embedding" edge cases (memory deleted, layer=None, empty
        text) also return True — there's nothing left to do and a
        re-enqueue would be pointless.
        """
        # Lazy imports analogous to memory_indexing.py — qdrant + DB are only
        # fully available in the container.
        from app.database import engine
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.models.memory import BoardMemory
        from app.services.embedding_service import embedding_service
        from app.services.memory_indexing import layer_for
        from app.services.qdrant_service import qdrant_service

        try:
            memory_id = uuid.UUID(item["memory_id"])
        except (KeyError, ValueError):
            logger.warning("EmbeddingRetry: malformed memory_id in payload: %r", item)
            return True  # nothing to do, "success" so it isn't re-enqueued

        async with AsyncSession(engine, expire_on_commit=False) as session:
            memory = await session.get(BoardMemory, memory_id)
            if not memory:
                logger.info("EmbeddingRetry: memory %s gone (deleted) — skipping", memory_id)
                return True
            layer = layer_for(memory)
            if layer is None:
                return True
            text_parts = []
            if memory.title:
                text_parts.append(memory.title)
            if memory.content:
                text_parts.append(memory.content)
            text = "\n".join(text_parts).strip()
            if not text:
                return True

        try:
            vec = await embedding_service.embed(text)
        except Exception as e:
            logger.warning("EmbeddingRetry: embed still failing for %s: %s", memory_id, e)
            return False

        payload_dict = {
            "memory_type": memory.memory_type,
            "agent_id": str(memory.agent_id) if memory.agent_id else None,
            "board_id": str(memory.board_id) if memory.board_id else None,
            "title": memory.title or "",
            "content_preview": (memory.content or "")[:500],
            "created_at": memory.created_at.timestamp() if memory.created_at else 0.0,
            "tags": memory.tags or [],
        }
        try:
            await qdrant_service.upsert(
                layer=layer,
                memory_id=str(memory.id),
                vector=vec,
                payload=payload_dict,
            )
        except Exception as e:
            logger.warning("EmbeddingRetry: qdrant upsert failed for %s: %s", memory_id, e)
            return False

        logger.info("EmbeddingRetry: drained memory %s (layer=%s)", memory_id, layer)
        return True


# Module-level singleton — analogous to intelligence.py:720 / watchdog / task_runner.
# Lifespan in main.py calls .start() / .stop() — no auto-start here (Pitfall 4).
embedding_retry = EmbeddingRetryLoop()
