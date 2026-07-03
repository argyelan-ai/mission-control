"""
Create built-in scheduled jobs (idempotent via INSERT ON CONFLICT DO NOTHING).
Called on startup.

As of 2026-05-19: BUILTIN_JOBS is intentionally empty.
The original built-in jobs (Session Reset Daily, AI Tech Digest, GitHub Monitor,
and a Morning Briefing that was supposed to be started by "Henry") were based on
the pre-Phase-28 architecture and used legacy action_types (`session_reset`, `chat_send`),
which the scheduler today only logs as "no longer supported".
A seed bug (AsyncSession.exec()/execute() API mismatch, also fixed 2026-05-19) had
caused the seeder to abort without result for a long time anyway — the jobs never
existed in running setups. The operator creates their real jobs (e.g. the active
"Morning Briefing" with action_type=create_task) via the UI / API.

If new built-in jobs are needed: action_type must be "create_task" or
"run_meeting", agent_name must not point to retired agents.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger("mc.schedule_seeder")

BUILTIN_JOBS: list[dict] = []


async def seed_builtin_jobs(session: AsyncSession) -> None:
    """Create built-in jobs — INSERT ON CONFLICT DO NOTHING (a real upsert).

    Uses session.execute() instead of session.exec() for parameterized raw SQL:
    SQLModel.AsyncSession.exec() only accepts 1 argument (the statement),
    while SQLAlchemy's execute() supports the (stmt, params) call.
    """
    for job_data in BUILTIN_JOBS:
        await session.execute(
            text("""
                INSERT INTO scheduled_jobs
                    (id, name, description, schedule_type, schedule_time, action_type,
                     agent_name, message, api_endpoint, enabled,
                     retry_max, retry_delay_minutes, notify_on_failure, created_at)
                VALUES
                    (:id, :name, :description, :schedule_type, :schedule_time, :action_type,
                     :agent_name, :message, :api_endpoint, :enabled,
                     0, 5, false, :created_at)
                ON CONFLICT (name) DO NOTHING
            """),
            {
                "id": str(uuid.uuid4()),
                "name": job_data["name"],
                "description": job_data.get("description"),
                "schedule_type": job_data["schedule_type"],
                "schedule_time": job_data.get("schedule_time"),
                "action_type": job_data["action_type"],
                "agent_name": job_data.get("agent_name"),
                "message": job_data.get("message"),
                "api_endpoint": job_data.get("api_endpoint"),
                "enabled": job_data.get("enabled", True),
                "created_at": datetime.now(timezone.utc),
            },
        )
        logger.info("Seeded (or skipped existing): %s", job_data["name"])

    await session.commit()
