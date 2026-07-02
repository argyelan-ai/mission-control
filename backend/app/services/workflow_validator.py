from __future__ import annotations

import re
import uuid
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board, Project
from app.models.secret import Secret

STEP_KEY_RE = re.compile(r"^[a-z0-9_]+$")
SECRET_REF_RE = re.compile(r"{{\s*secrets\.([a-zA-Z0-9_\-]+)\s*}}")

ALLOWED_TRIGGER_TYPES = {"manual", "scheduled", "event"}
ALLOWED_STATUSES = {"draft", "validated", "active", "archived"}
ALLOWED_STEP_TYPES = {"llm", "deterministic", "local"}
ALLOWED_EXECUTION_MODES = {"single", "swarm"}
ALLOWED_EXECUTOR_TYPES = {"internal_api", "webhook", "script_ref", "local_model"}
ALLOWED_SCHEDULE_TYPES = {"daily", "weekdays", "interval", "weekly"}
ALLOWED_WEEKLY_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


class WorkflowValidationError(ValueError):
    pass


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def extract_secret_refs(payload: Any) -> set[str]:
    refs: set[str] = set()
    for text in _iter_strings(payload):
        refs.update(SECRET_REF_RE.findall(text))
    return refs


async def validate_workflow_payload(
    session: AsyncSession,
    *,
    board_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    name: str,
    trigger_type: str,
    trigger_config: dict[str, Any] | None,
    status: str,
    current_definition: dict[str, Any] | None,
    delivery_config: dict[str, Any] | None,
    strict: bool = False,
) -> None:
    if not name.strip():
        raise WorkflowValidationError("Workflow name is required")

    if trigger_type not in ALLOWED_TRIGGER_TYPES:
        raise WorkflowValidationError(f"Unsupported trigger_type: {trigger_type}")

    if status not in ALLOWED_STATUSES:
        raise WorkflowValidationError(f"Unsupported workflow status: {status}")

    if board_id and not await session.get(Board, board_id):
        raise WorkflowValidationError("Board not found")

    if project_id and not await session.get(Project, project_id):
        raise WorkflowValidationError("Project not found")

    _validate_trigger(trigger_type, trigger_config)
    await _validate_definition(session, current_definition or {}, strict=strict)
    await _validate_delivery_config(session, delivery_config)


def _validate_trigger(trigger_type: str, trigger_config: dict[str, Any] | None) -> None:
    if trigger_type != "scheduled":
        return

    if not trigger_config:
        raise WorkflowValidationError("scheduled workflows need trigger_config")

    schedule_type = trigger_config.get("schedule_type")
    if schedule_type not in ALLOWED_SCHEDULE_TYPES:
        raise WorkflowValidationError("schedule_type must be daily, weekdays, weekly or interval")

    if schedule_type in {"daily", "weekdays"} and not trigger_config.get("schedule_time"):
        raise WorkflowValidationError("scheduled workflow needs schedule_time")

    if schedule_type == "interval":
        interval = trigger_config.get("schedule_interval_hours")
        if not isinstance(interval, int) or interval <= 0:
            raise WorkflowValidationError("interval workflows need schedule_interval_hours > 0")

    if schedule_type == "weekly":
        if not trigger_config.get("schedule_time"):
            raise WorkflowValidationError("weekly workflows need schedule_time")
        schedule_day = str(trigger_config.get("schedule_day") or "").strip().lower()
        if schedule_day not in ALLOWED_WEEKLY_DAYS:
            raise WorkflowValidationError("weekly workflows need schedule_day mon..sun")


