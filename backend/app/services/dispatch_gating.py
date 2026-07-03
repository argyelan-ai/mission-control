"""Pre-Dispatch Gating + Promote Orchestrator.

Phase 1: Central promote logic (promote_task_to_ready)
Phase 4A: Systemic promote decision (evaluate_promote_decision, process_planned_tasks)
"""
import logging
from fastapi import HTTPException
from sqlalchemy import update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import Task, TaskDependency
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger("mc.dispatch_gating")

TERMINAL_STATUSES = {"done", "failed", "aborted"}


async def promote_task_to_ready(task: Task, session: AsyncSession) -> Task:
    """Promote an executable child task from planning → ready.

    Atomic: uses UPDATE ... WHERE dispatch_phase='planning' to prevent race
    conditions between concurrent requests. Exactly one request wins,
    all others get 409.

    Guards (all must be satisfied):
    1. dispatch_phase == "planning"
    2. parent_task_id set (not a root/container task)
    3. assigned_agent_id set
    4. status == "inbox"
    5. dispatched_at is None
    6. status not terminal

    Returns: updated task
    Raises: HTTPException(409) on guard violation or race conflict
    """
    # Soft guards — give clear error messages BEFORE the atomic update
    if task.dispatch_phase != "planning":
        raise HTTPException(409, f"Task not in planning phase (current: {task.dispatch_phase})")

    if not task.parent_task_id:
        raise HTTPException(409, "Root/container tasks cannot be promoted")

    if not task.assigned_agent_id:
        raise HTTPException(409, "Task has no assigned agent — cannot promote")

    if task.status != "inbox":
        raise HTTPException(409, f"Task must be inbox to promote (current: {task.status})")

    if task.dispatched_at is not None:
        raise HTTPException(409, "Task already dispatched")

    if task.status in TERMINAL_STATUSES:
        raise HTTPException(409, f"Task in terminal status: {task.status}")

    # Dependency gate: tasks with open dependencies must NOT be promoted.
    # Dependencies override promote/release/approval — a task with open
    # predecessors must park, regardless of which path triggers the promote.
    from app.services.dispatch import dependencies_met
    if not await dependencies_met(session, task):
        from sqlmodel import select
        from app.models.task import Task as TaskModel
        # Build detail message with open dependencies
        dep_result = await session.exec(
            select(TaskDependency).where(TaskDependency.task_id == task.id)
        )
        open_deps = []
        for dep in dep_result.all():
            dep_task = await session.get(TaskModel, dep.depends_on_task_id)
            if dep_task and dep_task.status != "done":
                open_deps.append(f"'{dep_task.title[:30]}' ({dep_task.status})")
        detail = "Dependencies nicht erfuellt: " + ", ".join(open_deps) if open_deps else "Offene Dependencies"
        raise HTTPException(409, f"Promote blockiert — {detail}")

    # Atomic update — only exactly one concurrent request wins.
    # WHERE clause checks ALL central promote preconditions at the DB level.
    # If another request has changed the state in the meantime
    # (promote, dispatch, status change), WHERE won't match → rowcount=0.
    now = utcnow()
    result = await session.execute(
        update(Task)
        .where(
            Task.id == task.id,
            Task.dispatch_phase == "planning",
            Task.status == "inbox",
            Task.dispatched_at.is_(None),  # type: ignore[union-attr]
            Task.assigned_agent_id.isnot(None),  # type: ignore[union-attr]
            Task.parent_task_id.isnot(None),  # type: ignore[union-attr]
        )
        .values(dispatch_phase="ready", updated_at=now)
    )
    await session.commit()

    if result.rowcount == 0:  # type: ignore[union-attr]
        raise HTTPException(409, "Promote conflict — task state changed between validation and commit")

    # Refresh task object (in-memory)
    await session.refresh(task)

    await emit_event(
        session, "task.promoted",
        f"Task '{task.title}' von Planung freigegeben",
        severity="info",
        task_id=task.id,
        board_id=task.board_id,
    )

    logger.info("Task %s promoted to ready (board %s)", task.id, task.board_id)
    return task


# evaluate_planner_need() removed 2026-04-11 (Phase D, Migration 0071).
# The planner_mode field has been dropped from the schema, the planner path no longer exists.


# ── Phase 4A: Promote Orchestrator ────────────────────────────────────────────

# Promote decisions
AUTO_PROMOTE = "auto_promote"
NEEDS_APPROVAL = "needs_approval"
MANUAL_WAIT = "manual_wait"

# Tags that indicate higher-risk changes
HIGH_RISK_TAGS = {"infra", "db", "migration", "security"}


