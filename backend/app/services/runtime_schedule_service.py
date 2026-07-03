"""
RuntimeScheduleService — manages schedules for local model runtimes.

Runs start/stop actions via SSH on the DGX Spark.
Runs as an asyncio background loop (independent of APScheduler).
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.runtime_schedule import RuntimeSchedule, RuntimeScheduleRun
from app.services import runtime_manager

logger = logging.getLogger("mc.runtime_scheduler")

_ZURICH = ZoneInfo("Europe/Zurich")
_running_tasks: set[asyncio.Task] = set()


def _day_matches(days: str, weekday: int) -> bool:
    """Checks whether the current weekday matches the schedule.

    weekday: 0=Monday, 6=Sunday (Python datetime.weekday())
    """
    if days == "daily":
        return True
    if days == "weekdays":
        return weekday < 5
    if days == "weekends":
        return weekday >= 5
    return False


async def get_schedules(runtime_id: str) -> list[dict]:
    """All schedules for a runtime, including the last run."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        schedules = (
            await session.exec(
                select(RuntimeSchedule)
                .where(RuntimeSchedule.runtime_id == runtime_id)
                .order_by(RuntimeSchedule.created_at)
            )
        ).all()

        result = []
        for s in schedules:
            last_run = (
                await session.exec(
                    select(RuntimeScheduleRun)
                    .where(RuntimeScheduleRun.schedule_id == s.id)
                    .order_by(RuntimeScheduleRun.executed_at.desc())
                    .limit(1)
                )
            ).first()
            result.append(_to_dict(s, last_run))
        return result


