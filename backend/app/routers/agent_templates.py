import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.agent import Agent
from app.models.agent_template import AgentTemplate
from app.services.provisioning import provision_agent_background as _provision_agent_background
from app.utils import utcnow

router = APIRouter(prefix="/api/v1", tags=["agent-templates"])


class TemplateCreate(BaseModel):
    name: str
    emoji: str = "🤖"
    role: str | None = None
    default_model: str | None = None
    soul_md: str | None = None
    skills: list[str] = []
    scopes: list[str] = []


class TemplateUpdate(BaseModel):
    name: str | None = None
    emoji: str | None = None
    role: str | None = None
    default_model: str | None = None
    soul_md: str | None = None
    skills: list[str] | None = None
    scopes: list[str] | None = None


class InstantiateRequest(BaseModel):
    board_id: uuid.UUID
    model: str | None = None   # überschreibt template.default_model
    name: str | None = None    # überschreibt template.name


@router.get("/agent-templates")
async def list_templates(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    result = await session.exec(select(AgentTemplate).order_by(AgentTemplate.name))
    return result.all()


@router.post("/agent-templates", status_code=status.HTTP_201_CREATED)
async def create_template(
    payload: TemplateCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    template = AgentTemplate(
        name=payload.name,
        emoji=payload.emoji,
        role=payload.role,
        default_model=payload.default_model,
        soul_md=payload.soul_md,
        skills=payload.skills,
        scopes=payload.scopes,
        is_builtin=False,
    )
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


@router.patch("/agent-templates/{template_id}")
async def update_template(
    template_id: uuid.UUID,
    payload: TemplateUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    template = await session.get(AgentTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(template, k, v)
    template.updated_at = utcnow()
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


@router.delete("/agent-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    template = await session.get(AgentTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    if template.is_builtin:
        raise HTTPException(status_code=409, detail="Builtin templates cannot be deleted")
    await session.delete(template)
    await session.commit()


async def _do_instantiate(
    template: AgentTemplate,
    board_id: uuid.UUID | None,
    name: str | None,
    model: str | None,
    session: AsyncSession,
    is_board_lead: bool = False,
) -> tuple[Agent, str]:
    """
    Erstellt einen Agent aus einem Template und gibt (agent, raw_token) zurueck.
    Wird von user-auth und agent-auth Endpoints gemeinsam genutzt.
    """
    from app.auth import generate_agent_token
    from app.routers.agents import _generate_tools_md
    from app.scopes import get_default_scopes

    agent_name = name or template.name
    agent_model = model or template.default_model
    board_id_str = str(board_id) if board_id else None

    # Scopes: Template > Default-Lookup > leer
    agent_scopes = list(template.scopes or []) if template.scopes else get_default_scopes(template.name)

    raw_token, token_hash = generate_agent_token()
    tools_md = _generate_tools_md(
        agent_name, template.emoji, raw_token, board_id_str,
        is_board_lead=is_board_lead, scopes=agent_scopes,
    )

    agent = Agent(
        board_id=board_id,
        name=agent_name,
        emoji=template.emoji,
        role=template.role,
        model=agent_model,
        soul_md=template.soul_md,
        skills=list(template.skills or []),
        skill_filter=template.skill_filter,
        scopes=agent_scopes,
        tools_md=tools_md,
        agent_token_hash=token_hash,
        template_id=template.id,
        provision_status="local",
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)

    # Vault-Write mc_token_{slug} — /internal/bootstrap liefert den Token an
    # den Container. Ohne diesen Write crash-loopt ein frisch instantiierter
    # Agent mit 'MC_TOKEN is not set' (Fresh-Install-Fix 2026-07-02).
    from app.services.secrets_helper import upsert_agent_token_secret
    await upsert_agent_token_secret(session, agent.name, raw_token)

    return agent, raw_token


@router.post("/agent-templates/{template_id}/instantiate", status_code=status.HTTP_201_CREATED)
async def instantiate_template(
    template_id: uuid.UUID,
    body: InstantiateRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """
    Erstellt einen Agent aus einem Template.
    Modell-Priorität: body.model > template.default_model > None
    Rückgabe: { agent, token } — Token einmalig, nicht wieder abrufbar!
    """
    template = await session.get(AgentTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    agent, raw_token = await _do_instantiate(
        template=template,
        board_id=body.board_id,
        name=body.name,
        model=body.model,
        session=session,
    )

    background_tasks.add_task(_provision_agent_background, agent.id)
    return {
        "agent": agent,
        "token": raw_token,  # einmalig — danach nicht mehr abrufbar
    }