def evaluate_promote_decision(
    task: Task,
    parent_task: Task | None = None,
) -> tuple[str, str]:
    """Evaluate whether a planned child-task should be auto-promoted,
    needs approval, or should wait for manual action.

    Args:
        task: The child-task being evaluated
        parent_task: Optional parent/root task for inheriting operator-intent fields

    Returns (decision, reason).

    Decision matrix (conservative defaults):
    - AUTO_PROMOTE: only for explicitly clear low-risk cases
    - NEEDS_APPROVAL: elevated risk or unclear classification
    - MANUAL_WAIT: explicit manual control requested or insufficient classification
    """
    # Merge child fields with parent fields (Root carries Operator-Intent)
    autonomy = getattr(task, "autonomy_level", None)
    approval = getattr(task, "approval_policy", None)
    auth = getattr(task, "requires_auth", False)
    browser = getattr(task, "needs_browser", False)
    delegation = getattr(task, "delegation_type", None)
    tags_raw = getattr(task, "tags", None)

    # Inherit from parent if child fields are empty
    if parent_task:
        if not autonomy:
            autonomy = getattr(parent_task, "autonomy_level", None)
        if not approval:
            approval = getattr(parent_task, "approval_policy", None)
        if not auth:
            auth = getattr(parent_task, "requires_auth", False)
        if not browser:
            browser = getattr(parent_task, "needs_browser", False)

    # Check parent request_kind for mixed/unclear
    parent_kind = getattr(parent_task, "request_kind", None) if parent_task else None

    # Normalize tags
    task_tags: set[str] = set()
    if isinstance(tags_raw, list):
        for t in tags_raw:
            if isinstance(t, str):
                task_tags.add(t.lower())
            elif isinstance(t, dict) and "name" in t:
                task_tags.add(t["name"].lower())

    # 1. Explicit manual hold (highest priority)
    if autonomy in ("manual_dispatch_required", "advise_only", "draft_only"):
        return MANUAL_WAIT, f"autonomy_level={autonomy}"

    # 2. Explicit approval policy
    if approval in ("on_plan", "on_execution", "on_sensitive_action", "always"):
        return NEEDS_APPROVAL, f"approval_policy={approval}"

    # 3. High-risk tags — always approval regardless of credentials
    risky_tags = task_tags & HIGH_RISK_TAGS
    if risky_tags:
        return NEEDS_APPROVAL, f"high-risk tags: {', '.join(sorted(risky_tags))}"

    # 4. Mixed request_kind from parent → conservative
    if parent_kind == "mixed":
        return NEEDS_APPROVAL, "parent request_kind=mixed (unclear scope)"

    # 5. Credential/auth check — existing credentials = operator consent
    # If credentials were deliberately stored on the task or root,
    # that is the consent to use them for this task scope.
    # Credentials alone are then NO LONGER a reason for approval.
    # High-risk tags and mixed parent are already handled ABOVE.
    if auth or delegation == "credential_bound":
        has_creds = bool(getattr(task, "credentials_encrypted", None))
        if not has_creds and parent_task:
            has_creds = bool(getattr(parent_task, "credentials_encrypted", None))
        # Explicit credential_consent also counts
        consent = getattr(task, "credential_consent", None)
        if not consent and parent_task:
            consent = getattr(parent_task, "credential_consent", None)

        if has_creds or consent:
            # Credentials present = operator has deliberately given them for this scope.
            # credential_bound tasks with deliberate credentials → direct auto-promote.
            if delegation == "credential_bound":
                return AUTO_PROMOTE, "credential_bound + credentials present (operator consent)"
        else:
            return NEEDS_APPROVAL, "credentials/auth required but no credentials provided"

    # 7. Explicit execute intent → auto-promote
    if autonomy == "execute_low_risk":
        return AUTO_PROMOTE, "autonomy_level=execute_low_risk"

    if autonomy == "execute_with_approval_on_risk":
        return AUTO_PROMOTE, "autonomy_level=execute_with_approval_on_risk (no risk signals)"

    # 8. Null/unset autonomy: check if delegation_type gives enough signal
    if delegation in ("code_change", "review") and not auth and not browser:
        # Simple code change or review without special requirements
        # Still conservative: only auto-promote if approval_policy is explicitly "never"
        if approval == "never":
            return AUTO_PROMOTE, "approval_policy=never + simple delegation"

    # 9. Default: manual wait for insufficient classification
    return MANUAL_WAIT, "insufficient classification — manual promote required"


