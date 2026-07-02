"""Embedding-Retry-Loop — Background-Singleton fuer MSY-04 (Phase 5).

Wenn ``embedding_service.embed(...)`` fehlschlaegt (Spark / LM Studio offline),
landet die ``BoardMemory``-Zeile trotzdem in Postgres (fail-soft). Der dazu-
gehoerige Embedding-Auftrag wird in eine Redis-LIST gepusht
(``mc:embeddings:retry``); diese Klasse drained die Queue im Hintergrund,
sobald der Embedding-Service wieder erreichbar ist.

Mirror der Singleton-Pattern aus ``intelligence.py`` (3-fach im Codebase
verwendet: intelligence, watchdog, task_runner). Lifespan-Registrierung
erfolgt in ``main.py`` analog zu ``intelligence.start()`` /
``intelligence.stop()``.

Backoff: 60s, 5min, 15min, 1h, 6h × 4 (Summe ~24h, max 8 Versuche).
Queue-Cap: 1000 Eintraege (Pitfall 3, RESEARCH.md). Bei Cap → WARN +
skip enqueue (die BoardMemory-Zeile bleibt erhalten — sie wird halt nicht
weiter retried).

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

# Backoff schedule pro Attempt-Zaehler (1-basiert).
# Index 0 = nach 1. Fehlversuch.  Tuple sums to ~24h cumulative.
RETRY_BACKOFFS_SEC: tuple[int, ...] = (60, 300, 900, 3600, 21600, 21600, 21600, 21600)
MAX_ATTEMPTS = len(RETRY_BACKOFFS_SEC)  # 8
MAX_QUEUE_LEN = 1000  # Pitfall 3: harter Cap gegen unbegrenztes Wachstum
DRAIN_BATCH_SIZE = 50  # max Items pro Tick — bounded recovery work per tick

# In-process Drop-Counter (fuer Observability — D-21).
# Gelesen via get_dropped_total() — wird nach jedem max-attempts Drop incremented.
_dropped_total = 0


def get_dropped_total() -> int:
    """Returns the cumulative count of retry payloads dropped after MAX_ATTEMPTS.

    Resettable only by process restart (per D-21: in-process counter; long-term
    metric is the WARN/ERROR log line). Production observability via the same
    pattern intelligence.py uses for `_cycles_total`.
    """
    return _dropped_total


async def enqueue(memory_id: uuid.UUID, attempt: int = 1) -> bool:
    """Push einen Retry-Payload auf ``mc:embeddings:retry``.

    Returns ``True`` wenn enqueued, ``False`` wenn Queue-Cap voll oder Redis-
    Fehler. Caller (memory_indexing._enqueue_embedding_retry) wraps this and
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
    """Singleton Background-Loop. Mirror von ``IntelligenceService``.

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
        # Grace Period — Lifespan haengt sich noch an Qdrant + DB
        # (Mirror intelligence.py:100 — same 20s window).
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
        """Eine Drain-Iteration. Returnt Anzahl erfolgreich verarbeiteter Items.

        Direkt aufrufbar aus Tests (umgeht ``_run_loop`` + grace period).

        Strategie:
        1. Probe ``embedding_service.is_available()`` (2s timeout). Wenn down:
           skip — kein Sinn die Queue zu leeren wenn jeder embed() wieder fail't.
        2. Pop bis zu DRAIN_BATCH_SIZE Items. Fuer jedes:
           - next_at noch in Zukunft → zurueck an die TAIL und break (Items
             im LIST sind FIFO + monoton wachsend in der Praxis).
           - sonst: process_one() → success → done; failure → re-enqueue mit
             attempt+1, oder drop wenn attempt >= MAX_ATTEMPTS.
        """
        global _dropped_total

        try:
            redis = await get_redis()
        except Exception as e:
            logger.warning("EmbeddingRetry: Redis unavailable: %s", e)
            return 0

        # Probe vor dem Drain — kein Sinn zu drainen wenn Service down ist.
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
                # noch nicht reif — zurueck an die TAIL der LIST
                await redis.rpush(RedisKeys.embedding_retry(), raw)
                # Heuristik: alle weiteren Items duerften aehnliche oder
                # spaetere next_at-Werte tragen — break statt jedes einzeln zu
                # popen+rpushen.
                break

            success = await self._process_one(item)
            if success:
                processed += 1
            else:
                # erneut enqueuen mit naechstem Attempt
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
        """Versucht das Embedding fuer einen Memory-Eintrag zu generieren +
        Qdrant-Upsert.

        Returnt ``True`` bei Erfolg (Item bleibt entfernt), ``False`` bei
        transienten Fehlern (Item wird re-enqueued oder gedropt vom Caller).

        "Erfolg ohne Embedding" Edge-Cases (Memory geloescht, layer=None, leerer
        Text) returnen ebenfalls True — es gibt nichts mehr zu tun und ein
        Re-Enqueue waere sinnlos.
        """
        # Lazy-Imports analog memory_indexing.py — qdrant + DB nur im
        # Container voll verfuegbar.
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
            return True  # nichts zu tun, "Erfolg" damit es nicht erneut enqueued wird

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


# Modul-level Singleton — analog zu intelligence.py:720 / watchdog / task_runner.
# Lifespan in main.py ruft .start() / .stop() — kein auto-start hier (Pitfall 4).
embedding_retry = EmbeddingRetryLoop()
