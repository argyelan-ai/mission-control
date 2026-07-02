"""
Free Code Agent erstellen — Free Code CLI als Agent in Mission Control.

Aufruf: docker compose exec backend python3 scripts/create_free_code_agent.py

Der Free Code Agent arbeitet direkt im Filesystem via Free Code CLI,
nicht ueber den OpenClaw Gateway.
"""

from pathlib import Path
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from app.database import engine
    from sqlmodel.ext.asyncio.session import AsyncSession
    from sqlmodel import select
    from app.models.agent import Agent
    from app.models.board import Board

    async with AsyncSession(engine) as session:
        # Pruefen ob FreeCode schon existiert
        result = await session.exec(
            select(Agent).where(Agent.name == "FreeCode")
        )
        existing = result.first()
        if existing:
            print(f"FreeCode Agent existiert bereits (ID: {existing.id})")
            print(f"  Role: {existing.role}")
            print(f"  Workspace: {existing.workspace_path}")
            print(f"  Runtime: {existing.agent_runtime}")
            return

        # MC Dev Board finden
        board_result = await session.exec(
            select(Board).where(Board.slug == "mc-dev")
        )
        board = board_result.first()
        if not board:
            print("MC Dev Board (slug: mc-dev) nicht gefunden!")
            sys.exit(1)

        # FreeCode Agent erstellen
        agent = Agent(
            name="FreeCode",
            emoji="🤖",
            role="developer",
            model="minimax-m2.7",
            board_id=board.id,
            agent_runtime="free-code",
            workspace_path=str(Path.home() / "Workspace"),
            provision_status="provisioned",  # Braucht kein Gateway-Provisioning
            is_board_lead=False,
            total_tasks_completed=0,
            scopes=["tasks:read", "tasks:write", "knowledge:read", "knowledge:write",
                    "memory:read", "memory:write", "chat:write", "heartbeat"],
        )

        session.add(agent)
        await session.commit()
        await session.refresh(agent)

        print(f"FreeCode Agent erstellt!")
        print(f"  ID: {agent.id}")
        print(f"  Board: {board.name} ({board.id})")
        print(f"  Role: {agent.role}")
        print(f"  Model: {agent.model}")
        print(f"  Runtime: {agent.agent_runtime}")
        print(f"  Workspace: {agent.workspace_path}")
        print()
        print("Naechste Schritte:")
        print("  1. Token generieren: POST /api/v1/agents/{id}/reset-token")
        print("  2. Discord Channel zuweisen (via setup_discord_channels.py)")
        print("  3. Tasks zuweisen — Free Code wird automatisch gestartet")


if __name__ == "__main__":
    asyncio.run(main())
