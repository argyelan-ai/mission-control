"""First-run starter API (onboarding W3, PR 3-1) — services/first_run_starter.py.

``POST /api/v1/first-run/starter`` is the one call the setup wizard makes after
the operator picked an AI: lead agent + practice repo + first job. It works
today without the wizard, too (curl, a script). ``GET`` returns what an
earlier call created, for the wizard's progress chain.

Admin only: it creates an agent bound to a provider and writes into the repo
registry. Refusals come as ``{"detail": {"code", "message"[, "reason"]}}`` so the wizard
can word them in the UI language.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.database import get_session
from app.routers.agents import _auto_provision_cli_bridge as provision_new_agent
from app.services import first_run_starter as starter

router = APIRouter(prefix="/api/v1/first-run", tags=["first-run"])


class StarterRequest(BaseModel):
    # The wizard's "Which AI?" choice: a runtime UUID or slug. Empty = the
    # registry's lead runtime (role tag "lead").
    runtime_id: str | None = Field(default=None, max_length=128)
    # The operator's UI language — the agent talks to them in it (SOUL.md).
    language: str = Field(default="en", pattern=r"^[a-z]{2}(-[A-Za-z]{2})?$")


def _refusal(err: starter.StarterError) -> HTTPException:
    detail = {"code": err.code, "message": err.message}
    if err.reason:
        detail["reason"] = err.reason
    return HTTPException(status_code=err.status, detail=detail)


@router.post("/starter")
async def start_first_run(
    payload: StarterRequest,
    response: Response,
    background_tasks: BackgroundTasks,
    _user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """Create — or finish creating — the lead agent, the practice repo and the
    first job. 201 when something was created, 200 when everything existed."""
    try:
        result = await starter.start(
            session, runtime_ref=payload.runtime_id, language=payload.language,
        )
    except starter.StarterError as err:
        raise _refusal(err)
    if result.new_agent_token:
        # Same one-click chain as POST /agents with agent_runtime=cli-bridge:
        # host helper renders the files, then compose + container start. A
        # missing helper ends in an honest provision_failed event.
        background_tasks.add_task(provision_new_agent, result.agent.id, result.new_agent_token)
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return starter.describe(result)


@router.get("/starter")
async def get_first_run_starter(
    _user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """What the first run created, with live statuses (agent online? job running?)."""
    result = await starter.current(session)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_started", "message": "The first run has not been started."},
        )
    return starter.describe(result)


__all__ = ["router", "provision_new_agent"]
