"""One-shot data fix: Agent.role freetext -> AgentRole enum value.

Vorfall 94fda9f9 (2026-09-11): Review-Karten landeten beim Board-Lead statt
bei Rex, weil find_agent_by_role() strikt Agent.role == "reviewer" matcht,
Rex' role-Spalte aber eine Rollenbeschreibung als Freitext haelt
("Review & Security Expert — Code-Reviews, PRs, Sicherheit, Qualitaets-
sicherung"). Die Code-Seite ist in work_context.find_reviewer() /
dispatch.find_agent_by_role() gefixt (fallback_to_lead=False,
Namens-Fallback ohne role-IS-NULL-Filter). Diese Datenkorrektur schliesst
die eigentliche Ursache: Rex bekommt den Enum-Wert, damit die
role-basierte Suche (der Primaerpfad) ihn direkt findet statt ueber den
Namens-Fallback.

Gleiche Pruefung fuer Tester/Deployer/Researcher, weil laut Vorfall-Bericht
dieselbe Mechanik dort als Naechstes bricht (nur Grok hat einen
Enum-Rollenwert, Hermes hat None, alle anderen inkl. Rex Freitext).

Scope (bewusst eng): NUR die vier namentlich genannten Agenten. Andere
Freitext-Rollen (Davinci, Downloader, FreeCode, Installer, Kimi,
Shakespeare, Sparky) bilden keine 1:1 AgentRole-Entsprechung und sind
ausdruecklich NICHT Teil dieses Vorfalls/dieser Karte — Aendern waere ein
Rollensystem-Umbau, der laut Task explizit out of scope ist.

Ausfuehrung (braucht DB-Zugriff, den ein Agent-Token nicht hat — Aendern
fremder Agent-Rollen ist eine Operator-Aktion, siehe Team Charter Punkt 6):

    docker compose exec backend python -m scripts.fix_legacy_agent_roles --dry-run
    docker compose exec backend python -m scripts.fix_legacy_agent_roles

Idempotent: ein Agent, dessen role-Spalte bereits den Ziel-Enum-Wert
traegt, wird uebersprungen (kein no-op Write, kein doppeltes Logging).
Matched ausschliesslich per Agent-NAME (nicht per aktuellem role-Freitext,
der sich jederzeit aendern kann) — bewusst eine feste, auditierbare Liste
statt Heuristik.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.agent import Agent
from app.scopes import AgentRole
from app.utils import utcnow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fix_legacy_agent_roles")

# Agent-Name -> Ziel-Enum-Wert. Namentlich aus der Vorfall-Karte 94fda9f9.
TARGET_ROLES: dict[str, AgentRole] = {
    "Rex": AgentRole.REVIEWER,
    "Tester": AgentRole.TESTER,
    "Deployer": AgentRole.DEPLOYER,
    "Researcher": AgentRole.RESEARCHER,
}


async def _run(dry_run: bool) -> int:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(select(Agent).where(Agent.name.in_(TARGET_ROLES.keys())))
        agents = {a.name: a for a in result.all()}

        print("## Vorher")
        for name in TARGET_ROLES:
            a = agents.get(name)
            print(f"- {name}: {a.role!r}" if a else f"- {name}: NICHT GEFUNDEN")

        changed = 0
        for name, target in TARGET_ROLES.items():
            agent = agents.get(name)
            if not agent:
                logger.warning("Agent %r nicht auf dem Board gefunden — uebersprungen", name)
                continue
            if agent.role == target.value:
                logger.info("%s: role bereits %r — idempotent, kein Write", name, target.value)
                continue
            old_role = agent.role
            if not dry_run:
                agent.role = target.value
                agent.updated_at = utcnow()
                session.add(agent)
            logger.info(
                "%s: role %r -> %r%s", name, old_role, target.value,
                " (dry-run, nicht geschrieben)" if dry_run else "",
            )
            changed += 1

        if not dry_run and changed:
            await session.commit()
            for a in agents.values():
                await session.refresh(a)

        print("\n## Nachher" + (" (dry-run — Werte oben zeigen was geschrieben WUERDE)" if dry_run else ""))
        for name in TARGET_ROLES:
            a = agents.get(name)
            if not a:
                print(f"- {name}: NICHT GEFUNDEN")
            elif dry_run:
                print(f"- {name}: {a.role!r} (unveraendert, dry-run)")
            else:
                print(f"- {name}: {a.role!r}")

        print(f"\n{changed} Agent(en) {'wuerden geaendert' if dry_run else 'geaendert'}.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts schreiben")
    args = parser.parse_args()
    return asyncio.run(_run(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
