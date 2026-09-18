"""Health Checks Mixin — Agent-Health, System-Health, Approvals, Weekly Digest."""

import json
import logging

import psutil
from sqlalchemy import text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
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
        """Check agents in 'restarting' status — timeout after AGENT_RESTART_TIMEOUT_MINUTES."""
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
        """Expiry-Handling fuer pending Approvals.

        Blocker-/Klaerungs-Approvals laufen NICHT still ab (Fix E, Incident
        2026-07-04: expired Approval → Task blieb fuer immer blocked, niemand
        erinnerte mehr). Stattdessen: Renewal +24h + Telegram-Reminder +
        Event. Alle anderen Approval-Typen expiren wie bisher.
        """
        from datetime import timedelta
        from app.models.approval import Approval
        from app.routers.approvals import _CORE_HANDLED_ACTION_TYPES
        from app.verticals import hooks as vertical_hooks

        # Typen, deren Task ohne Entscheidung dauerhaft haengen wuerde.
        renewable_types = {"blocker_decision", "clarification_question"}

        now = utcnow()
        result = await session.exec(
            select(Approval).where(
                Approval.status == "pending",
                Approval.expires_at.isnot(None),  # type: ignore[arg-type]
                Approval.expires_at < now,  # type: ignore[operator]
            )
        )
        expired_approvals = result.all()

        expired_count = 0
        # Approvals that actually landed in status=expired this pass (not
        # renewed) — the generic vertical hook fires for these AFTER the
        # batch commit below, mirroring the approve/reject resolution paths
        # in approvals.py (ADR-044, review finding F1: expiry was the only
        # resolution path that silently skipped overlay verticals).
        newly_expired_for_hooks: list[Approval] = []
        for approval in expired_approvals:
            if approval.action_type in renewable_types:
                payload = dict(approval.payload or {})
                payload["renewal_count"] = int(payload.get("renewal_count", 0)) + 1
                approval.payload = payload
                approval.expires_at = now + timedelta(hours=24)
                session.add(approval)

                agent_name = payload.get("blocked_agent_name") or payload.get("agent_name") or "Agent"
                task_title = payload.get("task_title") or approval.description[:60]
                try:
                    from app.services import operator_approvals
                    await operator_approvals.send_approval(
                        approval.id, agent_name, task_title,
                        f"⏰ Reminder ({payload['renewal_count'] * 24}h offen): "
                        f"{payload.get('question') or approval.description}",
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("Approval-Renewal Telegram failed: %s", e)

                await emit_event(
                    session,
                    "approval.renewed",
                    f"Approval seit {payload['renewal_count'] * 24}h offen: {approval.description}",
                    severity="warning",
                    board_id=approval.board_id,
                    agent_id=approval.agent_id,
                    detail={
                        "approval_id": str(approval.id),
                        "action_type": approval.action_type,
                        "renewal_count": payload["renewal_count"],
                    },
                )
                logger.info(
                    "Approval renewed (#%d): %s (%s)",
                    payload["renewal_count"], approval.id, approval.action_type,
                )
                continue

            approval.status = "expired"
            approval.resolved_at = now
            session.add(approval)
            expired_count += 1
            newly_expired_for_hooks.append(approval)
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
            for approval in newly_expired_for_hooks:
                if approval.action_type not in _CORE_HANDLED_ACTION_TYPES:
                    await vertical_hooks.run_approval_resolved_hooks(session, approval, "expired")
            if expired_count:
                logger.info("Auto-expired %d approval(s)", expired_count)

        # Reconciliation: stale pending approvals whose task has left the approval reason
        from app.services.approval_cleanup import reconcile_stale_approvals
        await reconcile_stale_approvals(session)

        # Phase 29: _sweep_orphan_sessions removed (Gateway sunset — no
        # gateway sessions to clean up).
        # TODO Phase 31: add cli-bridge container health-check + orphan
        # task-queue sweep if cli-bridge accumulates abandoned queues.

    async def _check_system_health(self, session: AsyncSession) -> tuple[float | None, float | None]:
        """Check DB and Redis latency. Returns (db_latency_ms, redis_latency_ms)."""
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

    async def _collect_system_metrics(
        self,
        db_latency_ms: float | None,
        redis_latency_ms: float | None,
        session: AsyncSession | None = None,
    ) -> None:
        """Collect system metrics (CPU/RAM/disk) and store them in Redis."""
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

            # Die Plattenpruefung sitzt hier, weil dieser Snapshot `disk` schon
            # gemessen hat — ein zweiter psutil-Aufruf waere eine zweite
            # Messung derselben Sache, mit der Chance, dass beide
            # auseinanderlaufen. session ist optional, weil der Aufrufer sie
            # hat (core._check_all) und emit_event sie braucht.
            if session is not None:
                await self._check_disk_watchdog(session, disk.percent, disk.free)
        except Exception as e:
            logger.error("Failed to collect system metrics: %s", e)

    async def _check_disk_watchdog(
        self, session: AsyncSession, disk_pct: float, free_bytes: int
    ) -> None:
        """Meldet eine volllaufende Platte — MELDET NUR.

        Loescht nichts, raeumt nichts auf, aendert keinen Task-Status. Der
        Betreiber entscheidet; ein Watchdog, der selbst Prune laufen laesst,
        koennte den laufenden Bau zerstoeren, den er schuetzen soll.

        Warum die Schwelle VOR 100 % liegt: am 2026-09-16 lief die Platte voll
        (75,8 GB Build-Cache) und `docker compose up --build` starb mitten im
        Layer-Schreiben. Eine Meldung bei 100 % waere eine Beschreibung des
        Zustands nach dem Schaden; 95 % ist die Warnung davor.

        ``critical`` statt ``warning``: `warning` landet in
        ``discord_notify.DIGEST_KEY`` und wartet dort bis zu
        ``DIGEST_WINDOW_SECONDS`` (1800) auf den Sammelversand — eine halbe
        Stunde, in der jeder Bau weiter stirbt. `critical` geht sofort raus.
        Genau deshalb steht die Dedup-Marke hier selbst: `critical` umgeht in
        ``notify_event`` ALLE Sperren (und wird nie dedupliziert), also ist
        dieser Redis-Schluessel der einzige Schutz vor einer Meldung im
        Watchdog-Takt.
        """
        threshold = settings.disk_watchdog_percent
        if disk_pct < threshold:
            return

        redis = await get_redis()
        dedup_key = RedisKeys.disk_watchdog_notified(threshold)
        if await redis.exists(dedup_key):
            return

        # Erst melden, dann den Schluessel setzen: bricht emit_event ab, bleibt
        # der Schluessel ungesetzt und der naechste Takt versucht es erneut.
        # Andersherum verschluckte ein einzelner Fehler die Warnung fuer die
        # ganze TTL.
        await emit_event(
            session,
            "system.disk_high",
            f"Platte zu {disk_pct:.1f} % belegt — Schwelle {threshold} % erreicht",
            severity="critical",
            detail={
                "component": "disk",
                "disk_pct": round(disk_pct, 1),
                "threshold_percent": threshold,
                "free_gb": round(free_bytes / (1024 ** 3), 1),
                # Der Hinweis nennt die KONFIGURIERTE Grenze, nicht eine Zahl
                # aus dem Kopf: eine fest eingetippte 20g waere genau die
                # Art Drift, wegen der diese Karte existiert — wer
                # BUILD_CACHE_KEEP_GB verstellt, bekaeme hier sonst eine
                # Anweisung, die nicht zu seiner Einstellung passt.
                "note": "Meldung nur — es wird nichts geloescht. "
                        f"Aufraeumen: docker builder prune --keep-storage "
                        f"{settings.build_cache_keep_gb}g",
            },
        )
        await redis.set(dedup_key, 1, ex=settings.discord_dedup_ttl_seconds)
        logger.warning(
            "Platte zu %.1f %% belegt (Schwelle %d %%) — gemeldet, nichts geloescht",
            disk_pct, threshold,
        )

    async def _check_weekly_digest(self) -> None:
        """Generate a weekly digest on Sundays."""
        if utcnow().weekday() != 6:  # 0=Mon, 6=Sun
            return
        from app.services.auto_memory import generate_weekly_digest
        from app.services.watchdog.core import _create_background_task
        _create_background_task(generate_weekly_digest())
