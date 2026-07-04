"""Repos registry API (ADR-050) — manage GitHub repos + per-repo working rules.

Repos are first-class rows (models/repo.py). Multiple projects can share one
repo; per-repo rules_md is injected into dispatch directives. Deleting a repo
here NEVER touches GitHub — it only removes the MC registry row.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.board import Project
from app.models.repo import Repo
from app.services.repo_registry import (
    apply_repo_link,
    clear_repo_link,
    get_repo_by_full_name,
    upsert_repo,
)
from app.utils import utcnow

router = APIRouter(prefix="/api/v1", tags=["repos"])


class RepoCreate(BaseModel):
    full_name: str  # "owner/name" — must exist on GitHub (imported via gh view)


class RepoUpdate(BaseModel):
    description: str | None = None
    rules_md: str | None = None
    default_branch: str | None = None
    is_active: bool | None = None


class LinkProject(BaseModel):
    project_id: uuid.UUID


def _serialize(repo: Repo, projects: list[Project]) -> dict:
    return {
        **repo.model_dump(),
        "linked_projects": [
            {
                "id": str(p.id),
                "name": p.name,
                "status": p.status,
                "board_id": str(p.board_id),
            }
            for p in projects
        ],
    }


async def _linked_projects(session: AsyncSession, repo: Repo) -> list[Project]:
    result = await session.exec(select(Project).where(Project.repo_id == repo.id))
    return list(result.all())


@router.get("/repos")
async def list_repos(
    include_inactive: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    query = select(Repo).order_by(Repo.full_name)
    if not include_inactive:
        query = query.where(Repo.is_active == True)  # noqa: E712
    result = await session.exec(query)
    repos = list(result.all())
    # One query for all links instead of N+1 per repo.
    by_repo: dict = {}
    if repos:
        proj_result = await session.exec(
            select(Project).where(Project.repo_id.in_([r.id for r in repos]))  # type: ignore[union-attr]
        )
        for p in proj_result.all():
            by_repo.setdefault(p.repo_id, []).append(p)
    return [_serialize(repo, by_repo.get(repo.id, [])) for repo in repos]


@router.get("/repos/import-candidates")
async def list_import_candidates(
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """GitHub repos of the configured owner that are not yet registered."""
    from app.services.git_service import GitService

    git = GitService()
    try:
        gh_repos = await git.list_github_repos()
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"gh repo list fehlgeschlagen: {e}")

    result = await session.exec(select(Repo.full_name))
    known = set(result.all())
    return [r for r in gh_repos if r["full_name"] not in known and not r["is_archived"]]


@router.get("/repos/{repo_id}")
async def get_repo(
    repo_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    repo = await session.get(Repo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")
    return _serialize(repo, await _linked_projects(session, repo))


@router.post("/repos", status_code=status.HTTP_201_CREATED)
async def import_repo(
    payload: RepoCreate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Register an existing GitHub repo in the MC registry."""
    from app.services.git_service import GitService

    full_name = payload.full_name.strip()
    if "/" not in full_name:
        raise HTTPException(status_code=400, detail="full_name muss owner/name sein")

    existing = await get_repo_by_full_name(session, full_name)
    if existing:
        raise HTTPException(status_code=409, detail="Repo ist bereits registriert")

    git = GitService()
    try:
        meta = await git.fetch_repo_meta(full_name)
    except RuntimeError as e:
        raise HTTPException(
            status_code=404,
            detail=f"Repo auf GitHub nicht gefunden/lesbar: {e}",
        )

    repo = await upsert_repo(
        session,
        full_name=meta["full_name"],
        url=meta["url"],
        default_branch=meta["default_branch"],
        description=meta["description"],
        visibility=meta["visibility"],
        source="imported",
    )
    repo.last_synced_at = utcnow()
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent double-import: the unique index on full_name wins the
        # race — surface the same 409 as the pre-check instead of a 500.
        await session.rollback()
        raise HTTPException(status_code=409, detail="Repo ist bereits registriert")
    await session.refresh(repo)
    return _serialize(repo, [])


@router.patch("/repos/{repo_id}")
async def update_repo(
    repo_id: uuid.UUID,
    payload: RepoUpdate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    repo = await session.get(Repo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(repo, k, v)
    repo.updated_at = utcnow()
    session.add(repo)
    await session.commit()
    await session.refresh(repo)
    return _serialize(repo, await _linked_projects(session, repo))


@router.delete("/repos/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_repo(
    repo_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Remove the registry row. GitHub is NEVER touched."""
    repo = await session.get(Repo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")
    linked = await _linked_projects(session, repo)
    if linked:
        names = ", ".join(p.name for p in linked[:5])
        raise HTTPException(
            status_code=409,
            detail=f"Repo ist mit Projekten verknüpft ({names}) — erst entkoppeln",
        )
    await session.delete(repo)
    await session.commit()


@router.post("/repos/{repo_id}/sync")
async def sync_repo(
    repo_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Refresh metadata (default_branch, description, visibility) from GitHub."""
    from app.services.git_service import GitService

    repo = await session.get(Repo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    git = GitService()
    try:
        meta = await git.fetch_repo_meta(repo.full_name)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"gh repo view fehlgeschlagen: {e}")

    repo.default_branch = meta["default_branch"]
    repo.visibility = meta["visibility"]
    if meta["description"] is not None:
        repo.description = meta["description"]
    repo.last_synced_at = utcnow()
    repo.updated_at = utcnow()
    session.add(repo)
    await session.commit()
    await session.refresh(repo)
    return _serialize(repo, await _linked_projects(session, repo))


@router.post("/repos/{repo_id}/link-project")
async def link_project(
    repo_id: uuid.UUID,
    payload: LinkProject,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Link a project to this repo (syncs legacy github_repo_* fields)."""
    repo = await session.get(Repo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")
    project = await session.get(Project, payload.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    apply_repo_link(project, repo)
    session.add(project)
    await session.commit()
    return _serialize(repo, await _linked_projects(session, repo))


@router.delete("/repos/{repo_id}/link-project/{project_id}")
async def unlink_project(
    repo_id: uuid.UUID,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Unlink a project from this repo (clears legacy github_repo_* fields)."""
    repo = await session.get(Repo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")
    project = await session.get(Project, project_id)
    if not project or project.repo_id != repo_id:
        raise HTTPException(status_code=404, detail="Projekt ist nicht mit diesem Repo verknüpft")
    clear_repo_link(project)
    session.add(project)
    await session.commit()
    return _serialize(repo, await _linked_projects(session, repo))
