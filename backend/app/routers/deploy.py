"""
Deploy-Router — Monitoring und Tracking Endpoints.

Agent-scoped Endpoints (mit deploy:execute Scope):
- Health-Checks, Deploy-History schreiben

User-scoped Endpoints:
- Service-Status, Deploy-History lesen
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.agent import Agent
from app.scopes import Scope, require_scope
from app.services.deploy import (
    DEPLOYABLE_SERVICES,
    KNOWN_SERVICES,
    check_all_services,
    check_service_health,
    get_deploy_history,
    record_deploy,
)

router = APIRouter(tags=["deploy"])


# ── Schemas ────────────────────────────────────────────────────────────

class RecordDeployRequest(BaseModel):
    service: str
    action: str  # "rebuild", "restart", "rollback", "backup"
    success: bool = True
    rolled_back: bool = False
    health_status: str | None = None
    duration_seconds: float | None = None
    error: str | None = None
    logs_tail: str | None = None
    task_id: uuid.UUID | None = None


class DeployHistoryResponse(BaseModel):
    id: uuid.UUID
    service: str
    action: str
    triggered_by: str
    agent_id: uuid.UUID | None
    task_id: uuid.UUID | None
    success: bool
    rolled_back: bool
    health_status: str | None
    duration_seconds: float | None
    error: str | None
    created_at: datetime


# ── Agent-Scoped Endpoints (deploy:execute) ────────────────────────────

@router.get("/api/v1/agent/deploy/services")
async def agent_list_services(
    agent: Agent = Depends(require_scope(Scope.DEPLOY_EXECUTE)),
):
    """Alle Services mit Health-Status (Agent-Endpoint)."""
    results = await check_all_services()
    return {
        "services": results,
        "deployable": sorted(DEPLOYABLE_SERVICES),
    }


@router.get("/api/v1/agent/deploy/services/{name}/health")
async def agent_service_health(
    name: str,
    agent: Agent = Depends(require_scope(Scope.DEPLOY_EXECUTE)),
):
    """Health-Check fuer einen einzelnen Service."""
    if name not in KNOWN_SERVICES:
        raise HTTPException(404, f"Unknown service: {name}")
    return await check_service_health(name)


@router.get("/api/v1/agent/deploy/credentials")
async def agent_get_credentials(
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.DEPLOY_EXECUTE)),
):
    """Deployment-Credentials fuer externe Services abrufen."""
    from app.models.secret import Secret
    from app.services.encryption import safe_decrypt

    keys = ["vercel_token", "cloudflare_token", "cloudflare_zone_id", "supabase_token"]
    result = {}
    for key in keys:
        secret = await session.exec(select(Secret).where(Secret.key == key))
        s = secret.first()
        result[key] = safe_decrypt(s.encrypted_value) if s else None

    return result


@router.post("/api/v1/agent/deploy/record")
async def agent_record_deploy(
    body: RecordDeployRequest,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.DEPLOY_EXECUTE)),
):
    """Deploy-Aktion in der History speichern."""
    if body.service not in KNOWN_SERVICES and body.action != "backup":
        raise HTTPException(400, f"Unknown service: {body.service}")

    entry = await record_deploy(
        session,
        service=body.service,
        action=body.action,
        triggered_by=agent.name,
        agent_id=agent.id,
        task_id=body.task_id,
        success=body.success,
        rolled_back=body.rolled_back,
        health_status=body.health_status,
        duration_seconds=body.duration_seconds,
        error=body.error,
        logs_tail=body.logs_tail,
    )
    return {"id": str(entry.id), "recorded": True}


@router.get("/api/v1/agent/deploy/history")
async def agent_deploy_history(
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.DEPLOY_EXECUTE)),
):
    """Deploy-History abrufen (Agent-Endpoint)."""
    entries = await get_deploy_history(session, limit=limit)
    return [
        DeployHistoryResponse(
            id=e.id,
            service=e.service,
            action=e.action,
            triggered_by=e.triggered_by,
            agent_id=e.agent_id,
            task_id=e.task_id,
            success=e.success,
            rolled_back=e.rolled_back,
            health_status=e.health_status,
            duration_seconds=e.duration_seconds,
            error=e.error,
            created_at=e.created_at,
        )
        for e in entries
    ]


# ── User-Scoped Endpoints (fuer Dashboard) ────────────────────────────

@router.get("/api/v1/deploy/services")
async def user_list_services(
    _user=Depends(require_user),
):
    """Alle Services mit Health-Status (User-Endpoint)."""
    results = await check_all_services()
    return {
        "services": results,
        "deployable": sorted(DEPLOYABLE_SERVICES),
    }


@router.get("/api/v1/deploy/history")
async def user_deploy_history(
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
):
    """Deploy-History (User-Endpoint)."""
    entries = await get_deploy_history(session, limit=limit)
    return [
        DeployHistoryResponse(
            id=e.id,
            service=e.service,
            action=e.action,
            triggered_by=e.triggered_by,
            agent_id=e.agent_id,
            task_id=e.task_id,
            success=e.success,
            rolled_back=e.rolled_back,
            health_status=e.health_status,
            duration_seconds=e.duration_seconds,
            error=e.error,
            created_at=e.created_at,
        )
        for e in entries
    ]
