"""
Agent-scoped endpoint: POST /api/v1/agent/install-requests

Creates install/uninstall approval requests for skills and plugins.
The actual execution happens in InstallExecutor AFTER approval.

Auth:   Agent PBKDF2 token
Scope:  Any agent can request for themselves; agents:manage required for other targets
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_agent
from app.database import get_session
from app.models.agent import Agent
from app.models.approval import Approval
from app.redis_client import RedisKeys, get_redis
from app.scopes import get_agent_effective_scopes
from app.services.install_allowlist import AllowlistError, validate_source

router = APIRouter(prefix="/api/v1/agent/install-requests", tags=["install-requests"])


# ── Schemas ───────────────────────────────────────────────────────────────


class InstallRequestCreate(BaseModel):
    type: Literal["skill", "plugin", "mcp"]
    operation: Literal["install", "uninstall"] = "install"
    source: str | None = None
    name: str
    target_agent_id: uuid.UUID
    reason: str = Field(..., min_length=5, max_length=2000)
    autonomy_level: Literal["L1", "L2", "L3"] | None = "L2"
    proposed_config: dict | None = None
    # Callback-Koppelung: wenn der Requester den Request im Kontext einer
    # laufenden Task stellt, posten wir nach erfolgreicher Installation einen
    # install_completed-Comment auf diese Task (mirror zum subtask_completed-
    # Pattern). Ohne task_id kein Auto-Callback — Requester muss selbst pollen.
    task_id: uuid.UUID | None = None


class InstallRequestResponse(BaseModel):
    approval_id: uuid.UUID
    status: str
    existing: bool


# ── Helpers ───────────────────────────────────────────────────────────────


def _resource_field(install_type: str) -> str:
    """Map install type to the Agent field that holds installed items."""
    return {"skill": "cli_skills", "plugin": "cli_plugins", "mcp": "mcp_servers"}[install_type]


# ── Endpoint ─────────────────────────────────────────────────────────────


@router.post("", response_model=InstallRequestResponse)
async def create_install_request(
    body: InstallRequestCreate,
    response: Response,
    requester: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> InstallRequestResponse:
    """
    Create an install/uninstall approval request.

    Returns 201 on new request, 200 if an identical pending request already exists.
    """
    # 1. Allowlist check (install only — uninstall doesn't need a source)
    if body.operation == "install":
        if not body.source:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "source is required for install operations")
        try:
            validate_source(body.type, body.source)
        except AllowlistError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Source not in allowlist: {e}")

    # 2. Target agent must exist
    target = await session.get(Agent, body.target_agent_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Target agent {body.target_agent_id} not found")

    # 3. Scope check: agents:manage required when targeting another agent
    is_self = body.target_agent_id == requester.id
    effective_scopes = get_agent_effective_scopes(requester)
    has_manage = "agents:manage" in effective_scopes
    if not (is_self or has_manage):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Requester needs 'agents:manage' scope to target other agents",
        )

    # 4. Already-installed conflict (install only)
    if body.operation == "install":
        field = _resource_field(body.type)
        current: list = getattr(target, field, None) or []
        if body.name in current:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{body.name!r} is already installed on agent {target.name!r}",
            )

    # 5. Idempotency: return existing pending request if identical
    action_type = f"{body.operation}_{body.type}"
    existing_rows = (
        await session.exec(
            select(Approval).where(
                Approval.action_type == action_type,
                Approval.status == "pending",
            )
        )
    ).all()

    for row in existing_rows:
        payload = row.payload or {}
        if (
            payload.get("name") == body.name
            and payload.get("target_agent_id") == str(body.target_agent_id)
        ):
            response.status_code = status.HTTP_200_OK
            return InstallRequestResponse(
                approval_id=row.id,
                status=row.status,
                existing=True,
            )

    # 6. Target must have a board (required for Approval.board_id FK)
    if target.board_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Target agent must be assigned to a board before install requests can be created",
        )

    # 7. Create Approval row
    description = (
        f"{requester.name} requests to "
        f"{body.operation} {body.type} "
        f"{body.name!r} on agent {target.name!r}: "
        f"{body.reason[:200]}"
    )
    payload_data = {
        "name": body.name,
        "source": body.source,
        "target_agent_id": str(body.target_agent_id),
        "target_agent_name": target.name,
        "requester_agent_id": str(requester.id),
        "requester_agent_name": requester.name,
        "reason": body.reason,
        "proposed_config": body.proposed_config,
        "requester_task_id": str(body.task_id) if body.task_id else None,
    }
    approval = Approval(
        board_id=target.board_id,
        agent_id=requester.id,
        action_type=action_type,
        description=description,
        payload=payload_data,
        autonomy_level=body.autonomy_level,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="pending",
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)

    # 8. Publish SSE event (non-critical — don't fail the request if Redis is unavailable)
    try:
        redis = await get_redis()
        await redis.publish(
            RedisKeys.approvals_events(),
            (
                f'{{"type":"approval.created","approval_id":"{approval.id}",'
                f'"action_type":"{action_type}"}}'
            ),
        )
    except Exception:
        pass  # SSE is best-effort; approval is already persisted

    response.status_code = status.HTTP_201_CREATED
    return InstallRequestResponse(
        approval_id=approval.id,
        status=approval.status,
        existing=False,
    )
