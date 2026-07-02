"""CLI Plugins Router — CRUD fuer den zentralen Plugin-Store.

Kommuniziert mit der CLI-Bridge fuer Install/Update/Remove Operationen.
Liest den shared cache fuer Plugin-Listen.
"""

import asyncio
import logging
import uuid as uuid_mod
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.config import settings
from app.database import get_session
from app.models.agent import Agent
from app.models.activity import ActivityEvent
from app.services.plugin_manager import list_available_plugins, list_github_skill_repos, list_custom_skills

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["cli-plugins"])


class PluginInstallRequest(BaseModel):
    plugin_key: str


@router.get("/plugins")
async def get_plugins(current_user=Depends(require_user)):
    """Alle im shared cache installierten CLI Plugins auflisten."""
    plugins = list_available_plugins()
    return {
        "plugins": [p.model_dump() for p in plugins],
        "total": len(plugins),
    }


@router.get("/plugins/custom-skills")
async def get_custom_skills(current_user=Depends(require_user)):
    """Custom Skills aus ~/.mc/skills/ auflisten (fuer SkillMatrix)."""
    skills = list_custom_skills()
    return {
        "skills": [s.model_dump() for s in skills],
        "total": len(skills),
    }


@router.get("/plugins/github-skills")
async def get_github_skills(current_user=Depends(require_user)):
    """Installierte GitHub Skill-Repos aus skills-lock.json auflisten."""
    repos = list_github_skill_repos()
    return {"repos": [r.model_dump() for r in repos], "total": len(repos)}


@router.post("/plugins/install")
async def install_plugin(
    body: PluginInstallRequest,
    current_user=Depends(require_user),
):
    """Plugin im shared cache installieren via CLI-Bridge."""
    from app.routers.cli_terminal import _bridge_post
    result = _bridge_post("/plugins/install", {"plugin_key": body.plugin_key}, timeout=130)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Installation fehlgeschlagen"))
    return {"success": True, "plugin_key": body.plugin_key, "result": result}


@router.post("/plugins/{plugin_key:path}/update")
async def update_plugin(
    plugin_key: str,
    current_user=Depends(require_user),
):
    """Plugin im shared cache updaten via CLI-Bridge."""
    from app.routers.cli_terminal import _bridge_post
    result = _bridge_post(f"/plugins/{plugin_key}/update", {}, timeout=130)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Update fehlgeschlagen"))
    return {"success": True, "plugin_key": plugin_key, "result": result}


@router.delete("/plugins/{plugin_key:path}")
async def remove_plugin(
    plugin_key: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Plugin deinstallieren + aus allen Agents cli_plugins entfernen."""
    from app.routers.cli_terminal import _bridge_delete
    result = _bridge_delete(f"/plugins/{plugin_key}")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Deinstallation fehlgeschlagen"))

    # Aus allen Agents entfernen die das Plugin haben
    agents_result = await session.exec(
        select(Agent).where(Agent.agent_runtime == "cli-bridge")
    )
    updated_agents = []
    for agent in agents_result.all():
        if agent.cli_plugins and plugin_key in agent.cli_plugins:
            agent.cli_plugins = [p for p in agent.cli_plugins if p != plugin_key]
            session.add(agent)
            updated_agents.append(agent.name)

    if updated_agents:
        await session.commit()
        logger.info("Plugin %s entfernt von Agents: %s", plugin_key, updated_agents)

    return {
        "success": True,
        "plugin_key": plugin_key,
        "agents_updated": updated_agents,
    }


# ---------------------------------------------------------------------------
# Plugin Audit Trail — wann hat wer welche Plugins geaendert
# ---------------------------------------------------------------------------


@router.get("/plugins/audit")
async def get_plugins_audit(
    limit: int = Query(50, le=500),
    offset: int = Query(0),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Audit-Trail aller Plugin-Aenderungen (agent.plugins_updated Events).

    Beinhaltet auch install/update/remove Events (agent.skill_filter_updated).
    """
    event_types = ("agent.plugins_updated", "agent.skill_filter_updated")
    stmt = (
        select(ActivityEvent)
        .where(col(ActivityEvent.event_type).in_(event_types))
        .order_by(col(ActivityEvent.created_at).desc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.exec(stmt)
    events = result.all()
    return {"events": [e.model_dump() for e in events], "total": len(events)}


# ---------------------------------------------------------------------------
# Plugins Shell — tmux-Session im shared Plugin-Verzeichnis
# ---------------------------------------------------------------------------


@router.post("/plugins/shell")
async def start_plugins_shell(current_user=Depends(require_user)):
    """Plugin-Shell starten (tmux-Session in ~/.mc/plugins/)."""
    from app.routers.cli_terminal import _bridge_post
    result = _bridge_post("/plugins/shell", {})
    return result


@router.delete("/plugins/shell")
async def stop_plugins_shell(current_user=Depends(require_user)):
    """Plugin-Shell beenden."""
    from app.routers.cli_terminal import _bridge_delete
    result = _bridge_delete("/plugins/shell")
    return result


@router.websocket("/plugins/shell/ws")
async def plugins_shell_websocket(
    websocket: WebSocket,
    token: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """WebSocket-Proxy fuer die Plugin-Shell."""
    # Auth check
    if not token:
        await websocket.close(code=4001)
        return
    try:
        from jose import jwt as _jwt
        payload = _jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])
        if not payload.get("sub"):
            await websocket.close(code=4001)
            return
    except Exception:
        await websocket.close(code=4001)
        return

    await websocket.accept()

    # Bridge WebSocket URL
    bridge_ws_url = settings.free_code_bridge_url.replace("http://", "ws://").replace("https://", "wss://")
    bridge_ws_url = bridge_ws_url.replace(":18792", ":18793")
    ws_url = f"{bridge_ws_url}/plugins-shell"

    logger.info("Plugins shell WS proxy → %s", ws_url)

    try:
        import websockets as _ws
        async with _ws.connect(ws_url) as bridge_ws:
            async def browser_to_bridge():
                while True:
                    try:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("bytes"):
                            await bridge_ws.send(msg["bytes"])
                        elif msg.get("text"):
                            await bridge_ws.send(msg["text"])
                    except WebSocketDisconnect:
                        break
                    except Exception:
                        break

            async def bridge_to_browser():
                async for msg in bridge_ws:
                    try:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                    except Exception:
                        break

            await asyncio.gather(browser_to_bridge(), bridge_to_browser())

    except Exception as e:
        logger.error("Plugins shell WS proxy error: %s", e)
        try:
            await websocket.send_text(f"\r\n\x1b[31m[Bridge nicht erreichbar: {e}]\x1b[0m\r\n")
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
