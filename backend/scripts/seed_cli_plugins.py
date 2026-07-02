#!/usr/bin/env python3
"""Seed cli_plugins fuer alle CLI-Bridge Agents aus deren aktueller settings.json."""
import asyncio
import json
import os
import sys
from pathlib import Path

# In Docker, HOME_HOST points to the host home directory where .openclaw is mounted
_home = Path(os.environ.get("HOME_HOST", Path.home()))
AGENTS_DIR = _home / ".mc" / "agents"


async def main():
    sys.path.insert(0, str(Path(__file__).parent.parent))

    from sqlmodel import select
    from sqlmodel.ext.asyncio.session import AsyncSession
    from app.database import engine
    from app.models.agent import Agent

    async with AsyncSession(engine) as session:
        result = await session.exec(
            select(Agent).where(Agent.agent_runtime == "cli-bridge")
        )
        agents = result.all()

        for agent in agents:
            slug = agent.name.lower().replace(" ", "-")
            settings_file = AGENTS_DIR / slug / "settings.json"

            if not settings_file.exists():
                print(f"  SKIP {agent.name}: keine settings.json")
                continue

            try:
                data = json.loads(settings_file.read_text())
                enabled = data.get("enabledPlugins", {})
                plugin_keys = [k for k, v in enabled.items() if v]
            except (json.JSONDecodeError, OSError) as e:
                print(f"  ERROR {agent.name}: {e}")
                continue

            agent.cli_plugins = plugin_keys
            session.add(agent)
            print(f"  {agent.name}: {len(plugin_keys)} plugins")

        await session.commit()
        print(f"\nDone — {len(agents)} Agents aktualisiert")


if __name__ == "__main__":
    asyncio.run(main())
