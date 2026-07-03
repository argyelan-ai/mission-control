"""
Intelligence Service — MC thinks along.

Periodic analysis of task data, agent performance, and failure patterns.
Rule-based analysis (no LLM needed) + optional daily LLM distillation via Ollama.
Feeds insights back into the system (dispatch messages, ActivityEvents, AgentMetrics).

Same pattern as WatchdogService: singleton, asyncio loop, Redis lock.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.database import engine
from app.models.agent import Agent, AgentMetrics
from app.models.memory import BoardMemory
from app.utils import ensure_aware, utcnow
from app.models.task import Task, TaskComment
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event

logger = logging.getLogger("mc.intelligence")

# Known failure patterns for keyword matching
FAILURE_KEYWORDS: dict[str, list[str]] = {
    "timeout": ["timeout", "timed out", "zeitueberschreitung"],
    "permission": ["permission", "denied", "access", "zugriff"],
    "not_found": ["not found", "nicht gefunden", "missing", "fehlt"],
    "context_limit": ["context", "token limit", "compaction", "kontext"],
    "cors": ["cors", "origin"],
    "dependency": ["dependency", "import", "module", "package"],
}


class IntelligenceService:
    def __init__(self, interval: int | None = None):
        self._interval = interval or settings.intelligence_interval
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_analysis_at: datetime | None = None
        self._cycles_total = 0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_analysis_at(self) -> datetime | None:
        return self._last_analysis_at

    async def _get_config(self):
        """Read config from Redis, fallback to defaults."""
        from app.routers.system import IntelligenceConfig

        try:
            redis = await get_redis()
            raw = await redis.get(RedisKeys.intelligence_config())
            if raw:
                return IntelligenceConfig(**json.loads(raw))
        except Exception:
            pass
        return IntelligenceConfig()

    async def trigger_analysis(self) -> str:
        """Manual trigger — run analysis immediately."""
        await self._analyze_all()
        self._last_analysis_at = utcnow()
        self._cycles_total += 1
        return self._last_analysis_at.isoformat()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Intelligence started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Intelligence stopped")

    async def _run_loop(self) -> None:
        # Grace period: wait until DB + Redis + other services are ready
        await asyncio.sleep(20)
        while self._running:
            try:
                config = await self._get_config()
                if not config.enabled:
                    logger.debug("Intelligence disabled via config — skipping")
                    await asyncio.sleep(config.interval_seconds)
                    continue
                if await self._acquire_lock():
                    await self._analyze_all()
                    self._last_analysis_at = utcnow()
                    self._cycles_total += 1
                else:
                    # MEM-05: emit at WARNING (was DEBUG) so multi-worker dedup
                    # is observable at default log level. The fail-fast itself
                    # (return False from _acquire_lock + short-circuit here)
                    # already existed — only visibility changes.
                    logger.warning("intelligence: lock contention, skipping cycle")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Intelligence analysis error: %s", e)
            config = await self._get_config()
            await asyncio.sleep(config.interval_seconds)

    async def _acquire_lock(self) -> bool:
        """Redis lock so only one worker per cycle runs the analysis."""
        try:
            redis = await get_redis()
            acquired = await redis.set(
                RedisKeys.intelligence_lock(), "1", nx=True, ex=self._interval
            )
            return bool(acquired)
        except Exception:
            return True

    # ── Main Analysis ──────────────────────────────────────────────────

    async def _analyze_all(self) -> None:
        config = await self._get_config()
        async with AsyncSession(engine, expire_on_commit=False) as session:
            insights: dict = {}

            insights["task_durations"] = await self._analyze_task_durations(session, config)
            insights["agent_performance"] = await self._analyze_agent_performance(session, config)
            insights["failure_patterns"] = await self._detect_failure_patterns(session, config)

            anomalies = await self._detect_anomalies(session, insights, config)
            insights["anomalies"] = anomalies
            insights["analyzed_at"] = utcnow().isoformat()

            await self._cache_insights(insights)
            await self._populate_agent_metrics(session)
            await self._maybe_daily_destillation(insights, config)

            # Log summary
            td = insights["task_durations"]
            fp = insights["failure_patterns"]
            ap = insights["agent_performance"]
            logger.info(
                "Intelligence: analysis complete — %d tasks (avg %.1fmin), %d agents, %d failures, %d anomalies",
                td.get("total", 0),
                td.get("avg_minutes", 0),
                len(ap),
                fp.get("total", 0),
                len(anomalies),
            )

    # ── Task Duration Analysis ─────────────────────────────────────────

    async def _analyze_task_durations(self, session: AsyncSession, config=None) -> dict:
        """Analyze all done tasks of the last N days: average, outliers, per agent."""
        window_days = config.analysis_window_days if config else 7
        cutoff = utcnow() - timedelta(days=window_days)
        result = await session.exec(
            select(Task).where(
                Task.status == "done",
                Task.started_at.isnot(None),  # type: ignore[union-attr]
                Task.completed_at.isnot(None),  # type: ignore[union-attr]
                Task.completed_at >= cutoff,  # type: ignore[operator]
            )
        )
        tasks = result.all()

        if not tasks:
            return {"avg_minutes": 0, "total": 0, "outliers": [], "per_agent": {}}

        # Compute durations
        durations: list[dict] = []
        for t in tasks:
            started = ensure_aware(t.started_at)  # type: ignore[union-attr]
            completed = ensure_aware(t.completed_at)  # type: ignore[union-attr]
            minutes = (completed - started).total_seconds() / 60
            if minutes < 0:
                continue
            durations.append({
                "task_id": str(t.id),
                "title": t.title,
                "agent_id": str(t.assigned_agent_id) if t.assigned_agent_id else None,
                "minutes": round(minutes, 1),
            })

        if not durations:
            return {"avg_minutes": 0, "total": 0, "outliers": [], "per_agent": {}}

        avg = sum(d["minutes"] for d in durations) / len(durations)

        # Outliers: >Nx average
        multiplier = config.outlier_multiplier if config else 2.0
        outliers = [d for d in durations if d["minutes"] > avg * multiplier]

        # Per agent
        agent_durations: dict[str, list[float]] = {}
        for d in durations:
            aid = d["agent_id"] or "unassigned"
            agent_durations.setdefault(aid, []).append(d["minutes"])

        per_agent = {
            aid: round(sum(mins) / len(mins), 1)
            for aid, mins in agent_durations.items()
        }

        # Resolve agent names
        agent_ids = [uid for uid in per_agent if uid != "unassigned"]
        if agent_ids:
            name_result = await session.exec(
                select(Agent.id, Agent.name).where(
                    Agent.id.in_([uuid.UUID(aid) for aid in agent_ids])  # type: ignore[union-attr]
                )
            )
            name_map = {str(row[0]): row[1] for row in name_result.all()}
        else:
            name_map = {}

        per_agent_named = {
            name_map.get(aid, aid): avg_min
            for aid, avg_min in per_agent.items()
        }

        # Enrich outliers with agent names
        for o in outliers:
            o["agent"] = name_map.get(o.get("agent_id", ""), "unassigned")

        return {
            "avg_minutes": round(avg, 1),
            "total": len(durations),
            "outliers": outliers[:10],  # Max 10
            "per_agent": per_agent_named,
        }

    # ── Agent Performance ──────────────────────────────────────────────

    async def _analyze_agent_performance(self, session: AsyncSession, config=None) -> list[dict]:
        """Per agent: tasks done, failed, success rate, avg duration."""
        window_days = config.analysis_window_days if config else 7
        cutoff = utcnow() - timedelta(days=window_days)

        # Phase 30: gateway_agent_id filter dropped. Iterate all agents — the
        # downstream KPI calculation handles agents without recent activity.
        result = await session.exec(select(Agent))
        agents = result.all()

        performance: list[dict] = []
        for agent in agents:
            # Done tasks
            done_result = await session.exec(
                select(func.count()).where(
                    Task.assigned_agent_id == agent.id,
                    Task.status == "done",
                    Task.completed_at >= cutoff,  # type: ignore[operator]
                )
            )
            done_count = done_result.one()

            # Failed tasks
            failed_result = await session.exec(
                select(func.count()).where(
                    Task.assigned_agent_id == agent.id,
                    Task.status == "failed",
                    Task.updated_at >= cutoff,  # type: ignore[operator]
                )
            )
            failed_count = failed_result.one()

            total = done_count + failed_count
            success_rate = (done_count / total * 100) if total > 0 else 100.0

            # Average duration
            dur_result = await session.exec(
                select(Task).where(
                    Task.assigned_agent_id == agent.id,
                    Task.status == "done",
                    Task.started_at.isnot(None),  # type: ignore[union-attr]
                    Task.completed_at.isnot(None),  # type: ignore[union-attr]
                    Task.completed_at >= cutoff,  # type: ignore[operator]
                )
            )
            done_tasks = dur_result.all()
            if done_tasks:
                avg_min = sum(
                    (ensure_aware(t.completed_at) - ensure_aware(t.started_at)).total_seconds() / 60  # type: ignore[union-attr]
                    for t in done_tasks
                    if t.started_at and t.completed_at
                ) / len(done_tasks)
            else:
                avg_min = 0

            performance.append({
                "name": agent.name,
                "agent_id": str(agent.id),
                "done": done_count,
                "failed": failed_count,
                "success_rate": round(success_rate, 1),
                "avg_minutes": round(avg_min, 1),
            })

        return performance

    # ── Failure Pattern Detection ──────────────────────────────────────

    async def _detect_failure_patterns(self, session: AsyncSession, config=None) -> dict:
        """Failed tasks of the last N days: keyword matching on comments."""
        window_days = config.analysis_window_days if config else 7
        cutoff = utcnow() - timedelta(days=window_days)
        result = await session.exec(
            select(Task).where(
                Task.status == "failed",
                Task.updated_at >= cutoff,  # type: ignore[operator]
            )
        )
        failed_tasks = result.all()

        if not failed_tasks:
            return {"total": 0, "patterns": {}, "details": []}

        # Load agent names
        agent_ids = [t.assigned_agent_id for t in failed_tasks if t.assigned_agent_id]
        name_map: dict[str, str] = {}
        if agent_ids:
            name_result = await session.exec(
                select(Agent.id, Agent.name).where(
                    Agent.id.in_(agent_ids)  # type: ignore[union-attr]
                )
            )
            name_map = {str(row[0]): row[1] for row in name_result.all()}

        patterns: dict[str, int] = {}
        details: list[dict] = []

        for task in failed_tasks:
            # Load last comment as failure reason
            comment_result = await session.exec(
                select(TaskComment)
                .where(TaskComment.task_id == task.id)
                .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
                .limit(1)
            )
            comment = comment_result.first()
            reason = comment.content if comment else ""

            # Keyword matching
            text_lower = f"{task.title} {reason}".lower()
            matched_pattern = "unknown"
            for pattern_name, keywords in FAILURE_KEYWORDS.items():
                if any(kw in text_lower for kw in keywords):
                    matched_pattern = pattern_name
                    patterns[pattern_name] = patterns.get(pattern_name, 0) + 1
                    break
            else:
                patterns["unknown"] = patterns.get("unknown", 0) + 1

            agent_name = name_map.get(
                str(task.assigned_agent_id), "unassigned"
            ) if task.assigned_agent_id else "unassigned"

            details.append({
                "title": task.title,
                "agent": agent_name,
                "reason": reason[:200] if reason else "",
                "pattern": matched_pattern,
            })

        return {
            "total": len(failed_tasks),
            "patterns": patterns,
            "details": details[:20],  # Max 20
        }

    # ── Anomaly Detection ──────────────────────────────────────────────

    async def _detect_anomalies(
        self, session: AsyncSession, insights: dict, config=None
    ) -> list[dict]:
        """Proactive alerts for conspicuous patterns."""
        anomalies: list[dict] = []
        td = insights.get("task_durations", {})
        ap = insights.get("agent_performance", [])
        fp = insights.get("failure_patterns", {})

        multiplier = config.outlier_multiplier if config else 2.0
        success_threshold = config.success_rate_threshold if config else 50.0
        failure_threshold = config.failure_count_threshold if config else 5

        # 1. Outlier tasks (>Nx average)
        outliers = td.get("outliers", [])
        if outliers:
            anomalies.append({
                "type": "slow_tasks",
                "description": f"{len(outliers)} Tasks brauchten >{multiplier}x laenger als der Durchschnitt ({td.get('avg_minutes', 0):.1f}min)",
                "severity": "info",
            })

        # 2. Agents with low success rate
        for agent in ap:
            if agent["done"] + agent["failed"] >= 3 and agent["success_rate"] < success_threshold:
                desc = (
                    f"Agent {agent['name']} hat nur {agent['success_rate']}% Success Rate "
                    f"({agent['done']} done, {agent['failed']} failed)"
                )
                anomalies.append({
                    "type": "low_success_rate",
                    "description": desc,
                    "severity": "warning",
                    "agent_name": agent["name"],
                    "agent_id": agent["agent_id"],
                })

        # 3. Frequent failures
        total_failures = fp.get("total", 0)
        if total_failures > failure_threshold:
            top_pattern = max(fp.get("patterns", {}), key=fp["patterns"].get, default="unknown")
            anomalies.append({
                "type": "high_failure_rate",
                "description": f"{total_failures} fehlgeschlagene Tasks in 7 Tagen. Haeufigstes Muster: {top_pattern}",
                "severity": "warning",
            })

        # Emit anomaly events
        for anomaly in anomalies:
            if anomaly["severity"] == "warning":
                await emit_event(
                    session,
                    "intelligence.anomaly",
                    f"Intelligence: {anomaly['description']}",
                    severity="warning",
                    agent_id=uuid.UUID(anomaly["agent_id"]) if anomaly.get("agent_id") else None,
                    detail={"anomaly_type": anomaly["type"]},
                )

        return anomalies

    # ── AgentMetrics Populator ─────────────────────────────────────────

    async def _populate_agent_metrics(self, session: AsyncSession) -> None:
        """Fills the existing AgentMetrics table (hourly per agent)."""
        now = utcnow()
        hour_key = now.strftime("%Y%m%d%H")

        # Phase 30: gateway_agent_id filter dropped (was Phase 1 gating).
        result = await session.exec(select(Agent))
        agents = result.all()

        redis = await get_redis()

        for agent in agents:
            # Dedup: once per hour per agent
            dedup_key = RedisKeys.intelligence_metrics_dedup(str(agent.id), hour_key)
            already = await redis.get(dedup_key)
            if already:
                continue

            period_start = now.replace(minute=0, second=0, microsecond=0)
            period_end = period_start + timedelta(hours=1)

            # Tasks started (in this hour)
            started_result = await session.exec(
                select(func.count()).where(
                    Task.assigned_agent_id == agent.id,
                    Task.started_at >= period_start,  # type: ignore[operator]
                    Task.started_at < period_end,  # type: ignore[operator]
                )
            )
            tasks_started = started_result.one()

            # Tasks completed (in this hour)
            completed_result = await session.exec(
                select(func.count()).where(
                    Task.assigned_agent_id == agent.id,
                    Task.status == "done",
                    Task.completed_at >= period_start,  # type: ignore[operator]
                    Task.completed_at < period_end,  # type: ignore[operator]
                )
            )
            tasks_completed = completed_result.one()

            # Comments posted (in this hour)
            comments_result = await session.exec(
                select(func.count()).where(
                    TaskComment.author_agent_id == agent.id,
                    TaskComment.created_at >= period_start,  # type: ignore[operator]
                    TaskComment.created_at < period_end,  # type: ignore[operator]
                )
            )
            comments_posted = comments_result.one()

            # Avg task duration (all done tasks of the last 24h)
            cutoff_24h = now - timedelta(hours=24)
            dur_result = await session.exec(
                select(Task).where(
                    Task.assigned_agent_id == agent.id,
                    Task.status == "done",
                    Task.started_at.isnot(None),  # type: ignore[union-attr]
                    Task.completed_at.isnot(None),  # type: ignore[union-attr]
                    Task.completed_at >= cutoff_24h,  # type: ignore[operator]
                )
            )
            done_tasks = dur_result.all()
            avg_dur = None
            if done_tasks:
                total_min = sum(
                    (ensure_aware(t.completed_at) - ensure_aware(t.started_at)).total_seconds() / 60  # type: ignore[union-attr]
                    for t in done_tasks
                    if t.started_at and t.completed_at
                )
                avg_dur = int(total_min / len(done_tasks))

            metrics = AgentMetrics(
                agent_id=agent.id,
                period_start=period_start,
                period_end=period_end,
                tasks_started=tasks_started,
                tasks_completed=tasks_completed,
                comments_posted=comments_posted,
                avg_task_duration_minutes=avg_dur,
                context_tokens_avg=agent.context_tokens,
                context_tokens_max=agent.context_max,
            )
            session.add(metrics)

            # Set dedup flag (2h TTL)
            await redis.set(dedup_key, "1", ex=7200)

        await session.commit()

    # ── Insight Cache (Redis) ──────────────────────────────────────────

    async def _cache_insights(self, insights: dict) -> None:
        """Cache current analysis results as JSON in Redis."""
        try:
            redis = await get_redis()
            await redis.set(
                RedisKeys.intelligence_insights(),
                json.dumps(insights, default=str),
                ex=600,  # 10 minute TTL
            )
        except Exception as e:
            logger.warning("Failed to cache insights: %s", e)

    # ── LLM Distillation (daily) ──────────────────────────────────────

    async def _maybe_daily_destillation(self, insights: dict, config=None) -> None:
        """Once daily: distill patterns into readable insights via Ollama."""
        try:
            redis = await get_redis()
            # Dedup: max 1x per 20 hours
            dedup_key = RedisKeys.intelligence_daily_dedup()
            already = await redis.get(dedup_key)
            if already:
                return

            # Only distill if there's enough data
            td = insights.get("task_durations", {})
            if td.get("total", 0) < 3:
                return

            # Call Ollama
            response = await self._call_ollama(self._build_destillation_prompt(insights, config), config)
            if not response:
                return

            # Save as BoardMemory
            async with AsyncSession(engine, expire_on_commit=False) as session:
                today = utcnow().strftime("%Y-%m-%d")
                memory = BoardMemory(
                    board_id=None,
                    agent_id=None,
                    title=f"Intelligence Report {today}",
                    content=response,
                    memory_type="insight",
                    source="system",
                    auto_generated=True,
                    tags=["auto", "intelligence", "daily_report"],
                )
                session.add(memory)
                await session.commit()
                await session.refresh(memory)
                try:
                    from app.services.memory_indexing import index_memory
                    await index_memory(memory)
                except Exception as e:
                    logger.warning("intelligence daily_report index failed: %s", e)

                await emit_event(
                    session,
                    "intelligence.report",
                    f"Taeglicher Intelligence Report erstellt ({today})",
                    severity="info",
                )

            # Set dedup flag (20h TTL)
            await redis.set(dedup_key, "1", ex=72000)
            logger.info("Daily intelligence report created")

        except Exception as e:
            logger.warning("Daily destillation failed (non-critical): %s", e)

    def _build_destillation_prompt(self, insights: dict, config=None) -> str:
        """Build the Ollama prompt from the current insights."""
        td = insights.get("task_durations", {})
        ap = insights.get("agent_performance", [])
        fp = insights.get("failure_patterns", {})
        anomalies = insights.get("anomalies", [])

        agent_lines = "\n".join(
            f"  - {a['name']}: {a['done']} done, {a['failed']} failed, {a['success_rate']}% Success, avg {a['avg_minutes']}min"
            for a in ap
        ) or "  Keine Agent-Daten"

        pattern_lines = "\n".join(
            f"  - {k}: {v}x"
            for k, v in fp.get("patterns", {}).items()
        ) or "  Keine Muster"

        outlier_lines = "\n".join(
            f"  - '{o.get('title', '?')}' ({o.get('agent', '?')}): {o.get('minutes', 0)}min"
            for o in td.get("outliers", [])[:5]
        ) or "  Keine Outlier"

        anomaly_lines = "\n".join(
            f"  - [{a['severity']}] {a['description']}"
            for a in anomalies
        ) or "  Keine Anomalien"

        data_block = f"""Daten:
