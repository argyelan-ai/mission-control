import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_role, require_user
from app.database import get_session
from app.models.workflow import WorkflowRun, WorkflowStepRun, WorkflowTemplate
from app.services.scheduler import scheduler
from app.services.sse import make_sse_response
from app.services.workflow_service import workflow_service
from app.services.workflow_validator import WorkflowValidationError
from app.redis_client import RedisKeys

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


class WorkflowStepDefinition(BaseModel):
    model_config = ConfigDict(extra="allow")

    key: str
    name: str
    step_type: Literal["llm", "deterministic", "local"]
    execution_mode: Literal["single", "swarm"] = "single"
    input_template: str | None = ""
    timeout_seconds: int = Field(default=300, ge=5, le=7200)
    on_error: Literal["abort", "retry", "skip"] = "abort"
    retry_max_attempts: int = Field(default=0, ge=0, le=5)
    retry_delay_seconds: int = Field(default=0, ge=0, le=3600)
    retry_backoff: Literal["linear", "exponential"] = "linear"
    output_type: Literal["text", "json"] = "text"
    executor_type: str | None = None
    executor_config: dict[str, Any] | None = None
    agent_id: uuid.UUID | None = None
    skill_key: str | None = None
    evaluation_contract: dict[str, Any] | None = None


class WorkflowDefinition(BaseModel):
    steps: list[WorkflowStepDefinition] = Field(default_factory=list)


class WorkflowCreate(BaseModel):
    name: str
    description: str | None = None
    board_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    trigger_type: Literal["manual", "scheduled", "event"] = "manual"
    trigger_config: dict[str, Any] | None = None
    status: Literal["draft", "validated", "active", "archived"] = "draft"
    current_definition: WorkflowDefinition = Field(default_factory=WorkflowDefinition)
    max_runtime_minutes: int = Field(default=60, ge=1, le=1440)
    policy_profile: str = "safe"
    execution_policy: dict[str, Any] | None = None
    delivery_config: dict[str, Any] | None = None
    reflect_on: str = "manual"
    change_reason: str | None = None


class WorkflowUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    board_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    trigger_type: Literal["manual", "scheduled", "event"] | None = None
    trigger_config: dict[str, Any] | None = None
    status: Literal["draft", "validated", "active", "archived"] | None = None
    current_definition: WorkflowDefinition | None = None
    max_runtime_minutes: int | None = Field(default=None, ge=1, le=1440)
    policy_profile: str | None = None
    execution_policy: dict[str, Any] | None = None
    delivery_config: dict[str, Any] | None = None
    reflect_on: str | None = None
    change_reason: str | None = None


class WorkflowRunRequest(BaseModel):
    trigger_payload: dict[str, Any] | None = None


class WorkflowVersionCreate(BaseModel):
    change_reason: str | None = None


@router.get("/stream")
async def workflows_stream(current_user=Depends(require_user)):
    return make_sse_response([RedisKeys.workflow_events()])