async def process_planned_tasks(session: AsyncSession) -> dict:
    """Process all planned child-tasks and make promote/approval/wait decisions.

    Called by Watchdog every 30s.
    Returns stats dict with counts.
    """
    from sqlmodel import select
    from app.models.approval import Approval

    # Find all promotable candidates
    result = await session.exec(
        select(Task).where(
            Task.dispatch_phase == "planning",
            Task.status == "inbox",
            Task.parent_task_id.isnot(None),  # type: ignore[union-attr]
            Task.assigned_agent_id.isnot(None),  # type: ignore[union-attr]
            Task.dispatched_at.is_(None),  # type: ignore[union-attr]
        )
    )
    planned_tasks = result.all()

    if not planned_tasks:
        return {"checked": 0, "promoted": 0, "approval": 0, "manual": 0}

    stats = {"checked": len(planned_tasks), "promoted": 0, "approval": 0, "manual": 0}

    # Pre-load parent tasks for Root→Child field inheritance
    parent_ids = {t.parent_task_id for t in planned_tasks if t.parent_task_id}
    parent_map: dict = {}
    if parent_ids:
        for pid in parent_ids:
            parent = await session.get(Task, pid)
            if parent:
                parent_map[pid] = parent

    for task in planned_tasks:
        parent = parent_map.get(task.parent_task_id)

        # Board-lead-delegated: implicit operator consent for standard tasks.
        # If owner is a Board Lead OR a Planner (on behalf of the Board Lead)
        # + no risk tags → auto execute_low_risk.
        if task.owner_agent_id and not task.autonomy_level:
            from app.models.agent import Agent as _Agent
            owner = await session.get(_Agent, task.owner_agent_id)
            is_authorized_creator = False
            if owner and owner.is_board_lead:
                is_authorized_creator = True
            elif owner and "planner" in (owner.name or "").lower():
                # Phase 30: gateway_agent_id slug-match dropped. Planner agents
                # are identified by name substring (the slug-match was a legacy
                # OpenClaw session-id artifact, field removed in 30-02).
                is_authorized_creator = True
            if is_authorized_creator:
                task_tags: set[str] = set()
                tags_raw = getattr(task, "tags", None)
                if isinstance(tags_raw, list):
                    for t in tags_raw:
                        if isinstance(t, str):
                            task_tags.add(t.lower())
                        elif isinstance(t, dict) and "name" in t:
                            task_tags.add(t["name"].lower())
                risky = task_tags & HIGH_RISK_TAGS
                if not risky and not getattr(task, "requires_auth", False):
                    task.autonomy_level = "execute_low_risk"
                    session.add(task)
                    await session.commit()
                    logger.info(
                        "Board-lead-delegated auto-autonomy: %s → execute_low_risk",
                        task.title[:40],
                    )

        decision, reason = evaluate_promote_decision(task, parent_task=parent)

        if decision == AUTO_PROMOTE:
            try:
                await promote_task_to_ready(task, session)
                # Trigger dispatch
                from app.services.dispatch import auto_dispatch_task
                from app.utils import create_tracked_task
                create_tracked_task(auto_dispatch_task(task.id, task.board_id))
                stats["promoted"] += 1
                logger.info(
                    "Auto-promoted task %s: %s — %s",
                    task.id, task.title[:40], reason,
                )
            except Exception as e:
                logger.warning("Auto-promote failed for %s: %s", task.id, e)

        elif decision == NEEDS_APPROVAL:
            # Dedupe: no new approval if pending OR approved (but task still planning)
            existing = await session.exec(
                select(Approval).where(
                    Approval.task_id == task.id,
                    Approval.action_type == "promote_approval",
                    Approval.status.in_(["pending", "approved"]),  # type: ignore[union-attr]
                )
            )
            if not existing.first():
                approval = Approval(
                    board_id=task.board_id,
                    task_id=task.id,
                    agent_id=task.assigned_agent_id,
                    action_type="promote_approval",
                    description=f"Freigabe noetig: '{task.title}' — {reason}",
                    status="pending",
                )
                session.add(approval)
                await session.commit()

                await emit_event(
                    session, "task.promote_approval_required",
                    f"Freigabe noetig fuer '{task.title}': {reason}",
                    severity="info",
                    task_id=task.id,
                    board_id=task.board_id,
                )
                stats["approval"] += 1
                logger.info(
                    "Approval required for task %s: %s — %s",
                    task.id, task.title[:40], reason,
                )

        elif decision == MANUAL_WAIT:
            # Dedupe: log only once per task (Redis key with 1h TTL)
            from app.redis_client import get_redis
            try:
                redis = await get_redis()
                dedup_key = f"mc:promote_orchestrator:manual_wait:{task.id}"
                already_logged = await redis.get(dedup_key)
                if not already_logged:
                    await redis.set(dedup_key, "1", ex=3600)  # 1h TTL
                    await emit_event(
                        session, "task.promote_manual_wait",
                        f"Task '{task.title}' wartet auf manuelle Freigabe: {reason}",
                        severity="info",
                        task_id=task.id,
                        board_id=task.board_id,
                    )
                    logger.info(
                        "Manual wait for task %s: %s — %s",
                        task.id, task.title[:40], reason,
                    )
            except Exception:
                pass  # Redis down → skip dedup, still count
            stats["manual"] += 1

    return stats
