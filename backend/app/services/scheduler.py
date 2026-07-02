"""
Scheduler Service — Fuehrt geplante Jobs via APScheduler aus.

Laeuft als asyncio Background Service in der FastAPI Lifespan.
Jobs werden aus der DB geladen und in APScheduler registriert.
"""

import asyncio
import logging
import uuid

from app.utils import create_tracked_task
from datetime import datetime, timedelta, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.database import engine
from app.models.scheduled_job import ScheduledJob
from app.models.workflow import WorkflowTemplate
from app.services.activity import emit_event

logger = logging.getLogger("mc.scheduler")


# Lock-Lifecycle:
#   TTL kurz halten (120s) damit ein Crash schnell heilt — kein hängender 1h-Lock.
#   Refresh-Task hält den Lock am Leben solange der Service läuft.
#   Acquire mit Retry — wenn ein alter Worker den Lock noch hält, warten wir, statt
#   beim Boot sofort aufzugeben (Bug der bis 2026-05-19 dazu führte, dass ein
#   abrupter Container-Restart den Scheduler bis zum nächsten Restart komplett tot legte).
LOCK_TTL_SECONDS = 120
LOCK_REFRESH_INTERVAL_SECONDS = 60
LOCK_ACQUIRE_MAX_ATTEMPTS = 10
LOCK_ACQUIRE_RETRY_DELAY_SECONDS = 15


