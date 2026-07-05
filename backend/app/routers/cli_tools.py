"""CLI Tools API (CLI-Tool-Updates, Task 7).

Surfaces the version-check cache (Task 3, ``services/cli_update_check.py``)
and update orchestration (Task 6, ``services/cli_update_runner.py``) for the
frontend cockpit:

- ``GET  /api/v1/cli-tools``              — per-tool status + affected agents
- ``POST /api/v1/cli-tools/check``        — force an immediate re-check
- ``GET  /api/v1/cli-tools/update-status`` — current update progress (or idle)
- ``POST /api/v1/cli-tools/{tool}/update`` — start an update

Router ordering: the static ``/check`` and ``/update-status`` paths are
declared before ``/{tool}/update`` so FastAPI doesn't try to parse "check" or
"update-status" as a ``{tool}`` path segment.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role, require_user
from app.database import get_session
from app.models.agent import Agent
from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis
from app.services import cli_update_check
from app.services.agent_runtime_switch import is_agent_busy
from app.services.cli_update_runner import UnknownTool, UpdateAlreadyRunning, start_update
from app.services.cli_versions import TOOLS
from app.services.harness_compat import derive_harness

router = APIRouter(prefix="/api/v1/cli-tools", tags=["cli-tools"])


async def _effective_harness(session: AsyncSession, agent: Agent) -> str | None:
    """agent.harness if set (ADR-056), else derived from its runtime (legacy)."""
    if agent.harness:
        return agent.harness
    if agent.runtime_id is None:
        return None
    runtime = await session.get(Runtime, agent.runtime_id)
    return derive_harness(runtime)


async def _agents_affected(session: AsyncSession, tool: str) -> list[dict]:
    """cli-bridge agents whose effective harness matches this tool. Host
    agents (Boss/Hermes/Jarvis) never run these images and are excluded."""
    result = await session.exec(
        select(Agent).where(Agent.agent_runtime == "cli-bridge")
    )
    affected = []
    for agent in result.all():
        if await _effective_harness(session, agent) == tool:
            affected.append(
                {"id": str(agent.id), "name": agent.name, "busy": is_agent_busy(agent)}
            )
    return affected


async def _load_versions_cache(session: AsyncSession, redis) -> dict:
    """Redis cache (``mc:cli:versions``); an empty/corrupt cache triggers an
    on-demand check so the cockpit never shows a blank first load."""
    raw = await redis.get(RedisKeys.cli_versions_cache())
    if raw:
        try:
            cache = json.loads(raw)
        except json.JSONDecodeError:
            cache = {}
        if cache:
            return cache
    return await cli_update_check.run_check_once(session)


async def _build_state_for(redis, tool: str) -> str | None:
    """The in-flight update's phase if it's currently running for this tool,
    else None (idle, or another tool is mid-update)."""
    raw = await redis.get(RedisKeys.cli_update_progress())
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if data.get("tool") != tool:
        return None
    return data.get("phase")


async def _enriched_tools(session: AsyncSession, redis, cache: dict) -> list[dict]:
    """The cockpit list shape shared by GET / and POST /check — one response
    contract, so the frontend client can treat both identically."""
    tools = []
    for tool, config in TOOLS.items():
        entry = cache.get(tool, {})
        tools.append(
            {
                "tool": tool,
                "image": config["image"],
                "installed": entry.get("installed"),
                "target": entry.get("target"),
                "latest": entry.get("latest"),
                "update_available": entry.get("update_available", False),
                "checked_at": entry.get("checked_at"),
                "agents_affected": await _agents_affected(session, tool),
                "build_state": await _build_state_for(redis, tool),
            }
        )
    return tools


@router.get("")
async def list_cli_tools(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Per-tool status for the cockpit: versions, whether an update is
    available, which agents run it, and the current build phase (if any).

    NOTE: a cold/corrupt cache triggers run_check_once inline — up to
    ~25s/tool worst case (HTTP 15s + docker inspect 10s, sequential). The
    periodic checker keeps the cache warm, so this is a first-boot path.
    """
    redis = await get_redis()
    cache = await _load_versions_cache(session, redis)
    return {"tools": await _enriched_tools(session, redis, cache)}


@router.post("/check")
async def check_cli_tools(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.OPERATOR)),
):
    """Force an immediate re-check of all tools (the "Jetzt prüfen" button).

    Returns the same enriched list shape as GET / so the frontend can reuse
    one response type for both calls.
    """
    redis = await get_redis()
    cache = await cli_update_check.run_check_once(session)
    return {"tools": await _enriched_tools(session, redis, cache)}


@router.get("/update-status")
async def cli_update_status(current_user=Depends(require_user)):
    """Current update progress, or ``{"phase": "idle"}`` if none is running."""
    redis = await get_redis()
    raw = await redis.get(RedisKeys.cli_update_progress())
    if not raw:
        return {"phase": "idle"}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"phase": "idle"}


@router.post("/{tool}/update", status_code=202)
async def update_cli_tool(
    tool: str,
    current_user=Depends(require_role(Role.OPERATOR)),
):
    """Starts a background update for ``tool`` (manifest → build → recreate)."""
    try:
        await start_update(tool)
    except UnknownTool:
        raise HTTPException(status_code=404, detail=f"Unbekanntes CLI-Tool: '{tool}'")
    except UpdateAlreadyRunning:
        raise HTTPException(
            status_code=409, detail="Es läuft bereits ein CLI-Tool-Update."
        )
    return {"status": "started"}
