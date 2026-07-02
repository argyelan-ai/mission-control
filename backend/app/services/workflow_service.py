from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.database import engine
from app.models.agent import Agent
from app.models.workflow import (
    WorkflowRun,
    WorkflowStepRun,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.services.sse import broadcast
from app.services.workflow_delivery import deliver_run
from app.services.workflow_kinds import compile_guided_workflow_payload
from app.services.workflow_renderer import WorkflowRenderError, render_value
from app.services.workflow_validator import (
    WorkflowValidationError,
    validate_workflow_payload,
)
from app.utils import create_tracked_task, utcnow

logger = logging.getLogger("mc.workflows")

WORKFLOW_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts" / "workflows"
ACTIVE_RUN_STATUSES = {"running", "paused"}


class StepExecutionError(RuntimeError):
    def __init__(self, message: str, *, result: dict[str, Any] | None = None):
        super().__init__(message)
        self.result = result or {}


class WorkflowService:
    async def list_workflows(self, session: AsyncSession) -> list[WorkflowTemplate]:
        result = await session.exec(
            select(WorkflowTemplate).order_by(WorkflowTemplate.updated_at.desc())
        )
        return result.all()

    async def get_workflow(self, session: AsyncSession, workflow_id: uuid.UUID) -> WorkflowTemplate | None:
        return await session.get(WorkflowTemplate, workflow_id)

    async def create_workflow(
        self,
        session: AsyncSession,
        payload: dict[str, Any],
        *,
        created_by: str,
    ) -> WorkflowTemplate:
        payload = compile_guided_workflow_payload(payload)
        await validate_workflow_payload(
            session,
            board_id=payload.get("board_id"),
            project_id=payload.get("project_id"),
            name=payload["name"],
            trigger_type=payload.get("trigger_type", "manual"),
            trigger_config=payload.get("trigger_config"),
            status=payload.get("status", "draft"),
            current_definition=payload.get("current_definition"),
            delivery_config=payload.get("delivery_config"),
            strict=payload.get("status", "draft") in {"validated", "active"},
        )

        workflow = WorkflowTemplate(
            board_id=payload.get("board_id"),
            project_id=payload.get("project_id"),
            name=payload["name"].strip(),
            description=payload.get("description"),
            trigger_type=payload.get("trigger_type", "manual"),
            trigger_config=payload.get("trigger_config"),
            status=payload.get("status", "draft"),
            current_definition=payload.get("current_definition") or {"steps": []},
            max_runtime_minutes=payload.get("max_runtime_minutes", 60),
            policy_profile=payload.get("policy_profile", "safe"),
            execution_policy=payload.get("execution_policy"),
            delivery_config=payload.get("delivery_config"),
            reflect_on=payload.get("reflect_on", "manual"),
            created_by=created_by,
        )
        if workflow.status in {"validated", "active"}:
            workflow.last_validated_at = utcnow()

        session.add(workflow)
        await session.commit()
        await session.refresh(workflow)

        await self._store_version(
            session,
            workflow,
            version_number=1,
            change_reason=payload.get("change_reason"),
            created_by=created_by,
        )
        await session.refresh(workflow)
        return workflow

    async def update_workflow(
        self,
        session: AsyncSession,
        workflow: WorkflowTemplate,
        payload: dict[str, Any],
        *,
        updated_by: str,
    ) -> WorkflowTemplate:
        merged_payload = {
            "name": payload.get("name", workflow.name),
            "description": payload.get("description", workflow.description),
            "board_id": payload.get("board_id", workflow.board_id),
            "project_id": payload.get("project_id", workflow.project_id),
            "trigger_type": payload.get("trigger_type", workflow.trigger_type),
            "trigger_config": payload.get("trigger_config", workflow.trigger_config),
            "status": payload.get("status", workflow.status),
            "current_definition": payload.get("current_definition", workflow.current_definition),
            "max_runtime_minutes": payload.get("max_runtime_minutes", workflow.max_runtime_minutes),
            "policy_profile": payload.get("policy_profile", workflow.policy_profile),
            "execution_policy": payload.get("execution_policy", workflow.execution_policy),
            "delivery_config": payload.get("delivery_config", workflow.delivery_config),
            "reflect_on": payload.get("reflect_on", workflow.reflect_on),
            "change_reason": payload.get("change_reason"),
        }
        compiled_payload = compile_guided_workflow_payload(merged_payload)
        payload = {
            **payload,
            **{
                key: compiled_payload[key]
                for key in ("description", "current_definition", "execution_policy")
                if key in compiled_payload
            },
        }

        next_status = payload.get("status", workflow.status)
        next_board_id = payload.get("board_id", workflow.board_id)
        next_project_id = payload.get("project_id", workflow.project_id)
        next_definition = payload.get("current_definition", workflow.current_definition)
        next_trigger_type = payload.get("trigger_type", workflow.trigger_type)
        next_trigger_config = payload.get("trigger_config", workflow.trigger_config)
        next_delivery_config = payload.get("delivery_config", workflow.delivery_config)

        await validate_workflow_payload(
            session,
            board_id=next_board_id,
            project_id=next_project_id,
            name=payload.get("name", workflow.name),
            trigger_type=next_trigger_type,
            trigger_config=next_trigger_config,
            status=next_status,
            current_definition=next_definition,
            delivery_config=next_delivery_config,
            strict=next_status in {"validated", "active"},
        )

        for key, value in payload.items():
            if key == "change_reason":
                continue
            setattr(workflow, key, value)

        workflow.updated_at = utcnow()
        if workflow.status in {"validated", "active"}:
            workflow.last_validated_at = utcnow()

        session.add(workflow)
        await session.commit()
        await session.refresh(workflow)
        return workflow

    async def archive_workflow(self, session: AsyncSession, workflow: WorkflowTemplate) -> None:
        workflow.status = "archived"
        workflow.updated_at = utcnow()
        session.add(workflow)
        await session.commit()

    async def create_version(
        self,
        session: AsyncSession,
        workflow: WorkflowTemplate,
        *,
        created_by: str,
        change_reason: str | None = None,
    ) -> WorkflowTemplateVersion:
        version_number = await self._next_version_number(session, workflow.id)
        return await self._store_version(
            session,
            workflow,
            version_number=version_number,
            change_reason=change_reason,
            created_by=created_by,
        )

    async def get_versions(
        self,
        session: AsyncSession,
        workflow_id: uuid.UUID,
    ) -> list[WorkflowTemplateVersion]:
        result = await session.exec(
            select(WorkflowTemplateVersion)
            .where(WorkflowTemplateVersion.workflow_id == workflow_id)
            .order_by(WorkflowTemplateVersion.version.desc())
        )
        return result.all()

    async def rollback_to_version(
        self,
        session: AsyncSession,
        workflow: WorkflowTemplate,
        version_number: int,
        *,
        updated_by: str,
        change_reason: str | None = None,
    ) -> WorkflowTemplate:
        result = await session.exec(
            select(WorkflowTemplateVersion).where(
                WorkflowTemplateVersion.workflow_id == workflow.id,
                WorkflowTemplateVersion.version == version_number,
            )
        )
        version = result.first()
        if not version:
            raise WorkflowValidationError("Workflow version not found")

        snapshot = version.definition_snapshot or {}
        definition = snapshot.get("current_definition")
        if not isinstance(definition, dict):
            definition = {"steps": snapshot.get("steps", [])}
        board_id = uuid.UUID(snapshot["board_id"]) if snapshot.get("board_id") else None
        project_id = uuid.UUID(snapshot["project_id"]) if snapshot.get("project_id") else None
        status = snapshot.get("status", workflow.status)

        await validate_workflow_payload(
            session,
            board_id=board_id,
            project_id=project_id,
            name=snapshot.get("name", workflow.name),
            trigger_type=snapshot.get("trigger_type", workflow.trigger_type),
            trigger_config=snapshot.get("trigger_config"),
            status=status,
            current_definition=definition,
            delivery_config=snapshot.get("delivery_config"),
            strict=status in {"validated", "active"},
        )

        workflow.board_id = board_id
        workflow.project_id = project_id
        workflow.name = snapshot.get("name", workflow.name)
        workflow.description = snapshot.get("description")
        workflow.trigger_type = snapshot.get("trigger_type", workflow.trigger_type)
        workflow.trigger_config = snapshot.get("trigger_config")
        workflow.status = status
        workflow.current_definition = definition
        workflow.max_runtime_minutes = snapshot.get("max_runtime_minutes", workflow.max_runtime_minutes)
        workflow.policy_profile = snapshot.get("policy_profile", workflow.policy_profile)
        workflow.execution_policy = snapshot.get("execution_policy")
        workflow.delivery_config = snapshot.get("delivery_config")
        workflow.reflect_on = snapshot.get("reflect_on", workflow.reflect_on)
        workflow.current_version = version.version
        workflow.updated_at = utcnow()
        if workflow.status in {"validated", "active"}:
            workflow.last_validated_at = utcnow()
        session.add(workflow)
        await session.commit()
        await session.refresh(workflow)
        return workflow

    async def delete_version(
        self,
        session: AsyncSession,
        workflow: WorkflowTemplate,
        version_number: int,
    ) -> None:
        result = await session.exec(
            select(WorkflowTemplateVersion).where(
                WorkflowTemplateVersion.workflow_id == workflow.id,
                WorkflowTemplateVersion.version == version_number,
            )
        )
        version = result.first()
        if not version:
            raise WorkflowValidationError("Workflow version not found")
        if version.version == workflow.current_version:
            raise WorkflowValidationError("Cannot delete the current workflow version")

        await session.delete(version)
        await session.commit()

    async def start_run(
        self,
        session: AsyncSession,
        workflow: WorkflowTemplate,
        *,
        triggered_by: str,
        trigger_payload: dict[str, Any] | None = None,
    ) -> WorkflowRun:
        if workflow.status != "active":
            raise WorkflowValidationError("Only active workflows can be run")

        blocking_run = await session.exec(
            select(WorkflowRun.id).where(
                WorkflowRun.workflow_id == workflow.id,
                WorkflowRun.status.in_(ACTIVE_RUN_STATUSES),  # type: ignore[arg-type]
            )
        )
        if blocking_run.first():
            raise WorkflowValidationError("Another run of this workflow is still active")

        snapshot = self._build_snapshot(workflow)
        run = WorkflowRun(
            workflow_id=workflow.id,
            workflow_version=workflow.current_version,
            definition_snapshot=snapshot,
            triggered_by=triggered_by,
            trigger_payload=trigger_payload,
            status="running",
            current_step_key=None,
            context={"steps": {}},
            delivery_status="pending" if workflow.delivery_config else "skipped",
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        for idx, step in enumerate(snapshot.get("steps", [])):
            session.add(
                WorkflowStepRun(
                    run_id=run.id,
                    step_key=step["key"],
                    step_index=idx,
                    step_name=step["name"],
                    step_type=step["step_type"],
                    execution_mode=step.get("execution_mode", "single"),
                    executor_type=step.get("executor_type"),
                    status="pending",
                )
            )
        await session.commit()

        await self._emit_workflow_event(
            "workflow.run.started",
            {
                "workflow_id": str(workflow.id),
                "run_id": str(run.id),
                "workflow_name": workflow.name,
                "status": run.status,
            },
        )
        try:
            await emit_event(
                session,
                "workflow.run.started",
                f"Workflow gestartet: {workflow.name}",
                board_id=workflow.board_id,
                project_id=workflow.project_id,
                detail={"workflow_id": str(workflow.id), "run_id": str(run.id)},
            )
        except Exception as e:
            logger.warning("Workflow activity emit failed on start: %s", e)

        create_tracked_task(
            self.execute_run(run.id),
            name=f"workflow-run:{run.id}",
        )
        return run

    async def execute_run(self, run_id: uuid.UUID) -> None:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            run = await session.get(WorkflowRun, run_id)
            if not run or run.status not in {"running", "paused"}:
                return
            workflow = await session.get(WorkflowTemplate, run.workflow_id)
            if not workflow:
                return
            if run.status == "paused":
                run.status = "running"
                session.add(run)
                await session.commit()

        snapshot = run.definition_snapshot or {}
        steps = snapshot.get("steps", [])
        for step_index, step in enumerate(steps):
            async with AsyncSession(engine, expire_on_commit=False) as session:
                run = await session.get(WorkflowRun, run_id)
                workflow = await session.get(WorkflowTemplate, run.workflow_id) if run else None
                if not run or not workflow:
                    return
                if run.status != "running":
                    return

                step_run = await self._get_step_run(session, run.id, step["key"])
                if not step_run or step_run.status == "done":
                    continue
                if step_run.status == "skipped":
                    continue

                run.current_step_key = step["key"]
                step_run.status = "running"
                step_run.started_at = utcnow()
                session.add(run)
                session.add(step_run)
                await session.commit()

            try:
                await self._execute_step(run_id, step_index, step)
            except StepExecutionError:
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    run = await session.get(WorkflowRun, run_id)
                    if run and run.status == "running":
                        run.status = "failed"
                        run.completed_at = utcnow()
                        session.add(run)
                        await session.commit()
                break

            signal = await self._read_signal(run_id)
            if signal == "pause":
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    run = await session.get(WorkflowRun, run_id)
                    if run:
                        run.status = "paused"
                        session.add(run)
                        await session.commit()
                await self._emit_workflow_event(
                    "workflow.run.paused",
                    {"run_id": str(run_id)},
                )
                return
            if signal in {"stop", "force_stop"}:
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    run = await session.get(WorkflowRun, run_id)
                    if run:
                        run.status = "force_stopped" if signal == "force_stop" else "stopped"
                        run.completed_at = utcnow()
                        session.add(run)
                        await session.commit()
                await self._clear_signal(run_id)
                await self._emit_workflow_event(
                    "workflow.run.stopped",
                    {"run_id": str(run_id), "signal": signal},
                )
                return

        async with AsyncSession(engine, expire_on_commit=False) as session:
            run = await session.get(WorkflowRun, run_id)
            workflow = await session.get(WorkflowTemplate, run.workflow_id) if run else None
            if not run or not workflow:
                return

            if run.status == "running":
                step_runs = await self._get_step_runs(session, run.id)
                if any(step.status == "failed" for step in step_runs):
                    run.status = "failed"
                elif any(step.status == "skipped" for step in step_runs):
                    run.status = "partial"
                else:
                    run.status = "completed"
                run.completed_at = utcnow()

                delivery_status, delivery_error = await deliver_run(session, workflow, run)
                run.delivery_status = delivery_status
                run.delivery_error = delivery_error
                if delivery_status == "sent":
                    run.delivered_at = utcnow()
                session.add(run)
                await session.commit()

            try:
                await emit_event(
                    session,
                    "workflow.run.completed" if run.status in {"completed", "partial"} else "workflow.run.failed",
                    f"Workflow beendet: {workflow.name} ({run.status})",
                    board_id=workflow.board_id,
                    project_id=workflow.project_id,
                    severity="info" if run.status in {"completed", "partial"} else "warning",
                    detail={"workflow_id": str(workflow.id), "run_id": str(run.id), "status": run.status},
                )
            except Exception as e:
                logger.warning("Workflow activity emit failed on completion: %s", e)
            await self._emit_workflow_event(
                f"workflow.run.{run.status}",
                {
                    "workflow_id": str(workflow.id),
                    "run_id": str(run.id),
                    "workflow_name": workflow.name,
                    "status": run.status,
                    "delivery_status": run.delivery_status,
                },
            )
            await self._clear_signal(run.id)

    async def resume_run(self, session: AsyncSession, run: WorkflowRun) -> WorkflowRun:
        if run.status != "paused":
            raise WorkflowValidationError("Only paused runs can be resumed")
        run.status = "running"
        session.add(run)
        await session.commit()
        await self._clear_signal(run.id)
        create_tracked_task(self.execute_run(run.id), name=f"workflow-run:{run.id}:resume")
        return run

    async def signal_run(self, run_id: uuid.UUID, signal: str) -> None:
        redis = await get_redis()
        await redis.set(RedisKeys.workflow_run_signal(str(run_id)), signal, ex=3600)

    async def _execute_step(self, run_id: uuid.UUID, step_index: int, step: dict[str, Any]) -> None:
        key = step["key"]
        max_attempts = max(1, int(step.get("retry_max_attempts", 0)) + 1)
        on_error = step.get("on_error", "abort")
        retry_delay = int(step.get("retry_delay_seconds", 0) or 0)

        for attempt in range(1, max_attempts + 1):
            try:
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    run = await session.get(WorkflowRun, run_id)
                    step_run = await self._get_step_run(session, run_id, key)
                    if not run or not step_run:
                        return
                    step_run.attempt = attempt

                    rendered_input = await render_value(
                        session,
                        step.get("input_template", ""),
                        workflow_snapshot=run.definition_snapshot,
                        run={"id": str(run.id)},
                        context=run.context or {"steps": {}},
                    )
                    rendered_step = dict(step)
                    if step.get("executor_config") is not None:
                        rendered_step["executor_config"] = await render_value(
                            session,
                            step.get("executor_config"),
                            workflow_snapshot=run.definition_snapshot,
                            run={"id": str(run.id)},
                            context=run.context or {"steps": {}},
                        )
                    step_run.rendered_input = (
                        rendered_input if isinstance(rendered_input, str) else json.dumps(rendered_input)
                    )
                    session.add(step_run)
                    await session.commit()

                result = await self._dispatch_step(run_id, rendered_step, rendered_input)
            except (WorkflowRenderError, StepExecutionError) as exc:
                failure_result = exc.result if isinstance(exc, StepExecutionError) else {}
                if attempt < max_attempts:
                    if retry_delay > 0:
                        await asyncio.sleep(retry_delay)
                    continue

                async with AsyncSession(engine, expire_on_commit=False) as session:
                    step_run = await self._get_step_run(session, run_id, key)
                    if not step_run:
                        return
                    step_run.status = "skipped" if on_error == "skip" else "failed"
                    step_run.error_message = str(exc)
                    step_run.error_code = failure_result.get("error_code")
                    step_run.stdout = failure_result.get("stdout")
                    step_run.stderr = failure_result.get("stderr")
                    step_run.exit_code = failure_result.get("exit_code")
                    step_run.http_status = failure_result.get("http_status")
                    step_run.output_text = failure_result.get("output_text")
                    step_run.output_json = failure_result.get("output_json")
                    step_run.completed_at = utcnow()
                    session.add(step_run)
                    await session.commit()
                if on_error == "skip":
                    return
                raise StepExecutionError(str(exc), result=failure_result) from exc

            async with AsyncSession(engine, expire_on_commit=False) as session:
                run = await session.get(WorkflowRun, run_id)
                step_run = await self._get_step_run(session, run_id, key)
                if not run or not step_run:
                    return

                step_run.status = "done"
                step_run.session_key = result.get("session_key")
                step_run.output_text = result.get("output_text")
                step_run.output_json = result.get("output_json")
                step_run.stdout = result.get("stdout")
                step_run.stderr = result.get("stderr")
                step_run.exit_code = result.get("exit_code")
                step_run.http_status = result.get("http_status")
                step_run.artifacts = result.get("artifacts")
                step_run.completed_at = utcnow()
                step_run.tokens_used = result.get("tokens_used", 0)
                session.add(step_run)

                step_context = dict(run.context or {})
                step_outputs = dict(step_context.get("steps", {}))
                step_outputs[key] = {
                    "status": step_run.status,
                    "output": result.get("output_json")
                    if result.get("output_json") is not None
                    else result.get("output_text"),
                    "output_text": result.get("output_text"),
                    "output_json": result.get("output_json"),
                    "session_key": result.get("session_key"),
                    "http_status": result.get("http_status"),
                }
                step_context["steps"] = step_outputs
                run.context = step_context
                run.total_cost_tokens = int(run.total_cost_tokens or 0) + int(result.get("tokens_used", 0) or 0)
                session.add(run)
                await session.commit()

    async def _dispatch_step(self, run_id: uuid.UUID, step: dict[str, Any], rendered_input: Any) -> dict[str, Any]:
        step_type = step.get("step_type")
        if step_type == "deterministic":
            return await self._execute_deterministic_step(step, rendered_input)
        if step_type == "llm":
            return await self._execute_llm_step(run_id, step, rendered_input)
        raise StepExecutionError(f"Unsupported step_type '{step_type}'")

    async def _execute_deterministic_step(self, step: dict[str, Any], rendered_input: Any) -> dict[str, Any]:
        executor_type = step.get("executor_type")
        executor_config = step.get("executor_config") or {}
        timeout = float(step.get("timeout_seconds", 300))

        if executor_type == "internal_api":
            method = str(executor_config.get("method", "POST")).upper()
            path = str(executor_config.get("path") or "")
            if not path.startswith("/"):
                raise StepExecutionError("internal_api path must start with '/'")
            headers = dict(executor_config.get("headers") or {})
            if settings.local_auth_token:
                headers.setdefault("Authorization", f"Bearer {settings.local_auth_token}")
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(
                    method,
                    f"http://localhost:8000{path}",
                    headers=headers,
                    **self._build_request_kwargs(executor_config, rendered_input),
                )
                return self._response_to_result(resp)

        if executor_type == "webhook":
            method = str(executor_config.get("method", "POST")).upper()
            url = str(executor_config.get("url") or "")
            if not url.startswith(("http://", "https://")):
                raise StepExecutionError("webhook executor needs absolute url")
            headers = dict(executor_config.get("headers") or {})
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(
                    method,
                    url,
                    headers=headers,
                    **self._build_request_kwargs(executor_config, rendered_input),
                )
                return self._response_to_result(resp)

        if executor_type == "script_ref":
            script_name = str(executor_config.get("script") or "")
            if not script_name:
                raise StepExecutionError("script_ref executor needs script")
            script_path = (WORKFLOW_SCRIPTS_DIR / script_name).resolve()
            base = WORKFLOW_SCRIPTS_DIR.resolve()
            if not str(script_path).startswith(str(base)):
                raise StepExecutionError("script_ref points outside workflow scripts dir")
            if not script_path.exists():
                raise StepExecutionError(f"script_ref not found: {script_name}")

            args = [str(a) for a in executor_config.get("args", [])]
            env = os.environ.copy()
            if rendered_input not in (None, ""):
                env["WORKFLOW_INPUT"] = (
                    rendered_input if isinstance(rendered_input, str) else json.dumps(rendered_input)
                )
            proc = await asyncio.create_subprocess_exec(
                "python3",
                str(script_path),
                *args,
                cwd=str(WORKFLOW_SCRIPTS_DIR.parent),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                proc.kill()
                raise StepExecutionError(
                    f"script_ref timed out after {int(timeout)}s",
                    result={"error_code": "timeout"},
                ) from exc

            stdout = stdout_bytes.decode(errors="replace")
            stderr = stderr_bytes.decode(errors="replace")
            if proc.returncode != 0:
                raise StepExecutionError(
                    f"script_ref failed with exit code {proc.returncode}",
                    result={
                        "error_code": "script_failed",
                        "stdout": stdout,
                        "stderr": stderr,
                        "exit_code": proc.returncode,
                    },
                )
            return {
                "output_text": stdout.strip() or None,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": proc.returncode,
            }

        raise StepExecutionError(f"Unsupported executor_type '{executor_type}'")

    async def _execute_llm_step(self, run_id: uuid.UUID, step: dict[str, Any], rendered_input: Any) -> dict[str, Any]:
        """Phase 29 (Gateway Sunset, D-10): LLM steps blocked until Phase 31.

        The pre-sunset code dispatched LLM steps via the Gateway chat-send-
        isolated API plus chat-history polling. Both paths are gone with the
        Gateway. A
        cli-bridge-based async LLM-step delivery (Task creation + TaskComment
        polling for assistant output) is scheduled for Phase 31. Until then,
        LLM-typed workflow steps fail explicitly so workflows cannot silently
        skip them.

        Deterministic step types (internal_api / webhook / script_ref) are
        unaffected and continue to run.
        """
        # Pre-flight: ensure step is well-formed so the error message is precise.
        try:
            agent_id = uuid.UUID(str(step["agent_id"]))
        except (KeyError, ValueError) as exc:
            raise StepExecutionError(
                "LLM step missing agent_id; cannot dispatch after Gateway sunset",
            ) from exc

        async with AsyncSession(engine, expire_on_commit=False) as session:
            agent = await session.get(Agent, agent_id)
            agent_label = agent.name if agent else str(agent_id)

        logger.warning(
            "Workflow LLM step '%s' (agent=%s) deferred — Gateway sunset (Phase 29). "
            "Re-enable when Phase 31 ships the cli-bridge async LLM-step runner.",
            step.get("key"),
            agent_label,
        )
        raise StepExecutionError(
            f"LLM steps are temporarily disabled after OpenClaw Gateway sunset "
            f"(agent={agent_label}). Phase 31 will introduce the cli-bridge-based "
            f"async LLM-step runner. Deterministic workflow steps are unaffected.",
            result={"error_code": "llm_step_disabled_gateway_sunset"},
        )

    def _build_request_kwargs(self, executor_config: dict[str, Any], rendered_input: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if "params" in executor_config:
            kwargs["params"] = executor_config["params"]
        if "json_body" in executor_config:
            kwargs["json"] = executor_config["json_body"]
        elif isinstance(rendered_input, (dict, list)):
            kwargs["json"] = rendered_input
        elif rendered_input not in (None, "") and executor_config.get("send_input_as_body", True):
            kwargs["content"] = rendered_input if isinstance(rendered_input, str) else json.dumps(rendered_input)
        return kwargs

    def _response_to_result(self, resp: httpx.Response) -> dict[str, Any]:
        text = resp.text
        output_json: Any | None = None
        try:
            output_json = resp.json()
        except Exception:
            output_json = None

        result = {
            "http_status": resp.status_code,
            "output_text": text if text else None,
            "output_json": output_json,
        }
        if resp.status_code >= 400:
            raise StepExecutionError(
                f"HTTP request failed with status {resp.status_code}",
                result=result | {"error_code": "http_error"},
            )
        return result

    async def _store_version(
        self,
        session: AsyncSession,
        workflow: WorkflowTemplate,
        *,
        version_number: int,
        change_reason: str | None,
        created_by: str,
    ) -> WorkflowTemplateVersion:
        workflow.current_version = version_number
        version = WorkflowTemplateVersion(
            workflow_id=workflow.id,
            version=version_number,
            definition_snapshot=self._build_snapshot(workflow),
            change_reason=change_reason,
            created_by=created_by,
        )
        session.add(version)
        session.add(workflow)
        await session.commit()
        await session.refresh(version)
        return version

    async def _next_version_number(self, session: AsyncSession, workflow_id: uuid.UUID) -> int:
        result = await session.exec(
            select(WorkflowTemplateVersion.version)
            .where(WorkflowTemplateVersion.workflow_id == workflow_id)
            .order_by(WorkflowTemplateVersion.version.desc())
        )
        latest = result.first()
        return int(latest or 0) + 1

    def _build_snapshot(self, workflow: WorkflowTemplate) -> dict[str, Any]:
        definition = workflow.current_definition or {}
        return {
            "id": str(workflow.id),
            "board_id": str(workflow.board_id) if workflow.board_id else None,
            "project_id": str(workflow.project_id) if workflow.project_id else None,
            "name": workflow.name,
            "description": workflow.description,
            "trigger_type": workflow.trigger_type,
            "trigger_config": workflow.trigger_config,
            "status": workflow.status,
            "current_definition": definition,
            "max_runtime_minutes": workflow.max_runtime_minutes,
            "policy_profile": workflow.policy_profile,
            "execution_policy": workflow.execution_policy,
            "delivery_config": workflow.delivery_config,
            "reflect_on": workflow.reflect_on,
            "steps": definition.get("steps", []),
        }

    async def _get_step_run(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        step_key: str,
    ) -> WorkflowStepRun | None:
        result = await session.exec(
            select(WorkflowStepRun).where(
                WorkflowStepRun.run_id == run_id,
                WorkflowStepRun.step_key == step_key,
            )
        )
        return result.first()

    async def _get_step_runs(self, session: AsyncSession, run_id: uuid.UUID) -> list[WorkflowStepRun]:
        result = await session.exec(
            select(WorkflowStepRun)
            .where(WorkflowStepRun.run_id == run_id)
            .order_by(WorkflowStepRun.step_index.asc())
        )
        return result.all()

    async def _emit_workflow_event(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            await broadcast(RedisKeys.workflow_events(), event_type, data)
        except Exception as e:
            logger.warning("Workflow SSE broadcast failed for %s: %s", event_type, e)

    async def _read_signal(self, run_id: uuid.UUID) -> str | None:
        redis = await get_redis()
        return await redis.get(RedisKeys.workflow_run_signal(str(run_id)))

    async def _clear_signal(self, run_id: uuid.UUID) -> None:
        redis = await get_redis()
        await redis.delete(RedisKeys.workflow_run_signal(str(run_id)))


workflow_service = WorkflowService()
