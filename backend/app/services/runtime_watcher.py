"""Runtime Watcher (Runtime & Model Management v1, ADR-053).

"Engine leads, MC follows": the active model is changed at the inference
engine (vLLM / LM Studio / OpenAI-compatible); this service detects it.

Every tick it probes all enabled probeable runtimes via ``/v1/models``:
  1. writes a live status snapshot to Redis (cockpit feed for /runtimes),
  2. confirms model drift with TWO consecutive identical probes (guards
     against flapping during engine warm-up), then persists the new
     ``model_identifier``, invalidates the resolver cache, emits
     ``runtime.model_changed`` and flags bound cli-bridge agents,
  3. runs the propagation sync pass for flagged agents that are now idle.

Supersedes decision D-22 (periodic probing rejected) — see ADR-053.
Same lifecycle pattern as IntelligenceService: singleton, asyncio loop,
Redis lock for multi-worker dedup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.services.agent_runtime_switch import (
    _PROBEABLE_RUNTIME_TYPES,
    probe_runtime_model,
)
from app.services.runtime_model_resolver import (
    invalidate_cached_model,
    session_scope,
)
from app.services.runtime_propagation import (
    mark_agents_for_sync,
    sync_pending_agents,
)

logger = logging.getLogger(__name__)

# Emit runtime.unreachable only after this many consecutive failed probes
# (transient blips and engine restarts must not spam the activity feed).
UNREACHABLE_EVENT_THRESHOLD = 3
_STARTUP_GRACE = 20  # seconds — let DB/Redis/other services come up first


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeWatcher:
    def __init__(self, interval: int | None = None) -> None:
        self._interval = interval or settings.runtime_watcher_interval
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if not settings.runtime_watcher_enabled:
            logger.info("runtime watcher disabled via settings")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("runtime watcher started (interval=%ss)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        await asyncio.sleep(_STARTUP_GRACE)
        while self._running:
            try:
                if await self._acquire_lock():
                    await self.tick()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                logger.exception("runtime watcher tick failed")
            await asyncio.sleep(self._interval)

    async def _acquire_lock(self) -> bool:
        """One worker per tick. Redis down → run anyway (single-worker default)."""
        try:
            redis = await get_redis()
            return bool(
                await redis.set(
                    RedisKeys.runtime_watcher_lock(), "1",
                    nx=True, ex=max(self._interval - 5, 10),
                )
            )
        except Exception:  # noqa: BLE001
            return True

    async def tick(self, session: AsyncSession | None = None) -> None:
        """One probe + sync pass. ``session`` is injectable for tests."""
        if session is not None:
            await self._tick_inner(session)
            return
        async with session_scope() as own_session:
            await self._tick_inner(own_session)

    async def _tick_inner(self, session: AsyncSession) -> None:
        result = await session.exec(
            select(Runtime).where(
                Runtime.enabled.is_(True),
                Runtime.runtime_type.in_(sorted(_PROBEABLE_RUNTIME_TYPES)),
            )
        )
        for runtime in result.all():
            await self._probe_one(session, runtime)
        await sync_pending_agents(session)

    async def _probe_one(self, session: AsyncSession, runtime: Runtime) -> None:
        started = time.monotonic()
        served = await probe_runtime_model(runtime)
        latency_ms = int((time.monotonic() - started) * 1000)
        redis = await get_redis()

        if served is None:
            fails = await self._bump_failures(redis, runtime.slug)
            await self._write_live(
                redis, runtime.slug,
                reachable=False, served_model=None, latency_ms=None,
                consecutive_failures=fails,
            )
            if fails == UNREACHABLE_EVENT_THRESHOLD:
                await emit_event(
                    session,
                    "runtime.unreachable",
                    f"{runtime.slug}: endpoint unreachable "
                    f"({fails} consecutive probes)",
                    severity="warning",
                    detail={"slug": runtime.slug, "endpoint": runtime.endpoint},
                )
            return

        await redis.delete(self._fail_key(runtime.slug))
        await self._write_live(
            redis, runtime.slug,
            reachable=True, served_model=served, latency_ms=latency_ms,
            consecutive_failures=0,
        )
        if served != (runtime.model_identifier or ""):
            await self._handle_drift(session, redis, runtime, served)

    async def _handle_drift(
        self, session: AsyncSession, redis, runtime: Runtime, served: str
    ) -> None:
        key = RedisKeys.runtime_drift_candidate(runtime.slug)
        candidate = await redis.get(key)
        if isinstance(candidate, bytes):
            candidate = candidate.decode()
        if candidate != served:
            # First sighting (or the engine flapped to yet another model):
            # remember the candidate and wait for one confirming probe.
            await redis.setex(key, self._interval * 3, served)
            return

        await redis.delete(key)
        old = runtime.model_identifier
        runtime.model_identifier = served
        session.add(runtime)
        await session.commit()
        await session.refresh(runtime)
        await invalidate_cached_model(runtime.slug)
        logger.info("runtime %s model drift confirmed: %r → %r",
                    runtime.slug, old, served)
        await emit_event(
            session,
            "runtime.model_changed",
            f"{runtime.slug}: {old or 'n/a'} → {served}",
            severity="info",
            detail={"slug": runtime.slug, "old_model": old, "new_model": served},
        )
        await mark_agents_for_sync(session, runtime)

    # ── Redis helpers ────────────────────────────────────────────────────

    @staticmethod
    def _fail_key(slug: str) -> str:
        return f"{RedisKeys.runtime_live(slug)}:fails"

    async def _bump_failures(self, redis, slug: str) -> int:
        fails = int(await redis.incr(self._fail_key(slug)))
        await redis.expire(self._fail_key(slug), self._interval * 10)
        return fails

    async def _write_live(self, redis, slug: str, **fields) -> None:
        payload = {"last_probe_at": _utcnow_iso(), **fields}
        await redis.setex(
            RedisKeys.runtime_live(slug), self._interval * 3, json.dumps(payload)
        )


runtime_watcher = RuntimeWatcher()