@router.get("", dependencies=[Depends(require_role("viewer"))])
async def list_workflows(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    workflows = await workflow_service.list_workflows(session)
    return [workflow.model_dump() for workflow in workflows]


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_role("operator"))])
async def create_workflow(
    payload: WorkflowCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    try:
        workflow = await workflow_service.create_workflow(
            session,
            _serialize_payload(payload),
            created_by=str(current_user.id),
        )
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _sync_workflow_schedule(workflow)
    return workflow.model_dump()


@router.get("/{workflow_id}", dependencies=[Depends(require_role("viewer"))])
async def get_workflow(
    workflow_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    workflow = await workflow_service.get_workflow(session, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow.model_dump()


@router.patch("/{workflow_id}", dependencies=[Depends(require_role("operator"))])
async def update_workflow(
    workflow_id: uuid.UUID,
    payload: WorkflowUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    workflow = await workflow_service.get_workflow(session, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    try:
        updated = await workflow_service.update_workflow(
            session,
            workflow,
            _serialize_payload(payload, partial=True),
            updated_by=str(current_user.id),
        )
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _sync_workflow_schedule(updated)
    return updated.model_dump()


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_role("operator"))])
async def archive_workflow(
    workflow_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    workflow = await workflow_service.get_workflow(session, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    await workflow_service.archive_workflow(session, workflow)
    _sync_workflow_schedule(workflow)


@router.get("/{workflow_id}/versions", dependencies=[Depends(require_role("viewer"))])
async def get_workflow_versions(
    workflow_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    workflow = await workflow_service.get_workflow(session, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    versions = await workflow_service.get_versions(session, workflow_id)
    return [version.model_dump() for version in versions]


@router.post("/{workflow_id}/versions", dependencies=[Depends(require_role("operator"))])
async def create_workflow_version(
    workflow_id: uuid.UUID,
    payload: WorkflowVersionCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    workflow = await workflow_service.get_workflow(session, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        version = await workflow_service.create_version(
            session,
            workflow,
            created_by=str(current_user.id),
            change_reason=payload.change_reason,
        )
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return version.model_dump()


@router.delete("/{workflow_id}/versions/{version_number}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_role("operator"))])
async def delete_workflow_version(
    workflow_id: uuid.UUID,
    version_number: int,
    session: AsyncSession = Depends(get_session),
) -> None:
    workflow = await workflow_service.get_workflow(session, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        await workflow_service.delete_version(session, workflow, version_number)
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{workflow_id}/rollback/{version_number}", dependencies=[Depends(require_role("operator"))])
async def rollback_workflow(
    workflow_id: uuid.UUID,
    version_number: int,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
) -> dict[str, Any]:
    workflow = await workflow_service.get_workflow(session, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        updated = await workflow_service.rollback_to_version(
            session,
            workflow,
            version_number,
            updated_by=str(current_user.id),
        )
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _sync_workflow_schedule(updated)
    return updated.model_dump()


@router.post("/{workflow_id}/run", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_role("operator"))])
async def start_workflow_run(
    workflow_id: uuid.UUID,
    payload: WorkflowRunRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    workflow = await workflow_service.get_workflow(session, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        run = await workflow_service.start_run(
            session,
            workflow,
            triggered_by="user",
            trigger_payload=payload.trigger_payload,
        )
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return run.model_dump()


@router.get("/{workflow_id}/runs", dependencies=[Depends(require_role("viewer"))])
async def get_workflow_runs(
    workflow_id: uuid.UUID,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    workflow = await workflow_service.get_workflow(session, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    result = await session.exec(
        select(WorkflowRun)
        .where(WorkflowRun.workflow_id == workflow_id)
        .order_by(WorkflowRun.started_at.desc())
        .limit(limit)
    )
    return [run.model_dump() for run in result.all()]


@router.get("/runs/{run_id}", dependencies=[Depends(require_role("viewer"))])
async def get_workflow_run_detail(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    run = await session.get(WorkflowRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Workflow run not found")

    result = await session.exec(
        select(WorkflowStepRun)
        .where(WorkflowStepRun.run_id == run_id)
        .order_by(WorkflowStepRun.step_index.asc())
    )
    return {
        "run": run.model_dump(),
        "steps": [step.model_dump() for step in result.all()],
    }


@router.post("/runs/{run_id}/pause", dependencies=[Depends(require_role("operator"))])
async def pause_workflow_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    run = await session.get(WorkflowRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    if run.status != "running":
        raise HTTPException(status_code=409, detail="Only running runs can be paused")
    await workflow_service.signal_run(run_id, "pause")
    return {"status": "signal_sent", "signal": "pause", "run_id": str(run_id)}


@router.post("/runs/{run_id}/resume", dependencies=[Depends(require_role("operator"))])
async def resume_workflow_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    run = await session.get(WorkflowRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    try:
        run = await workflow_service.resume_run(session, run)
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run.model_dump()


@router.post("/runs/{run_id}/stop", dependencies=[Depends(require_role("operator"))])
async def stop_workflow_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    run = await session.get(WorkflowRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    if run.status not in {"running", "paused"}:
        raise HTTPException(status_code=409, detail="Run is not active")
    await workflow_service.signal_run(run_id, "stop")
    return {"status": "signal_sent", "signal": "stop", "run_id": str(run_id)}


def _serialize_payload(payload: BaseModel, partial: bool = False) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=partial)
    if "current_definition" in data and isinstance(data["current_definition"], dict):
        data["current_definition"] = data["current_definition"]
    return data


def _sync_workflow_schedule(workflow: WorkflowTemplate) -> None:
    if workflow.status == "active" and workflow.trigger_type == "scheduled":
        scheduler.register_workflow(workflow)
        return
    scheduler.unregister_workflow(str(workflow.id))
