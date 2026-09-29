"""Skill-candidate reads/writes behind the frozen skill-lab router.

Moved out of the removed playbook service (E5); behaviour unchanged.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.skill_lab import SkillCandidate
from app.utils import utcnow


async def list_skill_candidates(
    session: AsyncSession,
    *,
    board_id: uuid.UUID | None = None,
) -> list[SkillCandidate]:
    query = select(SkillCandidate).order_by(SkillCandidate.updated_at.desc())
    if board_id:
        query = query.where((SkillCandidate.board_id == board_id) | (SkillCandidate.board_id.is_(None)))
    result = await session.exec(query)
    return result.all()


async def get_skill_candidate(session: AsyncSession, candidate_id: uuid.UUID) -> SkillCandidate | None:
    return await session.get(SkillCandidate, candidate_id)


async def update_skill_candidate(
    session: AsyncSession,
    candidate: SkillCandidate,
    payload: dict[str, Any],
    *,
    reviewed_by: str,
) -> SkillCandidate:
    for key in ("title", "summary", "status", "target_skill_key", "evidence", "source_run_ids", "draft_skill_content"):
        if key in payload:
            setattr(candidate, key, payload[key])
    if "status" in payload and payload["status"] in {"approved", "rejected", "applied"}:
        candidate.reviewed_by = reviewed_by
        candidate.reviewed_at = utcnow()
    candidate.updated_at = utcnow()
    session.add(candidate)
    await session.commit()
    await session.refresh(candidate)
    return candidate
