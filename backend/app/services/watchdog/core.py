"""WatchdogService — orchestrator for all periodic checks."""

import asyncio
import logging
from datetime import datetime

from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.redis_client import RedisKeys, get_redis
from app.utils import utcnow

from app.services.watchdog.health_checks import HealthChecksMixin
from app.services.watchdog.session_monitor import SessionMonitorMixin
from app.services.watchdog.task_monitor import TaskMonitorMixin

logger = logging.getLogger("mc.watchdog")

# Global set to hold references to background tasks (prevents GC)
_background_tasks: set[asyncio.Task] = set()


def _create_background_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


class WatchdogService(HealthChecksMixin, SessionMonitorMixin, TaskMonitorMixin):
    """Periodic monitoring of all critical components.

    Inherits from three mixins:
    - HealthChecksMixin: agent health, system health, approvals
    - SessionMonitorMixin: heartbeat health (DB-based) — post Phase 29, no RPC
    - TaskMonitorMixin: phases, queues, dispatches
    """

    def __init__(self, interval: int = 30):
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._running = False

        self._last_check_at: datetime | None = None
        self._checks_total = 0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_check_at(self) -> datetime | None:
        return self._last_check_at

    @property
    def checks_total(self) -> int:
        return self._checks_total

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        # Phase 29: RPC state-change callback registration removed (Gateway sunset)
        logger.info("Watchdog started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Watchdog stopped")

    async def _run_loop(self) -> None:
        await asyncio.sleep(10)
        while self._running:
            try:
                if await self._acquire_lock():
                    await self._check_all()
                    self._last_check_at = utcnow()
                    self._checks_total += 1
                else:
                    logger.debug("Watchdog skipped — another worker holds the lock")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Watchdog check error: %s", e)
            await asyncio.sleep(self._interval)

    async def _acquire_lock(self) -> bool:
        """Redis lock so only one worker per cycle runs the checks."""
        try:
            redis = await get_redis()
            acquired = await redis.set(
                RedisKeys.watchdog_lock(), "1", nx=True, ex=self._interval * 3
            )
            return bool(acquired)
        except Exception:
            return True

    async def _check_all(self) -> None:
        from app.config import settings
        from app.services.operations import get_system_mode

        system_mode = await get_system_mode()

        async with AsyncSession(engine, expire_on_commit=False) as session:
            # Health checks always run (even when HALTED)
            await self._check_agent_health(session)
            # Phase 29: _check_rpc_connection removed (Gateway sunset — no RPC to check)
            await self._check_expired_approvals(session)

            # Phase 29: Gateway sunset — no more sessions_list() polls.
            # - _check_agent_sessions/_check_session_recovery/_sync_agent_tokens/
            #   _compact_overflowed_sessions/_check_session_health are dropped.
            # - Heartbeat-based offline detection (DB-only) stays active.
            await self._check_heartbeat_health(session)

            # Task checks (when HALTED only passive checks, no dispatch)
            await self._check_phase_completions(session)
            await self._check_blocked_tasks(session)
            await self._check_dependency_zombies(session)
            await self._check_review_tasks(session)
            await self._check_stuck_orchestrator_close(session)

            # Orphan recovery: tasks stuck in in_progress without agent heartbeat
            recovered = await self._recover_orphaned_tasks(session)
            if recovered > 0:
                logger.info("[WATCHDOG] %d orphaned tasks reset to inbox", recovered)

            if system_mode != "halted":
                # Phase 4A: Promote orchestrator — auto-promote/approval for planned tasks
                if settings.enable_promote_orchestrator:
                    from app.services.dispatch_gating import process_planned_tasks
                    try:
                        stats = await process_planned_tasks(session)
                        if stats["checked"] > 0:
                            logger.info(
                                "Promote orchestrator: %d checked, %d promoted, %d approval, %d manual",
                                stats["checked"], stats["promoted"], stats["approval"], stats["manual"],
                            )
                    except Exception as e:
                        logger.warning("Promote orchestrator failed: %s", e)

                # Undispatched recovery: tasks assigned but never dispatched
                # (e.g. backend was offline at creation time, pairing error)
                await self._check_undispatched_tasks(session)

                # Phase 29: legacy queue-recovery paths dropped
                # (_recover_aborted_tasks/_process_task_queues/_process_pending_dispatches
                # were all gateway-only — D-07 stale-task ownership lives in
                # task_runner._check_dispatch_ack). _check_spawn_timeouts was
                # gateway-only (TODO Phase 31: cli-bridge task-queue timeouts).

            db_latency, redis_latency = await self._check_system_health(session)
            await self._collect_system_metrics(db_latency, redis_latency)

            # Token harvester: Phase 31 — reads JSONL transcripts, inserts
            # model_usage_events. Runs every 5 cycles (~2.5 min at 30s interval).
            # try/except like the old collect_session_costs — never crash the watchdog.
            if self._checks_total % 5 == 0:
                try:
                    from app.services.token_harvester import run_harvest
                    harvest_stats = await run_harvest(session)
                    if harvest_stats["new_events"] > 0:
                        logger.info(
                            "Token harvester: %d new events, %d private skipped",
                            harvest_stats["new_events"],
                            harvest_stats["skipped_private"],
                        )
                        from app.services.cost_collector import check_budget_warnings
                        await check_budget_warnings(session)
                except Exception as e:
                    logger.debug("Token harvester error: %s", e)

        # Weekly digest (outside the DB session, creates its own)
        await self._check_weekly_digest()
