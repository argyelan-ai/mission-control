"""Discord Router — Channel CRUD (per ADR-039 / Phase 29 + Phase 30).

Standalone router that owns all Discord-bot HTTP endpoints. Replaces the
Discord paths formerly served by `routers/gateway.py` and `routers/agents.py`.

Design (D-04 / D-05 / D-06 / D-18):
- Pure HTTP routing + permission check. All Discord HTTP calls live in
  `services/discord.py`.
- Phase 30: reads guild_id + category_id from the `discord_config` table
  (single row, application-enforced). Phase 29 stop-gap (settings.discord_*)
  superseded — only `settings.discord_bot_token` remains (it's a secret
  per ADR-033). New admin endpoints `GET /config` + `PATCH /config` expose
  the row to the UI.
- JWT auth on every endpoint via `Depends(require_user)` (same model as
  `routers/cli_plugins.py`).
- No legacy-gateway code imports (D-13 forbidden-symbol allowlist).

Phase 28 transactional discipline (D-15): all `session.commit()` calls
happen AFTER the Discord API call succeeds so 502 paths leave the DB
untouched.
"""

import logging
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.config import settings
from app.database import get_session
from app.models.agent import Agent
from app.models.discord_config import DiscordConfig
from app.services.activity import emit_event
from app.services.discord import (
    create_guild_text_channel,
    get_discord_config,
    list_guild_channels,
)
from app.utils import utcnow

logger = logging.getLogger("mc.discord")
router = APIRouter(prefix="/api/v1/discord", tags=["discord"])


# ---------------------------------------------------------------------------
# Payload models
# ---------------------------------------------------------------------------


class DiscordChannelCreate(BaseModel):
    name: str
    context: str = ""
    category_id: Optional[str] = None


class DiscordChannelRename(BaseModel):
    new_name: str


class DiscordConfigUpdate(BaseModel):
    guild_id: str | None = None
    category_id: str | None = None
    bot_configured: bool | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _bot_ready(session: AsyncSession) -> bool:
    """Bot is ready when bot_token (env, ADR-033 secret) + guild_id (DB,
    Phase 30 discord_config) are both set.
    """
    if not settings.discord_bot_token:
        return False
    cfg = await get_discord_config(session)
    return bool(cfg and cfg.guild_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/channels")
async def list_channels(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """List all Discord text channels in the configured guild."""
    cfg = await get_discord_config(session)
    if not (settings.discord_bot_token and cfg and cfg.guild_id):
        raise HTTPException(
            status_code=400, detail="Discord bot not configured"
        )
    try:
        return await list_guild_channels(cfg.guild_id)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Discord API error: {exc}"
        ) from exc


@router.post(
    "/agents/{agent_id}/channel", status_code=status.HTTP_201_CREATED
)
async def create_agent_channel(
    agent_id: uuid.UUID,
    payload: DiscordChannelCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Create a Discord channel and bind it to this agent.

    409 if the agent already has a Discord channel bound.
    400 if the Discord bot is not configured.
    502 if Discord API call fails (DB stays clean — D-15).
    """
    cfg = await get_discord_config(session)
    if not (settings.discord_bot_token and cfg and cfg.guild_id):
        raise HTTPException(
            status_code=400, detail="Discord bot not configured"
        )

    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.discord_channel_id:
        raise HTTPException(
            status_code=409, detail="Agent already has a Discord channel"
        )

    category_id = payload.category_id or cfg.category_id or None

    try:
        channel = await create_guild_text_channel(
            cfg.guild_id,
            name=payload.name,
            context=payload.context,
            category_id=category_id,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Discord channel creation failed: {exc}",
        ) from exc

    # D-15: only commit after the Discord side has succeeded.
    channel_id = str(channel.get("id") or channel.get("channel_id") or "")
    agent.discord_channel_id = channel_id
    agent.discord_channel_name = payload.name
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()

    await emit_event(
        session,
        "agent.discord_channel_created",
        f"Discord channel '{payload.name}' created for {agent.name}",
        severity="info",
        agent_id=agent.id,
        board_id=agent.board_id,
    )

    return {"channel_id": channel_id, "name": payload.name}


@router.patch("/agents/{agent_id}/channel")
async def rename_agent_channel(
    agent_id: uuid.UUID,
    payload: DiscordChannelRename,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Rename the bound Discord channel via Discord Bot API.

    404 if no channel is bound to the agent.
    502 if Discord API call fails (DB stays clean — D-15).

    TODO Phase 31: move the direct PATCH into services/discord.py as
    `rename_guild_channel()`. Kept inline here to avoid churn in the
    services layer during the sunset.
    """
    agent = await session.get(Agent, agent_id)
    if not agent or not agent.discord_channel_id:
        raise HTTPException(
            status_code=404, detail="No Discord channel bound"
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"https://discord.com/api/v10/channels/{agent.discord_channel_id}",
                json={"name": payload.new_name},
                headers={
                    "Authorization": f"Bot {settings.discord_bot_token}"
                },
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Discord rename failed: {exc}"
        ) from exc

    # D-15: commit only after the Discord side has accepted the rename.
    old_name = agent.discord_channel_name
    agent.discord_channel_name = payload.new_name
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()
    return {"old_name": old_name, "new_name": payload.new_name}


@router.delete("/agents/{agent_id}/channel")
async def remove_agent_channel(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Unbind (NOT delete) the Discord channel from this agent.

    By design we do not delete the channel on Discord's side — the operator
    keeps history. The agent simply loses its binding.

    404 if no channel is bound to the agent.
    """
    agent = await session.get(Agent, agent_id)
    if not agent or not agent.discord_channel_id:
        raise HTTPException(
            status_code=404, detail="No Discord channel bound"
        )

    agent.discord_channel_id = None
    agent.discord_channel_name = None
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()
    return {"unbound": True}


# ---------------------------------------------------------------------------
# Phase 30 — Admin: GET + PATCH /api/v1/discord/config
# ---------------------------------------------------------------------------


@router.get("/config")
async def get_config(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Return the single discord_config row JSON.

    Defensive: if Plan 30-03 migration hasn't run yet or the row was wiped,
    returns a "not configured" default rather than 404 so the UI can render
    an empty form.
    """
    cfg = await get_discord_config(session)
    if cfg is None:
        return {"guild_id": None, "category_id": None, "bot_configured": False}
    return {
        "guild_id": cfg.guild_id,
        "category_id": cfg.category_id,
        "bot_configured": cfg.bot_configured,
    }


@router.patch("/config")
async def update_config(
    payload: DiscordConfigUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Update the single discord_config row. Creates the row if it doesn't
    exist (single-row invariant is application-enforced — get_discord_config
    + LIMIT 1 + INSERT-only-when-None).

    Threat model T-30-02-03 (Tampering): concurrent PATCHes could race to
    create two rows. Acceptable risk for solo-developer MC; would need an
    advisory lock or ON CONFLICT for multi-writer scenarios.
    """
    cfg = await get_discord_config(session)
    if cfg is None:
        cfg = DiscordConfig()
        session.add(cfg)
    if payload.guild_id is not None:
        cfg.guild_id = payload.guild_id
    if payload.category_id is not None:
        cfg.category_id = payload.category_id
    if payload.bot_configured is not None:
        cfg.bot_configured = payload.bot_configured
    cfg.updated_at = utcnow()
    session.add(cfg)
    await session.commit()
    return {
        "guild_id": cfg.guild_id,
        "category_id": cfg.category_id,
        "bot_configured": cfg.bot_configured,
    }
