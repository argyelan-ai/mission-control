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


async def resolve_adhoc_repo_target(
    session: AsyncSession, task,
) -> tuple[str, str]:
    """Resolve (clone_url, dir_slug) for a git-requiring agent on an ad-hoc
    task (no task.project_id) — task af914128, "Ad-hoc-Karten ohne Projekt
    bekommen kein Repo".

    Precedence, most to least specific — never returns "no repo": a
    git-requiring agent must always land in SOME real clone, never a plain
    non-git directory (that silence is what let an agent self-clone the
    wrong `gh repo clone <shortname>` result and burn an hour of unusable
    work, incident 2026-09-14):

      1. ``task.repo_id`` — explicit choice made in the ad-hoc-card Maske
         (ADR-052). Always full clone URL via the registry (clone_url_for),
         never a short name.
      2. ``board.default_project_id`` — the board's own "cards without a
         project are about repo X" default. Reuses the existing Board field
         (task_create.py already treats it as project-resolution fallback
         at card-creation time) instead of introducing a new tag/label an
         operator has to remember to set per card.
      3. The shared ``ADHOC_REPO`` scratch repo (``mc-workspace``) — last
         resort so step "immer" holds even when neither of the above is
         configured. Created on demand (idempotent) via
         ``git_service.ensure_adhoc_repo``.

    Tags were considered and rejected for step "how do we recognize a card
    as repo-relevant" — an operator has to remember to add them per card,
    where repo_id/default_project_id are either an explicit choice already
    made in the UI or a one-time board setting. Whether to call this
    resolver at all is decided by the caller via
    ``agent.requires_git_workflow`` (already the authoritative per-agent
    flag for "does this agent's output belong in git", see
    dispatch_message_builder.py's git_section selection) — non-coder
    ad-hoc tasks (Research/Writing) never reach here.
    """
    from app.models.repo import Repo

    if task.repo_id:
        registry_repo = await session.get(Repo, task.repo_id)
        if registry_repo is None or not registry_repo.is_active:
            # PR #584 review N4/N5: task.repo_id is an EXPLICIT choice made
            # in the ad-hoc-card Maske — silently substituting board-default
            # or the shared scratch repo when it doesn't resolve (archived
            # in the registry, or — unlikely given the FK, but the caller's
            # hard-fail contract is cheap insurance — deleted) is the more
            # expensive failure direction than a loud abort. Both callers
            # already wrap this in a try/except that hard-fails (blocker
            # comment + status=blocked + terminal-unassign), same contract
            # as every other resolution failure here.
            raise ValueError(
                f"task.repo_id={task.repo_id} verweist auf kein aktives "
                "Registry-Repo — explizite Repo-Wahl wird nicht still durch "
                "board-default/scratch ersetzt."
            )
        return (
            clone_url_for(registry_repo),
            registry_repo.full_name.split("/", 1)[-1],
        )

    if task.board_id:
        from app.models.board import Board

        board = await session.get(Board, task.board_id)
        if board and board.default_project_id:
            project = await session.get(Project, board.default_project_id)
            if project and project.github_repo_url:
                from app.services.git_service import slugify_project

                return project.github_repo_url, slugify_project(project.name)

    from app.services.git_service import ADHOC_REPO, git_service

    repo_url = await git_service.ensure_adhoc_repo()
    return repo_url, ADHOC_REPO


async def get_repo_rules_for_task(
    session: AsyncSession, task, project: Project | None
) -> tuple[str, str] | None:
    """Regel-Lookup mit Task-Vorrang (ADR-052).

    Explizit am Task gewähltes Registry-Repo gewinnt; sonst gilt der
    Projekt-Pfad (repo_id → Legacy-Name-Fallback).
    """
    repo_id = getattr(task, "repo_id", None)
    if repo_id:
        try:
            repo = await session.get(Repo, repo_id)
        except Exception:
            logger.warning("Task-Repo-Regel-Lookup fehlgeschlagen", exc_info=True)
            repo = None
        if repo and repo.rules_md and repo.rules_md.strip():
            return repo.full_name, repo.rules_md.strip()
        if repo is not None:
            return None  # explizites Repo ohne Regeln — Projekt-Regeln gelten NICHT
    return await get_repo_rules_for_project(session, project)
