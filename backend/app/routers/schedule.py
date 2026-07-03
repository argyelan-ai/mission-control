"""
Schedule Router — CRUD for scheduled jobs + run history + SSE stream.

Router order: static paths (/stream, /jobs) before parameterized (/jobs/{id}).
"""
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_role, require_user
from app.database import get_session
from app.models.scheduled_job import ScheduledJob
from app.redis_client import RedisKeys
from app.services.scheduler import scheduler
from app.services.sse import make_sse_response

router = APIRouter(prefix="/api/v1/schedule", tags=["schedule"])


# ── Pydantic Schemas ──────────────────────────────────────────────────────────

class JobCreate(BaseModel):
    name: str
    description: str | None = None
    enabled: bool = True
    schedule_type: Literal["daily", "weekdays", "interval", "cron", "weekly_custom"]
    schedule_time: str | None = None
    schedule_interval_hours: int | None = None
    # v2 schedule fields
    schedule_cron: str | None = None
    schedule_weekdays: list[int] | None = None
    start_date: str | None = None   # ISO date string, e.g. "2026-06-01"
    end_date: str | None = None
    action_type: Literal["chat_send", "api_call", "create_task", "session_reset"]
    agent_id: uuid.UUID | None = None
    agent_name: str | None = None
    message: str | None = None
    api_endpoint: str | None = None
    # v2 task payload + tags
    task_payload: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    # Retry
    retry_max: int = Field(default=0, ge=0, le=5)
    retry_delay_minutes: int = Field(default=5, ge=1, le=60)
    # Dependencies
    depends_on_job_id: uuid.UUID | None = None
    # Notifications
    notify_on_failure: bool = False
    # create_task
    task_board_id: uuid.UUID | None = None
    task_title: str | None = None
    task_priority: str | None = None
    task_skip_review: bool = False
    # Discord delivery
    discord_channel_id: str | None = None
    discord_channel_name: str | None = None
    # v2 snooze
    snoozed_until: str | None = None   # ISO datetime string


class JobUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    schedule_type: Literal["daily", "weekdays", "interval", "cron", "weekly_custom"] | None = None
    schedule_time: str | None = None
    schedule_interval_hours: int | None = None
    # v2 schedule fields
    schedule_cron: str | None = None
    schedule_weekdays: list[int] | None = None
    start_date: str | None = None
    end_date: str | None = None
    action_type: Literal["chat_send", "api_call", "create_task", "session_reset"] | None = None
    agent_id: uuid.UUID | None = None
    agent_name: str | None = None
    message: str | None = None
    api_endpoint: str | None = None
    # v2 task payload + tags
    task_payload: dict | None = None
    tags: list[str] | None = None
    retry_max: int | None = Field(default=None, ge=0, le=5)
    retry_delay_minutes: int | None = Field(default=None, ge=1, le=60)
    depends_on_job_id: uuid.UUID | None = None
    notify_on_failure: bool | None = None
    task_board_id: uuid.UUID | None = None
    task_title: str | None = None
    task_priority: str | None = None
    task_skip_review: bool | None = None
    discord_channel_id: str | None = None
    discord_channel_name: str | None = None
    # v2 snooze
    snoozed_until: str | None = None


# ── SSE Stream — must come BEFORE /jobs/{job_id}! ────────────────────────────

@router.get("/stream")
async def schedule_stream(current_user=Depends(require_user)):
    return make_sse_response([RedisKeys.schedule_events()])


# ── Upcoming Firings (top-level, before /jobs/{job_id}) ──────────────────────

