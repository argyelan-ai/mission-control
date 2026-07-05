import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from sqlalchemy import delete, update

from app.auth import require_user
from app.database import get_session
from app.models.board import Board, BoardGroup, Project, PlannerMessage
from app.models.task import Task, TaskComment, TaskDependency
from app.utils import utcnow

router = APIRouter(prefix="/api/v1", tags=["boards"])


# ── Board Groups ─────────────────────────────────────────────────────────────

class BoardGroupCreate(BaseModel):
    name: str
    slug: str
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    sort_order: int = 0


class BoardGroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    sort_order: int | None = None


@router.get("/board-groups")
async def list_board_groups(
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    result = await session.exec(select(BoardGroup).order_by(BoardGroup.sort_order))
    return result.all()


@router.post("/board-groups", status_code=status.HTTP_201_CREATED)
async def create_board_group(
    payload: BoardGroupCreate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    group = BoardGroup(**payload.model_dump())
    session.add(group)
    await session.commit()
    await session.refresh(group)
    return group


@router.patch("/board-groups/{group_id}")
async def update_board_group(
    group_id: uuid.UUID,
    payload: BoardGroupUpdate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    group = await session.get(BoardGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Board group not found")
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(group, k, v)
    group.updated_at = utcnow()
    session.add(group)
    await session.commit()
    await session.refresh(group)
    return group


@router.delete("/board-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_board_group(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    group = await session.get(BoardGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Board group not found")
    await session.delete(group)
    await session.commit()


# ── Boards ───────────────────────────────────────────────────────────────────

class BoardCreate(BaseModel):
    # Phase 30: `gateway_id` removed — Gateway model is dropped in Plan 30-02.
    name: str
    slug: str
    board_group_id: uuid.UUID | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    objective: str | None = None
    require_approval_for_done: bool = False
    require_review_before_done: bool = False
    only_lead_can_change_status: bool = False
    auto_dispatch_enabled: bool = False


class BoardUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    objective: str | None = None
    require_approval_for_done: bool | None = None
    require_review_before_done: bool | None = None
    only_lead_can_change_status: bool | None = None
    auto_dispatch_enabled: bool | None = None
    is_archived: bool | None = None


@router.get("/boards")
async def list_boards(
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    result = await session.exec(
        select(Board).where(Board.is_archived == False).order_by(Board.sort_order)  # noqa: E712
    )
    return result.all()


@router.post("/boards", status_code=status.HTTP_201_CREATED)
async def create_board(
    payload: BoardCreate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    board = Board(**payload.model_dump())
    session.add(board)
    await session.commit()
    await session.refresh(board)
    return board


@router.get("/boards/{board_id}")
async def get_board(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    board = await session.get(Board, board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Board not found")
    return board


@router.patch("/boards/{board_id}")
async def update_board(
    board_id: uuid.UUID,
    payload: BoardUpdate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    board = await session.get(Board, board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Board not found")
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(board, k, v)
    board.updated_at = utcnow()
    session.add(board)
    await session.commit()
    await session.refresh(board)
    return board


@router.delete("/boards/{board_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_board(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    board = await session.get(Board, board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Board not found")
    board.is_archived = True
    # Free up the slug: archiving is a soft delete, but boards.slug is
    # UNIQUE — without renaming, a deleted board blocks its slug forever
    # (re-creation -> 500 UniqueViolation).
    if "--archived-" not in board.slug:
        board.slug = f"{board.slug}--archived-{board.id.hex[:8]}"
    board.updated_at = utcnow()
    session.add(board)
    await session.commit()


@router.get("/boards/{board_id}/snapshot")
async def board_snapshot(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Full board snapshot for agents: board + agents + active tasks + memory."""
    from app.models.agent import Agent
    from app.models.memory import BoardMemory
    from app.models.task import Task

    board = await session.get(Board, board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Board not found")

    agents = (await session.exec(select(Agent).where(Agent.board_id == board_id))).all()
    tasks = (
        await session.exec(
            select(Task).where(Task.board_id == board_id, Task.status != "done")
        )
    ).all()
    memory = (
        await session.exec(
            select(BoardMemory).where(BoardMemory.board_id == board_id).limit(50)
        )
    ).all()

    return {
        "board": board,
        "agents": agents,
        "tasks": tasks,
        "memory": memory,
    }


# ── Projects ─────────────────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    name: str
    description: str | None = None
    status: str = "draft"
    priority: str = "medium"
    workspace_path: str | None = None
    project_type: str = "feature"


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    plan_summary: str | None = None
    progress_pct: int | None = None
    workspace_path: str | None = None


@router.get("/boards/{board_id}/projects")
async def list_projects(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    # Research and planner projects are managed via their own routers
    # and should not appear in the general project list (e.g. sidebar).
    HIDDEN_TYPES = ["research"]
    result = await session.exec(
        select(Project)
        .where(Project.board_id == board_id, Project.project_type.notin_(HIDDEN_TYPES))  # type: ignore[attr-defined]
        .order_by(Project.created_at.desc())  # type: ignore[attr-defined]
    )
    return result.all()


@router.post("/boards/{board_id}/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    board_id: uuid.UUID,
    payload: ProjectCreate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    board = await session.get(Board, board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Board not found")
    project = Project(board_id=board_id, **payload.model_dump())
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


@router.get("/boards/{board_id}/projects/{project_id}")
async def get_project(
    board_id: uuid.UUID,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    project = await session.get(Project, project_id)
    if not project or project.board_id != board_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.patch("/boards/{board_id}/projects/{project_id}")
async def update_project(
    board_id: uuid.UUID,
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    project = await session.get(Project, project_id)
    if not project or project.board_id != board_id:
        raise HTTPException(status_code=404, detail="Project not found")
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(project, k, v)
    project.updated_at = utcnow()
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


@router.post("/boards/{board_id}/projects/{project_id}/init-repo")
async def init_project_repo(
    board_id: uuid.UUID,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Initialize a GitHub repo for a project.

    Creates {GITHUB_OWNER}/mc-{slug} (always private), initializes it with briefing.md,
    and stores github_repo_url + github_repo_name on the project.
    """
    from app.services.git_service import GitService, slugify_project

    project = await session.get(Project, project_id)
    if not project or project.board_id != board_id:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.github_repo_url:
        raise HTTPException(
            status_code=409,
            detail=f"Projekt hat bereits ein Repo: {project.github_repo_url}",
        )

    slug = slugify_project(project.name)
    git = GitService()
    try:
        clone_url = await git.create_project_repo(slug, description=project.description or "")
    except RuntimeError as e:
        if "GITHUB_OWNER" in str(e):
            raise HTTPException(status_code=400, detail=str(e))
        raise

    # Register in the repos registry (ADR-050) + link — apply_repo_link
    # syncs the legacy github_repo_* fields with the canonical owner/name.
    from app.services.github_config import require_github_owner
    from app.services.repo_registry import apply_repo_link, upsert_repo

    full_name = f"{await require_github_owner(session)}/mc-{slug}"
    repo = await upsert_repo(
        session,
        full_name=full_name,
        url=clone_url,
        description=project.description,
        source="mc",
    )
    await session.flush()  # ensure repo.id is assigned before linking
    apply_repo_link(project, repo)
    session.add(project)
    await session.commit()
    await session.refresh(project)

    return {
        "github_repo_url": project.github_repo_url,
        "github_repo_name": project.github_repo_name,
        "repo_id": str(project.repo_id),
    }


@router.delete("/boards/{board_id}/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    board_id: uuid.UUID,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    project = await session.get(Project, project_id)
    if not project or project.board_id != board_id:
        raise HTTPException(status_code=404, detail="Project not found")

    # Delete planner messages
    await session.exec(  # type: ignore[call-overload]
        delete(PlannerMessage).where(PlannerMessage.project_id == project_id)
    )

    # Referenz-Dateien (ADR-053): Projekt-Referenzen (Rows + Dateien) mitlöschen.
    from app.services.reference_cleanup import delete_references_for
    await delete_references_for(session, project_id=project_id)

    # Collect task IDs for cascading delete
    task_ids_result = await session.exec(
        select(Task.id).where(Task.project_id == project_id)
    )
    task_ids = list(task_ids_result.all())

    # Task-Referenzen der Projekt-Tasks (ADR-053) — die Tasks selbst werden
    # unten per Bulk-SQL gelöscht, ohne den Task-Delete-Endpoint zu passieren.
    from app.services.reference_cleanup import delete_references_for_tasks
    await delete_references_for_tasks(session, task_ids)

    if task_ids:
        # 1) Resolve parent references (self-FK)
        await session.exec(  # type: ignore[call-overload]
            update(Task)
            .where(Task.parent_task_id.in_(task_ids))  # type: ignore[union-attr]
            .values(parent_task_id=None)
        )
        # 2) Delete comments
        await session.exec(  # type: ignore[call-overload]
            delete(TaskComment).where(TaskComment.task_id.in_(task_ids))  # type: ignore[union-attr]
        )
        # 3) Delete dependencies
        await session.exec(  # type: ignore[call-overload]
            delete(TaskDependency).where(
                TaskDependency.task_id.in_(task_ids)  # type: ignore[union-attr]
                | TaskDependency.depends_on_task_id.in_(task_ids)  # type: ignore[union-attr]
            )
        )
        # 4) Delete tasks
        await session.exec(  # type: ignore[call-overload]
            delete(Task).where(Task.project_id == project_id)
        )

    await session.delete(project)
    await session.commit()