class SchedulerService:
    def __init__(self):
        self._scheduler = AsyncIOScheduler(
            timezone="Europe/Zurich",
            job_defaults={
                "misfire_grace_time": 3600,  # 1h Nachholzeit nach verpasstem Trigger
                "coalesce": True,            # Mehrere verpasste Läufe → nur 1x nachholen
            },
        )
        self._running = False
        self._refresh_task: asyncio.Task | None = None

    async def _acquire_lock(self) -> bool:
        """Versucht den Redis-Lock zu erwerben, mit Retry.

        Returns True wenn der Lock geholt wurde, False wenn nach allen Versuchen nicht.
        Die Retry-Strategie deckt den Fall ab, in dem ein alter Worker beim
        Container-Restart noch nicht ganz tot ist oder einen stale Lock hinterlassen
        hat — durch die kurze TTL läuft dieser spätestens nach LOCK_TTL_SECONDS aus.
        """
        from app.redis_client import RedisKeys, get_redis
        redis = await get_redis()
        for attempt in range(1, LOCK_ACQUIRE_MAX_ATTEMPTS + 1):
            acquired = await redis.set(
                RedisKeys.scheduler_lock(), "1", nx=True, ex=LOCK_TTL_SECONDS
            )
            if acquired:
                if attempt > 1:
                    logger.info("Scheduler lock acquired after %d attempts", attempt)
                return True
            logger.info(
                "Scheduler lock held by another worker — retry %d/%d in %ds",
                attempt,
                LOCK_ACQUIRE_MAX_ATTEMPTS,
                LOCK_ACQUIRE_RETRY_DELAY_SECONDS,
            )
            await asyncio.sleep(LOCK_ACQUIRE_RETRY_DELAY_SECONDS)
        return False

    async def _refresh_lock_loop(self):
        """Hält den Lock am Leben solange der Service läuft.

        Refresh alle LOCK_REFRESH_INTERVAL_SECONDS via EXPIRE. Wenn der Refresh
        einmal fehlschlägt, läuft der Lock nach LOCK_TTL_SECONDS aus — APScheduler
        läuft trotzdem mit den schon registrierten Jobs weiter (der Lock ist nur
        ein Boot-Gate, kein Pre-Trigger-Check). Bei Recovery erwirbt der nächste
        Worker den Lock automatisch via _acquire_lock.
        """
        from app.redis_client import RedisKeys, get_redis
        redis = await get_redis()
        while self._running:
            try:
                await asyncio.sleep(LOCK_REFRESH_INTERVAL_SECONDS)
                if not self._running:
                    break
                await redis.expire(RedisKeys.scheduler_lock(), LOCK_TTL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler lock refresh failed (will retry next tick)")

    async def start(self):
        """Service starten — Redis-Lock verhindert Doppel-Start bei mehreren Workern.

        Bei besetztem Lock wird mit Retry gewartet (siehe _acquire_lock). Damit ist
        ein abrupter Container-Restart kein Killer-Szenario mehr: der alte Lock
        läuft spätestens nach LOCK_TTL_SECONDS aus und der neue Worker übernimmt.
        """
        if not await self._acquire_lock():
            logger.warning(
                "Failed to acquire scheduler lock after %d attempts — service not started",
                LOCK_ACQUIRE_MAX_ATTEMPTS,
            )
            return
        self._scheduler.start()
        self._running = True
        await self._load_jobs_from_db()
        self._refresh_task = create_tracked_task(self._refresh_lock_loop())
        logger.info("SchedulerService started")

    async def stop(self):
        """Service stoppen."""
        if self._running:
            self._running = False
            if self._refresh_task is not None:
                self._refresh_task.cancel()
                self._refresh_task = None
            self._scheduler.shutdown(wait=False)
            from app.redis_client import RedisKeys, get_redis
            redis = await get_redis()
            await redis.delete(RedisKeys.scheduler_lock())
        logger.info("SchedulerService stopped")

    async def _load_jobs_from_db(self):
        """Beim Start alle enabled Jobs aus DB in APScheduler laden."""
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await session.exec(
                select(ScheduledJob).where(ScheduledJob.enabled == True)  # noqa: E712
            )
            jobs = result.all()

            workflow_result = await session.exec(
                select(WorkflowTemplate).where(
                    WorkflowTemplate.status == "active",
                    WorkflowTemplate.trigger_type == "scheduled",
                )
            )
            workflows = workflow_result.all()

        for job in jobs:
            self._register_job(job)
            logger.info("Loaded scheduled job: %s", job.name)
        for workflow in workflows:
            self.register_workflow(workflow)
            logger.info("Loaded scheduled workflow: %s", workflow.name)

    def _build_trigger(self, job: ScheduledJob) -> tuple[str, dict]:
        """APScheduler-Trigger aus Job-Config bauen."""
        # Collect optional start_date / end_date kwargs
        date_kwargs: dict = {}
        if job.start_date:
            date_kwargs["start_date"] = job.start_date
        if job.end_date:
            date_kwargs["end_date"] = job.end_date

        if job.schedule_type == "daily" and job.schedule_time:
            hour, minute = map(int, job.schedule_time.split(":"))
            return ("cron", {"hour": hour, "minute": minute, **date_kwargs})
        elif job.schedule_type == "weekdays" and job.schedule_time:
            hour, minute = map(int, job.schedule_time.split(":"))
            return ("cron", {"hour": hour, "minute": minute, "day_of_week": "mon-fri", **date_kwargs})
        elif job.schedule_type == "interval" and job.schedule_interval_hours:
            return ("interval", {"hours": job.schedule_interval_hours, **date_kwargs})
        elif job.schedule_type == "cron" and job.schedule_cron:
            from apscheduler.triggers.cron import CronTrigger
            trigger = CronTrigger.from_crontab(
                job.schedule_cron,
                timezone="Europe/Zurich",
            )
            # start_date / end_date are not easily injectable into a pre-built CronTrigger,
            # so we pass them as constructor kwargs instead by building directly.
            if date_kwargs:
                trigger = CronTrigger.from_crontab(
                    job.schedule_cron,
                    timezone="Europe/Zurich",
                    **date_kwargs,
                )
            # Return sentinel so _register_job knows to use the trigger object directly
            return ("__trigger_object__", {"__trigger__": trigger})
        elif job.schedule_type == "weekly_custom":
            from apscheduler.triggers.cron import CronTrigger
            # schedule_weekdays is [0,1,2,3,4] where 0=Mon (APScheduler: 0=mon)
            days = ",".join(str(d) for d in (job.schedule_weekdays or [0, 1, 2, 3, 4]))
            h, m = (job.schedule_time or "09:00").split(":")
            trigger = CronTrigger(
                day_of_week=days,
                hour=int(h),
                minute=int(m),
                timezone="Europe/Zurich",
                **date_kwargs,
            )
            return ("__trigger_object__", {"__trigger__": trigger})
        else:
            raise ValueError(
                f"Invalid schedule config for job {job.id}: "
                f"type={job.schedule_type}, time={job.schedule_time}, "
                f"interval={job.schedule_interval_hours}"
            )

    def _register_job(self, job: ScheduledJob):
        """Job in APScheduler registrieren."""
        try:
            trigger_type, trigger_kwargs = self._build_trigger(job)

            if trigger_type == "__trigger_object__":
                # Pre-built CronTrigger object (cron / weekly_custom)
                trigger_obj = trigger_kwargs["__trigger__"]
                self._scheduler.add_job(
                    self._execute_job,
                    trigger=trigger_obj,
                    id=str(job.id),
                    args=[str(job.id)],
                    replace_existing=True,
                )
            else:
                self._scheduler.add_job(
                    self._execute_job,
                    trigger=trigger_type,
                    id=str(job.id),
                    args=[str(job.id)],
                    replace_existing=True,
                    **trigger_kwargs,
                )
            create_tracked_task(self._update_next_run(str(job.id)))
        except Exception as e:
            logger.error("Failed to register job %s: %s", job.name, e)

    def _unregister_job(self, job_id: str):
        """Job aus APScheduler entfernen."""
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass

    def _build_workflow_trigger(self, workflow: WorkflowTemplate) -> tuple[str, dict]:
        trigger_config = workflow.trigger_config or {}
        schedule_type = trigger_config.get("schedule_type")
        if schedule_type == "daily" and trigger_config.get("schedule_time"):
            hour, minute = map(int, str(trigger_config["schedule_time"]).split(":"))
            return ("cron", {"hour": hour, "minute": minute})
        if schedule_type == "weekdays" and trigger_config.get("schedule_time"):
            hour, minute = map(int, str(trigger_config["schedule_time"]).split(":"))
            return ("cron", {"hour": hour, "minute": minute, "day_of_week": "mon-fri"})
        if schedule_type == "weekly" and trigger_config.get("schedule_time"):
            hour, minute = map(int, str(trigger_config["schedule_time"]).split(":"))
            schedule_day = str(trigger_config.get("schedule_day") or "mon").lower()
            return ("cron", {"hour": hour, "minute": minute, "day_of_week": schedule_day})
        if schedule_type == "interval" and trigger_config.get("schedule_interval_hours"):
            return ("interval", {"hours": int(trigger_config["schedule_interval_hours"])})
        raise ValueError(f"Invalid workflow schedule for {workflow.id}")

    def register_workflow(self, workflow: WorkflowTemplate):
        try:
            trigger_type, trigger_kwargs = self._build_workflow_trigger(workflow)
            workflow_job_id = f"workflow:{workflow.id}"
            self._scheduler.add_job(
                self._execute_workflow,
                trigger=trigger_type,
                id=workflow_job_id,
                args=[str(workflow.id)],
                replace_existing=True,
                **trigger_kwargs,
            )
            create_tracked_task(self._update_workflow_next_run(str(workflow.id)))
        except Exception as e:
            logger.error("Failed to register workflow %s: %s", workflow.name, e)

    def unregister_workflow(self, workflow_id: str):
        self._unregister_job(f"workflow:{workflow_id}")

    async def _update_workflow_next_run(self, workflow_id: str):
        await asyncio.sleep(0.1)
        ap_job = self._scheduler.get_job(f"workflow:{workflow_id}")
        next_run_time = ap_job.next_run_time if ap_job else None
        async with AsyncSession(engine, expire_on_commit=False) as session:
            workflow = await session.get(WorkflowTemplate, uuid.UUID(workflow_id))
            if workflow:
                workflow.next_run_at = next_run_time
                session.add(workflow)
                await session.commit()

    async def _execute_workflow(self, workflow_id: str):
        from app.services.workflow_service import workflow_service

        async with AsyncSession(engine, expire_on_commit=False) as session:
            workflow = await session.get(WorkflowTemplate, uuid.UUID(workflow_id))
            if not workflow or workflow.status != "active":
                return
            try:
                await workflow_service.start_run(
                    session,
                    workflow,
                    triggered_by="scheduler",
                    trigger_payload={"job_id": f"workflow:{workflow_id}"},
                )
            except Exception as e:
                logger.error("Scheduled workflow %s failed to start: %s", workflow.name, e)
        await self._update_workflow_next_run(workflow_id)

    async def _update_next_run(self, job_id: str):
        """next_run_at aus APScheduler lesen + in DB schreiben."""
        await asyncio.sleep(0.1)
        ap_job = self._scheduler.get_job(job_id)
        if ap_job and getattr(ap_job, "next_run_time", None):
            async with AsyncSession(engine, expire_on_commit=False) as session:
                job = await session.get(ScheduledJob, uuid.UUID(job_id))
                if job:
                    job.next_run_at = ap_job.next_run_time
                    session.add(job)
                    await session.commit()

    async def _execute_job(self, job_id: str, retry_attempt: int = 0):
        """Job ausfuehren + Run-Record in DB schreiben + SSE broadcast."""
        from app.models.scheduled_job_run import ScheduledJobRun
        from app.redis_client import RedisKeys
        from app.services.sse import broadcast

        async with AsyncSession(engine, expire_on_commit=False) as session:
            job = await session.get(ScheduledJob, uuid.UUID(job_id))
            if not job or not job.enabled:
                return

            # Snooze-Check: Job ist bis snoozed_until pausiert
            now = datetime.now(timezone.utc)
            if job.snoozed_until and job.snoozed_until > now:
                logger.info(
                    "Job %s is snoozed until %s, skipping", job.id, job.snoozed_until
                )
                return  # Kein Run-Record für snoozed Jobs

            logger.info("Executing job: %s (attempt %d)", job.name, retry_attempt)

            # Run-Record anlegen
            run = ScheduledJobRun(
                job_id=uuid.UUID(job_id),
                started_at=datetime.now(timezone.utc),
                status="running",
                retry_attempt=retry_attempt,
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            run_id = str(run.id)

        # SSE: Job gestartet
        await broadcast(
            RedisKeys.schedule_events(),
            "job.started",
            {"job_id": job_id, "run_id": run_id, "job_name": job.name},
        )

        success = False
        error = None
        detail: dict = {}

        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                job = await session.get(ScheduledJob, uuid.UUID(job_id))
                if not job:
                    return

                if job.action_type == "create_task":
                    success, error, detail = await self._do_create_task(session, job)

                elif job.action_type == "run_meeting":
                    async with AsyncSession(engine, expire_on_commit=False) as meet_session:
                        success, error, detail = await self._do_run_meeting(meet_session, job)

                else:
                    # Legacy action_type (chat_send, session_reset, api_call) — no longer supported
                    logger.warning(
                        "Job %s has legacy action_type '%s', skipping. Disable this job.",
                        job.id,
                        job.action_type,
                    )
                    success = False
                    error = f"Legacy action_type '{job.action_type}' no longer supported"

        except Exception as e:
            error = str(e)
            logger.error("Job %s failed: %s", job.name, e)

        finished_at = datetime.now(timezone.utc)

        # consecutive_failures aktualisieren + ggf. auto-disable
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job = await session.get(ScheduledJob, uuid.UUID(job_id))
            if job:
                if success:
                    if job.consecutive_failures > 0:
                        job.consecutive_failures = 0
                        session.add(job)
                        await session.commit()
                else:
                    job.consecutive_failures = (job.consecutive_failures or 0) + 1
                    if job.consecutive_failures >= 3:
                        job.enabled = False
                        logger.warning(
                            "Job %s auto-disabled after %d consecutive failures",
                            job.id,
                            job.consecutive_failures,
                        )
                    session.add(job)
                    await session.commit()

        # Run-Record finalisieren
        async with AsyncSession(engine, expire_on_commit=False) as session:
            run = await session.get(ScheduledJobRun, uuid.UUID(run_id))
            if run:
                run.finished_at = finished_at
                run.status = "success" if success else "failed"
                run.error = error
                run.detail = detail if detail else None
                session.add(run)

            # last_run_* auf ScheduledJob aktualisieren (backward compat)
            job = await session.get(ScheduledJob, uuid.UUID(job_id))
            if job:
                job.last_run_at = finished_at
                job.last_run_status = "success" if success else "failed"
                job.last_run_error = error
                session.add(job)

            await session.commit()

        # Run-History beschneiden
        await self._prune_run_history(job_id)

        # Retry-Logik
        if not success and job.retry_max > 0 and retry_attempt < job.retry_max:
            await self._schedule_retry(job_id, retry_attempt + 1, job.retry_delay_minutes)

        # Failure-Notification
        if not success and job.notify_on_failure:
            await self._send_failure_notification(job, error)

        # Dependent Jobs triggern wenn erfolgreich
        if success:
            await self._trigger_dependent_jobs(job_id)

        # SSE: Job abgeschlossen
        await broadcast(
            RedisKeys.schedule_events(),
            "job.completed",
            {
                "job_id": job_id,
                "run_id": run_id,
                "job_name": job.name,
                "status": "success" if success else "failed",
                "error": error,
            },
        )

        # Activity Feed
        async with AsyncSession(engine, expire_on_commit=False) as emit_session:
            await emit_event(
                emit_session,
                event_type="job.executed",
                title=(
                    f"Job: {job.name} — {'success' if success else 'failed'}"
                    + (f" ({error})" if error else "")
                ),
                severity="info" if success else "warning",
                detail={"job_id": job_id, "job_name": job.name, "run_id": run_id},
            )

        # Scheduler Status → #dev-log (mc:discord:channel:jobs).
        # Vorher hardcoded job_channel_map = {Morning Briefing: briefing, ...}
        # postete "Erfolgreich ausgefuehrt." in den Content-Channel und
        # blockierte den Channel fuer den eigentlichen Job-Output
        # (das echte Briefing kommt vom Researcher selbst). 2026-05-18.
        try:
            from app.services.discord_router import get_channel_id
            from app.services.discord import send_to_discord_channel
            ch_id = await get_channel_id("jobs")
            if ch_id:
                status_emoji = "✅" if success else "❌"
                desc = "Erfolgreich ausgefuehrt." if success else (
                    f"Fehlgeschlagen — {error or 'unbekannt'}"
                )
                await send_to_discord_channel(ch_id, embed={
                    "title": f"{status_emoji} Job: {job.name}",
                    "description": desc,
                    "color": 0x00CC88 if success else 0xEF4444,
                })
        except Exception:
            logger.warning("Scheduler Discord-Push fehlgeschlagen", exc_info=True)

        await self._update_next_run(job_id)

    async def _prune_run_history(self, job_id: str, max_runs: int = 50):
        """Alte Run-Records löschen — max. 50 pro Job."""
        from sqlalchemy import delete as sa_delete
        from app.models.scheduled_job_run import ScheduledJobRun

        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await session.exec(
                select(ScheduledJobRun.id)
                .where(ScheduledJobRun.job_id == uuid.UUID(job_id))
                .order_by(ScheduledJobRun.started_at.desc())
            )
            all_ids = result.all()
            if len(all_ids) > max_runs:
                ids_to_delete = all_ids[max_runs:]
                await session.exec(
                    sa_delete(ScheduledJobRun).where(
                        ScheduledJobRun.id.in_(ids_to_delete)
                    )
                )
                await session.commit()

    async def _schedule_retry(self, job_id: str, attempt: int, delay_minutes: int):
        """Einmaliger Retry-Job nach delay_minutes."""
        from apscheduler.triggers.date import DateTrigger

        run_at = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        retry_id = f"{job_id}:retry:{attempt}:{int(run_at.timestamp())}"
        self._scheduler.add_job(
            self._execute_job,
            trigger=DateTrigger(run_date=run_at),
            id=retry_id,
            args=[job_id, attempt],
            replace_existing=True,
        )
        logger.info("Scheduled retry %d for job %s at %s", attempt, job_id, run_at)

    async def _trigger_dependent_jobs(self, completed_job_id: str):
        """Alle Jobs triggern die auf diesen Job warten."""
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await session.exec(
                select(ScheduledJob).where(
                    ScheduledJob.depends_on_job_id == uuid.UUID(completed_job_id),
                    ScheduledJob.enabled == True,  # noqa: E712
                )
            )
            dependent_jobs = result.all()
        for dep in dependent_jobs:
            logger.info("Triggering dependent job: %s", dep.name)
            create_tracked_task(self._execute_job(str(dep.id)))

    async def _send_failure_notification(self, job: ScheduledJob, error: str | None):
        """Telegram-Notification bei Job-Fehler (Phase 29: direct HTTPS path)."""
        try:
            from app.services.telegram_bot import telegram_bot
            await telegram_bot.send_message(
                f"<b>Job fehlgeschlagen: {job.name}</b>\n"
                f"Error: {error or 'unbekannt'}"
            )
        except Exception as e:
            logger.warning("Failure notification failed: %s", e)

    async def _do_create_task(
        self, session: AsyncSession, job: ScheduledJob
    ) -> tuple[bool, str | None, dict]:
        """Task erstellen via create_task_internal + aktiven Dispatch."""
        from app.services.task_create import create_task_internal

        # Payload aus task_payload (neu) mit Fallback auf alte Felder
        payload = job.task_payload or {}
        board_id = payload.get("board_id") or job.task_board_id
        title = payload.get("title") or job.task_title or job.name
        priority = payload.get("priority") or job.task_priority or "medium"
        skip_review = (
            payload["skip_review"]
            if "skip_review" in payload
            else job.task_skip_review
        )
        description = payload.get("description")
        assigned_agent_id_raw = payload.get("assigned_agent_id")

        # Fallback: agent_id aus Job-Feld (für alte Jobs)
        if not assigned_agent_id_raw and job.agent_id:
            assigned_agent_id_raw = str(job.agent_id)

        # Discord-Hinweis in Description einbauen (backward compat)
        if job.discord_channel_id:
            channel_label = job.discord_channel_name or job.discord_channel_id
            discord_note = (
                f"\n\n📤 Discord Delivery: Sende das Ergebnis an Discord Channel "
                f"**#{channel_label}** (ID: `{job.discord_channel_id}`)"
            )
            description = (description or job.message or title) + discord_note

        if not board_id:
            return False, f"Job {job.id} has no board_id in task_payload or task_board_id", {}

        try:
            assigned_agent_id = (
                uuid.UUID(str(assigned_agent_id_raw)) if assigned_agent_id_raw else None
            )
            task = await create_task_internal(
                session,
                board_id=uuid.UUID(str(board_id)),
                title=title,
                description=description,
                priority=priority,
                skip_review=bool(skip_review),
                is_auto_created=True,
                auto_reason=f"scheduled:{job.name}",
                assigned_agent_id=assigned_agent_id,
                report_back_enabled=bool(job.discord_channel_id),
                report_back_channel="discord" if job.discord_channel_id else None,
                dispatch=True,
            )
            return True, None, {"task_id": str(task.id), "task_title": task.title}
        except Exception as e:
            return False, str(e), {}

    async def _do_run_meeting(
        self, session: AsyncSession, job: ScheduledJob
    ) -> tuple[bool, str | None, dict]:
        """Meeting starten via MeetingService."""
        from app.services.meeting_service import MeetingError, start_meeting

        board_id = job.task_board_id  # Board-ID aus dem Job
        if not board_id:
            return False, "task_board_id (= Meeting Board) fehlt", {}

        title = job.task_title or f"Weekly Meeting — {job.name}"
        # Agenda aus message-Feld (JSON-Liste) oder Default
        agenda = []
        if job.message:
            import json as _json
            try:
                parsed = _json.loads(job.message)
                if isinstance(parsed, list):
                    agenda = parsed
            except (ValueError, TypeError):
                pass
        if not agenda:
            agenda = [
                "Was lief gut diese Woche?",
                "Was lief schlecht?",
                "Was nehmen wir uns fuer naechste Woche vor?",
            ]

        try:
            meeting = await start_meeting(
                session,
                board_id=board_id,
                title=title,
                agenda=agenda,
                meeting_type="weekly",
            )
            return True, None, {"meeting_id": str(meeting.id)}
        except MeetingError as e:
            return False, str(e), {}
        except Exception as e:
            return False, str(e), {}

    async def _resolve_agent_id(self, session: AsyncSession, job: ScheduledJob) -> str | None:
        """agent_id aus Job holen, oder by name suchen."""
        if job.agent_id:
            return str(job.agent_id)
        if job.agent_name:
            from app.models.agent import Agent
            result = await session.exec(
                select(Agent).where(Agent.name == job.agent_name)
            )
            agent = result.first()
            return str(agent.id) if agent else None
        return None

    # ── Public API ─────────────────────────────────────────────────────────

    async def add_job(self, job: ScheduledJob):
        if job.enabled:
            self._register_job(job)

    async def update_job(self, job: ScheduledJob):
        self._unregister_job(str(job.id))
        if job.enabled:
            self._register_job(job)

    async def remove_job(self, job_id: str):
        self._unregister_job(job_id)

    async def trigger_now(self, job_id: str):
        create_tracked_task(self._execute_job(job_id))


scheduler = SchedulerService()
