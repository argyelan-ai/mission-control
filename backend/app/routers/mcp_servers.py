"""Admin CRUD for MCP servers + per-agent assignment (user-auth).

Install-via-Approval-flow lives at /api/v1/agent/install-requests (agent-auth).
These endpoints are the user-facing admin UI (Settings → MCP).
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.auth import require_user
from app.database import get_session
from app.models.agent import Agent
from app.services.mcp_registry import MCPRegistry, MCPRegistryError
from app.services.mcp_sync import sync_agent_mcp_to_disk

router = APIRouter(prefix="/api/v1", tags=["mcp-servers"])
logger = logging.getLogger(__name__)


class MCPServerOut(BaseModel):
    name: str
    transport: str
    description: str | None = None
    source: str | None = None
    installed_version: str | None = None
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    installed_at: str | None = None


class MCPServerCreate(BaseModel):
    name: str
    transport: Literal["stdio", "http", "sse"]
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    description: str | None = None
    source: str | None = None


class AgentMCPServersUpdate(BaseModel):
    mcp_servers: list[str] | None


@router.get("/mcp-servers", response_model=list[MCPServerOut])
async def list_mcp_servers(user=Depends(require_user)) -> list[MCPServerOut]:
    registry = MCPRegistry()
    return [
        MCPServerOut(
            name=m.name, transport=m.transport, description=m.description,
            source=m.source, installed_version=m.installed_version,
            command=m.command, args=m.args, url=m.url,
        )
        for m in registry.list_installed()
    ]


@router.get("/mcp-servers/{name}", response_model=MCPServerOut)
async def get_mcp_server(name: str, user=Depends(require_user)) -> MCPServerOut:
    try:
        m = MCPRegistry().get_manifest(name)
    except MCPRegistryError:
        raise HTTPException(404, f"MCP server {name!r} not installed")
    return MCPServerOut(
        name=m.name, transport=m.transport, description=m.description,
        source=m.source, installed_version=m.installed_version,
        command=m.command, args=m.args, url=m.url,
    )


@router.post("/mcp-servers", response_model=MCPServerOut, status_code=201)
async def create_mcp_server(
    body: MCPServerCreate,
    user=Depends(require_user),
) -> MCPServerOut:
    # T-12-01: Reject names with path-traversal characters
    if not body.name or "/" in body.name or ".." in body.name or not body.name.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "Invalid server name: must contain only alphanumeric characters, hyphens, and underscores")

    registry = MCPRegistry()
    # 409 if already exists
    srv_dir = registry.root / body.name
    if srv_dir.exists() and (srv_dir / "manifest.json").exists():
        raise HTTPException(409, f"MCP server {body.name!r} already exists")

    # Write manifest
    srv_dir.mkdir(parents=True, exist_ok=True)
    installed_at = datetime.now(timezone.utc).isoformat()
    manifest_data = {
        k: v for k, v in {
            "name": body.name,
            "transport": body.transport,
            "command": body.command,
            "args": body.args,
            "url": body.url,
            "env": body.env,
            "headers": body.headers,
            "description": body.description,
            "source": body.source,
            "installed_at": installed_at,
        }.items() if v is not None
    }
    (srv_dir / "manifest.json").write_text(json.dumps(manifest_data, indent=2))

    return MCPServerOut(
        name=body.name,
        transport=body.transport,
        command=body.command,
        args=body.args,
        url=body.url,
        description=body.description,
        source=body.source,
        installed_at=installed_at,
    )


@router.delete("/mcp-servers/{name}")
async def delete_mcp_server(
    name: str,
    user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # Apply same validation as POST to prevent path traversal
    if not name or "/" in name or ".." in name or not name.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "Invalid server name")
    registry = MCPRegistry()
    # 404 if not found
    try:
        registry.get_manifest(name)
    except MCPRegistryError:
        raise HTTPException(404, f"MCP server {name!r} not found")

    # Uninstall from filesystem
    registry.uninstall(name)

    # Clean from agent assignments
    result = await session.exec(select(Agent))
    agents = result.all()
    cleaned_agents: list[str] = []
    for agent in agents:
        if agent.mcp_servers and name in agent.mcp_servers:
            agent.mcp_servers = [s for s in agent.mcp_servers if s != name]
            cleaned_agents.append(agent.name)
            try:
                sync_agent_mcp_to_disk(agent)
            except Exception as e:
                logger.warning("sync_agent_mcp_to_disk failed for %s: %s", agent.name, e)

    if cleaned_agents:
        await session.commit()

    return {"ok": True, "cleaned_agents": cleaned_agents}


@router.patch("/agents/{agent_id}/mcp-servers")
async def update_agent_mcp_servers(
    agent_id: uuid.UUID,
    body: AgentMCPServersUpdate,
    user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    result = await session.exec(select(Agent).where(Agent.id == agent_id))
    agent = result.first()
    if agent is None:
        raise HTTPException(404, "Agent not found")

    agent.mcp_servers = body.mcp_servers
    await session.commit()

    try:
        sync_agent_mcp_to_disk(agent)
    except Exception as e:
        return {"ok": True, "synced": False, "sync_error": str(e)}

    return {"ok": True, "synced": True, "mcp_servers": agent.mcp_servers}
