"""Prompt Library CRUD — Benchmark Studio Baustein 3 (core, PR 2).

Generic prompt storage for the operator (design 2026-07-11). Auth is the
user JWT (require_user) like routers/references.py — this is an operator
tool, NOT an agent-scoped endpoint. Schemas live in-file per repo pattern
(routers/x_posts.py).

The management UI lives in the Studio vertical (PR 3); this router plus the
frontend api client is the complete core surface.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.prompt_template import PromptTemplate

router = APIRouter(prefix="/api/v1/prompt-templates", tags=["prompt-templates"])


# ── Schemas ────────────────────────────────────────────────────────────────


class PromptTemplateCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1)
    tags: list[str] = []


class PromptTemplateUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1)
    tags: list[str] | None = None


# ── Helpers ────────────────────────────────────────────────────────────────


async def _get_or_404(session: AsyncSession, template_id: uuid.UUID) -> PromptTemplate:
    tpl = await session.get(PromptTemplate, template_id)
    if not tpl:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt template not found")
    return tpl


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("")
async def list_prompt_templates(
    q: str | None = None,
    tag: str | None = None,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> list[PromptTemplate]:
    """List templates, newest edits first. ?q= searches the title
    (case-insensitive substring), ?tag= filters by exact tag membership."""
    stmt = select(PromptTemplate).order_by(PromptTemplate.updated_at.desc())  # type: ignore[attr-defined]
    if q:
        stmt = stmt.where(PromptTemplate.title.ilike(f"%{q}%"))  # type: ignore[attr-defined]
    rows = (await session.exec(stmt)).all()
    if tag:
        # tags is a JSON column — membership filter in Python keeps it
        # portable across Postgres (prod) and SQLite (tests). Library-sized
        # data (operator-curated prompts), not a hot path.
        rows = [r for r in rows if tag in (r.tags or [])]
    return list(rows)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_prompt_template(
    body: PromptTemplateCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> PromptTemplate:
    tpl = PromptTemplate(title=body.title, body=body.body, tags=body.tags)
    session.add(tpl)
    await session.commit()
    await session.refresh(tpl)
    return tpl


@router.get("/{template_id}")
async def get_prompt_template(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> PromptTemplate:
    return await _get_or_404(session, template_id)


@router.patch("/{template_id}")
async def update_prompt_template(
    template_id: uuid.UUID,
    body: PromptTemplateUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> PromptTemplate:
    tpl = await _get_or_404(session, template_id)
    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(tpl, field, value)
    session.add(tpl)
    await session.commit()
    await session.refresh(tpl)
    return tpl


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt_template(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> None:
    tpl = await _get_or_404(session, template_id)
    await session.delete(tpl)
    await session.commit()
