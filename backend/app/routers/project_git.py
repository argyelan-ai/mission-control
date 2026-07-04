"""Project Git-Info endpoint — repo status + branches for task creation UI."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.board import Project
from app.services.git_service import GitService

router = APIRouter(prefix="/api/v1/boards/{board_id}/projects/{project_id}", tags=["project-git"])


class ProjectGitInfoResponse(BaseModel):
    has_repo: bool
    repo_name: str | None
    repo_url: str | None
    branches: list[str]
    # ADR-052: Registry-Anbindung für die Task-Maske (Regeln-Badge + Link)
    repo_id: str | None = None
    has_rules: bool = False


@router.get("/git-info", response_model=ProjectGitInfoResponse)
async def get_project_git_info(
    board_id: uuid.UUID,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user=Depends(require_user),
):
    result = await session.exec(
        select(Project).where(Project.id == project_id, Project.board_id == board_id)
    )
    project = result.first()
    if not project:
        raise HTTPException(404, "Project not found")

    if not project.github_repo_name:
        return ProjectGitInfoResponse(has_repo=False, repo_name=None, repo_url=None, branches=[])

    git = GitService()
    branches = await git.list_repo_branches(project.github_repo_name)

    from app.services.repo_registry import resolve_repo_for_project
    registry_repo = await resolve_repo_for_project(session, project)

    return ProjectGitInfoResponse(
        has_repo=True,
        repo_name=project.github_repo_name,
        repo_url=project.github_repo_url,
        branches=branches,
        repo_id=str(registry_repo.id) if registry_repo else None,
        has_rules=bool(registry_repo and (registry_repo.rules_md or "").strip()),
    )


@router.get("/deliverables")
async def get_project_deliverables(
    board_id: uuid.UUID,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """List all deliverables from tasks belonging to this project."""
    from app.models.deliverable import TaskDeliverable
    from app.models.task import Task as TaskModel

    # Get all task IDs for this project
    task_q = select(TaskModel.id).where(
        TaskModel.project_id == project_id,
        TaskModel.board_id == board_id,
    )
    task_ids = (await session.exec(task_q)).all()

    if not task_ids:
        return []

    # Get deliverables for those tasks
    deliv_q = (
        select(TaskDeliverable)
        .where(TaskDeliverable.task_id.in_(task_ids))
        .order_by(TaskDeliverable.created_at.desc())
    )
    deliverables = (await session.exec(deliv_q)).all()
    return deliverables
