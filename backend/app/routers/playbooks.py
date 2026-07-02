import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_role, require_user
from app.database import get_session
from app.models.playbook import Automation, Playbook
from app.services.henry_service import henry_service
from app.services.playbook_service import playbook_service
from app.services.workflow_validator import WorkflowValidationError

router = APIRouter(prefix="/api/v1/playbooks", tags=["playbooks"])


class PlaybookCreate(BaseModel):
    kind: str
    name: str
    summary: str | None = None
    goal: str | None = None
    board_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    skill_pack_id: uuid.UUID | None = None
    default_agent_id: uuid.UUID | None = None
    scope: Literal["global", "board", "project"] = "global"
    status: Literal["draft", "review", "active", "archived"] = "draft"
    current_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] | None = None
    review_notes: str | None = None


class PlaybookUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: str | None = None
    name: str | None = None
    summary: str | None = None
    goal: str | None = None
    board_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    skill_pack_id: uuid.UUID | None = None
    default_agent_id: uuid.UUID | None = None
    scope: Literal["global", "board", "project"] | None = None
    status: Literal["draft", "review", "active", "archived"] | None = None
    current_config: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    review_notes: str | None = None


class PlaybookVersionCreate(BaseModel):
    change_reason: str | None = None


class AutomationCreate(BaseModel):
    name: str
    summary: str | None = None
    board_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    trigger_type: Literal["manual", "scheduled"] = "scheduled"
    trigger_config: dict[str, Any] | None = None
    delivery_config: dict[str, Any] | None = None
    status: Literal["draft", "active", "paused", "archived"] = "draft"
    runtime_overrides: dict[str, Any] | None = None


class HenrySessionStart(BaseModel):
    board_id: uuid.UUID
    kind: str | None = None
    playbook_id: uuid.UUID | None = None


class HenrySessionMessage(BaseModel):
    content: str


@router.get("/catalog", dependencies=[Depends(require_role("viewer"))])
async def get_playbook_catalog() -> dict[str, Any]:
    return {"playbooks": playbook_service.get_catalog()}


@router.get("/skill-packs", dependencies=[Depends(require_role("viewer"))])
async def get_skill_packs(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    packs = await playbook_service.list_skill_packs(session)
    return [pack.model_dump() for pack in packs]


@router.get("", dependencies=[Depends(require_role("viewer"))])
async def list_playbooks(
    board_id: uuid.UUID | None = Query(default=None),
    include_archived: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    playbooks = await playbook_service.list_playbooks(
        session,
        board_id=board_id,
        include_archived=include_archived,
    )
    return [playbook.model_dump() for playbook in playbooks]


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_role("operator"))])
async def create_playbook(
    payload: PlaybookCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    try:
        playbook = await playbook_service.create_playbook(
            session,
            payload.model_dump(),
            created_by=str(current_user.id),
        )
    except (WorkflowValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return playbook.model_dump()


@router.get("/{playbook_id}", dependencies=[Depends(require_role("viewer"))])
async def get_playbook(
    playbook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    playbook = await playbook_service.get_playbook(session, playbook_id)
    if not playbook:
        raise HTTPException(status_code=404, detail="Playbook not found")
    return playbook.model_dump()


@router.patch("/{playbook_id}", dependencies=[Depends(require_role("operator"))])
async def update_playbook(
    playbook_id: uuid.UUID,
    payload: PlaybookUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    playbook = await playbook_service.get_playbook(session, playbook_id)
    if not playbook:
        raise HTTPException(status_code=404, detail="Playbook not found")
    try:
        updated = await playbook_service.update_playbook(
            session,
            playbook,
            payload.model_dump(exclude_unset=True),
            updated_by=str(current_user.id),
        )
    except (WorkflowValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return updated.model_dump()


@router.post("/{playbook_id}/approve", dependencies=[Depends(require_role("operator"))])
async def approve_playbook(
    playbook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    playbook = await playbook_service.get_playbook(session, playbook_id)
    if not playbook:
        raise HTTPException(status_code=404, detail="Playbook not found")
    try:
        approved = await playbook_service.approve_playbook(
            session,
            playbook,
            approved_by=str(current_user.id),
        )
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return approved.model_dump()


@router.get("/{playbook_id}/versions", dependencies=[Depends(require_role("viewer"))])
async def get_playbook_versions(
    playbook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    playbook = await playbook_service.get_playbook(session, playbook_id)
    if not playbook:
        raise HTTPException(status_code=404, detail="Playbook not found")
    versions = await playbook_service.get_versions(session, playbook_id)
    return [version.model_dump() for version in versions]


@router.post("/{playbook_id}/versions", dependencies=[Depends(require_role("operator"))])
async def create_playbook_version(
    playbook_id: uuid.UUID,
    payload: PlaybookVersionCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    playbook = await playbook_service.get_playbook(session, playbook_id)
    if not playbook:
        raise HTTPException(status_code=404, detail="Playbook not found")
    version = await playbook_service.create_version(
        session,
        playbook,
        created_by=str(current_user.id),
        change_reason=payload.change_reason,
    )
    return version.model_dump()


@router.get("/{playbook_id}/automations", dependencies=[Depends(require_role("viewer"))])
async def get_playbook_automations(
    playbook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    playbook = await playbook_service.get_playbook(session, playbook_id)
    if not playbook:
        raise HTTPException(status_code=404, detail="Playbook not found")
    result = await session.exec(
        select(Automation)
        .where(Automation.playbook_id == playbook_id)
        .order_by(Automation.updated_at.desc())
    )
    return [automation.model_dump() for automation in result.all()]


@router.post("/{playbook_id}/automations", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_role("operator"))])
async def create_playbook_automation(
    playbook_id: uuid.UUID,
    payload: AutomationCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    playbook = await playbook_service.get_playbook(session, playbook_id)
    if not playbook:
        raise HTTPException(status_code=404, detail="Playbook not found")
    try:
        automation = await playbook_service.create_automation(
            session,
            playbook,
            payload.model_dump(),
            created_by=str(current_user.id),
        )
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return automation.model_dump()


@router.get("/runs/recent", dependencies=[Depends(require_role("viewer"))])
async def get_recent_playbook_runs(
    board_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    return await playbook_service.list_recent_runs(session, board_id=board_id, limit=limit)


@router.get("/henry/current", dependencies=[Depends(require_role("viewer"))])
async def get_current_henry_session(
    board_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any] | None:
    return await henry_service.get_current_session_state(session, board_id=board_id)


@router.post("/henry/sessions/start", dependencies=[Depends(require_role("operator"))])
async def start_henry_session(
    payload: HenrySessionStart,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    try:
        return await henry_service.start_session(
            session,
            board_id=payload.board_id,
            created_by=str(current_user.id),
            kind=payload.kind,
            playbook_id=payload.playbook_id,
        )
    except (WorkflowValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/henry/sessions/{session_id}/message", dependencies=[Depends(require_role("operator"))])
async def send_henry_message(
    session_id: uuid.UUID,
    payload: HenrySessionMessage,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    try:
        return await henry_service.send_message(
            session,
            project_id=session_id,
            content=payload.content,
            updated_by=str(current_user.id),
        )
    except (WorkflowValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