async def create_schedule(runtime_id: str, data: dict) -> dict:
    """Create a new schedule."""
    schedule = RuntimeSchedule(
        runtime_id=runtime_id,
        name=data["name"],
        action=data["action"],
        time_of_day=data["time_of_day"],
        days=data["days"],
        unload_first=data.get("unload_first", False),
        enabled=data.get("enabled", True),
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(schedule)
        await session.commit()
        await session.refresh(schedule)
    return _to_dict(schedule, None)


async def update_schedule(schedule_id: uuid.UUID, data: dict) -> dict | None:
    """Update a schedule (patch semantics — only supplied fields)."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        schedule = await session.get(RuntimeSchedule, schedule_id)
        if not schedule:
            return None
        for field in ("name", "action", "time_of_day", "days", "unload_first", "enabled"):
            if field in data:
                setattr(schedule, field, data[field])
        session.add(schedule)
        await session.commit()
        await session.refresh(schedule)
    return _to_dict(schedule, None)


async def delete_schedule(schedule_id: uuid.UUID) -> bool:
    """Delete a schedule. Returns False if not found."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        schedule = await session.get(RuntimeSchedule, schedule_id)
        if not schedule:
            return False
        await session.delete(schedule)
        await session.commit()
    return True


async def get_runs(schedule_id: uuid.UUID, limit: int = 5) -> list[dict]:
    """Last N executions of a schedule."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        runs = (
            await session.exec(
                select(RuntimeScheduleRun)
                .where(RuntimeScheduleRun.schedule_id == schedule_id)
                .order_by(RuntimeScheduleRun.executed_at.desc())
                .limit(limit)
            )
        ).all()
    return [
        {
            "id": str(r.id),
            "executed_at": r.executed_at.isoformat(),
            "success": r.success,
            "message": r.message,
        }
        for r in runs
    ]


async def _execute_schedule(schedule: RuntimeSchedule) -> None:
    """Executes a schedule and stores the result."""
    success = True
    message = None
    try:
        if schedule.action == "kv_reset":
            # KV reset: remember current state → unload everything → reload
            loaded_models = await runtime_manager.lms_get_loaded_models()
            if not loaded_models:
                message = "Keine Modelle geladen — KV Reset übersprungen."
                logger.info("KV Reset: keine Modelle geladen, übersprungen.")
            else:
                logger.info("KV Reset: entlade %d Modell(e): %s", len(loaded_models), loaded_models)
                unload_result = await runtime_manager.lms_unload_all()
                if not unload_result["ok"]:
                    raise ValueError(f"Unload fehlgeschlagen: {unload_result['message']}")
                await asyncio.sleep(3)
                errors = []
                for model_id in loaded_models:
                    load_result = await runtime_manager.lms_load_by_id(model_id)
                    if not load_result["ok"]:
                        errors.append(model_id)
                        logger.warning("KV Reset: Reload fehlgeschlagen für %s: %s", model_id, load_result["message"])
                if errors:
                    success = False
                    message = f"Reload fehlgeschlagen für: {', '.join(errors)}"
                else:
                    message = f"KV Reset OK — {len(loaded_models)} Modell(e) neu geladen: {', '.join(loaded_models)}"
        else:
            runtime = runtime_manager.get_runtime(schedule.runtime_id)
            if not runtime:
                raise ValueError(f"Runtime '{schedule.runtime_id}' nicht in runtimes.json gefunden")

            if schedule.action == "start":
                if schedule.unload_first and runtime.get("runtime_type") == "lmstudio":
                    unload_result = await runtime_manager.lms_unload_all()
                    if not unload_result["ok"]:
                        logger.warning("lms unload --all schlug fehl: %s", unload_result["message"])
                result = await runtime_manager.start_runtime(runtime)
            elif schedule.action == "stop":
                result = await runtime_manager.stop_runtime(runtime)
            else:
                raise ValueError(f"Unbekannte Aktion: {schedule.action}")

            if not result["ok"]:
                success = False
                message = result["message"]
            else:
                message = result["message"]

    except Exception as e:
        success = False
        message = str(e)
        logger.error("Schedule-Ausführung fehlgeschlagen für %s: %s", schedule.name, e)

    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            run = RuntimeScheduleRun(
                schedule_id=schedule.id,
                executed_at=datetime.now(timezone.utc),
                success=success,
                message=message,
            )
            session.add(run)
            await session.commit()
    except Exception as db_err:
        logger.error("Fehler beim Speichern des Schedule-Runs für %s: %s", schedule.name, db_err)

    logger.info(
        "Schedule ausgeführt: %s (%s) → %s",
        schedule.name,
        schedule.action,
        "OK" if success else f"FEHLER: {message}",
    )


async def _scheduler_loop() -> None:
    """Runs every minute — checks which schedules should execute now."""
    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(_ZURICH)
            current_time = now.strftime("%H:%M")
            current_weekday = now.weekday()

            async with AsyncSession(engine, expire_on_commit=False) as session:
                schedules = (
                    await session.exec(
                        select(RuntimeSchedule).where(
                            RuntimeSchedule.enabled == True,  # noqa: E712
                            RuntimeSchedule.time_of_day == current_time,
                        )
                    )
                ).all()

            for schedule in schedules:
                if _day_matches(schedule.days, current_weekday):
                    task = asyncio.create_task(_execute_schedule(schedule))
                    _running_tasks.add(task)
                    task.add_done_callback(_running_tasks.discard)

        except Exception as e:
            logger.error("Fehler im Runtime-Scheduler-Loop: %s", e)


class RuntimeScheduleService:
    def __init__(self):
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(_scheduler_loop())
        logger.info("RuntimeScheduleService gestartet")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("RuntimeScheduleService gestoppt")


runtime_schedule_service = RuntimeScheduleService()


def _to_dict(schedule: RuntimeSchedule, last_run: RuntimeScheduleRun | None) -> dict:
    return {
        "id": str(schedule.id),
        "runtime_id": schedule.runtime_id,
        "name": schedule.name,
        "action": schedule.action,
        "time_of_day": schedule.time_of_day,
        "days": schedule.days,
        "unload_first": schedule.unload_first,
        "enabled": schedule.enabled,
        "created_at": schedule.created_at.isoformat(),
        "last_run": {
            "executed_at": last_run.executed_at.isoformat(),
            "success": last_run.success,
            "message": last_run.message,
        }
        if last_run
        else None,
    }
