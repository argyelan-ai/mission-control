"""Shared internal helper for programmatic task creation.

Use this instead of inlining Task(...) + session.add() + dispatch everywhere.
Callers: scheduler.py, consensus.py, and any future internal task factories.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import Task
from app.services.activity import emit_event
from app.utils import create_tracked_task


async def create_task_internal(
    session: AsyncSession,
    *,
    board_id: uuid.UUID,
    title: str,
    description: Optional[str] = None,
    priority: str = "medium",
    status: str = "inbox",
    assigned_agent_id: Optional[uuid.UUID] = None,
    owner_agent_id: Optional[uuid.UUID] = None,
    callback_agent_id: Optional[uuid.UUID] = None,
    project_id: Optional[uuid.UUID] = None,
    parent_task_id: Optional[uuid.UUID] = None,
    phase_id: Optional[uuid.UUID] = None,
    task_type: Optional[str] = None,
    skip_review: bool = False,
    is_auto_created: bool = False,
    auto_reason: Optional[str] = None,
    tags: Optional[list[str]] = None,  # noqa: ARG001 — reserved for future TagAssignment support
    due_at: Optional[datetime] = None,
    report_back_enabled: bool = False,
    report_back_channel: Optional[str] = None,
    report_back_format: Optional[list[str]] = None,  # noqa: ARG001 — reserved; Task stores report_back_requirements (str)
    intake_mode: Optional[str] = None,
    extra_fields: Optional[dict[str, Any]] = None,
    dispatch: bool = True,
) -> Task:
    """Create a task and optionally dispatch it to an agent.

    Handles project_id resolution (parent → board.default_project_id),
    emits task.created activity event, and triggers auto-dispatch when
    board.auto_dispatch_enabled is True and dispatch=True.

    Args:
        session: Active async DB session.
        board_id: Board to create the task on.
        title: Task title (required).
        description: Optional markdown description.
        priority: low | medium (default) | high | critical.
        status: Initial status — defaults to "inbox".
        assigned_agent_id: Pre-assign to a specific agent.
        owner_agent_id: Immutable creator/delegator reference.
        callback_agent_id: Who gets the done-notification (null = board lead).
        project_id: Explicit project. Resolved from parent or board default if None.
        parent_task_id: Subtask relationship.
        phase_id: Project phase assignment.
        task_type: story | bug | revision | chore. Defaults to "story".
        skip_review: Skip review gate (useful for scheduler tasks).
        is_auto_created: Mark as auto-created (appears in UI badge).
        auto_reason: Human-readable reason for auto-creation.
        tags: Tag slugs — reserved for future TagAssignment support (no-op for now).
        due_at: Optional deadline.
        report_back_enabled: Maps to Task.report_back_required.
        report_back_channel: "telegram" | "discord" | None.
        report_back_format: Reserved — maps to comma-joined report_back_requirements.
        intake_mode: "quick" | "structured" | None.
        extra_fields: Escape hatch for less-common Task fields (merged last).
        dispatch: Trigger auto_dispatch_task after creation (default True).

    Returns:
        The newly created and committed Task instance.
    """
    # ── 1. Resolve project_id ──────────────────────────────────────────────
    if project_id is None:
        if parent_task_id is not None:
            parent = await session.get(Task, parent_task_id)
            if parent and parent.project_id:
                project_id = parent.project_id

        if project_id is None:
            from app.models.board import Board
            board_obj = await session.get(Board, board_id)
            if board_obj and board_obj.default_project_id:
                project_id = board_obj.default_project_id

    # ── 2. Build task ──────────────────────────────────────────────────────
    task = Task(
        board_id=board_id,
        title=title,
        description=description,
        priority=priority,
        status=status,
        assigned_agent_id=assigned_agent_id,
        owner_agent_id=owner_agent_id,
        callback_agent_id=callback_agent_id,
        project_id=project_id,
        parent_task_id=parent_task_id,
        phase_id=phase_id,
        task_type=task_type or "story",
        skip_review=skip_review,
        is_auto_created=is_auto_created,
        auto_reason=auto_reason,
        due_at=due_at,
        report_back_required=report_back_enabled,
        report_back_channel=report_back_channel,
        # report_back_format list → comma-joined string (Task.report_back_requirements)
        report_back_requirements=",".join(report_back_format) if report_back_format else None,
        intake_mode=intake_mode,
    )

    # Merge extra fields (escape hatch for less-common columns)
    if extra_fields:
        for key, value in extra_fields.items():
            setattr(task, key, value)

    session.add(task)
    await session.commit()
    await session.refresh(task)

    # ── 3. Emit activity event ─────────────────────────────────────────────
    await emit_event(
        session,
        "task.created",
        f"Task created: {task.title}",
        board_id=board_id,
        task_id=task.id,
        agent_id=assigned_agent_id,
    )

    # ── 4. Trigger auto-dispatch ───────────────────────────────────────────
    if dispatch:
        from app.models.board import Board
        from app.services.dispatch import auto_dispatch_task

        board_for_dispatch = await session.get(Board, board_id)
        if board_for_dispatch and board_for_dispatch.auto_dispatch_enabled:
            create_tracked_task(auto_dispatch_task(task.id, board_id))

    return task
