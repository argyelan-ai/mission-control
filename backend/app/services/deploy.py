"""
Deploy-Service — Health-Monitoring und Deploy-Tracking.

Der Deployer-Agent fuehrt Docker-Befehle direkt auf dem Host aus (via OpenClaw).
Dieses Service stellt Monitoring + Tracking bereit:
- Health-Checks via HTTP (Docker-Netzwerk)
- Deploy-History in der DB
- Service-Status Abfragen
"""
import logging

import httpx
from sqlmodel import desc, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.deploy_history import DeployHistory

logger = logging.getLogger("mc.deploy")

# Services die der Agent deployen darf — Allowlist
KNOWN_SERVICES = {"backend", "frontend", "caddy", "db", "redis", "external"}
DEPLOYABLE_SERVICES = {"backend", "frontend", "caddy"}

# Health-Check Endpoints (von innerhalb des Docker-Netzwerks)
SERVICE_HEALTH_URLS = {
    "backend": "http://localhost:8000/health",
    "frontend": "http://frontend:3000",
    "caddy": "http://caddy:80",
}


async def check_service_health(service: str) -> dict:
    """Health-Check fuer einen Service via HTTP."""
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
    """Health-Check fuer alle bekannten Services."""
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
    """Deploy-Aktion in der History speichern."""
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
    """Letzte Deploy-Eintraege abrufen."""
    result = await session.exec(
        select(DeployHistory).order_by(desc(DeployHistory.created_at)).limit(limit)
    )
    return list(result.all())
