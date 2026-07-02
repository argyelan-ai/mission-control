import json
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy import text

from app.auth import Role, require_role, require_user
from app.config import settings
from app.database import get_session
from app.redis_client import RedisKeys, get_redis
from app.services.task_runner import task_runner
from app.services.watchdog import watchdog
from app.utils import ensure_aware, utcnow

router = APIRouter()

# "online" im Produktsinn = der Agent lebt (heartbeatet). idle/busy/working
# sind Lebenszeichen — nur das woertliche "online" zu zaehlen liess die UI
# "0/N online" zeigen, waehrend die ganze Flotte lief (2. Instanz des
# A2-Bugs vom 2026-07-02; Fix an der Quelle statt pro Frontend-Komponente).
ALIVE_AGENT_STATUSES = ("online", "busy", "idle", "working")
_start_time = utcnow()


@router.get("/health")
async def health(request: Request, session: AsyncSession = Depends(get_session)):
    """Liefert den Health-Status mit allgemeinem Status, App-Version und Review-Monitoring-Daten.

    Die Antwort enthaelt `status`, `version` sowie `review_monitoring` mit Anzahl der
    Review-Tasks und dem Alter des aeltesten Review-Tasks in Minuten.
    """
    from app.models.task import Task
    from sqlmodel import func, select

    review_tasks_count = (
        await session.exec(select(func.count(Task.id)).where(Task.status == "review"))
    ).one()
    oldest_review_updated_at = (
        await session.exec(select(func.min(Task.updated_at)).where(Task.status == "review"))
    ).one()

    oldest_review_task_age_minutes = None
    if oldest_review_updated_at:
        oldest_review_task_age_minutes = int(
            (utcnow() - ensure_aware(oldest_review_updated_at)).total_seconds() / 60
        )

    return {
        "status": "ok",
        "version": request.app.version,
        "review_monitoring": {
            "review_tasks_count": review_tasks_count,
            "oldest_review_task_age_minutes": oldest_review_task_age_minutes,
        },
    }


@router.get("/api/v1/system/version")
async def system_version(current_user = Depends(require_user)):
    """Aktuelle Version + Update-Hinweis (GitHub-Releases, 24h-Cache)."""
    from app.services.update_check import get_latest_release, is_newer

    latest = await get_latest_release()
    return {
        "current": settings.app_version,
        "latest": latest.get("tag"),
        "release_url": latest.get("url"),
        "update_available": is_newer(latest.get("tag"), settings.app_version),
    }


@router.get("/api/v1/system/status")
async def system_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    components: dict = {}

    # Database check
    try:
        t0 = utcnow()
        await session.execute(text("SELECT 1"))
        latency = (utcnow() - t0).total_seconds() * 1000
        components["database"] = {"status": "ok", "latency_ms": round(latency, 1)}
    except Exception as e:
        components["database"] = {"status": "error", "error": str(e)}

    # Redis check
    try:
        redis = await get_redis()
        t0 = utcnow()
        await redis.ping()
        latency = (utcnow() - t0).total_seconds() * 1000
        components["redis"] = {"status": "ok", "latency_ms": round(latency, 1)}
    except Exception as e:
        components["redis"] = {"status": "error", "error": str(e)}

    # Phase 29: Gateway component removed. The openclaw gateway runtime is
    # sunset; the /api/v1/system/status response no longer includes
    # `components["gateway"]`. Frontend (Phase 31) will adapt.

    # Watchdog status
    components["watchdog"] = {
        "status": "running" if watchdog.running else "stopped",
        "last_check": watchdog.last_check_at.isoformat() if watchdog.last_check_at else None,
        "checks_total": watchdog.checks_total,
    }

    # Task Runner status
    components["task_runner"] = {
        "status": "running" if task_runner.running else "stopped",
    }

    overall = "healthy" if all(
        c.get("status") in ("ok", "running") for c in components.values()
    ) else "degraded"

    # Agent health summary
    from app.models.agent import Agent
    from sqlmodel import func, select

    agents_total = (await session.exec(select(func.count(Agent.id)))).one()
    agents_online = (
        await session.exec(select(func.count(Agent.id)).where(Agent.status.in_(ALIVE_AGENT_STATUSES)))
    ).one()
    agents_offline = (
        await session.exec(select(func.count(Agent.id)).where(Agent.status == "offline"))
    ).one()

    # Current system resources from Redis
    resources = None
    try:
        redis = await get_redis()
        raw = await redis.get(RedisKeys.system_metrics_current())
        if raw:
            resources = json.loads(raw)
    except Exception:
        pass

    return {
        "status": overall,
        "components": components,
        "resources": resources,
        "agents": {
            "total": agents_total,
            "online": agents_online,
            "offline": agents_offline,
        },
        "uptime_seconds": int((utcnow() - _start_time).total_seconds()),
        "version": request.app.version,
    }


