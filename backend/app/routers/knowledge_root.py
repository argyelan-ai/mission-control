"""Read endpoints of the knowledge root (ADR-104, slice 1).

Admin only — the same as the vault reads (``routers/vault.py``), because the
index covers the vault. While ``KNOWLEDGE_ROOT_ENABLED`` is off every route
answers 404 ``{"reason": "knowledge_root_disabled"}``.

The prefix is ``/knowledge-root`` because ``/api/v1/knowledge`` is the legacy
``board_memory`` API (``routers/memory.py``), which stays until slice 5.
"""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.database import get_session
from app.services import knowledge, knowledge_import

router = APIRouter(
    prefix="/api/v1/knowledge-root",
    tags=["knowledge-root"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)

Drawer = Literal["notes", "skills", "projects"]
Source = Literal["knowledge", "vault", "skills"]


def _require_enabled() -> None:
    if not knowledge.enabled():
        raise HTTPException(status_code=404, detail={"reason": "knowledge_root_disabled"})


@router.get("/status")
async def knowledge_status(session: AsyncSession = Depends(get_session)):
    """Counts per drawer and source, flagged files, the last sweep."""
    _require_enabled()
    out = {"enabled": True, "root": str(knowledge.knowledge_root())}
    out.update(await knowledge.counts(session))
    out["lastSync"] = knowledge_import.last_sync()
    return out


@router.get("/projects")
async def knowledge_projects(session: AsyncSession = Depends(get_session)):
    """Every project folder with its name and number of entries."""
    _require_enabled()
    return {"projects": await knowledge.list_projects(session)}


@router.get("/search")
async def knowledge_search(
    q: str = Query(..., min_length=2, max_length=200),
    drawer: Drawer | None = None,
    project: str | None = Query(None, max_length=120),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Full-text search over the index (title, tags, capped body)."""
    _require_enabled()
    return {"hits": await knowledge.search(session, q, drawer=drawer, project=project, limit=limit)}


@router.get("/entries")
async def knowledge_entries(
    drawer: Drawer | None = None,
    project: str | None = Query(None, max_length=120),
    source: Source | None = None,
    problems: bool = False,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """One page of the index; ``problems=true`` lists only flagged files."""
    _require_enabled()
    return await knowledge.list_entries(
        session, drawer=drawer, project=project, source=source, problems=problems, limit=limit, offset=offset
    )


@router.get("/entries/{entry_id}")
async def knowledge_entry(entry_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    """One entry with its body, read from the file now. ``stale`` = the file
    changed since the last sweep. 410 when the file is gone."""
    _require_enabled()
    entry = await knowledge.get_entry(session, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    body = knowledge.read_body(entry)
    if body is None:
        raise HTTPException(status_code=410, detail={"reason": "file_gone"})
    out = knowledge.entry_dict(entry)
    out["body"], out["stale"] = body
    return out


@router.post("/_admin/sync")
async def knowledge_sync_now(session: AsyncSession = Depends(get_session)):
    """Run one sweep now: layout, project folders, index. Writes only into the
    knowledge root and the index; the vault and the skills folder are read."""
    _require_enabled()
    return (await knowledge_import.sync_all(session)).to_api()
