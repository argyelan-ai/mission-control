"""Rückweg für die Slot-Runtimes (ADR-078).

Was das Skript macht
--------------------
1. Es hängt jeden Agenten, der an einer Slot-Zeile hängt, wieder an eine
   Rezept-Zeile SEINER Box zurück — bevorzugt an die eingeschaltete Instanz,
   die dort gerade läuft; sonst an die, an der er vorher hing (aus dem
   Aktivitäts-Ereignis ``agent.slot_rebound``, das ``ensure_slot_runtimes``
   je Agent schreibt).
2. Danach löscht es die Slot-Zeilen.

Warum ein Skript und keine Down-Migration: das Umhängen ist ein Update auf
``agents.runtime_id``. Eine Alembic-Down-Revision kennt die alte ID nicht mehr
— deshalb steht sie im Ereignis, und deshalb ist der Rückweg ein Befehl statt
Handarbeit.

Benutzung (im Backend-Container):

    docker compose exec backend python -m scripts.slot_rollback --dry-run
    docker compose exec backend python -m scripts.slot_rollback
    docker compose exec backend python -m scripts.slot_rollback --keep-rows

``--dry-run`` schreibt nichts und zeigt nur, was passieren würde.
``--keep-rows`` hängt die Agenten zurück, lässt die Slot-Zeilen aber stehen
(nützlich, wenn nur die Bindung zurück soll).

Nach dem Lauf müssen die betroffenen Container ihr Modell neu holen — das
Skript setzt dafür ``pending_runtime_sync``, den der Laufzeit-Wächter im
nächsten Takt abarbeitet.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.activity import ActivityEvent
from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.slot_runtimes import COMMAND_DRIVEN_TYPES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mc.slot_rollback")


async def _previous_runtime_slug(session: AsyncSession, agent: Agent) -> str | None:
    """Die Zeile, an der dieser Agent vor dem Umhängen hing — aus dem Ereignis."""
    rows = (
        await session.exec(
            select(ActivityEvent)
            .where(
                ActivityEvent.event_type == "agent.slot_rebound",
                ActivityEvent.agent_id == agent.id,
            )
            .order_by(ActivityEvent.created_at.desc())  # type: ignore[union-attr]
        )
    ).all()
    for row in rows:
        detail = row.detail if isinstance(row.detail, dict) else {}
        slug = detail.get("previous_runtime_slug")
        if slug:
            return str(slug)
    return None


async def rollback(*, dry_run: bool, keep_rows: bool) -> int:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        slots = list(
            (
                await session.exec(select(Runtime).where(Runtime.is_slot == True))  # noqa: E712
            ).all()
        )
        if not slots:
            logger.info("Keine Slot-Zeilen gefunden — nichts zu tun.")
            return 0

        runtimes = list((await session.exec(select(Runtime))).all())
        moved = 0
        for slot in slots:
            candidates = [
                rt
                for rt in runtimes
                if not rt.is_slot
                and rt.host_id == slot.host_id
                and rt.runtime_type in COMMAND_DRIVEN_TYPES
            ]
            by_slug = {rt.slug: rt for rt in candidates}
            active = next((rt for rt in candidates if rt.enabled), None)

            agents = list(
                (await session.exec(select(Agent).where(Agent.runtime_id == slot.id))).all()
            )
            for agent in agents:
                previous_slug = await _previous_runtime_slug(session, agent)
                target = by_slug.get(previous_slug or "") or active
                if target is None:
                    logger.warning(
                        "%s: keine Rezept-Zeile auf der Box %s gefunden — Bindung "
                        "bleibt unverändert, bitte von Hand setzen",
                        agent.name, slot.slug,
                    )
                    continue
                logger.info(
                    "%s: %s → %s%s",
                    agent.name, slot.slug, target.slug, " (Probelauf)" if dry_run else "",
                )
                if dry_run:
                    moved += 1
                    continue
                agent.runtime_id = target.id
                if target.model_identifier:
                    agent.model = target.model_identifier
                agent.pending_runtime_sync = True
                session.add(agent)
                moved += 1
            if not dry_run:
                await session.commit()

        if not keep_rows:
            for slot in slots:
                logger.info(
                    "Slot-Zeile %s wird gelöscht%s",
                    slot.slug, " (Probelauf)" if dry_run else "",
                )
                if not dry_run:
                    await session.delete(slot)
            if not dry_run:
                await session.commit()

        logger.info(
            "Fertig: %s Agenten zurückgehängt, %s Slot-Zeilen%s.",
            moved, len(slots), " behalten" if keep_rows else " gelöscht",
        )
        return moved


def main() -> int:
    parser = argparse.ArgumentParser(description="Slot-Runtimes zurückbauen (ADR-078)")
    parser.add_argument("--dry-run", action="store_true", help="nur zeigen, nichts schreiben")
    parser.add_argument(
        "--keep-rows",
        action="store_true",
        help="Agenten zurückhängen, Slot-Zeilen aber stehen lassen",
    )
    args = parser.parse_args()
    asyncio.run(rollback(dry_run=args.dry_run, keep_rows=args.keep_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