@router.get("/upcoming", dependencies=[Depends(require_role("viewer"))])
async def get_upcoming_firings(
    hours: int = 24,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Next firings across all jobs within the given hour window."""
    from datetime import datetime, timezone, timedelta

    cutoff = datetime.now(timezone.utc) + timedelta(hours=hours)
    result = []

    for ap_job in scheduler.scheduler.get_jobs():
        next_run = ap_job.next_run_time
        if next_run and next_run <= cutoff:
            try:
                job_uuid = uuid.UUID(ap_job.id)
                job = await session.get(ScheduledJob, job_uuid)
                if job and job.enabled:
                    result.append({
                        "job_id": str(job.id),
                        "job_name": job.name,
                        "fire_at": next_run.isoformat(),
                        "tags": job.tags or [],
                    })
            except (ValueError, Exception):
                pass  # skip malformed job ids

    result.sort(key=lambda x: x["fire_at"])
    return result


# ── Preview Firings (top-level, before /jobs/{job_id}) ───────────────────────

@router.post("/preview-firings", dependencies=[Depends(require_role("viewer"))])
async def preview_firings(body: dict) -> dict[str, Any]:
    """Return next N firing times for a given schedule configuration."""
    from datetime import datetime, timezone
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    schedule_type = body.get("schedule_type", "cron")
    count = min(body.get("count", 5), 10)
    now = datetime.now(timezone.utc)

    try:
        if schedule_type == "cron":
            cron_expr = body.get("schedule_cron", "0 9 * * *")
            trigger = CronTrigger.from_crontab(cron_expr, timezone="Europe/Zurich")
        elif schedule_type == "daily":
            time_str = body.get("schedule_time", "09:00")
            h, m = time_str.split(":")
            trigger = CronTrigger(hour=int(h), minute=int(m), timezone="Europe/Zurich")
        elif schedule_type == "weekdays":
            time_str = body.get("schedule_time", "09:00")
            h, m = time_str.split(":")
            trigger = CronTrigger(day_of_week="mon-fri", hour=int(h), minute=int(m), timezone="Europe/Zurich")
        elif schedule_type == "weekly_custom":
            days = ",".join(str(d) for d in (body.get("schedule_weekdays") or [0, 1, 2, 3, 4]))
            time_str = body.get("schedule_time", "09:00")
            h, m = time_str.split(":")
            trigger = CronTrigger(day_of_week=days, hour=int(h), minute=int(m), timezone="Europe/Zurich")
        elif schedule_type == "interval":
            trigger = IntervalTrigger(hours=body.get("schedule_interval_hours", 1))
        else:
            return {"firings": [], "description": "Unknown schedule type"}

        firings = []
        next_time = now
        for _ in range(count):
            next_time = trigger.get_next_fire_time(None, next_time)
            if next_time:
                firings.append(next_time.isoformat())
            else:
                break

        return {"firings": firings, "description": ""}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid schedule configuration: {e}")


# ── Jobs CRUD ─────────────────────────────────────────────────────────────────

@router.get("/jobs", dependencies=[Depends(require_role("viewer"))])
async def list_jobs(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    result = await session.exec(select(ScheduledJob).order_by(ScheduledJob.created_at))
    return [j.model_dump() for j in result.all()]


@router.post("/jobs", status_code=201, dependencies=[Depends(require_role("operator"))])
async def create_job(
    payload: JobCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    job = ScheduledJob(**payload.model_dump())
    session.add(job)
    await session.commit()
    await session.refresh(job)
    await scheduler.add_job(job)
    return job.model_dump()


@router.get("/jobs/{job_id}/stats", dependencies=[Depends(require_role("viewer"))])
async def get_job_stats(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Per-job success rates, durations, and trend chart data (30d window)."""
    from datetime import datetime, timezone, timedelta
    from collections import defaultdict
    from app.models.scheduled_job_run import ScheduledJobRun

    job = await session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)

    runs_result = await session.exec(
        select(ScheduledJobRun)
        .where(ScheduledJobRun.job_id == job_id, ScheduledJobRun.started_at >= cutoff_30d)
        .order_by(ScheduledJobRun.started_at)
    )
    runs = runs_result.all()
    runs_7d = [r for r in runs if r.started_at >= cutoff_7d]

    def success_rate(run_list: list) -> float:
        if not run_list:
            return 0.0
        return sum(1 for r in run_list if r.status == "success") / len(run_list)

    def avg_duration_ms(run_list: list) -> float:
        durations = [
            (r.finished_at - r.started_at).total_seconds() * 1000
            for r in run_list
            if r.finished_at and r.started_at
        ]
        return sum(durations) / len(durations) if durations else 0.0

    def p95_duration_ms(run_list: list) -> float:
        durations = sorted([
            (r.finished_at - r.started_at).total_seconds() * 1000
            for r in run_list
            if r.finished_at and r.started_at
        ])
        if not durations:
            return 0.0
        idx = int(len(durations) * 0.95)
        return durations[min(idx, len(durations) - 1)]

    day_map: dict = defaultdict(lambda: {"success": 0, "failed": 0})
    for r in runs:
        day = r.started_at.date().isoformat()
        if r.status == "success":
            day_map[day]["success"] += 1
        elif r.status == "failed":
            day_map[day]["failed"] += 1

    runs_by_day = [{"date": d, **v} for d, v in sorted(day_map.items())]

    return {
        "success_rate_7d": success_rate(runs_7d),
        "success_rate_30d": success_rate(runs),
        "avg_duration_ms": avg_duration_ms(runs),
        "p95_duration_ms": p95_duration_ms(runs),
        "total_runs_30d": len(runs),
        "runs_by_day": runs_by_day,
    }


