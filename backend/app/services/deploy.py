"""
Deploy Service — health monitoring and deploy tracking.

The deployer agent runs Docker commands directly on the host (via OpenClaw).
This service provides monitoring + tracking:
- Health checks via HTTP (Docker network)
- Deploy history in the DB
- Service status queries
"""
import logging

import httpx
from sqlmodel import desc, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.deploy_history import DeployHistory

logger = logging.getLogger("mc.deploy")

# Services the agent is allowed to deploy — allowlist
KNOWN_SERVICES = {"backend", "frontend", "caddy", "db", "redis", "external"}
DEPLOYABLE_SERVICES = {"backend", "frontend", "caddy"}

# Health check endpoints (from within the Docker network)
SERVICE_HEALTH_URLS = {
    "backend": "http://localhost:8000/health",
    "frontend": "http://frontend:3000",
    "caddy": "http://caddy:80",
}


async def check_service_health(service: str) -> dict:
    """Health check for a service via HTTP."""
    if service not in SERVICE_HEALTH_URLS:
        return {"service": service, "status": "unknown", "detail": "No health endpoint configured"}

    url = SERVICE_HEALTH_URLS[service]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            status = "healthy" if resp.status_code < 400 else "unhealthy"
            return {"service": service, "status": status, "status_code": resp.status_code}
    except httpx.ConnectError:
        return {"service": service, "status": "unreachable", "detail": "Connection refused"}
    except httpx.TimeoutException:
        return {"service": service, "status": "timeout", "detail": "Health check timed out"}
    except Exception as e:
        return {"service": service, "status": "error", "detail": str(e)}


async def check_all_services() -> list[dict]:
    """Health check for all known services."""
    results = []
    for service in SERVICE_HEALTH_URLS:
        result = await check_service_health(service)
        results.append(result)
    return results


async def record_deploy(
    session: AsyncSession,
    *,
    service: str,
    action: str,
    triggered_by: str,
    agent_id=None,
    task_id=None,
    success: bool = True,
    rolled_back: bool = False,
    health_status: str | None = None,
    duration_seconds: float | None = None,
    error: str | None = None,
    logs_tail: str | None = None,
) -> DeployHistory:
    """Save a deploy action in the history."""
    entry = DeployHistory(
        service=service,
        action=action,
        triggered_by=triggered_by,
        agent_id=agent_id,
        task_id=task_id,
        success=success,
        rolled_back=rolled_back,
        health_status=health_status,
        duration_seconds=duration_seconds,
        error=error,
        logs_tail=logs_tail,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    logger.info("Deploy recorded: %s %s by %s — %s", action, service, triggered_by, "OK" if success else "FAILED")
    return entry


async def get_deploy_history(session: AsyncSession, limit: int = 20) -> list[DeployHistory]:
    """Fetch the most recent deploy entries."""
    result = await session.exec(
        select(DeployHistory).order_by(desc(DeployHistory.created_at)).limit(limit)
    )
    return list(result.all())