- Tasks erledigt: {td.get('total', 0)} | Durchschnittsdauer: {td.get('avg_minutes', 0):.1f} Minuten
- Fehlgeschlagen: {fp.get('total', 0)}
- Outlier (ueberdurchschnittlich langsame Tasks):
{outlier_lines}
- Agent-Performance:
{agent_lines}
- Fehler-Muster:
{pattern_lines}
- Anomalien:
{anomaly_lines}"""

        # Custom system prompt if set
        if config and config.system_prompt.strip():
            return f"""{config.system_prompt.strip()}

{data_block}"""

        return f"""Du bist der Intelligence-Analyst fuer Mission Control, ein AI Agent Command Center.
Analysiere die Daten der letzten 7 Tage und erstelle 3-5 Erkenntnisse.

{data_block}

Jede Erkenntnis: **Titel** + kurze Erklaerung + Empfehlung.
Schreibe auf Deutsch, maximal 500 Woerter. Sei konkret und praxisnah."""

    async def _call_ollama(self, prompt: str, config=None) -> str | None:
        """HTTP POST to local Ollama. Graceful degradation on error."""
        model = config.ollama_model if config else "qwen2.5-coder:14b"
        temperature = config.temperature if config else 0.3
        max_tokens = config.max_tokens if config else 1024
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{settings.ollama_url}/api/generate",
                    json={
                        "model": model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": temperature,
                            "num_predict": max_tokens,
                        },
                    },
                )
                if resp.status_code != 200:
                    logger.warning("Ollama returned %d: %s", resp.status_code, resp.text[:200])
                    return None
                data = resp.json()
                return data.get("response", "").strip() or None
        except httpx.ConnectError:
            logger.info("Ollama not reachable at %s — skipping destillation", settings.ollama_url)
            return None
        except Exception as e:
            logger.warning("Ollama call failed: %s", e)
            return None


# ── Helper Function for Dispatch ──────────────────────────────────

async def fetch_recent_insights(session: AsyncSession, limit: int = 2) -> list[BoardMemory]:
    """Load the newest auto-generated intelligence insights (for dispatch messages)."""
    result = await session.exec(
        select(BoardMemory)
        .where(
            BoardMemory.auto_generated == True,  # noqa: E712
            BoardMemory.memory_type == "insight",
        )
        .order_by(BoardMemory.created_at.desc())  # type: ignore[union-attr]
        .limit(limit)
    )
    return list(result.all())


# Singleton instance
intelligence = IntelligenceService()