@router.get("/jobs/{job_id}/heatmap", dependencies=[Depends(require_role("viewer"))])
async def get_job_heatmap(
    job_id: uuid.UUID,
    days: int = 30,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """7×24 activity heatmap: {weekday, hour, count} for successful/failed runs."""
    from datetime import datetime, timezone, timedelta
    from collections import defaultdict
    from app.models.scheduled_job_run import ScheduledJobRun

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.exec(
        select(ScheduledJobRun).where(
            ScheduledJobRun.job_id == job_id,
            ScheduledJobRun.started_at >= cutoff,
            ScheduledJobRun.status.in_(["success", "failed"]),
        )
    )
    runs = result.all()

    counts: dict = defaultdict(int)
    for r in runs:
        counts[(r.started_at.weekday(), r.started_at.hour)] += 1

    return [
        {"weekday": wd, "hour": h, "count": c}
        for (wd, h), c in counts.items()
    ]


@router.get("/jobs/{job_id}/tasks", dependencies=[Depends(require_role("viewer"))])
async def get_job_tasks(
    job_id: uuid.UUID,
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Tasks created by this scheduled job (joined via scheduled_job_runs.task_id)."""
    from app.models.task import Task
    from app.models.scheduled_job_run import ScheduledJobRun

    stmt = (
        select(Task)
        .join(ScheduledJobRun, ScheduledJobRun.task_id == Task.id)
        .where(ScheduledJobRun.job_id == job_id)
        .order_by(Task.created_at.desc())
        .limit(limit)
    )
    result = await session.exec(stmt)
    return [t.model_dump() for t in result.all()]


@router.patch("/jobs/{job_id}/snooze", dependencies=[Depends(require_role("operator"))])
async def snooze_job(
    job_id: uuid.UUID,
    body: dict,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Set snoozed_until to now + hours (default 24)."""
    from datetime import datetime, timezone, timedelta

    job = await session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    hours = int(body.get("hours", 24))
    job.snoozed_until = datetime.now(timezone.utc) + timedelta(hours=hours)
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job.model_dump()


@router.post("/jobs/{job_id}/duplicate", status_code=201, dependencies=[Depends(require_role("operator"))])
async def duplicate_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Clone a job with 'Copy of' prefix, disabled by default."""
    job = await session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    new_job = ScheduledJob(
        name=f"Copy of {job.name}",
        description=job.description,
        enabled=False,
        schedule_type=job.schedule_type,
        schedule_time=job.schedule_time,
        schedule_interval_hours=job.schedule_interval_hours,
        schedule_cron=job.schedule_cron,
        schedule_weekdays=job.schedule_weekdays,
        start_date=job.start_date,
        end_date=job.end_date,
        action_type=job.action_type,
        task_payload=job.task_payload,
        tags=job.tags,
        agent_id=job.agent_id,
        retry_max=job.retry_max,
        retry_delay_minutes=job.retry_delay_minutes,
        depends_on_job_id=None,  # don't copy dependencies
        notify_on_failure=job.notify_on_failure,
    )
    session.add(new_job)
    await session.commit()
    await session.refresh(new_job)
    # Register with APScheduler (disabled job — add_job is a no-op for disabled)
    await scheduler.add_job(new_job)
    return new_job.model_dump()


@router.get("/jobs/{job_id}", dependencies=[Depends(require_role("viewer"))])
async def get_job(
    job_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    job = await session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.model_dump()


@router.patch("/jobs/{job_id}", dependencies=[Depends(require_role("operator"))])
async def update_job(
    job_id: uuid.UUID,
    payload: JobUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    job = await session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(job, key, value)

    session.add(job)
    await session.commit()
    await session.refresh(job)
    await scheduler.update_job(job)
    return job.model_dump()


@router.delete(
    "/jobs/{job_id}", status_code=204, dependencies=[Depends(require_role("operator"))]
)
async def delete_job(job_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    job = await session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    await session.delete(job)
    await session.commit()
    await scheduler.remove_job(str(job_id))


@router.post(
    "/jobs/{job_id}/trigger",
    status_code=202,
    dependencies=[Depends(require_role("operator"))],
)
async def trigger_job(job_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    job = await session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    await scheduler.trigger_now(str(job_id))
    return {"status": "triggered", "job_id": str(job_id)}


# ── Run History ───────────────────────────────────────────────────────────────

@router.get("/jobs/{job_id}/runs", dependencies=[Depends(require_role("viewer"))])
async def get_job_runs(
    job_id: uuid.UUID,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    job = await session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    from app.models.scheduled_job_run import ScheduledJobRun

    result = await session.exec(
        select(ScheduledJobRun)
        .where(ScheduledJobRun.job_id == job_id)
        .order_by(ScheduledJobRun.started_at.desc())
        .limit(limit)
    )
    return [r.model_dump() for r in result.all()]
