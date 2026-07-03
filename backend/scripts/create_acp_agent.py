"""
Create ACP agent — Claude Code CLI as an agent in Mission Control.

Usage: docker compose exec backend python3 scripts/create_acp_agent.py

The ACP agent works directly in the filesystem via Claude Code CLI,
not through the OpenClaw Gateway.
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
        # Check whether ACP already exists
        result = await session.exec(
            select(Agent).where(Agent.name == "ACP")
        )
        existing = result.first()
        if existing:
            print(f"ACP Agent existiert bereits (ID: {existing.id})")
            print(f"  Role: {existing.role}")
            print(f"  Workspace: {existing.workspace_path}")
            print(f"  Runtime: {existing.agent_runtime}")
            return

        # Find MC Dev board
        board_result = await session.exec(
            select(Board).where(Board.slug == "mc-dev")
        )
        board = board_result.first()
        if not board:
            print("MC Dev Board (slug: mc-dev) nicht gefunden!")
            sys.exit(1)

        # Create ACP agent
        agent = Agent(
            name="ACP",
            emoji="🧠",
            role="developer",
            model="claude-opus-4-6",
            board_id=board.id,
            agent_runtime="claude-code",
            workspace_path=str(Path.home() / "Workspace"),
            provision_status="provisioned",  # Doesn't need gateway provisioning
            is_board_lead=False,
            total_tasks_completed=0,
            scopes=["tasks:read", "tasks:write", "knowledge:read", "knowledge:write",
                    "memory:read", "memory:write", "chat:write", "heartbeat"],
        )

        session.add(agent)
        await session.commit()
        await session.refresh(agent)

        print(f"ACP Agent erstellt!")
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
        print("  3. Tasks zuweisen und via Claude CLI arbeiten")


if __name__ == "__main__":
    asyncio.run(main())