@router.get("/api/v1/system/metrics/history")
async def system_metrics_history(
    current_user=Depends(require_user),
):
    """Letzte 60 System-Metriken Snapshots (aelteste zuerst) fuer Sparklines."""
    try:
        redis = await get_redis()
        raw_list = await redis.lrange(RedisKeys.system_metrics_history(), 0, 59)
        # Redis List ist LIFO (neueste zuerst), reversed fuer chronologisch
        snapshots = [json.loads(item) for item in reversed(raw_list)]
    except Exception:
        snapshots = []

    return {"snapshots": snapshots, "count": len(snapshots)}


@router.get("/api/v1/system/metrics")
async def system_metrics(
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    from app.models.agent import Agent
    from app.models.approval import Approval
    from app.models.task import Task
    from sqlmodel import func, select

    tasks_total = (await session.exec(select(func.count(Task.id)))).one()
    tasks_active = (
        await session.exec(select(func.count(Task.id)).where(Task.status == "in_progress"))
    ).one()
    agents_total = (await session.exec(select(func.count(Agent.id)))).one()
    agents_online = (
        await session.exec(select(func.count(Agent.id)).where(Agent.status.in_(ALIVE_AGENT_STATUSES)))
    ).one()
    approvals_pending = (
        await session.exec(
            select(func.count(Approval.id)).where(Approval.status == "pending")
        )
    ).one()

    return {
        "tasks": {"total": tasks_total, "active": tasks_active},
        "agents": {"total": agents_total, "online": agents_online},
        "approvals": {"pending": approvals_pending},
    }


@router.get("/api/v1/intelligence/insights")
async def intelligence_insights(
    current_user=Depends(require_user),
):
    """Aktuelle Intelligence-Analyse aus Redis-Cache."""
    try:
        redis = await get_redis()
        raw = await redis.get(RedisKeys.intelligence_insights())
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {
        "task_durations": {"avg_minutes": 0, "total": 0, "outliers": [], "per_agent": {}},
        "agent_performance": [],
        "failure_patterns": {"total": 0, "patterns": {}, "details": []},
        "anomalies": [],
        "analyzed_at": None,
    }


class IntelligenceConfig(BaseModel):
    enabled: bool = True
    interval_seconds: int = Field(default=600, ge=60)  # MEM-05: raise default to 10 min
    analysis_window_days: int = Field(default=7, ge=1)
    ollama_model: str = "qwen2.5-coder:14b"
    temperature: float = Field(default=0.3, ge=0.0, le=1.0)
    max_tokens: int = Field(default=1024, ge=100, le=8192)
    system_prompt: str = ""
    outlier_multiplier: float = Field(default=2.0, gt=1.0)
    success_rate_threshold: float = Field(default=50.0, ge=0.0, le=100.0)
    failure_count_threshold: int = Field(default=5, ge=1)


@router.get("/api/v1/intelligence/config")
async def get_intelligence_config(
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Intelligence-Config aus Redis lesen (Defaults wenn nicht vorhanden)."""
    try:
        redis = await get_redis()
        raw = await redis.get(RedisKeys.intelligence_config())
        if raw:
            return IntelligenceConfig(**json.loads(raw)).model_dump()
    except Exception:
        pass
    return IntelligenceConfig().model_dump()


@router.put("/api/v1/intelligence/config")
async def update_intelligence_config(
    config: IntelligenceConfig,
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Intelligence-Config in Redis speichern (kein TTL)."""
    redis = await get_redis()
    await redis.set(
        RedisKeys.intelligence_config(),
        json.dumps(config.model_dump()),
    )
    return config.model_dump()


@router.post("/api/v1/intelligence/trigger")
async def trigger_intelligence(
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Sofort Intelligence-Analyse ausloesen."""
    from app.services.intelligence import intelligence

    analyzed_at = await intelligence.trigger_analysis()
    return {"analyzed_at": analyzed_at}


@router.get("/api/v1/intelligence/reports")
async def intelligence_reports(
    limit: int = 5,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Taegliche LLM-generierte Intelligence Reports aus BoardMemory."""
    from app.models.memory import BoardMemory
    from sqlmodel import select

    result = await session.exec(
        select(BoardMemory)
        .where(
            BoardMemory.memory_type == "insight",
            BoardMemory.auto_generated == True,  # noqa: E712
        )
        .order_by(BoardMemory.created_at.desc())  # type: ignore[union-attr]
        .limit(limit)
    )
    return list(result.all())


# ── Cost Tracking ──────────────────────────────────────────────────


@router.get("/api/v1/intelligence/costs")
async def intelligence_costs(
    days: int = 30,
    include_sessions: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Kosten-Uebersicht: pro Agent + Gesamt fuer die letzten N Tage.

    Datenquelle: model_usage_events (Token Harvester — Phase 31).
    Response-Schema: CostOverview / CostAgentSummary / CostSessionSummary
    (kompatibel zu frontend-v2/src/lib/types.ts).
    Mit include_sessions=true auch Session-Level Breakdown (Top-100).
    """
    import datetime
    from sqlalchemy import func
    from sqlmodel import select
    from app.models.model_usage import ModelUsageEvent
    from app.models.agent import Agent as AgentModel

    cutoff = utcnow() - datetime.timedelta(days=days)

    # Pro Agent aggregieren (agent_id NULL = Boss ohne Attribution / unbekannt)
    agent_result = await session.exec(
        select(
            ModelUsageEvent.agent_id,
            func.sum(ModelUsageEvent.input_tokens).label("total_in"),
            func.sum(ModelUsageEvent.output_tokens).label("total_out"),
            func.sum(ModelUsageEvent.cost_usd).label("total_cost"),
            func.count(ModelUsageEvent.id).label("event_count"),
        )
        .where(ModelUsageEvent.ts >= cutoff)
        .group_by(ModelUsageEvent.agent_id)
    )

    # Agent-Namen einmal laden (Batch)
    agent_name_cache: dict = {}
    agent_rows = agent_result.all()
    for row in agent_rows:
        if row.agent_id and str(row.agent_id) not in agent_name_cache:
            agent_obj = await session.get(AgentModel, row.agent_id)
            agent_name_cache[str(row.agent_id)] = agent_obj.name if agent_obj else "?"

    agent_costs = []
    total_in = total_out = 0
    total_cost = 0.0
    for row in agent_rows:
        a_in = row.total_in or 0
        a_out = row.total_out or 0
        a_cost = float(row.total_cost or 0.0)
        total_in += a_in
        total_out += a_out
        total_cost += a_cost

        # agent_id NULL → "Unattributiert" (Boss-Zeilen ohne cwd-Match etc.)
        # Das UI-Schema benoetigt agent_id als String → leeren String fuer NULL.
        aid_str = str(row.agent_id) if row.agent_id else ""
        aname = agent_name_cache.get(aid_str, "Unattributiert") if aid_str else "Unattributiert"

        agent_costs.append({
            "agent_id": aid_str,
            "agent_name": aname,
            "tokens_in": a_in,
            "tokens_out": a_out,
            "cost_usd": round(a_cost, 4),
            "event_count": row.event_count,
        })

    result: dict = {
        "period_days": days,
        "total_tokens_in": total_in,
        "total_tokens_out": total_out,
        "total_cost_usd": round(total_cost, 4),
        "agents": sorted(agent_costs, key=lambda x: x["cost_usd"], reverse=True),
    }

    # Optional: Session-Level Breakdown (Top-100, nach Kosten sortiert)
    if include_sessions:
        session_result = await session.exec(
            select(
                ModelUsageEvent.agent_id,
                ModelUsageEvent.session_id,
                func.sum(ModelUsageEvent.input_tokens).label("total_in"),
                func.sum(ModelUsageEvent.output_tokens).label("total_out"),
                func.sum(ModelUsageEvent.cost_usd).label("total_cost"),
                func.count(ModelUsageEvent.id).label("event_count"),
                func.max(ModelUsageEvent.ts).label("last_event_at"),
            )
            .where(ModelUsageEvent.ts >= cutoff)
            .group_by(ModelUsageEvent.agent_id, ModelUsageEvent.session_id)
            .order_by(func.sum(ModelUsageEvent.cost_usd).desc())
            .limit(100)
        )
        sessions = []
        for row in session_result.all():
            aid_str = str(row.agent_id) if row.agent_id else ""
            aname = agent_name_cache.get(aid_str, "Unattributiert") if aid_str else "Unattributiert"
            sessions.append({
                "agent_id": aid_str,
                "agent_name": aname,
                # session_key ist im neuen Schema session_id (JSONL sessionId)
                "session_key": row.session_id or "",
                "tokens_in": row.total_in or 0,
                "tokens_out": row.total_out or 0,
                "cost_usd": round(float(row.total_cost or 0.0), 4),
                "event_count": row.event_count,
                "last_event_at": row.last_event_at.isoformat() if row.last_event_at else None,
            })
        result["sessions"] = sessions

    return result


@router.get("/api/v1/intelligence/costs/by-model")
async def costs_by_model(
    days: int = 30,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Token- und Kostenaufschluesselung pro Modell.

    Response: Liste von {model, harness_list, event_count, input_tokens,
    output_tokens, cache_read_tokens, cache_write_tokens, cost_usd}
    Sortiert nach cost_usd DESC.
    """
    import datetime as dt
    from sqlalchemy import func
    from sqlmodel import select
    from app.models.model_usage import ModelUsageEvent

    cutoff = utcnow() - dt.timedelta(days=days)

    result = await session.exec(
        select(
            ModelUsageEvent.model,
            func.count(ModelUsageEvent.id).label("event_count"),
            func.sum(ModelUsageEvent.input_tokens).label("input_tokens"),
            func.sum(ModelUsageEvent.output_tokens).label("output_tokens"),
            func.sum(ModelUsageEvent.cache_read_tokens).label("cache_read_tokens"),
            func.sum(ModelUsageEvent.cache_write_tokens).label("cache_write_tokens"),
            func.sum(ModelUsageEvent.cost_usd).label("cost_usd"),
        )
        .where(ModelUsageEvent.ts >= cutoff)
        .group_by(ModelUsageEvent.model)
        .order_by(func.sum(ModelUsageEvent.cost_usd).desc())
    )

    # Harness-Liste pro Modell (separate Query)
    harness_result = await session.exec(
        select(ModelUsageEvent.model, ModelUsageEvent.harness)
        .where(ModelUsageEvent.ts >= cutoff)
        .distinct()
    )
    harness_map: dict[str, set[str]] = {}
    for row in harness_result.all():
        harness_map.setdefault(row.model, set()).add(row.harness)

    return [
        {
            "model": row.model,
            "harness_list": sorted(harness_map.get(row.model, set())),
            "event_count": row.event_count,
            "input_tokens": row.input_tokens or 0,
            "output_tokens": row.output_tokens or 0,
            "cache_read_tokens": row.cache_read_tokens or 0,
            "cache_write_tokens": row.cache_write_tokens or 0,
            "cost_usd": round(float(row.cost_usd or 0.0), 6),
        }
        for row in result.all()
    ]


@router.get("/api/v1/intelligence/costs/timeseries")
async def costs_timeseries(
    days: int = 30,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Kosten-Zeitreihe — pro Tag: tokens_in, tokens_out, cost_usd.

    Response: [{date: "YYYY-MM-DD", tokens_in, tokens_out, cost_usd}]
    Sortiert chronologisch.
    """
    import datetime as dt
    from sqlalchemy import func, cast, Date
    from sqlmodel import select
    from app.models.model_usage import ModelUsageEvent

    cutoff = utcnow() - dt.timedelta(days=days)

    # func.date() funktioniert in SQLite (gibt "YYYY-MM-DD" String) und
    # PostgreSQL (gibt date-Objekt). str() macht beides einheitlich zum String.
    date_expr = func.date(ModelUsageEvent.ts)

    result = await session.exec(
        select(
            date_expr.label("date"),
            func.sum(ModelUsageEvent.input_tokens).label("tokens_in"),
            func.sum(ModelUsageEvent.output_tokens).label("tokens_out"),
            func.sum(ModelUsageEvent.cost_usd).label("cost_usd"),
        )
        .where(ModelUsageEvent.ts >= cutoff)
        .group_by(date_expr)
        .order_by(date_expr)
    )

    return [
        {
            "date": str(row.date),
            "tokens_in": row.tokens_in or 0,
            "tokens_out": row.tokens_out or 0,
            "cost_usd": round(float(row.cost_usd or 0.0), 6),
        }
        for row in result.all()
    ]


@router.get("/api/v1/intelligence/costs/by-task")
async def costs_by_task(
    days: int = 30,
    limit: int = 10,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Teuerste Tasks — Top N nach cost_usd.

    Response: [{task_id, task_title, event_count, input_tokens, output_tokens, cost_usd}]
    Nur Events mit task_id != NULL.
    """
    import datetime as dt
    from sqlalchemy import func
    from sqlmodel import select
    from app.models.model_usage import ModelUsageEvent
    from app.models.task import Task

    cutoff = utcnow() - dt.timedelta(days=days)

    result = await session.exec(
        select(
            ModelUsageEvent.task_id,
            func.count(ModelUsageEvent.id).label("event_count"),
            func.sum(ModelUsageEvent.input_tokens).label("input_tokens"),
            func.sum(ModelUsageEvent.output_tokens).label("output_tokens"),
            func.sum(ModelUsageEvent.cost_usd).label("cost_usd"),
        )
        .where(
            ModelUsageEvent.ts >= cutoff,
            ModelUsageEvent.task_id.isnot(None),
        )
        .group_by(ModelUsageEvent.task_id)
        .order_by(func.sum(ModelUsageEvent.cost_usd).desc())
        .limit(limit)
    )

    rows = result.all()

    # Task-Titel laden (Batch)
    task_cache: dict = {}
    for row in rows:
        if row.task_id and str(row.task_id) not in task_cache:
            task_obj = await session.get(Task, row.task_id)
            task_cache[str(row.task_id)] = task_obj.title if task_obj else "—"

    return [
        {
            "task_id": str(row.task_id),
            "task_title": task_cache.get(str(row.task_id), "—"),
            "event_count": row.event_count,
            "input_tokens": row.input_tokens or 0,
            "output_tokens": row.output_tokens or 0,
            "cost_usd": round(float(row.cost_usd or 0.0), 6),
        }
        for row in rows
    ]


# ── System Mode (Operational Controls) ─────────────────────────────

class SystemModeUpdate(BaseModel):
    mode: str = Field(..., pattern="^(active|draining|halted)$")
    reason: str = ""


@router.get("/api/v1/system/mode")
async def get_system_mode_endpoint(
    current_user=Depends(require_user),
):
    """Aktuellen System Mode und Meta-Daten lesen."""
    from app.services.operations import get_system_mode_meta
    return await get_system_mode_meta()


@router.put("/api/v1/system/mode")
async def set_system_mode_endpoint(
    payload: SystemModeUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """System Mode ändern (Admin only)."""
    from app.services.operations import set_system_mode
    from app.services.activity import emit_event

    meta = await set_system_mode(payload.mode, str(current_user.id), payload.reason)

    await emit_event(
        session, "system.mode_changed",
        f"System Mode: {meta['previous_mode']} → {payload.mode}",
        detail=meta,
    )

    return meta


# ── Autonomy Settings (Theme 3: Wave 2) ──────────────────────────


@router.get("/api/v1/settings/autonomy")
async def get_autonomy_settings(
    current_user=Depends(require_user),
):
    """Autonomy levels fuer alle Action-Types."""
    from app.services.autonomy import get_autonomy_config, AUTONOMY_DEFAULTS
    config = await get_autonomy_config()
    return {"levels": config, "defaults": AUTONOMY_DEFAULTS}


class AutonomyUpdate(BaseModel):
    levels: dict[str, str]  # action_type -> "L1"|"L2"|"L3"


@router.patch("/api/v1/settings/autonomy")
async def update_autonomy_settings(
    payload: AutonomyUpdate,
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Autonomy levels anpassen (Admin only)."""
    from app.services.autonomy import set_autonomy_config
    config = await set_autonomy_config(payload.levels)
    return {"levels": config}


# ── Usage / Model Tracking V1 (Theme 4: Wave 2) ──────────────────
#
# Ehrliches V1: Zeigt nur was wirklich messbar ist.
# - Welcher Agent nutzt welches Modell (aus agent.model DB-Feld)
# - Context-Snapshot pro Agent (Momentanwert, kein kumulierter Counter)
# - Tasks completed pro Agent (aus agent.total_tasks_completed)
# - Heartbeat model_id zeigt aktives Modell (Redis-Snapshot, kein Counter)
#
# Was NICHT gemessen wird (V2):
# - Token In/Out pro Request (braucht Gateway-Integration)
# - Kosten (braucht Token-Messung)
# - Zeitfenster-basierte Aggregation (braucht periodische Snapshots)


@router.get("/api/v1/analytics/usage")
async def get_usage_analytics(
    agent_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Agent & Model Usage — ehrliche Momentaufnahme.

    Zeigt: Welcher Agent nutzt welches Modell, Context-Stand, Tasks erledigt.
    Zeigt NICHT: Token-Verbrauch, Kosten (nicht messbar ohne Gateway-Integration).
    """
    import uuid as _uuid
    from app.models.agent import Agent
    from sqlmodel import select

    query = select(Agent).where(Agent.status != "archived")
    if agent_id:
        query = query.where(Agent.id == _uuid.UUID(agent_id))

    result = await session.exec(query)
    agents = result.all()

    # Redis: aktuelles Modell aus letztem Heartbeat
    redis = await get_redis()
    agents_data = []
    model_summary: dict[str, int] = {}  # model_id -> agent_count

    for agent in agents:
        # Heartbeat model_id aus Redis (Snapshot, nicht Counter)
        heartbeat_model = None
        try:
            raw = await redis.get(f"mc:agent:{agent.id}:heartbeat_model")
            if raw:
                heartbeat_model = raw.decode() if isinstance(raw, bytes) else raw
        except Exception:
            pass

        active_model = heartbeat_model or agent.model
        if active_model:
            model_summary[active_model] = model_summary.get(active_model, 0) + 1

        agents_data.append({
            "agent_id": str(agent.id),
            "name": agent.name,
            "emoji": agent.emoji,
            "model": active_model,
            "status": agent.status,
            "run_state": agent.run_state,
            "context_tokens": agent.context_tokens,
            "context_max": agent.context_max,
            "context_pct": round(agent.context_tokens / agent.context_max * 100) if agent.context_max > 0 else 0,
            "tasks_completed": agent.total_tasks_completed,
            "total_compactions": agent.total_compactions,
            "last_seen_at": agent.last_seen_at.isoformat() if agent.last_seen_at else None,
        })

    return {
        "agents": agents_data,
        "models": model_summary,
        "total_agents": len(agents_data),
        "total_tasks_completed": sum(a["tasks_completed"] for a in agents_data),
    }
