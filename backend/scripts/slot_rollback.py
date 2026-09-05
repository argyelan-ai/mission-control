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

**Alles oder nichts.** Findet sich für auch nur EINEN Agenten kein Ziel, bricht
das Skript ab, BEVOR es irgendetwas schreibt (Exit-Code 2). Und eine Slot-Zeile
wird nur gelöscht, wenn nachweislich kein Agent mehr an ihr hängt. Der frühere
Ablauf konnte auf halber Strecke stehenbleiben und dabei eine Zeile löschen, an
der noch ein Agent hing — eine Fremdschlüssel-Verletzung im schlechtesten
denkbaren Zwischenzustand.

**Zwei Schritte, nicht einer.** Damit der Rückbau einen Backend-Neustart
überlebt, muss zusätzlich ``SLOT_RUNTIMES_ENABLED=false`` in der ``.env``
stehen: ``main.py`` ruft beim Start ``ensure_slot_runtimes()``, das die Zeilen
sonst sofort wieder anlegt und die Agenten erneut umhängt. Das Skript erinnert
am Ende jedes Laufs daran.

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

        # ── Schritt 1: PLANEN. Nichts wird geschrieben. ──────────────────────
        # Alles-oder-nichts (Review 05.09.2026, H3): vorher wurde Agent für
        # Agent umgehängt und committet, und wenn für einen kein Ziel zu finden
        # war, blieb er an der Slot-Zeile — die trotzdem gelöscht wurde. Das
        # Ergebnis wäre eine Fremdschlüssel-Verletzung mitten in einem halb
        # ausgeführten Rückbau: der schlechteste Zustand von allen.
        plan: list[tuple[Agent, Runtime, Runtime]] = []   # (Agent, Slot, Ziel)
        homeless: list[tuple[str, str]] = []              # (Agent, Slot)
        for slot in slots:
            candidates = sorted(
                (
                    rt
                    for rt in runtimes
                    if not rt.is_slot
                    and rt.host_id == slot.host_id
                    and rt.runtime_type in COMMAND_DRIVEN_TYPES
                ),
                key=lambda rt: (rt.ui_order or 999, rt.slug),
            )
            by_slug = {rt.slug: rt for rt in candidates}
            active = next((rt for rt in candidates if rt.enabled), None)

            agents = list(
                (await session.exec(select(Agent).where(Agent.runtime_id == slot.id))).all()
            )
            for agent in agents:
                previous_slug = await _previous_runtime_slug(session, agent)
                target = by_slug.get(previous_slug or "") or active
                if target is None:
                    homeless.append((agent.name, slot.slug))
                    continue
                plan.append((agent, slot, target))

        if homeless:
            for name, slot_slug in homeless:
                logger.error(
                    "%s hängt an der Slot-Zeile %s, und auf dieser Box gibt es "
                    "keine Rezept-Zeile, an die er zurück könnte.",
                    name, slot_slug,
                )
            logger.error(
                "Abbruch — es wurde NICHTS geändert. Erst für diese Agenten ein "
                "Ziel schaffen (Rezept auf der Box anlegen oder die Bindung von "
                "Hand setzen), dann erneut laufen lassen."
            )
            return -1

        for agent, slot, target in plan:
            logger.info(
                "%s: %s → %s%s",
                agent.name, slot.slug, target.slug, " (Probelauf)" if dry_run else "",
            )
        for slot in slots:
            logger.info(
                "Slot-Zeile %s wird %s%s",
                slot.slug,
                "behalten (--keep-rows)" if keep_rows else "gelöscht",
                " (Probelauf)" if dry_run else "",
            )

        if dry_run:
            logger.info(
                "Probelauf beendet — %s Agenten würden zurückgehängt, "
                "%s Slot-Zeilen %s. Es wurde nichts geschrieben.",
                len(plan), len(slots), "bleiben" if keep_rows else "würden gelöscht",
            )
            _remind_about_the_switch()
            return len(plan)

        # ── Schritt 2: SCHREIBEN. Erst umhängen, dann löschen. ───────────────
        for agent, _slot, target in plan:
            agent.runtime_id = target.id
            if target.model_identifier:
                agent.model = target.model_identifier
            agent.pending_runtime_sync = True
            session.add(agent)
        await session.commit()

        if not keep_rows:
            for slot in slots:
                # Gegenprobe VOR dem Löschen: hängt wirklich niemand mehr dran?
                # Ein Agent, der zwischen Plan und Schreiben dazukam, darf nicht
                # in eine Fremdschlüssel-Verletzung laufen.
                still = (
                    await session.exec(select(Agent).where(Agent.runtime_id == slot.id))
                ).all()
                if still:
                    logger.error(
                        "Slot-Zeile %s wird NICHT gelöscht — es hängen noch %s "
                        "Agenten daran (%s). Die Umhängungen sind gespeichert; "
                        "das Skript kann nach dem Aufräumen erneut laufen.",
                        slot.slug, len(still), ", ".join(a.name for a in still),
                    )
                    continue
                await session.delete(slot)
            await session.commit()

        logger.info(
            "Fertig: %s Agenten zurückgehängt, %s Slot-Zeilen%s.",
            len(plan), len(slots), " behalten" if keep_rows else " gelöscht",
        )
        _remind_about_the_switch()
        return len(plan)


def _remind_about_the_switch() -> None:
    """Ohne diesen Hinweis hält der Rückbau nur bis zum nächsten Deploy.

    ``main.py`` ruft beim Start ``ensure_slot_runtimes()`` — die Zeilen wären
    also sofort wieder da und die Agenten wieder umgehängt (Review, H2).
    """
    logger.warning(
        "WICHTIG: Damit der Rückbau einen Backend-Neustart überlebt, muss "
        "SLOT_RUNTIMES_ENABLED=false in der .env stehen — sonst legt der "
        "nächste Start die Slot-Zeilen wieder an und hängt die Agenten erneut um."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Slot-Runtimes zurückbauen (ADR-078)")
    parser.add_argument("--dry-run", action="store_true", help="nur zeigen, nichts schreiben")
    parser.add_argument(
        "--keep-rows",
        action="store_true",
        help="Agenten zurückhängen, Slot-Zeilen aber stehen lassen",
    )
    args = parser.parse_args()
    result = asyncio.run(rollback(dry_run=args.dry_run, keep_rows=args.keep_rows))
    # Exit-Code ≠ 0, wenn abgebrochen wurde — damit ein Skript drumherum das
    # merkt und nicht fröhlich weitermacht.
    return 2 if result < 0 else 0


if __name__ == "__main__":
    sys.exit(main())
