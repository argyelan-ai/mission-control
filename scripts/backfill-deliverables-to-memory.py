"""Backfill existing TaskDeliverables into BoardMemory.

Vor dem Fix (commit 29b5fc9) haben Agents Deliverables registriert ohne dass
ein BoardMemory-Eintrag erzeugt wurde. Dieses Script erstellt retroaktiv je
einen Memory-Eintrag pro existierendem Deliverable, getaggt mit
"backfilled" damit er von normalen Eintraegen unterscheidbar ist.

Idempotent: ein Deliverable das bereits einen Memory-Eintrag mit Tag
"deliverable:<id>" hat, wird uebersprungen.

Aufruf (im Backend-Container):
    docker compose exec -T -e PYTHONPATH=/app backend \\
        python3 /app/scripts/backfill-deliverables-to-memory.py

Oder vom Host mit Bind-Mount des scripts-Ordners:
    docker cp scripts/backfill-deliverables-to-memory.py \\
        mission-control-backend-1:/tmp/backfill.py
    docker compose exec -T -e PYTHONPATH=/app backend python3 /tmp/backfill.py
"""
import asyncio
import sys
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.agent import Agent
from app.models.deliverable import TaskDeliverable
from app.models.memory import BoardMemory
from app.models.task import Task


_TYPE_TO_MEMORY_TYPE = {
    "document": "knowledge",
    "data": "knowledge",
    "url": "reference",
    "file": "reference",
    "artifact": "reference",
    "screenshot": "reference",
}


async def main() -> int:
    created = 0
    skipped = 0
    missing_task = 0
    missing_agent = 0

    async with AsyncSession(engine, expire_on_commit=False) as db:
        result = await db.exec(select(TaskDeliverable))
        deliverables = list(result.all())
        print(f"scanning {len(deliverables)} deliverables...")

        # Set aller existierenden deliverable:<id> Tags (in Python filtern — JSONB-LIKE
        # funktioniert nicht portabel via ORM, und N Deliverables sind typisch klein).
        mem_res = await db.exec(select(BoardMemory))
        linked_ids: set[str] = set()
        for m in mem_res.all():
            for t in (m.tags or []):
                if isinstance(t, str) and t.startswith("deliverable:"):
                    linked_ids.add(t.split(":", 1)[1])

        for d in deliverables:
            if str(d.id) in linked_ids:
                skipped += 1
                continue

            task = await db.get(Task, d.task_id)
            if not task:
                missing_task += 1
                continue

            agent = await db.get(Agent, d.agent_id)
            if not agent:
                missing_agent += 1
                continue

            parts: list[str] = []
            if d.description:
                parts.append(d.description)
            if d.content:
                parts.append(d.content)
            elif d.path:
                parts.append(f"Datei: `{d.path}`")
            body = "\n\n".join(parts).strip() or (d.title or "")

            tags = list(d.tags or [])
            tags.extend([f"task:{d.task_id}", f"deliverable:{d.id}", "backfilled"])

            entry = BoardMemory(
                board_id=task.board_id,
                agent_id=d.agent_id,
                memory_type=_TYPE_TO_MEMORY_TYPE.get(d.deliverable_type, "reference"),
                title=d.title or "Untitled deliverable",
                content=body[:20000],
                tags=tags,
                source=agent.name,
                auto_generated=True,
            )
            db.add(entry)
            created += 1

        await db.commit()

    print(f"  created:       {created}")
    print(f"  skipped:       {skipped} (already linked)")
    print(f"  missing_task:  {missing_task}")
    print(f"  missing_agent: {missing_agent}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
