import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_role, require_user
from app.database import get_session
from app.services.playbook_service import playbook_service
from app.services.workflow_validator import WorkflowValidationError

router = APIRouter(prefix="/api/v1/automations", tags=["automations"])


class AutomationUpdate(BaseModel):
    name: str | None = None
    summary: str | None = None
    board_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    trigger_type: Literal["manual", "scheduled"] | None = None
    trigger_config: dict[str, Any] | None = None
    delivery_config: dict[str, Any] | None = None
    status: Literal["draft", "active", "paused", "archived"] | None = None
    runtime_overrides: dict[str, Any] | None = None


@router.get("", dependencies=[Depends(require_role("viewer"))])
async def list_automations(
    board_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    automations = await playbook_service.list_automations(session, board_id=board_id)
    return [automation.model_dump() for automation in automations]


@router.get("/{automation_id}", dependencies=[Depends(require_role("viewer"))])
async def get_automation(
    automation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    automation = await playbook_service.get_automation(session, automation_id)
    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")
    return automation.model_dump()


@router.patch("/{automation_id}", dependencies=[Depends(require_role("operator"))])
async def update_automation(
    automation_id: uuid.UUID,
    payload: AutomationUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    automation = await playbook_service.get_automation(session, automation_id)
    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")
    try:
        updated = await playbook_service.update_automation(
            session,
            automation,
            payload.model_dump(exclude_unset=True),
            updated_by=str(current_user.id),
        )
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated.model_dump()


@router.post("/{automation_id}/activate", dependencies=[Depends(require_role("operator"))])
async def activate_automation(
    automation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    automation = await playbook_service.get_automation(session, automation_id)
    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")
    updated = await playbook_service.update_automation(
        session,
        automation,
        {"status": "active"},
        updated_by=str(current_user.id),
    )
    return updated.model_dump()


@router.post("/{automation_id}/pause", dependencies=[Depends(require_role("operator"))])
async def pause_automation(
    automation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    automation = await playbook_service.get_automation(session, automation_id)
    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")
    updated = await playbook_service.update_automation(
        session,
        automation,
        {"status": "paused"},
        updated_by=str(current_user.id),
    )
    return updated.model_dump()


@router.post("/{automation_id}/run", dependencies=[Depends(require_role("operator"))])
async def run_automation(
    automation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    automation = await playbook_service.get_automation(session, automation_id)
    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")
    try:
        run = await playbook_service.run_automation(session, automation)
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return run.model_dump()
