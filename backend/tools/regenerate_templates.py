#!/usr/bin/env python3
"""
Einmaliges Script: Regeneriert soul_md und tools_md fuer alle provisionierten Agents
aus den aktuellen Templates (SOUL.md.j2 + tools_md_builder).

Ausfuehren im Backend-Container:
  docker compose exec backend python tools/regenerate_templates.py
"""
import asyncio
import sys
import os

# App-Pfad setzen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import select
from app.database import engine, AsyncSession
from app.models.agent import Agent
from app.services.template_renderer import render_all_agent_files
from app.services.provisioning import extract_token_from_tools_md
from app.services.tools_md_builder import generate_tools_md


async def main():
    async with AsyncSession(engine) as session:
        result = await session.exec(
            select(Agent).where(Agent.provision_status == "provisioned")
        )
        agents = list(result.all())
        print(f"Gefunden: {len(agents)} provisionierte Agents\n")

        # Alle Agents auf dem Board sammeln (fuer USER.md Context)
        all_agents = agents

        for agent in agents:
            print(f"--- {agent.emoji} {agent.name} ---")

            # 1) SOUL.md aus Template rendern
            try:
                board_id_str = str(agent.board_id) if agent.board_id else None
                board_agents = [a for a in all_agents if a.board_id == agent.board_id]
                rendered = render_all_agent_files(
                    agent,
                    board_id=board_id_str,
                    agents_on_board=board_agents,
                )
                if rendered.get("SOUL.md"):
                    agent.soul_md = rendered["SOUL.md"]
                    print(f"  SOUL.md: regeneriert ({len(agent.soul_md)} chars)")
                else:
                    print(f"  SOUL.md: Template lieferte nichts — uebersprungen")
            except Exception as e:
                print(f"  SOUL.md: FEHLER — {e}")

            # 2) TOOLS.md mit bestehendem Token regenerieren
            if agent.tools_md:
                existing_token = extract_token_from_tools_md(agent.tools_md)
                if existing_token:
                    board_id_str = str(agent.board_id) if agent.board_id else None
                    agent.tools_md = generate_tools_md(
                        agent.name,
                        agent.emoji or "🤖",
                        existing_token,
                        board_id_str,
                        is_board_lead=agent.is_board_lead or False,
                        scopes=list(agent.scopes) if agent.scopes else [],
                    )
                    print(f"  TOOLS.md: regeneriert ({len(agent.tools_md)} chars)")
                else:
                    print(f"  TOOLS.md: kein Token gefunden — uebersprungen")
            else:
                print(f"  TOOLS.md: nicht vorhanden — uebersprungen")

            session.add(agent)

        await session.commit()
        print(f"\n✓ {len(agents)} Agents in DB aktualisiert.")
        print("Naechster Schritt: sync-config fuer jeden Agent aufrufen.")


if __name__ == "__main__":
    asyncio.run(main())
