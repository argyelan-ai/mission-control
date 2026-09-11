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

    docker compose exec backend python -m scripts.fix_legacy_agent_roles --board-id <id> --dry-run
    docker compose exec backend python -m scripts.fix_legacy_agent_roles --board-id <id>

Idempotent: ein Agent, dessen role-Spalte bereits den Ziel-Enum-Wert
traegt, wird uebersprungen (kein no-op Write, kein doppeltes Logging).
Matched ausschliesslich per Agent-NAME (nicht per aktuellem role-Freitext,
der sich jederzeit aendern kann) — bewusst eine feste, auditierbare Liste
statt Heuristik.

Was passiert mit dem heutigen Freitext (Boss-Review, task 94fda9f9,
seq 3)? Er wird bewusst NICHT in eine andere Spalte verschoben — es gibt
keine passende: `identity_md` ist bereits ein vollstaendiges, pro Rolle
templatiertes Dokument (IDENTITY.md), `soul_persona_md` ist ein fest
seedetes 80-120-Token-Charakter-Snippet fuer einen anderen, spezifischen
9-Agenten-Satz, `dispatch_config` ist behaviorales JSON, kein Label-Feld
— jedes davon fuer einen 1-zeiligen Rollentitel zweckentfremdet und mit
bestehendem Inhalt kollidierend. Der Freitext wird stattdessen bewusst
verworfen, aber vollstaendig auditierbar: jeder alte Wert erscheint
unveraendert in der Vorher-Spalte dieses Skripts (--dry-run wie live)
UND wird vor der Ausfuehrung als Kommentar auf der Vorfall-Karte
archiviert.

Das --dry-run (und das normale) Board-Reporting zeigt IMMER ALLE Agenten
des Boards, nicht nur die vier Ziel-Agenten (Boss-Review, task 94fda9f9,
seq 3: "Ich will vorher sehen, wen es sonst noch trifft") — nur die vier
namentlich gelisteten werden geschrieben, der Rest ist reine Sichtbarkeit.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

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


async def _run(board_id: uuid.UUID, dry_run: bool) -> int:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(
            select(Agent).where(Agent.board_id == board_id).order_by(Agent.name)
        )
        board_agents = list(result.all())
        by_name = {a.name: a for a in board_agents}

        # Full board report FIRST — Boss-Review (task 94fda9f9, seq 3):
        # "Ich will vorher sehen, wen es sonst noch trifft", not just the
        # four target agents. Only the four named ones ever get written.
        print(f"## Vorher — alle {len(board_agents)} Agenten auf Board {board_id}")
        for a in board_agents:
            marker = " <- wird geschrieben" if a.name in TARGET_ROLES else ""
            print(f"- {a.name}: {a.role!r}{marker}")

        missing = [name for name in TARGET_ROLES if name not in by_name]
        for name in missing:
            logger.warning("Ziel-Agent %r nicht auf Board %s gefunden — uebersprungen", name, board_id)

        changed = 0
        for name, target in TARGET_ROLES.items():
            agent = by_name.get(name)
            if not agent:
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
                "%s: role %r -> %r%s (alter Freitext bewusst verworfen, siehe Docstring)",
                name, old_role, target.value,
                " (dry-run, nicht geschrieben)" if dry_run else "",
            )
            changed += 1

        if not dry_run and changed:
            await session.commit()
            for a in board_agents:
                await session.refresh(a)

        print(
            "\n## Nachher — alle Agenten"
            + (" (dry-run — Werte zeigen den HEUTIGEN Stand, nichts wurde geschrieben)" if dry_run else "")
        )
        for a in board_agents:
            if a.name in TARGET_ROLES and dry_run:
                target = TARGET_ROLES[a.name].value
                would = target if a.role != target else a.role
                print(f"- {a.name}: {a.role!r} (waere: {would!r})")
            else:
                print(f"- {a.name}: {a.role!r}")

        print(f"\n{changed} Agent(en) {'wuerden geaendert' if dry_run else 'geaendert'} (von {len(TARGET_ROLES)} Ziel-Agenten).")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--board-id", required=True, type=uuid.UUID, help="Board-UUID (z.B. 7bd0be90-c45a-4a15-9037-ebb72f15ba09)")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts schreiben")
    args = parser.parse_args()
    return asyncio.run(_run(board_id=args.board_id, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
