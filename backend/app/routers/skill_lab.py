import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_role, require_user
from app.database import get_session
from app.services.playbook_service import playbook_service

router = APIRouter(prefix="/api/v1/skill-lab", tags=["skill-lab"])


class SkillCandidateUpdate(BaseModel):
    title: str | None = None
    summary: str | None = None
    status: Literal["open", "approved", "rejected", "applied"] | None = None
    target_skill_key: str | None = None
    evidence: dict[str, Any] | None = None
    source_run_ids: list[str] | None = None
    draft_skill_content: str | None = None


@router.get("/candidates", dependencies=[Depends(require_role("viewer"))])
async def list_skill_candidates(
    board_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    candidates = await playbook_service.list_skill_candidates(session, board_id=board_id)
    return [candidate.model_dump() for candidate in candidates]


@router.patch("/candidates/{candidate_id}", dependencies=[Depends(require_role("operator"))])
async def update_skill_candidate(
    candidate_id: uuid.UUID,
    payload: SkillCandidateUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    candidate = await playbook_service.get_skill_candidate(session, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Skill candidate not found")
    updated = await playbook_service.update_skill_candidate(
        session,
        candidate,
        payload.model_dump(exclude_unset=True),
        reviewed_by=str(current_user.id),
    )
    return updated.model_dump()
