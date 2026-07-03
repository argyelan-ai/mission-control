"""
Discord server setup — creates categories + channels and stores IDs in Redis.

Usage: docker compose exec backend python3 scripts/setup_discord_channels.py

Prerequisite: DISCORD_BOT_TOKEN in .env + discord_config row with guild_id in DB
(Phase 30 — the gateway table was replaced by discord_config).
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

# Channel structure: (category_name, [(channel_name, redis_purpose)])
CHANNEL_STRUCTURE = [
    ("MISSION CONTROL", [
        ("mc-alerts", "alerts"),
        ("mc-reviews", "reviews"),
    ]),
    ("DAILY", [
        ("morning-briefing", "briefing"),
        ("ai-tech-digest", "ideas"),
    ]),
    ("AGENTS", [
        ("agent-henry", None),
        ("agent-cody", None),
        ("agent-neo", None),
        ("agent-rex", None),
        ("agent-deployer", None),
        ("agent-acp", None),
    ]),
    ("SYSTEM", [
        ("deploy-log", "deploy"),
        ("github-updates", "github"),
    ]),
]

# Agent name → channel name mapping (for discord_channel_id update)
AGENT_CHANNEL_MAP = {
    "henry": "agent-henry",
    "cody": "agent-cody",
    "neo": "agent-neo",
    "rex": "agent-rex",
    "deployer": "agent-deployer",
    "acp": "agent-acp",
}


class DiscordAPI:
    def __init__(self, bot_token: str):
        self.client = httpx.AsyncClient(
            timeout=15.0,
            headers={"Authorization": f"Bot {bot_token}"},
            base_url="https://discord.com/api/v10",
        )

    async def create_category(self, guild_id: str, name: str) -> dict:
        """Create a category (type=4)."""
        resp = await self.client.post(
            f"/guilds/{guild_id}/channels",
            json={"name": name, "type": 4},
        )
        resp.raise_for_status()
        return resp.json()

    async def create_text_channel(self, guild_id: str, name: str, parent_id: str) -> dict:
        """Create a text channel under a category (type=0)."""
        resp = await self.client.post(
            f"/guilds/{guild_id}/channels",
            json={"name": name, "type": 0, "parent_id": parent_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_channels(self, guild_id: str) -> list[dict]:
        """List all channels of a guild."""
        resp = await self.client.get(f"/guilds/{guild_id}/channels")
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        await self.client.aclose()


async def main():
    from app.config import settings

    bot_token = settings.discord_bot_token
    if not bot_token:
        print("DISCORD_BOT_TOKEN nicht gesetzt. Bitte in .env eintragen.")
        sys.exit(1)

    # Read guild ID from DB or environment
    guild_id = os.environ.get("DISCORD_GUILD_ID")

    if not guild_id:
        # Read from DB (Phase 30: discord_config instead of gateways)
        from app.database import engine
        from sqlmodel.ext.asyncio.session import AsyncSession
        from sqlmodel import select
        from app.models.discord_config import DiscordConfig

        async with AsyncSession(engine) as session:
            result = await session.exec(select(DiscordConfig).limit(1))
            cfg = result.first()
            if cfg and cfg.guild_id:
                guild_id = cfg.guild_id

    if not guild_id:
        print("Keine Guild ID gefunden. Setze DISCORD_GUILD_ID oder PATCH /api/v1/discord/config mit guild_id.")
        sys.exit(1)

    print(f"Guild ID: {guild_id}")
    print(f"Bot Token: {bot_token[:20]}...")
    print()

    api = DiscordAPI(bot_token)

    try:
        # Check existing channels (split by type)
        existing = await api.list_channels(guild_id)
        existing_categories = {ch["name"]: ch for ch in existing if ch["type"] == 4}
        existing_text = {ch["name"]: ch for ch in existing if ch["type"] == 0}
        existing_names = {ch["name"]: ch for ch in existing}

        # Redis connection
        import redis.asyncio as aioredis
        redis = aioredis.from_url(settings.redis_url, decode_responses=True)

        channel_ids: dict[str, str] = {}  # name → id
        created = 0
        skipped = 0

        for category_name, channels in CHANNEL_STRUCTURE:
            # Create category or reuse (only match type=4)
            cat_slug = category_name.lower().replace(" ", "-")
            cat_name_lower = category_name.lower()
            matched_cat = existing_categories.get(cat_slug) or existing_categories.get(cat_name_lower) or existing_categories.get(category_name)
            if matched_cat:
                category = matched_cat
                print(f"  Category '{category_name}' existiert bereits (ID: {category['id']})")
            else:
                category = await api.create_category(guild_id, category_name)
                print(f"+ Category '{category_name}' erstellt (ID: {category['id']})")
                created += 1

            category_id = category["id"]

            for channel_name, redis_purpose in channels:
                if channel_name in existing_text:
                    ch = existing_text[channel_name]
                    print(f"  #{channel_name} existiert bereits (ID: {ch['id']})")
                    skipped += 1
                else:
                    ch = await api.create_text_channel(guild_id, channel_name, category_id)
                    print(f"+ #{channel_name} erstellt (ID: {ch['id']})")
                    created += 1

                channel_ids[channel_name] = ch["id"]

                # Set Redis key for purpose routing
                if redis_purpose:
                    await redis.set(f"mc:discord:channel:{redis_purpose}", ch["id"])
                    print(f"  -> Redis: mc:discord:channel:{redis_purpose} = {ch['id']}")

        # Update agent discord_channel_id via raw SQL (asyncpg-compatible)
        print()
        print("Agent-Channels in DB updaten...")
        import asyncpg
        db_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(db_url)
        try:
            agents = await conn.fetch("SELECT id, name FROM agents")
            for agent in agents:
                agent_key = agent["name"].lower().strip()
                expected_channel = AGENT_CHANNEL_MAP.get(agent_key)
                if expected_channel and expected_channel in channel_ids:
                    await conn.execute(
                        "UPDATE agents SET discord_channel_id = $1, discord_channel_name = $2 WHERE id = $3",
                        channel_ids[expected_channel], expected_channel, agent["id"],
                    )
                    print(f"  {agent['name']} -> #{expected_channel} ({channel_ids[expected_channel]})")
        finally:
            await conn.close()

        await redis.aclose()
        print()
        print(f"Fertig: {created} erstellt, {skipped} uebersprungen.")

    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
