"""Health Checks Mixin — Agent-Health, System-Health, Approvals, Weekly Digest."""

import json
import logging

import psutil
from sqlalchemy import text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.utils import ensure_aware, utcnow

logger = logging.getLogger("mc.watchdog")

# Thresholds
AGENT_RESTART_TIMEOUT_MINUTES = 3
LATENCY_WARNING_MS = 2000


class HealthChecksMixin:
    """Agent-Health, System-Health, Approval-Expiry, Weekly Digest."""

    async def _check_agent_health(self, session: AsyncSession) -> None:
        """Agents im 'restarting' Status pruefen — Timeout nach AGENT_RESTART_TIMEOUT_MINUTES."""
        result = await session.exec(
            select(Agent).where(Agent.status == "restarting")
        )
        agents = result.all()
        now = utcnow()

        for agent in agents:
            updated = ensure_aware(agent.updated_at)
            minutes_ago = (now - updated).total_seconds() / 60

            if minutes_ago >= AGENT_RESTART_TIMEOUT_MINUTES:
                agent.status = "error"
                agent.updated_at = now
                session.add(agent)
                await session.commit()

                await emit_event(
                    session,
                    "agent.restart_failed",
                    f"{agent.emoji or '🤖'} {agent.name}: Neustart fehlgeschlagen (keine Gateway-Session seit {int(minutes_ago)}min)",
                    severity="error",
                    agent_id=agent.id,
                    board_id=agent.board_id,
                    detail={"agent_name": agent.name, "restarting_since_minutes": round(minutes_ago, 1)},
                )
                logger.warning("Agent %s restart failed (no session for %dmin)", agent.name, int(minutes_ago))

    async def _check_expired_approvals(self, session: AsyncSession) -> None:
        """Abgelaufene Approvals automatisch auf 'expired' setzen."""
        from app.models.approval import Approval

        now = utcnow()
        result = await session.exec(
            select(Approval).where(
                Approval.status == "pending",
                Approval.expires_at.isnot(None),  # type: ignore[arg-type]
                Approval.expires_at < now,  # type: ignore[operator]
            )
        )
        expired_approvals = result.all()

        for approval in expired_approvals:
            approval.status = "expired"
            approval.resolved_at = now
            session.add(approval)
            await emit_event(
                session,
                "approval.expired",
                f"Approval abgelaufen: {approval.description}",
                severity="warning",
                board_id=approval.board_id,
                agent_id=approval.agent_id,
                detail={"approval_id": str(approval.id), "action_type": approval.action_type},
            )

        if expired_approvals:
            await session.commit()
            logger.info("Auto-expired %d approval(s)", len(expired_approvals))

        # Reconciliation: Stale pending Approvals deren Task den Approval-Grund verlassen hat
        from app.services.approval_cleanup import reconcile_stale_approvals
        await reconcile_stale_approvals(session)

        # Phase 29: _sweep_orphan_sessions removed (Gateway sunset — no
        # gateway sessions to clean up).
        # TODO Phase 31: add cli-bridge container health-check + orphan
        # task-queue sweep if cli-bridge accumulates abandoned queues.

    async def _check_system_health(self, session: AsyncSession) -> tuple[float | None, float | None]:
        """DB und Redis Latenz pruefen. Gibt (db_latency_ms, redis_latency_ms) zurueck."""
        db_latency_ms: float | None = None
        redis_latency_ms: float | None = None

        try:
            t0 = utcnow()
            await session.execute(text("SELECT 1"))
            db_latency_ms = (utcnow() - t0).total_seconds() * 1000
            if db_latency_ms > LATENCY_WARNING_MS:
                await emit_event(
                    session,
                    "system.slow_response",
                    f"Datenbank antwortet langsam ({int(db_latency_ms)}ms)",
                    severity="warning",
                    detail={"component": "database", "latency_ms": round(db_latency_ms, 1)},
                )
        except Exception as e:
            await emit_event(
                session,
                "system.component_down",
                f"Datenbank nicht erreichbar: {e}",
                severity="error",
                detail={"component": "database", "error": str(e)},
            )

        try:
            redis = await get_redis()
            t0 = utcnow()
            await redis.ping()
            redis_latency_ms = (utcnow() - t0).total_seconds() * 1000
            if redis_latency_ms > LATENCY_WARNING_MS:
                await emit_event(
                    session,
                    "system.slow_response",
                    f"Redis antwortet langsam ({int(redis_latency_ms)}ms)",
                    severity="warning",
                    detail={"component": "redis", "latency_ms": round(redis_latency_ms, 1)},
                )
        except Exception as e:
            await emit_event(
                session,
                "system.component_down",
                f"Redis nicht erreichbar: {e}",
                severity="error",
                detail={"component": "redis", "error": str(e)},
            )

        return db_latency_ms, redis_latency_ms

    async def _collect_system_metrics(self, db_latency_ms: float | None, redis_latency_ms: float | None) -> None:
        """System-Metriken (CPU/RAM/Disk) sammeln und in Redis speichern."""
        try:
            cpu_pct = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")

            snapshot = {
                "ts": utcnow().isoformat(),
                "cpu_pct": round(cpu_pct, 1),
                "memory_pct": round(mem.percent, 1),
                "memory_used_gb": round(mem.used / (1024 ** 3), 1),
                "memory_total_gb": round(mem.total / (1024 ** 3), 1),
                "disk_pct": round(disk.percent, 1),
                "disk_used_gb": round(disk.used / (1024 ** 3), 0),
                "disk_total_gb": round(disk.total / (1024 ** 3), 0),
                "db_latency_ms": round(db_latency_ms, 2) if db_latency_ms is not None else None,
                "redis_latency_ms": round(redis_latency_ms, 2) if redis_latency_ms is not None else None,
            }

            snapshot_json = json.dumps(snapshot)
            redis = await get_redis()

            history_key = RedisKeys.system_metrics_history()
            await redis.lpush(history_key, snapshot_json)
            await redis.ltrim(history_key, 0, 59)

            current_key = RedisKeys.system_metrics_current()
            await redis.set(current_key, snapshot_json, ex=120)

            logger.debug(
                "System metrics: CPU=%.1f%% RAM=%.1f%% Disk=%.1f%%",
                cpu_pct, mem.percent, disk.percent,
            )
        except Exception as e:
            logger.error("Failed to collect system metrics: %s", e)

    async def _check_weekly_digest(self) -> None:
        """Sonntags einen woechentlichen Digest generieren."""
        if utcnow().weekday() != 6:  # 0=Mo, 6=So
            return
        from app.services.auto_memory import generate_weekly_digest
        from app.services.watchdog.core import _create_background_task
        _create_background_task(generate_weekly_digest())
