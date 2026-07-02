"""Projects router — ProjectPhase CRUD und Projekt-Kontext.

Endpunkte:
  GET  /projects/{project_id}                           — Projekt + Phasen
  GET  /projects/{project_id}/phases                    — Phasen auflisten
  POST /projects/{project_id}/phases                    — Phase erstellen
  PATCH /projects/{project_id}/phases/{phase_id}        — Phase updaten
  DELETE /projects/{project_id}/phases/{phase_id}       — Phase löschen
  POST /projects/{project_id}/phases/{phase_id}/complete — Phase abschliessen
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.board import Project
from app.models.project_phase import ProjectPhase

router = APIRouter(prefix="/api/v1", tags=["projects"])

logger = logging.getLogger("mc.projects")


class PhaseCreate(BaseModel):
    title: str
    order: int = 0
    depends_on_phases: list[str] | None = None
    gate_required: bool = False
    failure_policy: str = "retry"
    default_agent_id: uuid.UUID | None = None
    git_branch: str | None = None


class PhaseUpdate(BaseModel):
    title: str | None = None
    order: int | None = None
    status: str | None = None
    depends_on_phases: list[str] | None = None
    gate_required: bool | None = None
    failure_policy: str | None = None
    default_agent_id: uuid.UUID | None = None
    git_branch: str | None = None


@router.get("/projects/{project_id}")
async def get_project_with_phases(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Projekt + alle Phasen zurückgeben."""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    phases_result = await session.exec(
        select(ProjectPhase)
        .where(ProjectPhase.project_id == project_id)
        .order_by(ProjectPhase.order)
    )
    phases = phases_result.all()

    return {**project.model_dump(), "phases": [p.model_dump() for p in phases]}


@router.get("/projects/{project_id}/phases")
async def list_phases(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    result = await session.exec(
        select(ProjectPhase)
        .where(ProjectPhase.project_id == project_id)
        .order_by(ProjectPhase.order)
    )
    return result.all()


@router.post("/projects/{project_id}/phases", status_code=status.HTTP_201_CREATED)
async def create_phase(
    project_id: uuid.UUID,
    payload: PhaseCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    phase = ProjectPhase(
        project_id=project_id,
        title=payload.title,
        order=payload.order,
        depends_on_phases=payload.depends_on_phases,
        gate_required=payload.gate_required,
        failure_policy=payload.failure_policy,
        default_agent_id=payload.default_agent_id,
        git_branch=payload.git_branch or f"phase/{payload.title.lower().replace(' ', '-')}",
    )
    session.add(phase)
    await session.commit()
    await session.refresh(phase)
    return phase


@router.patch("/projects/{project_id}/phases/{phase_id}")
async def update_phase(
    project_id: uuid.UUID,
    phase_id: uuid.UUID,
    payload: PhaseUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    phase = await session.get(ProjectPhase, phase_id)
    if not phase or phase.project_id != project_id:
        raise HTTPException(status_code=404, detail="Phase not found")

    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(phase, k, v)
    session.add(phase)
    await session.commit()
    await session.refresh(phase)
    return phase


@router.delete("/projects/{project_id}/phases/{phase_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_phase(
    project_id: uuid.UUID,
    phase_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    phase = await session.get(ProjectPhase, phase_id)
    if not phase or phase.project_id != project_id:
        raise HTTPException(status_code=404, detail="Phase not found")
    await session.delete(phase)
    await session.commit()


@router.post("/projects/{project_id}/phases/{phase_id}/complete")
async def complete_phase(
    project_id: uuid.UUID,
    phase_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Phase als abgeschlossen markieren.

    1. Phase status → completed, completed_at gesetzt
    2. Wenn Projekt ein Git-Repo hat: Phase-PR öffnen + Git-Tag
    3. Alle Phasen des Projekts prüfen: können neue aktiviert werden?
    4. project.last_active_phase_id + briefing_doc updaten
    """
    from datetime import datetime, timezone
    from app.services.phase_engine import can_activate_phase
    from app.services.git_service import GitService

    phase = await session.get(ProjectPhase, phase_id)
    if not phase or phase.project_id != project_id:
        raise HTTPException(status_code=404, detail="Phase not found")

    if phase.status == "completed":
        raise HTTPException(status_code=409, detail="Phase ist bereits abgeschlossen")

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # 1. Phase abschliessen
    phase.status = "completed"
    phase.completed_at = datetime.now(timezone.utc)
    session.add(phase)

    # 2. Git: PR + Tag (nur wenn workspace_path und Repo vorhanden)
    pr_url: str | None = None
    if project.workspace_path and project.github_repo_url:
        try:
            git = GitService()
            phase_slug = (phase.git_branch or f"phase/{phase.title}").replace("phase/", "")
            pr_url = await git.create_phase_pr(
                project_dir=project.workspace_path,
                phase_slug=phase_slug,
                title=f"Phase complete: {phase.title}",
            )
            tag_name = f"project/{project.name}/phase-{phase.order}-done"
            await git.create_git_tag(project.workspace_path, tag_name)
        except Exception as e:
            logger.warning("Git-Operationen bei Phase-Abschluss fehlgeschlagen: %s", e)

    # 3. Nächste Phasen aktivieren
    all_phases_result = await session.exec(
        select(ProjectPhase).where(ProjectPhase.project_id == project_id)
    )
    all_phases = all_phases_result.all()

    completed_ids = {
        str(p.id) for p in all_phases if p.status == "completed" or p.id == phase_id
    }

    activated: list[str] = []
    for p in all_phases:
        if p.status == "pending" and can_activate_phase(p, completed_ids):
            if not p.gate_required:
                p.status = "active"
                session.add(p)
                activated.append(str(p.id))
            else:
                p.status = "awaiting_approval"
                session.add(p)

    # 4. Projekt updaten
    project.last_active_phase_id = phase_id
    briefing_update = f"\n\n## Phase abgeschlossen: {phase.title}\n\nDatum: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n"
    if pr_url:
        briefing_update += f"PR: {pr_url}\n"
    if activated:
        briefing_update += f"Aktiviert: {len(activated)} neue Phase(n)\n"
    project.briefing_doc = (project.briefing_doc or "") + briefing_update
    session.add(project)

    await session.commit()

    return {
        "phase_id": str(phase_id),
        "status": "completed",
        "pr_url": pr_url,
        "activated_phases": activated,
    }