async def _validate_definition(
    session: AsyncSession,
    definition: dict[str, Any],
    *,
    strict: bool,
) -> None:
    steps = definition.get("steps", [])
    if not isinstance(steps, list):
        raise WorkflowValidationError("current_definition.steps must be a list")

    if strict and not steps:
        raise WorkflowValidationError("Active workflows need at least one step")

    seen_keys: set[str] = set()
    secret_refs = extract_secret_refs(definition)

    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            raise WorkflowValidationError(f"Step {idx + 1} must be an object")

        key = str(step.get("key") or "").strip()
        name = str(step.get("name") or "").strip()
        step_type = step.get("step_type")
        execution_mode = step.get("execution_mode", "single")
        executor_type = step.get("executor_type")

        if not key:
            raise WorkflowValidationError(f"Step {idx + 1} is missing key")
        if not STEP_KEY_RE.match(key):
            raise WorkflowValidationError(
                f"Invalid step key '{key}' (allowed: lowercase letters, numbers, underscore)"
            )
        if key in seen_keys:
            raise WorkflowValidationError(f"Duplicate step key '{key}'")
        seen_keys.add(key)

        if not name:
            raise WorkflowValidationError(f"Step '{key}' is missing name")

        if step_type not in ALLOWED_STEP_TYPES:
            raise WorkflowValidationError(f"Step '{key}' has unsupported step_type '{step_type}'")

        if execution_mode not in ALLOWED_EXECUTION_MODES:
            raise WorkflowValidationError(
                f"Step '{key}' has unsupported execution_mode '{execution_mode}'"
            )

        if strict and execution_mode != "single":
            raise WorkflowValidationError("MVP only supports execution_mode='single'")

        if strict and step_type == "local":
            raise WorkflowValidationError("MVP does not support local workflow steps yet")

        if step_type == "deterministic":
            if executor_type not in ALLOWED_EXECUTOR_TYPES:
                raise WorkflowValidationError(
                    f"Step '{key}' needs executor_type internal_api, webhook or script_ref"
                )
            if not isinstance(step.get("executor_config"), dict):
                raise WorkflowValidationError(f"Step '{key}' needs executor_config")

        if step_type == "llm":
            agent_id = step.get("agent_id")
            if not agent_id:
                raise WorkflowValidationError(f"LLM step '{key}' needs agent_id")
            try:
                agent_uuid = uuid.UUID(str(agent_id))
            except ValueError as exc:
                raise WorkflowValidationError(f"LLM step '{key}' has invalid agent_id") from exc

            agent = await session.get(Agent, agent_uuid)
            if not agent:
                raise WorkflowValidationError(f"LLM step '{key}' references unknown agent")
            # Phase 30: Gateway-session gating dropped. The runtime layer
            # (cli-bridge / host poll loops) selects deliverable agents now.

    if secret_refs:
        result = await session.exec(select(Secret.key).where(Secret.key.in_(sorted(secret_refs))))  # type: ignore[arg-type]
        existing = set(result.all())
        missing = sorted(secret_refs - existing)
        if missing:
            raise WorkflowValidationError(
                f"Unknown secrets referenced: {', '.join(missing)}"
            )


async def _validate_delivery_config(
    session: AsyncSession,
    delivery_config: dict[str, Any] | None,
) -> None:
    if not delivery_config:
        return

    delivery_mode = delivery_config.get("delivery_mode", "none")
    if delivery_mode == "none":
        return

    if delivery_mode != "discord_channel":
        raise WorkflowValidationError("Only delivery_mode='discord_channel' is supported right now")

    # Phase 30: `gateway_id` key was a stub since Phase 29 (Gateway singleton
    # deleted). Legacy workflow payloads may still carry it — silently ignore
    # rather than raise to keep older saved workflows loadable. Discord
    # delivery is identified by `channel_id` + `channel_name` going forward;
    # the Discord guild_id lives in `discord_config` (read by the deliverer).
    channel_id = str(delivery_config.get("channel_id") or "").strip()
    channel_name = str(delivery_config.get("channel_name") or "").strip()
    deliver_on = delivery_config.get("deliver_on", "success")
    delivery_format = delivery_config.get("delivery_format", "summary_card")

    if not channel_id:
        raise WorkflowValidationError("Discord delivery needs channel_id")
    if not channel_name:
        raise WorkflowValidationError("Discord delivery needs channel_name")
    if deliver_on not in {"success", "failure", "always"}:
        raise WorkflowValidationError("deliver_on must be success, failure or always")
    if delivery_format not in {"summary_card", "markdown"}:
        raise WorkflowValidationError("delivery_format must be summary_card or markdown")
