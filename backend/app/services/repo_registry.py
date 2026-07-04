"""Repo registry helpers (ADR-050).

Single place for the Repo↔Project linking contract: linking a project
always syncs the legacy github_repo_url/github_repo_name fields from the
repo row, so every existing clone/PR/merge flow keeps working unchanged.
"""

import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.board import Project
from app.models.repo import Repo
from app.utils import utcnow

logger = logging.getLogger("mc.repo_registry")


def clone_url_for(repo: Repo) -> str:
    """https clone URL (legacy github_repo_url format ends in .git)."""
    base = repo.url.removesuffix(".git")
    return f"{base}.git"


def apply_repo_link(project: Project, repo: Repo) -> None:
    """Link a project to a repo row + sync the legacy string fields."""
    project.repo_id = repo.id
    project.github_repo_name = repo.full_name
    project.github_repo_url = clone_url_for(repo)
    project.updated_at = utcnow()


def clear_repo_link(project: Project) -> None:
    project.repo_id = None
    project.github_repo_name = None
    project.github_repo_url = None
    project.updated_at = utcnow()


async def get_repo_by_full_name(session: AsyncSession, full_name: str) -> Repo | None:
    result = await session.exec(select(Repo).where(Repo.full_name == full_name))
    return result.first()


async def upsert_repo(
    session: AsyncSession,
    *,
    full_name: str,
    url: str,
    default_branch: str = "main",
    description: str | None = None,
    visibility: str = "private",
    source: str = "mc",
) -> Repo:
    """Insert or refresh a repo row by full_name. Does not commit."""
    repo = await get_repo_by_full_name(session, full_name)
    if repo:
        repo.url = url.removesuffix(".git")
        repo.default_branch = default_branch or repo.default_branch
        if description is not None:
            repo.description = description
        repo.visibility = visibility or repo.visibility
        repo.updated_at = utcnow()
    else:
        repo = Repo(
            full_name=full_name,
            url=url.removesuffix(".git"),
            default_branch=default_branch or "main",
            description=description,
            visibility=visibility or "private",
            source=source,
        )
    session.add(repo)
    return repo


async def resolve_repo_for_project(
    session: AsyncSession, project: Project | None
) -> Repo | None:
    """Repo row for a project — via repo_id, fallback legacy full_name match."""
    if project is None:
        return None
    if project.repo_id:
        repo = await session.get(Repo, project.repo_id)
        if repo:
            return repo
    if project.github_repo_name:
        return await get_repo_by_full_name(session, project.github_repo_name)
    return None


async def get_repo_rules_for_project(
    session: AsyncSession, project: Project | None
) -> tuple[str, str] | None:
    """(full_name, rules_md) if the project's repo carries working rules."""
    try:
        repo = await resolve_repo_for_project(session, project)
    except Exception:
        logger.warning("Repo-Regel-Lookup fehlgeschlagen", exc_info=True)
        return None
    if repo and repo.rules_md and repo.rules_md.strip():
        return repo.full_name, repo.rules_md.strip()
    return None
