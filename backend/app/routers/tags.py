import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.auth import require_user
from app.database import get_session
from app.models.tag import Tag, TagAssignment

router = APIRouter(prefix="/api/v1", tags=["tags"])


class TagCreate(BaseModel):
    name: str
    slug: str
    color: str | None = None


class TagUpdate(BaseModel):
    name: str | None = None
    color: str | None = None


class ProjectTagAssign(BaseModel):
    tag_id: uuid.UUID | None = None
    name: str | None = None
    color: str | None = None


@router.get("/tags")
async def list_tags(
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    result = await session.exec(select(Tag).order_by(Tag.name))
    return result.all()


@router.post("/tags", status_code=status.HTTP_201_CREATED)
async def create_tag(
    payload: TagCreate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    tag = Tag(**payload.model_dump())
    session.add(tag)
    await session.commit()
    await session.refresh(tag)
    return tag


@router.patch("/tags/{tag_id}")
async def update_tag(
    tag_id: uuid.UUID,
    payload: TagUpdate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    tag = await session.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(tag, k, v)
    session.add(tag)
    await session.commit()
    await session.refresh(tag)
    return tag


@router.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(
    tag_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    tag = await session.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    await session.delete(tag)
    await session.commit()


def _slugify(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-")


# ── Project Tag Endpoints ───────────────────────────────────────────────────────


@router.get("/projects/{project_id}/tags")
async def list_project_tags(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    result = await session.exec(
        select(Tag)
        .join(TagAssignment, TagAssignment.tag_id == Tag.id)
        .where(TagAssignment.project_id == project_id)
        .order_by(Tag.name)
    )
    return result.all()


@router.post("/projects/{project_id}/tags", status_code=status.HTTP_201_CREATED)
async def assign_project_tag(
    project_id: uuid.UUID,
    payload: ProjectTagAssign,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    if payload.tag_id:
        tag = await session.get(Tag, payload.tag_id)
        if not tag:
            raise HTTPException(status_code=404, detail="Tag not found")
    elif payload.name:
        slug = _slugify(payload.name)
        result = await session.exec(select(Tag).where(Tag.slug == slug))
        tag = result.first()
        if not tag:
            tag = Tag(name=payload.name, slug=slug, color=payload.color)
            session.add(tag)
            await session.flush()
    else:
        raise HTTPException(status_code=400, detail="tag_id or name required")

    existing = await session.exec(
        select(TagAssignment).where(
            TagAssignment.tag_id == tag.id,
            TagAssignment.project_id == project_id,
        )
    )
    if existing.first():
        return tag

    assignment = TagAssignment(tag_id=tag.id, project_id=project_id)
    session.add(assignment)
    await session.commit()
    await session.refresh(tag)
    return tag


@router.delete(
    "/projects/{project_id}/tags/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_project_tag(
    project_id: uuid.UUID,
    tag_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    result = await session.exec(
        select(TagAssignment).where(
            TagAssignment.tag_id == tag_id,
            TagAssignment.project_id == project_id,
        )
    )
    assignment = result.first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Tag assignment not found")
    await session.delete(assignment)
    await session.commit()
