"""
Built-in Scheduled Jobs anlegen (idempotent via INSERT ON CONFLICT DO NOTHING).
Wird beim Startup aufgerufen.

Stand 2026-05-19: BUILTIN_JOBS ist absichtlich leer.
Die ursprünglichen Built-in Jobs (Session Reset Daily, AI Tech Digest, GitHub Monitor,
und ein Morning Briefing das von "Henry" hätte gestartet werden sollen) basierten auf
der pre-Phase-28-Architektur und nutzten Legacy-action_types (`session_reset`, `chat_send`),
die der Scheduler heute nur noch als "no longer supported" loggt.
Seed-Bug (AsyncSession.exec()/execute() API-Mismatch, ebenfalls 2026-05-19 gefixt) hatte den
Seeder ohnehin seit langem ergebnislos abbrechen lassen — die Jobs existierten nie in
laufenden Setups. Der Operator legt seine echten Jobs (z.B. das aktive "Morning Briefing" mit
action_type=create_task) über die UI / API an.

Wenn neue Built-in Jobs benötigt werden: action_type muss "create_task" oder
"run_meeting" sein, agent_name darf nicht auf retired Agents zeigen.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger("mc.schedule_seeder")

BUILTIN_JOBS: list[dict] = []


async def seed_builtin_jobs(session: AsyncSession) -> None:
    """Built-in Jobs anlegen — INSERT ON CONFLICT DO NOTHING (echter Upsert).

    Nutzt session.execute() statt session.exec() für parametrisiertes Raw-SQL:
    SQLModel.AsyncSession.exec() akzeptiert nur 1 Argument (das Statement),
    während SQLAlchemy's execute() den (stmt, params)-Aufruf unterstützt.
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
