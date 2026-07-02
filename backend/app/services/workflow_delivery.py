from __future__ import annotations

from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.workflow import WorkflowRun, WorkflowStepRun, WorkflowTemplate
from app.services.discord import send_to_discord_channel


def should_deliver(run_status: str, delivery_config: dict[str, Any] | None) -> bool:
    if not delivery_config:
        return False
    if delivery_config.get("delivery_mode", "none") == "none":
        return False

    deliver_on = delivery_config.get("deliver_on", "success")
    if deliver_on == "always":
        return True
    if deliver_on == "success":
        return run_status in {"completed", "partial"}
    if deliver_on == "failure":
        return run_status in {"failed", "stopped", "force_stopped"}
    return False


async def deliver_run(
    session: AsyncSession,
    workflow: WorkflowTemplate,
    run: WorkflowRun,
) -> tuple[str, str | None]:
    delivery_config = workflow.delivery_config or {}
    if not should_deliver(run.status, delivery_config):
        return "skipped", None

    if delivery_config.get("delivery_mode") != "discord_channel":
        return "skipped", None

    channel_id = str(delivery_config.get("channel_id") or "").strip()
    if not channel_id:
        return "warning", "Discord delivery missing channel_id"

    result_text = await _find_result_text(session, run.id)
    if delivery_config.get("delivery_format") == "markdown" and result_text:
        await send_to_discord_channel(channel_id, content=result_text[:1800])
    else:
        await send_to_discord_channel(channel_id, embed=_build_summary_embed(workflow, run, result_text))

    return "sent", None


async def _find_result_text(session: AsyncSession, run_id) -> str | None:
    result = await session.exec(
        select(WorkflowStepRun)
        .where(WorkflowStepRun.run_id == run_id)
        .order_by(WorkflowStepRun.step_index.desc())
    )
    for step_run in result.all():
        if step_run.output_text:
            return step_run.output_text
    return None


def _build_summary_embed(
    workflow: WorkflowTemplate,
    run: WorkflowRun,
    result_text: str | None,
) -> dict[str, Any]:
    color = 0x00CC88 if run.status in {"completed", "partial"} else 0xEF4444
    description = result_text[:400] if result_text else "Workflow abgeschlossen."
    return {
        "title": f"Workflow: {workflow.name}",
        "description": description,
        "color": color,
        "fields": [
            {"name": "Status", "value": run.status, "inline": True},
            {"name": "Run", "value": str(run.id), "inline": False},
        ],
    }
