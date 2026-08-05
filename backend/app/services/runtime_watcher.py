"""Runtime Watcher (Runtime & Model Management v1, ADR-054).

"Engine leads, MC follows": the active model is changed at the inference
engine (vLLM / LM Studio / OpenAI-compatible); this service detects it.

Every tick it probes all enabled probeable runtimes via ``/v1/models``:
  1. writes a live status snapshot to Redis (cockpit feed for /runtimes),
  2. confirms model drift with TWO consecutive identical probes (guards
     against flapping during engine warm-up), then persists the new
     ``model_identifier``, invalidates the resolver cache, emits
     ``runtime.model_changed`` and flags bound cli-bridge agents,
  3. runs the propagation sync pass for flagged agents that are now idle.

PR5 adds two operational behaviours on top:
  - switch grace: a runtime marked in-flight by ``runtime_grace`` (recipe
    switch, manual start, cold load) is reported as ``switching`` instead of
    counted as a failure — planned downtime no longer pages the operator,
  - auto-recovery: a confirmed outage on a docker engine whose host answers
    again gets exactly ONE start attempt per cooldown window, giving up after
    two consecutive attempts (see :meth:`RuntimeWatcher._maybe_auto_recover`).

Supersedes decision D-22 (periodic probing rejected) — see ADR-054.
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
from app.services.host_resolver import resolve_host_for_runtime
from app.services.runtime_grace import (
    SOURCE_AUTO_RECOVERY,
    clear_switching,
    get_switching,
)
from app.services.runtime_manager import DOCKER_ENGINE_TYPES
from app.services.runtime_model_resolver import (
    invalidate_cached_model,
    session_scope,
)
from app.services.runtime_propagation import (
    mark_agents_for_sync,
    recreate_pending_agents,
    sync_pending_agents,
)

logger = logging.getLogger(__name__)

# Emit runtime.unreachable only after this many consecutive failed probes
# (transient blips and engine restarts must not spam the activity feed).
UNREACHABLE_EVENT_THRESHOLD = 3
_STARTUP_GRACE = 20  # seconds — let DB/Redis/other services come up first

# Auto-recovery (PR5). One attempt per runtime per cooldown window; after this
# many consecutive attempts that did not bring the engine back, stop and hand
# over to the operator rather than restarting in a loop.
AUTO_RECOVERY_COOLDOWN = 900  # 15 min — longer than a normal warmup
AUTO_RECOVERY_MAX_ATTEMPTS = 2
AUTO_RECOVERY_FAILURE_TTL = 6 * 3600  # attempts "age out" after 6h of quiet


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
        # CLI-Tool-Updates: recreate agents flagged by the CLI update check.
        # Runs after the model-sync pass so a same-tick model change is applied
        # by a plain restart before we (potentially) force-recreate.
        await recreate_pending_agents(session)

    async def _probe_one(self, session: AsyncSession, runtime: Runtime) -> None:
        started = time.monotonic()
        served = await probe_runtime_model(runtime)
        latency_ms = int((time.monotonic() - started) * 1000)
        redis = await get_redis()
        switching = await get_switching(runtime.slug, redis)

        if served is None:
            if switching is not None:
                # Planned downtime (recipe switch, cold load, recovery start).
                # No failure counting, no event — that combination is what
                # produced the notification storm on every switch. The 20-min
                # TTL on the marker guarantees we fall back to normal
                # alerting even if nobody ever clears it.
                await self._write_live(
                    redis, runtime.slug,
                    reachable=False, served_model=None, latency_ms=None,
                    consecutive_failures=await self._read_failures(redis, runtime.slug),
                    status="switching",
                    phase=switching.get("phase"),
                    switch_source=switching.get("source"),
                )
                return
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
            await self._maybe_auto_recover(session, redis, runtime, fails)
            return

        if switching is not None:
            # The engine answers again — whatever start put us in grace has
            # finished. This is the single place a switch window ends, so no
            # caller has to poll for readiness itself.
            await clear_switching(runtime.slug)
        await redis.delete(self._fail_key(runtime.slug))
        await redis.delete(RedisKeys.runtime_recovery_failures(runtime.slug))
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

    # ── Auto-recovery ────────────────────────────────────────────────────

    async def _maybe_auto_recover(
        self, session: AsyncSession, redis, runtime: Runtime, fails: int
    ) -> None:
        """One start attempt for a docker engine whose host is back but whose
        container is gone (PR5).

        The autostart flag file (``runtime_autostart``) only covers "start on
        host boot". After a hard crash the Spark comes back, the flag never
        fires for an already-running host, and the engine stays down until
        someone notices. This closes that gap — deliberately narrow:

        - only a CONFIRMED outage (fail counter at the unreachable threshold),
          never a single blip,
        - never during a planned switch (the caller returns early on grace),
        - only docker engines on an SSH-reachable host — nothing else can be
          started without operator context,
        - only when the box itself answers again, so we don't hammer a box
          that is simply off,
        - one attempt per 15 min (Redis SET-nx claim, which also makes this
          safe across workers),
        - and after two consecutive attempts that did not bring the engine
          back, we stop and tell the operator instead of retrying forever.

        Everything is best-effort: any failure here must leave the watcher
        exactly as functional as it was before.
        """
        if not settings.runtime_auto_recovery_enabled:
            return
        if fails < UNREACHABLE_EVENT_THRESHOLD:
            return
        if not runtime.enabled or runtime.runtime_type not in DOCKER_ENGINE_TYPES:
            return
        try:
            host = await resolve_host_for_runtime(session, runtime)
        except Exception as exc:  # noqa: BLE001
            logger.debug("auto-recovery: host resolution failed for %s: %s",
                         runtime.slug, exc)
            return
        if host is None or host.kind != "ssh":
            return

        failures = await self._read_recovery_failures(redis, runtime.slug)
        if failures >= AUTO_RECOVERY_MAX_ATTEMPTS:
            return  # given up already — the event was emitted at the transition

        if not await self._host_answers(runtime, host):
            return  # box is down, not just the container — nothing to recover

        if not await self._claim_recovery_cooldown(redis, runtime.slug):
            return

        # Counted BEFORE the attempt: a start call that returns ok but never
        # produces a serving engine must not be able to loop forever. The
        # counter is cleared by the next probe that actually sees the engine.
        attempt = await self._bump_recovery_failures(redis, runtime.slug)
        await emit_event(
            session,
            "runtime.auto_recovery_started",
            f"{runtime.slug}: host reachable but engine down — starting "
            f"(attempt {attempt}/{AUTO_RECOVERY_MAX_ATTEMPTS})",
            severity="info",
            detail={"slug": runtime.slug, "attempt": attempt,
                    "consecutive_failures": fails},
        )

        from app.services.runtime_manager import start_runtime
        from app.services.sparkrun_manager import _to_runtime_dict  # noqa: SLF001

        try:
            result = await start_runtime(
                _to_runtime_dict(runtime), host=host,
                grace_source=SOURCE_AUTO_RECOVERY,
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "message": f"start_runtime raised: {exc}"}

        if result.get("ok"):
            await emit_event(
                session,
                "runtime.auto_recovery_succeeded",
                f"{runtime.slug}: start accepted — {result.get('message')}",
                severity="info",
                detail={"slug": runtime.slug, "attempt": attempt},
            )
            return

        await emit_event(
            session,
            "runtime.auto_recovery_failed",
            f"{runtime.slug}: auto-recovery start failed — {result.get('message')}",
            severity="warning",
            detail={"slug": runtime.slug, "attempt": attempt,
                    "reason": result.get("message")},
        )
        if attempt >= AUTO_RECOVERY_MAX_ATTEMPTS:
            await emit_event(
                session,
                "runtime.auto_recovery_given_up",
                f"{runtime.slug}: {attempt} auto-recovery attempts failed — "
                f"no further attempts until an operator starts it",
                severity="warning",
                detail={"slug": runtime.slug, "attempts": attempt},
            )

    async def _host_answers(self, runtime: Runtime, host) -> bool:
        """Is the box itself up (container missing) or the whole box down?

        A trivial SSH command is the cheapest honest answer — `get_runtime_state`
        would do the same round trip plus a docker inspect we don't need.
        """
        from app.services.runtime_manager import _ssh_run  # noqa: SLF001

        try:
            _, _, exit_code = await _ssh_run("true", host=host, timeout=15)
        except Exception as exc:  # noqa: BLE001 — box unreachable is the norm here
            logger.debug("auto-recovery: %s host not reachable: %s",
                         runtime.slug, exc)
            return False
        return exit_code == 0

    # ── Redis helpers ────────────────────────────────────────────────────

    @staticmethod
    def _fail_key(slug: str) -> str:
        return f"{RedisKeys.runtime_live(slug)}:fails"

    async def _bump_failures(self, redis, slug: str) -> int:
        fails = int(await redis.incr(self._fail_key(slug)))
        await redis.expire(self._fail_key(slug), self._interval * 10)
        return fails

    async def _read_failures(self, redis, slug: str) -> int:
        """Current fail count WITHOUT incrementing (grace snapshots)."""
        try:
            return int(await redis.get(self._fail_key(slug)) or 0)
        except (TypeError, ValueError):
            return 0

    async def _read_recovery_failures(self, redis, slug: str) -> int:
        try:
            return int(await redis.get(RedisKeys.runtime_recovery_failures(slug)) or 0)
        except (TypeError, ValueError):
            return 0

    async def _bump_recovery_failures(self, redis, slug: str) -> int:
        key = RedisKeys.runtime_recovery_failures(slug)
        count = int(await redis.incr(key))
        await redis.expire(key, AUTO_RECOVERY_FAILURE_TTL)
        return count

    async def _claim_recovery_cooldown(self, redis, slug: str) -> bool:
        """SET-nx claim — the winner is the single worker that may act."""
        return bool(
            await redis.set(
                RedisKeys.runtime_recovery_cooldown(slug), "1",
                nx=True, ex=AUTO_RECOVERY_COOLDOWN,
            )
        )

    async def _write_live(self, redis, slug: str, **fields) -> None:
        payload = {"last_probe_at": _utcnow_iso(), **fields}
        await redis.setex(
            RedisKeys.runtime_live(slug), self._interval * 3, json.dumps(payload)
        )


runtime_watcher = RuntimeWatcher()
