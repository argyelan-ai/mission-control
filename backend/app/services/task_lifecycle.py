"""
TaskLifecycleService — central place for all status-transition side effects.

Eliminates the duplication between agent_scoped.py and tasks.py for:
- Review handoff (in_progress → review)
- Review rejection (review → in_progress)
- Review decision (approve / request_changes / hold)
- Task completion/failure auto-memory
- Feedback-lesson capture

Both routers delegate to this service instead of implementing the logic themselves.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Literal, NamedTuple

from fastapi import HTTPException
from sqlalchemy import or_, and_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Project
from app.models.task import Task, TaskComment, TaskEvent
from app.redis_client import get_redis
from app.utils import utcnow
from app.services.activity import emit_event
from app.services.telegram_reports import telegram_reports

logger = logging.getLogger(__name__)


async def record_task_event(
    session: AsyncSession,
    task_id: uuid.UUID,
    from_status: str,
    to_status: str,
    changed_by: str = "system",
    agent_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> None:
    """Log a task-status event (event sourcing).

    Called on EVERY status change — regardless of whether it's User, Agent, Watchdog, or System.
    """
    event = TaskEvent(
        task_id=task_id,
        from_status=from_status,
        to_status=to_status,
        changed_by=changed_by,
        agent_id=agent_id,
        reason=reason,
    )
    session.add(event)
    # No separate commit — caller commits together with the status update


async def reopen_parent_for_new_subtask(
    session: AsyncSession,
    parent_task_id: uuid.UUID,
    new_subtask_title: str | None = None,
) -> bool:
    """Automatically resets the parent task to in_progress when a new subtask
    is created while the parent is already waiting on `review` or `done`.

    Reason: phase approval sets the parent to review as soon as all existing
    subtasks are done. If the Board Lead then creates a follow-up subtask
    (e.g. "New concept based on research"), the parent would stay on review —
    the operator sees a review task even though new sub-work is running
    underneath. That's a deadlock for the task lifecycle.

    Behavior:
      - parent.status == 'review'  -> back to in_progress + event + True
      - parent.status == 'done'    -> NO auto-reopen (caller must raise 422)
      - parent.status == 'failed'  -> NO auto-reopen
      - otherwise                  -> no intervention, False

    Returns: True if the parent was reopened, otherwise False.
    No commit — caller commits together with the subtask insert.
    """
    parent = await session.get(Task, parent_task_id)
    if parent is None or parent.status != "review":
        return False

    old_status = parent.status
    parent.status = "in_progress"
    parent.updated_at = utcnow()
    parent.completed_at = None  # reset if already set
    session.add(parent)

    await record_task_event(
        session,
        parent.id,
        old_status,
        "in_progress",
        changed_by="system",
        reason="parent_reopened_for_new_subtask",
    )
    try:
        await emit_event(
            session,
            "task.parent_reopened",
            f"Parent-Task '{parent.title[:50]}' von review zurueck auf in_progress — neuer Subtask hinzugekommen",
            board_id=parent.board_id,
            task_id=parent.id,
            severity="info",
            detail={"new_subtask_title": new_subtask_title[:80] if new_subtask_title else None},
        )
    except Exception as e:
        logger.warning("parent_reopened event emission failed: %s", e)
    return True


def clear_spawn_tracking(task: Task) -> None:
    """Clear spawn-session IDs.

    Lifecycle:
    - done/failed/blocked -> session has ended, IDs are irrelevant
    - inbox (requeue) -> old dispatch is invalid

    Post Phase 29 / Gateway-Sunset: spawn_session_key + spawn_run_id are
    gateway-only artifacts (cli-bridge / host runtimes don't use them).
    They linger on legacy rows; clearing them is a no-op cleanup. The
    Gateway-Session deletion call (`sessions.delete`) is dropped — there
    is no gateway anymore.

    # TODO Phase 30: drop spawn_session_key + spawn_run_id columns from Task.

    NOT included: dispatch_attempt_id clear. Callers that need that call
    `dispatch_attempt_audit.clear_dispatch_attempt_id` with their own
    caller/reason, so the audit trail in task_attempt_audit knows the
    calling context (see the double-dispatch incident 2026-05-15).
    """
    task.spawn_run_id = None
    task.spawn_session_key = None


# ── Terminal Unassign ───────────────────────────────────────────────────
# Protection against the cancel loop in agent_poll: if a task goes to failed
# or blocked and assigned_agent_id stays set, agent_poll checks FIRST
# whether a failed task exists for the agent → returns
# state="cancelled" → poll.sh sends ESC → next poll: same
# response. Endless. New tasks are NEVER delivered because the failed task
# always takes precedence.
#
# Solution: set assigned_agent_id to NULL on the transition to failed/blocked.
# Whoever wants to free up the task again (the operator via approval, manual
# re-assign, planner) has to re-assign it anyway — that's consistent with
# the fact that failed/blocked tasks need human intervention
# and no more worker polling.
#
# Exception: blocked with blocked_by_task_id (callback wait via help_request,
# delegate). The parent agent must stay assigned so the resume after
# subtask-done can route back to the right agent.

async def apply_terminal_unassign(
    session: AsyncSession,
    task: Task,
    new_status: str,
) -> bool:
    """Clear assigned_agent_id on the transition to failed/blocked.

    Prevents the silent cancel loop (see module doc above). Called
    from all paths that set `task.status` to failed/blocked —
    user PATCH, worker PATCH, backend cleanup.

    Args:
        session: active DB session (no commit here — caller commits)
        task: task with the new status ALREADY set (or before the set;
              the method only reads new_status for the decision)
        new_status: the target status after the transition

    Returns:
        True if unassigned, False if nothing was changed.

    Behavior (after fix 2026-04-24):
        - new_status == "failed"   -> always unassign (terminal)
        - new_status == "blocked"  -> NEVER unassign (blocked is ALWAYS temporary —
          either a callback wait, mc blocked clarification, or manual stop.
          assigned_agent_id is needed so the worker gets the task back on
          resume and clarification-resolution comments can be
          delivered via poll.)
        - otherwise -> no intervention
        - assigned_agent_id already None -> no crash, returns False

    Additionally clears agent.current_task_id if this task is referenced
    there (release the lock). update_agent_active_task also does this, but
    only if old_status == "in_progress". Paths like inbox→failed or
    review→failed don't hit that — hence defensive in depth here.

    Live lessons that led to this logic:
    - PR #107: stop_task_run no longer calls apply_terminal_unassign →
      the operator's stop/resume no longer loses the agent.
    - PR #111 (here): blocker_decision via mc blocked lost assigned_agent_id
      → resolution comment no longer delivered → worker orphaned,
      task escalated to Board Lead as a workaround. Now assignment stays
      intact on blocked.
    """
    if new_status != "failed":
        # blocked is always temporary — assigned_agent_id is needed for resume
        # and resolution-comment delivery.
        if new_status == "blocked":
            # Lock strategy depends on the blocker type:
            # - blocked_by_task_id set (callback wait): worker is ACTIVELY waiting
            #   on a subtask — KEEP current_task_id (worker isn't doing anything
            #   else in parallel). Behavior as before PR #111.
            # - blocked_by_task_id None (mc blocked, human wait): worker is
            #   effectively idle while the operator answers — release the lock so
            #   the worker can pick up other tasks. On resume the task comes
            #   back via poll (assigned_agent_id intact).
            if task.blocked_by_task_id is None and task.assigned_agent_id is not None:
                agent = await session.get(Agent, task.assigned_agent_id)
                if agent is not None and agent.current_task_id == task.id:
                    agent.current_task_id = None
                    if agent.run_state in ("running", None):
                        agent.run_state = "blocked"
                    session.add(agent)
            return False
        return False

    # Defensive: watchdog/cleanup may have already unassigned the task
    if task.assigned_agent_id is None:
        return False

    # Release the agent lock if this task is in current_task_id
    agent_id_to_clear = task.assigned_agent_id
    agent = await session.get(Agent, agent_id_to_clear)
    if agent is not None and agent.current_task_id == task.id:
        agent.current_task_id = None
        # Only adjust run_state if it previously mirrored this task
        if agent.run_state in ("running", None):
            agent.run_state = "blocked" if new_status == "blocked" else "idle"
        session.add(agent)

    task.assigned_agent_id = None
    session.add(task)
    return True


# ── Active-Task Tracking ────────────────────────────────────────────────
# An agent has at most one active main task. current_task_id on the
# Agent object is set/cleared when a task becomes in_progress or
# leaves that state. Dispatch checks this field as a guard.
#
# NOTE: with use_subagent_dispatch=True, workers have parallel sessions.
# current_task_id can only track ONE task and is therefore just a hint
# for workers (not a lock). The busy check in dispatch is skipped.

async def update_agent_active_task(
    session: AsyncSession,
    agent_id: uuid.UUID,
    task: Task,
    new_status: str,
    old_status: str,
) -> None:
    """Set/clear current_task_id on the agent for status changes.

    Sets current_task_id when:
    - task changes to in_progress AND the agent has no active task
      (or the active task is this one)

    Clears current_task_id when:
    - task leaves in_progress (review, done, blocked, failed, inbox)
      AND current_task_id == task.id

    With use_subagent_dispatch: workers skip current_task_id tracking
    (parallel sessions → the field can only represent one task).
    """
    from app.config import settings

    # Clear spawn tracking when the task reaches a terminal/inactive state.
    # On in_progress the IDs are kept (session is still running).
    # On handoff/rejection/dispatch they get overwritten by the caller.
    if new_status in ("done", "failed", "blocked", "inbox"):
        clear_spawn_tracking(task)
        from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
        await clear_dispatch_attempt_id(
            session, task,
            caller="task_lifecycle",
            reason=f"status_to_{new_status}",
        )

    agent = await session.get(Agent, agent_id)
    if not agent:
        return

    # Workers with isolated sessions: don't set/clear current_task_id
    # (parallel tasks → field would be immediately inconsistent)
    # Still update run_state for UI display.
    if settings.use_subagent_dispatch and not agent.is_board_lead:
        if new_status == "in_progress" and old_status != "in_progress":
            agent.run_state = "running"
            session.add(agent)
        elif old_status == "in_progress" and new_status != "in_progress":
            # run_state only goes to idle if no other task is still in_progress
            other_active = (await session.exec(
                select(Task).where(
                    Task.assigned_agent_id == agent_id,
                    Task.id != task.id,
                    Task.status == "in_progress",
                )
            )).first()
            if not other_active:
                if new_status == "blocked":
                    agent.run_state = "blocked"
                elif new_status == "aborted":
                    agent.run_state = "aborted"
                else:
                    agent.run_state = "idle"
                session.add(agent)
        return

    if new_status == "in_progress" and old_status != "in_progress":
        # Task becomes active — set the agent lock
        if agent.current_task_id is None or agent.current_task_id == task.id:
            agent.current_task_id = task.id
            agent.run_state = "running"
            session.add(agent)
        else:
            # Agent already has another active task — log but don't block
            # (the busy check in dispatch should prevent this)
            logger.warning(
                "Agent %s hat bereits aktiven Task %s, neuer Task %s wird trotzdem in_progress",
                agent.name, agent.current_task_id, task.id,
            )
            agent.current_task_id = task.id
            agent.run_state = "running"
            session.add(agent)

    elif old_status == "in_progress" and new_status != "in_progress":
        # Task leaves in_progress — release the agent lock
        if agent.current_task_id == task.id:
            agent.current_task_id = None
            # run_state based on the new status
            if new_status == "blocked":
                agent.run_state = "blocked"
            elif new_status == "aborted":
                agent.run_state = "aborted"
            else:
                agent.run_state = "idle"
            session.add(agent)


async def _merge_pr_if_exists(
    session: AsyncSession,
    task: Task,
    actor_agent: Agent | None,
) -> None:
    """Merge PR if one exists (best effort). Extracted from agent_scoped.py."""
    if not task.project_id:
        return
    try:
        from app.services.git_service import git_service, slugify_project
        project = await session.get(Project, task.project_id)
        if not project or not project.github_repo_url:
            return

        pr_result = await session.exec(
            select(TaskComment)
            .where(
                TaskComment.task_id == task.id,
                TaskComment.content.like("%PR erstellt:%"),
            )
            .order_by(TaskComment.created_at.desc())
            .limit(1)
        )
        pr_comment = pr_result.first()
        if not pr_comment:
            return

        pr_match = re.search(r"/pull/(\d+)", pr_comment.content)
        if not pr_match:
            return

        pr_number = int(pr_match.group(1))
        project_slug = slugify_project(project.name)

        # Find workspace (reviewer or fallback)
        workspace = actor_agent.workspace_path if actor_agent else None
        if workspace:
            reviewer_dir = os.path.join(workspace, project_slug)
            if os.path.isdir(reviewer_dir):
                await git_service.merge_pr(reviewer_dir, pr_number)
                logger.info("PR #%d gemerged fuer Task '%s'", pr_number, task.title)
                return

        # Fallback: global gh CLI
        if project.github_repo_name:
            await git_service._run_cmd(
                "gh", "pr", "merge", str(pr_number),
                "--repo", project.github_repo_name,
                "--squash", "--delete-branch",
            )
            logger.info("PR #%d gemerged (global) fuer Task '%s'", pr_number, task.title)
    except Exception as e:
        logger.warning("PR-Merge fehlgeschlagen: %s", e)


async def get_review_worker_agent_ids(session: AsyncSession, task: Task) -> set[uuid.UUID]:
    """Return the set of agent ids that did IMPLEMENTATION work on `task`,
    derived from its chronological TaskEvent history (transitions into
    in_progress/review).

    A-2 (adversarial review): classification is ROLE-INDEPENDENT. The old
    version exempted review-shaped transitions only when agent.role ==
    "reviewer" — a developer-role agent handed a task purely to review was
    misclassified as worker and blocked from legitimately approving. But a
    naive "exempt all review-shaped transitions" would let a worker launder
    itself: after a review-reject, the WORKER re-enters via review →
    in_progress exactly like a reviewer ACK. The robust signal available in
    task_events (from_status/to_status/agent_id ordered by created_at) is
    the ORDER of an agent's transitions:

      * review → in_progress with no prior worker classification = the agent
        ENTERED the task while it was in review — a review-cycle participant
        (reviewer ACK), regardless of role.
      * in_progress → review by an agent that previously entered from review
        = completing that review cycle (hand-back) — still review work.
      * in_progress → review WITHOUT a prior review-entry = a developer
        handoff ("I finished implementing") — worker.
      * ANY other transition (inbox → in_progress ACK, blocked →
        in_progress, ...) = implementation work — worker. Once worker,
        later review-shaped transitions (reject re-entry) never un-classify.

    A worker's reject re-entry is therefore still caught: its original
    inbox → in_progress ACK (or its handoff before the reject) already
    marked it as worker before the review-shaped re-entry occurs.

    Shared by `execute_review_decision`'s self-review guard (M3, Fix 2) and
    the generic PATCH review→done path in routers/agent_task_status.py —
    both must agree on who counts as "the assignee that did the work" so an
    agent can't bypass the guard by using the generic PATCH endpoint instead
    of POST /review.
    """
    events_result = await session.exec(
        select(TaskEvent).where(
            TaskEvent.task_id == task.id,
            TaskEvent.to_status.in_(["in_progress", "review"]),  # type: ignore[union-attr]
            TaskEvent.agent_id.isnot(None),  # type: ignore[union-attr]
        ).order_by(TaskEvent.created_at)  # type: ignore[arg-type]
    )
    worker_agent_ids: set[uuid.UUID] = set()
    review_entrants: set[uuid.UUID] = set()  # entered the task FROM review status
    for event in events_result.all():
        aid = event.agent_id
        if not aid:
            continue
        if event.from_status == "review" and event.to_status == "in_progress":
            # Review-cycle entry (reviewer ACK) — unless this agent already
            # did implementation work (worker reject re-entry stays worker).
            if aid not in worker_agent_ids:
                review_entrants.add(aid)
            continue
        if event.from_status == "in_progress" and event.to_status == "review":
            if aid in review_entrants:
                continue  # hand-back completing a review cycle — review work
            worker_agent_ids.add(aid)  # developer handoff — implementation
            continue
        # Any non-review-shaped transition = implementation work.
        worker_agent_ids.add(aid)
        review_entrants.discard(aid)
    return worker_agent_ids


async def execute_review_decision(
    session: AsyncSession,
    task: Task,
    board_id: uuid.UUID,
    decision: Literal["approve", "request_changes", "hold"],
    comment_text: str,
    actor_agent: Agent | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """The single source of truth for review decisions.

    Makes everything atomic: comment + decision + status transition + events.
    Three outcomes: approve (→done), request_changes (→in_progress), hold (stays review).
    """
    # ── Guards ──────────────────────────────────────────────
    if task.status != "review":
        raise HTTPException(409, "Task ist nicht im Review")
    if task.run_control in ("stopped", "manual_hold"):
        raise HTTPException(409, f"Task run_control={task.run_control}")

    # Parent/child guard: on approve, check whether all children are done
    if decision == "approve":
        from app.task_status import check_children_complete
        children_ok, children_detail = await check_children_complete(task.id, session)
        if not children_ok:
            raise HTTPException(400, children_detail)

    # Self-review guard: the agent that WORKED on the task may not approve it.
    # Reviewer ACK (review → in_progress by the reviewer) does NOT count as work.
    if decision == "approve" and actor_agent:
        worker_agent_ids = await get_review_worker_agent_ids(session, task)

        if actor_agent.id in worker_agent_ids:
            if not actor_agent.is_board_lead:
                # Self-review blocked → escalate to Board Lead instead of hard-blocking
                _bl_result = await session.exec(
                    select(Agent).where(
                        Agent.board_id == board_id,
                        Agent.is_board_lead == True,  # noqa: E712
                    ).limit(1)
                )
                _board_lead = _bl_result.first()
                if _board_lead and _board_lead.id != actor_agent.id:
                    task.assigned_agent_id = _board_lead.id
                    session.add(task)
                    await session.commit()
                    logger.info(
                        "Self-review blocked: %s → eskaliert an Board Lead %s",
                        actor_agent.name, _board_lead.name,
                    )
                    await emit_event(
                        session, "review.self_review_escalated",
                        f"Self-review von {actor_agent.name} blockiert — eskaliert an {_board_lead.name}",
                        board_id=board_id, task_id=task.id, agent_id=actor_agent.id,
                        severity="warning",
                    )
                    return  # Return without approve — Board Lead must decide
                else:
                    raise HTTPException(
                        409,
                        f"Self-review not allowed: Agent '{actor_agent.name}' war als Bearbeiter beteiligt. "
                        f"Kein Board Lead fuer Eskalation verfuegbar.",
                    )

    # ── Consistency guard: ship-ready ↔ review_decision ────
    # "ship-ready" in the comment + request_changes/hold = contradiction
    comment_lower = (comment_text or "").lower()
    has_ship_ready = "ship-ready" in comment_lower and "not ship-ready" not in comment_lower
    has_not_ship_ready = "not ship-ready" in comment_lower

    if decision == "request_changes" and has_ship_ready:
        raise HTTPException(
            409,
            "Widerspruch: request_changes + ship-ready. "
            "Bei Aenderungsbedarf muss das Urteil 'not ship-ready' sein.",
        )
    if decision == "hold" and has_ship_ready:
        raise HTTPException(
            409,
            "Widerspruch: hold + ship-ready. "
            "Bei Hold darf kein ship-ready Urteil gegeben werden.",
        )
    if decision == "approve" and has_not_ship_ready:
        raise HTTPException(
            409,
            "Widerspruch: approve + not ship-ready. "
            "Bei Blocker-Findings muss request_changes statt approve verwendet werden.",
        )

    old_status = task.status
    actor_name = actor_agent.name if actor_agent else "Operator"

    # ── 1. Comment (always, atomic with the decision) ──────
    comment = TaskComment(
        task_id=task.id,
        author_type="agent" if actor_agent else "user",
        author_agent_id=actor_agent.id if actor_agent else None,
        comment_type="review",
        content=comment_text,
    )
    session.add(comment)

    # ── 2. Set decision fields ──────────────────────────
    decision_map = {
        "approve": "approved",
        "request_changes": "changes_requested",
        "hold": "hold",
    }
    task.review_decision = decision_map[decision]
    task.review_decided_at = utcnow()

    # ── 3. Release the reviewer agent ──────────────────────────
    if decision in ("approve", "request_changes"):
        reviewer_id = task.assigned_agent_id
        if reviewer_id:
            reviewer = (
                actor_agent if (actor_agent and actor_agent.id == reviewer_id)
                else await session.get(Agent, reviewer_id)
            )
            if reviewer:
                target_status = "done" if decision == "approve" else "in_progress"
                await update_agent_active_task(
                    session, reviewer.id, task, target_status, old_status,
                )

    # ── 4. Status transition (decision-dependent) ───────────
    if decision == "approve":
        # Phase parents with subtasks → user_test gate instead of straight to done
        # Everything else (single tasks, subtasks) → straight to done
        _has_children = False
        if task.parent_task_id is None:  # Root-Level
            _children_result = await session.exec(
                select(Task.id).where(Task.parent_task_id == task.id).limit(1)
            )
            _has_children = _children_result.first() is not None

        # user_test for browser-relevant phase tasks — or whenever the
        # operator explicitly requested E2E testing in the task mask
        # (e2e_test_required works for single tasks without children too).
        _needs_test = bool(getattr(task, "e2e_test_required", None)) or (
            _has_children and (
                getattr(task, "needs_browser", None)
                or getattr(task, "delegation_type", None) == "visual_proof"
            )
        )

        if _needs_test:
            task.status = "user_test"
            logger.info("Review approved → user_test Gate: '%s'", task.title[:40])
        else:
            # Report-back hard gate (analog to the PATCH handler in agent_scoped.py):
            # /review with decision=approve may not close a root task with an open
            # report_back_required obligation. The reviewer must ask the developer
            # to send `mc telegram` first, or ask the operator directly.
            if (
                task.parent_task_id is None
                and task.report_back_required
                and (task.report_back_channel or "telegram") == "telegram"
                and not task.report_sent_to_telegram
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Review-Approve abgelehnt: Root-Task hat report_back_required=true "
                        "aber es wurde noch kein Report via `mc telegram` gesendet. "
                        "Der urspruengliche Owner muss zuerst liefern — oder du sendest "
                        "den Report selbst wenn du den Kontext hast."
                    ),
                )

            task.status = "done"
            task.completed_at = utcnow()
            # Follow-up (PR #109 review, 2026-07-14): dispatch_intent is a
            # per-review-cycle label ("review_rework" etc.) consumed by
            # dispatch_message_builder to gate the Review-Feedback block and
            # by operations.is_continuation_flow. Nothing else resets it —
            # left stale, it leaks a rework-cycle-specific block (with a
            # STALE review comment) into a later, unrelated dispatch of the
            # same task row. "done" is the point every review cycle
            # unambiguously concludes, so it's the natural reset point.
            task.dispatch_intent = "root"

        # PR merge — only for a real done, NOT for user_test (test gate before merge)
        if task.status == "done":
            await _merge_pr_if_exists(session, task, actor_agent)

            # Vertical hooks (news_studio pipeline advance, bench_studio artifact
            # collection). The PATCH routers (tasks.py, agent_task_status.py) fire
            # these on status=done — this review-approve path is the third way a
            # task reaches done and skipped them (2026-07-12 incident: bench entry
            # stuck in 'generating' after `mc approve`). Hooks self-filter and
            # swallow errors.
            session.add(task)
            await session.commit()
            from app.verticals import hooks as vertical_hooks
            await vertical_hooks.run_task_done_hooks(session, task)

        # Test handoff: dispatch a tester agent for user_test (if one exists)
        if task.status == "user_test":
            try:
                tester = await handle_test_handoff(session, task, board_id)
                if tester:
                    logger.info("Test-Handoff: '%s' → %s", task.title[:40], tester.name)
                elif bool(getattr(task, "e2e_test_required", None)):
                    # Operator explicitly requested E2E — a silent skip would
                    # fake a passed gate. Block visibly instead.
                    task.status = "blocked"
                    await apply_terminal_unassign(session, task, "blocked")
                    session.add(TaskComment(
                        task_id=task.id,
                        author_type="system",
                        comment_type="blocker",
                        content=(
                            "**E2E-Test angefordert, aber kein Tester-Agent auf diesem Board.**\n\n"
                            "Der Auftrag verlangt human-simulating E2E-Testing "
                            "(Toggle in der Auftragsmaske), es ist aber kein Agent "
                            "mit Rolle `tester` verfuegbar.\n\n"
                            "**Question for @Operator** — Tester-Agent anlegen/aktivieren "
                            "oder das E2E-Gate fuer diesen Task aufheben?"
                        ),
                    ))
                    session.add(task)
                    await session.commit()
            except Exception as e:
                logger.warning("Test-Handoff fehlgeschlagen: %s", e)

        # TaskEvent
        await record_task_event(
            session, task.id, old_status, "done",
            changed_by="agent" if actor_agent else "user",
            agent_id=actor_agent.id if actor_agent else None,
            reason="review_approved",
        )

        # Activity Event
        await emit_event(
            session, "task.review_approved",
            f"Review approved von {actor_name} — '{task.title}'",
            board_id=board_id, task_id=task.id,
            agent_id=actor_agent.id if actor_agent else None,
            detail={"decision": "approved", "actor": actor_name},
        )

        # Auto-Memory + Feedback-Lessons
        trigger_auto_memory(task, "done", old_status)
        await trigger_feedback_lesson(session, task, "done", old_status)

        # Agent completion counter
        if actor_agent:
            actor_agent.total_tasks_completed += 1
            actor_agent.last_task_activity_at = utcnow()
            session.add(actor_agent)

        # Dispatch dependent tasks
        from app.models.task import TaskDependency
        from app.services.dispatch import dependencies_met, auto_dispatch_task
        dep_result = await session.exec(
            select(TaskDependency).where(TaskDependency.depends_on_task_id == task.id)
        )
        for dep in dep_result.all():
            dependent_task = await session.get(Task, dep.task_id)
            # in_progress + dispatched_at=NULL = reopened Rewrite-Dependent,
            # der auf diesen Upstream gewartet hat (Fix C — done→inbox ist
            # durch den Prod-Transition-Trigger verboten).
            if (dependent_task
                    and dependent_task.status in ("inbox", "in_progress")
                    and not dependent_task.dispatched_at
                    and await dependencies_met(session, dependent_task)):
                from app.utils import create_tracked_task
                create_tracked_task(auto_dispatch_task(dependent_task.id, dependent_task.board_id))

        # ── Board Lead completion callback ──────────────────────
        # Informs Henry (Board Lead) so he can respond to the operator
        from app.utils import create_tracked_task
        create_tracked_task(
            _notify_lead_on_completion(session, task, board_id, actor_name)
        )

    elif decision == "request_changes":
        # Provisional only — handle_review_rejection ALWAYS resolves + commits
        # the task's final status itself (every outcome does, including the
        # former same-agent "noop" case, follow-up fix 2026-07-14). It must
        # never be left sitting in this provisional in_progress state with no
        # agent working it — that was the silent ghost-state bug (Bug C,
        # 2026-07-12).
        task.status = "in_progress"

        # handle_review_rejection mutates + commits `task` in place (same
        # ORM instance) in every non-"noop" outcome, so task.status already
        # reflects the authoritative final state (inbox) by the time this
        # returns — nothing further to reconcile here.
        await handle_review_rejection(
            session, task, board_id, rejecting_agent=actor_agent,
        )

    elif decision == "hold":
        # No status change, no dispatch. Task stays in review.
        await emit_event(
            session, "task.review_hold",
            f"Review angehalten von {actor_name} — '{task.title}'",
            board_id=board_id, task_id=task.id,
            agent_id=actor_agent.id if actor_agent else None,
            severity="warning",
            detail={"decision": "hold", "actor": actor_name},
        )

    task.updated_at = utcnow()
    session.add(task)
    await session.commit()


async def handle_review_handoff(
    session: AsyncSession,
    task: Task,
    board_id: uuid.UUID,
    developer: Agent | None = None,
) -> Agent | None:
    """Review handoff: hand the task to a reviewer + push notification.

    Shared between agent_scoped.py (agent sets review) and tasks.py (user sets review).
    Git/PR creation stays in the respective router (agent-specific).

    Returns: reviewer agent or None.
    """
    from app.routers.agent_scoped import _find_reviewer

    # ── Dedupe: if the task is already assigned to a reviewer, no second handoff
    if task.assigned_agent_id and task.dispatch_intent == "review_handoff":
        existing_reviewer = await session.get(Agent, task.assigned_agent_id)
        if existing_reviewer and existing_reviewer.role == "reviewer":
            logger.info("Review-Handoff dedupe: '%s' bereits bei %s", task.title, existing_reviewer.name)
            return existing_reviewer

    reviewer = await _find_reviewer(session, board_id)
    if not reviewer:
        return None
    if developer and reviewer.id == developer.id:
        return None  # Reviewer must not be the same agent

    # Set dispatch_intent + operational controls guard
    task.dispatch_intent = "review_handoff"
    from app.services.operations import check_dispatch_allowed
    allowed, reason = await check_dispatch_allowed(task, reviewer, session)
    if not allowed:
        logger.info("Review-Handoff blocked: '%s' — %s", task.title, reason)
        return None

    # Release the developer lock (clear current_task_id)
    if developer and developer.current_task_id == task.id:
        developer.current_task_id = None
        session.add(developer)

    # Commit the reviewer assignment (WITHOUT dispatched_at — that only comes after RPC success)
    task.assigned_agent_id = reviewer.id
    task.ack_at = None
    task.completed_at = None  # Reset in case it was set from a previous cycle
    task.review_decision = None  # New review round starts clean
    task.review_decided_at = None
    task.updated_at = utcnow()
    clear_spawn_tracking(task)  # Clear old spawn-session IDs
    from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
    await clear_dispatch_attempt_id(
        session, task,
        caller="task_lifecycle", reason="review_handoff",
    )
    session.add(task)
    await session.commit()

    await emit_event(
        session, "task.review_handoff",
        f"Review-Handoff: '{task.title}' -> {reviewer.name}",
        board_id=board_id, task_id=task.id, agent_id=reviewer.id,
    )

    # Push notification to the reviewer
    # Post Phase 29 / Gateway-Sunset: no gateway_agent_id gate, no RPC.
    # Re-dispatch via auto_dispatch_task → the dispatcher picks the right
    # runtime delivery (cli-bridge / host / claude-code) and the reviewer
    # gets the review message via their poll.sh / launchd.
    # We do NOT set dispatched_at here — auto_dispatch_task sets it itself
    # after successful delivery.
    from app.services.dispatch import auto_dispatch_task
    task.dispatched_at = None
    task.ack_at = None
    session.add(task)
    await session.commit()
    asyncio.create_task(auto_dispatch_task(task.id, board_id))

    return reviewer


async def handle_human_review_handoff(
    session: AsyncSession,
    task: Task,
    board_id: uuid.UUID,
    developer: Agent | None = None,
) -> None:
    """Human review handoff: skip the agent reviewer, leave the task for Mark.

    Mirrors handle_review_handoff's non-dispatch side effects (release the
    developer lock, reset the review-round bookkeeping, clear spawn/dispatch-
    attempt tracking) but assigns no reviewer agent — the task stays in
    `review` with assigned_agent_id=None, which is enough for the Inbox
    query (filters on status=review) to surface it. Mark decides via the
    existing POST .../review endpoint.
    """
    # Release the developer lock (clear current_task_id) — same as the
    # agent-reviewer path, the developer is done with this task either way.
    if developer and developer.current_task_id == task.id:
        developer.current_task_id = None
        session.add(developer)

    task.assigned_agent_id = None
    task.ack_at = None
    task.dispatched_at = None
    task.completed_at = None  # Reset in case it was set from a previous cycle
    task.review_decision = None  # New review round starts clean
    task.review_decided_at = None
    task.dispatch_intent = "human_review"
    task.updated_at = utcnow()
    clear_spawn_tracking(task)
    from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
    await clear_dispatch_attempt_id(
        session, task,
        caller="task_lifecycle", reason="human_review_handoff",
    )
    session.add(task)

    comment = TaskComment(
        task_id=task.id,
        author_type="system",
        comment_type="handoff",
        content=(
            f"Human-Review angefordert fuer '{task.title}' — wartet auf Mark "
            "(kein Agent-Reviewer dispatcht)."
        ),
    )
    session.add(comment)
    await session.commit()

    await emit_event(
        session, "task.human_review_requested",
        f"Human-Review angefordert: '{task.title}' wartet auf Mark",
        board_id=board_id, task_id=task.id,
    )

    from app.services.telegram_bot import telegram_bot
    from app.config import phone_test_url
    if telegram_bot.configured:
        tailscale_url = phone_test_url()
        await telegram_bot.send_message(
            f"<b>Human-Review angefordert: {task.title}</b>\n\n"
            f"Bitte selbst freigeben:\n{tailscale_url}\n\n"
            f"Task-ID: {task.id}"
        )


async def handle_test_handoff(
    session: AsyncSession,
    task: Task,
    board_id: uuid.UUID,
) -> Agent | None:
    """Test handoff: hand the task to a tester on user_test.

    Analogous to handle_review_handoff, but for QA/user testing.
    The tester checks via browser whether the result works from the user's perspective.
    """
    from app.scopes import AgentRole
    from app.services.dispatch import find_agent_by_role

    # Latent bug until 2026-07-05: the string "tester" was passed here, but
    # find_agent_by_role does role.value → AttributeError on EVERY handoff,
    # silently swallowed by the caller's try/except. user_test tasks never
    # got a tester dispatched.
    tester = await find_agent_by_role(session, board_id, AgentRole.TESTER)
    if not tester:
        logger.info("Test-Handoff: kein Tester-Agent fuer Board %s", board_id)
        return None

    task.assigned_agent_id = tester.id
    task.dispatch_intent = "test_handoff"
    task.ack_at = None
    task.updated_at = utcnow()
    clear_spawn_tracking(task)
    from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
    await clear_dispatch_attempt_id(
        session, task,
        caller="task_lifecycle", reason="test_handoff",
    )
    session.add(task)
    await session.commit()

    await emit_event(
        session, "task.test_handoff",
        f"Test-Handoff: '{task.title}' -> {tester.name}",
        board_id=board_id, task_id=task.id, agent_id=tester.id,
    )

    # Dispatch to the tester
    # Post Phase 29 / Gateway-Sunset: re-dispatch via auto_dispatch_task.
    # The dispatcher picks the right runtime delivery (cli-bridge / host /
    # claude-code) and sends the test message via poll.sh / launchd.
    from app.services.dispatch import auto_dispatch_task
    task.dispatched_at = None
    task.ack_at = None
    session.add(task)
    await session.commit()
    asyncio.create_task(auto_dispatch_task(task.id, board_id))

    return tester


class ReviewRejectionResult(NamedTuple):
    """Outcome of handle_review_rejection (Bug C, 2026-07-12).

    developer: the original developer, if one could be determined.
    outcome:
      - "redispatched" — dev found, allowed, re-dispatched immediately.
      - "queued"       — dev found, allowed, but busy → queued for them.
      - "no_developer" — no developer could be reconstructed; task forced
        to inbox unassigned + explicitly kicked via auto_dispatch_task
        (routes to the Board Lead — an unassigned inbox task has no other
        self-collecting mechanism, task_runner/watchdog both require
        assigned_agent_id IS NOT NULL).
      - "dispatch_blocked" — dev found but check_dispatch_allowed()
        vetoed (paused/asleep/run_control/halted); task forced to inbox,
        assigned to the dev, System-Kommentar with the reason.

    Same-agent reject (rejecting_agent == original_dev, e.g. a developer
    classified as their own reviewer) used to short-circuit to a "noop"
    outcome here, leaving the task in the caller's provisional in_progress
    state untouched — the same ghost-state bug class as Bug C, just missed
    by that fix. Follow-up (2026-07-14): removed the special case; it now
    falls through to "redispatched"/"queued" like every other developer.
    """
    developer: Agent | None
    outcome: Literal[
        "redispatched", "queued", "no_developer", "dispatch_blocked",
    ]


async def handle_review_rejection(
    session: AsyncSession,
    task: Task,
    board_id: uuid.UUID,
    rejecting_agent: Agent | None = None,
) -> ReviewRejectionResult:
    """Review rejection: send the task back to the original developer.

    Shared between agent_scoped.py and tasks.py.
    Includes a busy check, queue fallback, and rejection counter.

    Bug C (2026-07-12): previously this returned a bare `None` when no
    developer could be found, or when check_dispatch_allowed() vetoed the
    re-dispatch — silently, without persisting any state change. The caller
    (execute_review_decision) had already optimistically set
    task.status="in_progress" and committed it regardless, leaving a ghost
    in_progress task with assigned_agent_id=None that no watchdog recognizes
    and that never gets picked up again. Both branches now explicitly
    resolve the task to a claimable `inbox` state (mirrors
    resolve_unblock_action's explicit-branches pattern) and return a
    ReviewRejectionResult so the caller never has to guess.

    Returns: ReviewRejectionResult (developer + outcome, see above).
    """
    from app.routers.agent_scoped import _find_last_developer

    # Rejection counter (only for agent rejections)
    if rejecting_agent:
        try:
            from app.services.task_queue import increment_rejection_count, MAX_REJECTIONS
            from app.models.approval import Approval
            rejection_count = await increment_rejection_count(str(task.id))
            if rejection_count >= MAX_REJECTIONS:
                approval = Approval(
                    board_id=board_id,
                    task_id=task.id,
                    agent_id=rejecting_agent.id,
                    action_type="review_escalation",
                    description=(
                        f"Task '{task.title}' wurde {rejection_count}x vom Review abgelehnt. "
                        f"Manuelle Pruefung erforderlich."
                    ),
                )
                session.add(approval)
                await emit_event(
                    session, "task.review_escalated",
                    f"Task '{task.title}' eskaliert ({rejection_count}x abgelehnt)",
                    board_id=board_id, task_id=task.id, agent_id=rejecting_agent.id,
                    severity="warning",
                )
        except Exception:
            logger.warning("Rejection counter failed for task %s", task.id)

    # Rejection routing: ALWAYS prefer the original developer (context
    # preservation). Board Lead is only a fallback when no developer
    # can be determined. Changed order compared to before: previously
    # root tasks (parent_task_id=NULL) went to the Board Lead first — that
    # rerouted specifically-dispatched tasks (e.g. argyelan-viral-shorts to Davinci)
    # to Boss on rejection, who never had the context. See the
    # 2026-05-08 viral-shorts E2E run: Davinci had P1-P6 done, the
    # operator's "change" click sent the task to Boss instead of back to Davinci.
    original_dev = await _find_last_developer(session, task)

    # Fallback: Board Lead for root tasks with no identifiable developer
    # (e.g. tasks created directly by the operator without anyone ever
    # having set them to in_progress → review).
    if not original_dev and task.parent_task_id is None:
        from sqlmodel import select as _sel
        _board_lead = (await session.exec(
            _sel(Agent).where(
                Agent.board_id == board_id,
                Agent.is_board_lead == True,  # noqa: E712
            )
        )).first()
        if _board_lead:
            original_dev = _board_lead
            logger.info(
                "Review-rejection: no developer found → Board Lead '%s'",
                _board_lead.name,
            )

    if not original_dev:
        # Bug C branch (a): no developer reconstructable at all. Force the
        # task to inbox unassigned instead of leaving whatever provisional
        # status the caller set — an unassigned inbox task is NOT self-
        # collecting: task_runner / get_next_task / the watchdog all require
        # assigned_agent_id IS NOT NULL, so nothing would ever pick this up
        # on its own (verified 2026-07-12 review). auto_dispatch_task's
        # find_dispatch_target() routes unassigned tasks to the Board Lead,
        # so we explicitly kick a dispatch here instead of leaving the task
        # to rot silently.
        _old_status = task.status
        task.status = "inbox"
        task.assigned_agent_id = None
        task.dispatched_at = None
        task.ack_at = None
        session.add(task)
        session.add(TaskComment(
            task_id=task.id,
            author_type="system",
            comment_type="system",
            content=(
                "Rework angefordert, urspruenglicher Entwickler nicht ermittelbar "
                "— an Board Lead weitergeleitet."
            ),
        ))
        await record_task_event(
            session, task.id, _old_status, "inbox",
            changed_by="system",
            agent_id=rejecting_agent.id if rejecting_agent else None,
            reason="review_rejection_no_developer",
        )
        await session.commit()
        await emit_event(
            session, "task.review_rejected",
            f"Review abgelehnt: '{task.title}' — kein Entwickler ermittelbar, an Board Lead weitergeleitet",
            board_id=board_id, task_id=task.id,
            severity="warning",
        )
        from app.services.dispatch import auto_dispatch_task
        asyncio.create_task(auto_dispatch_task(task.id, board_id))
        return ReviewRejectionResult(None, "no_developer")

    # Self-reject (rejecting_agent == original_dev, e.g. a developer-turned-
    # reviewer classified as their own original developer, see
    # get_review_worker_agent_ids) used to `return noop` here and leave the
    # task sitting in the caller's provisional in_progress status with no
    # comment, no event, and stale dispatch bookkeeping (dispatched_at/
    # ack_at from the earlier review-handoff dispatch) — the same
    # ghost-state bug class as Bug C above, just unreachable by the two
    # branches that bug fixed. There's no reason to special-case it: the
    # agent is already the target of the redispatch below, the busy-check
    # excludes this task by id, and check_dispatch_allowed still applies —
    # falling through gives the same explicit inbox+comment+redispatch
    # treatment (with a fresh review_rework dispatch message) as every
    # other outcome.

    # Set dispatch_intent + operational controls guard
    task.dispatch_intent = "review_rework"
    from app.services.operations import check_dispatch_allowed
    allowed, reason = await check_dispatch_allowed(task, original_dev, session)
    if not allowed:
        # Bug C branch (b): a developer WAS found, but dispatch is currently
        # not allowed (agent paused/asleep, run_control, halted, ...). Force
        # inbox — assigned to the developer, so once dispatch is allowed
        # again the normal claim/requeue flow re-delivers it (see
        # resolve_unblock_action's redispatch/requeue mechanics reused
        # elsewhere) instead of sitting silently as a ghost in_progress task.
        logger.info("Review-Rejection re-dispatch blocked: '%s' — %s", task.title, reason)
        _old_status = task.status
        task.status = "inbox"
        task.assigned_agent_id = original_dev.id
        task.dispatched_at = None
        task.ack_at = None
        session.add(task)
        session.add(TaskComment(
            task_id=task.id,
            author_type="system",
            comment_type="system",
            content=(
                f"Rework fuer {original_dev.name} angefordert, Dispatch aktuell "
                f"nicht erlaubt ({reason}). Task wartet in der Inbox — wird "
                "automatisch zugestellt, sobald Dispatch wieder erlaubt ist."
            ),
        ))
        await record_task_event(
            session, task.id, _old_status, "inbox",
            changed_by="system",
            agent_id=rejecting_agent.id if rejecting_agent else None,
            reason="review_rejection_dispatch_blocked",
        )
        await session.commit()
        await emit_event(
            session, "task.review_rejected",
            f"Review abgelehnt: '{task.title}' — Dispatch fuer {original_dev.name} "
            f"blockiert ({reason}), wartet in Inbox",
            board_id=board_id, task_id=task.id, agent_id=original_dev.id,
            severity="warning",
        )
        return ReviewRejectionResult(original_dev, "dispatch_blocked")

    # Release the reviewer lock
    if rejecting_agent and rejecting_agent.current_task_id == task.id:
        rejecting_agent.current_task_id = None
        session.add(rejecting_agent)

    task.assigned_agent_id = original_dev.id
    task.updated_at = utcnow()
    task.dispatched_at = None
    task.ack_at = None

    # Busy check: does the developer already have an active task?
    # With isolated sessions, the busy check for workers is skipped
    from app.config import settings as _settings
    _skip_busy = _settings.use_subagent_dispatch and not original_dev.is_board_lead

    dev_active = None
    if not _skip_busy:
        dev_active = (await session.exec(
            select(Task).where(
                Task.assigned_agent_id == original_dev.id,
                Task.id != task.id,
                or_(
                    Task.status == "in_progress",
                    and_(Task.status == "inbox", Task.dispatched_at.isnot(None)),
                ),
            )
        )).first()

    old_status = task.status  # review or in_progress

    if dev_active:
        await record_task_event(
            session, task.id, old_status, "inbox",
            changed_by="system",
            agent_id=rejecting_agent.id if rejecting_agent else None,
            reason="review_rejection_queued",
        )
        task.status = "inbox"
        session.add(task)
        await session.commit()
        from app.services.task_queue import enqueue_task
        await enqueue_task(str(original_dev.id), str(task.id))
        await emit_event(
            session, "task.review_rejected",
            f"Review abgelehnt: '{task.title}' in Queue fuer {original_dev.name} (busy)",
            board_id=board_id, task_id=task.id, agent_id=original_dev.id,
        )
    else:
        # Set status to inbox — the agent must ACK (PATCH status: in_progress)
        # This ensures the task runner detects the ACK timeout
        await record_task_event(
            session, task.id, old_status, "inbox",
            changed_by="system",
            agent_id=rejecting_agent.id if rejecting_agent else None,
            reason="review_rejection_redispatch",
        )
        task.status = "inbox"

        session.add(task)
        await session.commit()
        await emit_event(
            session, "task.review_rejected",
            f"Review abgelehnt: '{task.title}' zurueck an {original_dev.name}",
            board_id=board_id, task_id=task.id, agent_id=original_dev.id,
        )
        # Re-dispatch after review rejection.
        # Post Phase 29 / Gateway-Sunset: no gateway_agent_id gate, no RPC.
        # Re-dispatch via auto_dispatch_task — the dispatcher handles the
        # runtime-specific delivery (cli-bridge / host / claude-code).
        # Context preservation: TaskComment history is kept — poll.sh
        # delivers the history (including review feedback) via /agent/me/poll.
        from app.services.dispatch import auto_dispatch_task
        task.dispatched_at = None
        task.ack_at = None
        session.add(task)
        await session.commit()
        asyncio.create_task(auto_dispatch_task(task.id, board_id))

    return ReviewRejectionResult(
        original_dev, "queued" if dev_active else "redispatched",
    )


async def resolve_unblock_action(
    session: AsyncSession,
    task: Task,
) -> Literal["redispatch", "requeue", "notify", "skip"]:
    """B2 (W2-B, audit G3 + review fix B-2): decide how a blocked→in_progress
    unblock should reach the assigned agent.

    Previously the unblock path only ever posted an `unblock_notify`
    TaskComment — no liveness check, no redispatch. If the blocked agent's
    process had died in the meantime, nobody read the comment and the task
    silently stalled until the 15-45min stale-recovery ladder caught it.

    Decision:
      - No assigned agent → "skip" (nothing to notify/redispatch).
      - Assigned agent DEAD (last_seen_at NULL or stale beyond the wrapper
        liveness floor — reuses task_runner._liveness_floor_seconds, the
        same 2x-heartbeat-interval proof-of-life used by the stale-recovery
        watchdog) → "redispatch". The agent's poll.sh wrapper is gone, so a
        TaskComment would sit unread; a full auto_dispatch_task re-delivery
        (with recovery context, W1-deduped checklist) is the only thing that
        reaches it — dispatch's own fallback logic then targets the lead if
        the agent stays dead.
      - Assigned agent ALIVE but OCCUPIED with a DIFFERENT task
        (current_task_id points elsewhere, or another in_progress task is
        assigned) → "requeue" (review fix B-2). Leaving the unblocked task
        in_progress here creates TWO in_progress tasks for one agent — and
        poll's active-query would surface the just-unblocked one (freshest
        updated_at, ack_at still set) while the real session runs the other
        task. Instead the task goes back to a claimable state; the normal
        claim/dispatch flow re-delivers it after the current work, without
        interrupting the agent.
      - Assigned agent ALIVE and idle (or already on THIS task) → "notify" —
        the existing comment-only path, delivered via poll.
    """
    if not task.assigned_agent_id:
        return "skip"
    target = await session.get(Agent, task.assigned_agent_id)
    if target is None:
        return "skip"

    from app.services.task_runner import _liveness_floor_seconds
    from app.utils import ensure_aware

    last_seen = target.last_seen_at
    if last_seen is None:
        return "redispatch"
    seen_age = (utcnow() - ensure_aware(last_seen)).total_seconds()
    if seen_age >= _liveness_floor_seconds(target):
        return "redispatch"

    # Occupancy check (review fix B-2): is the agent actively working on a
    # DIFFERENT task right now?
    if target.current_task_id is not None and target.current_task_id != task.id:
        return "requeue"
    other_active = (await session.exec(
        select(Task)
        .where(
            Task.assigned_agent_id == target.id,
            Task.status == "in_progress",
            Task.id != task.id,
        )
        .limit(1)
    )).first()
    if other_active is not None:
        return "requeue"

    return "notify"


async def requeue_unblocked_task(
    session: AsyncSession,
    task: Task,
    board_id: uuid.UUID,
) -> None:
    """B2 (review fix B-2): occupied-agent branch of resolve_unblock_action.

    The unblock route has already flipped the task to in_progress — but the
    assigned agent is mid-flight on ANOTHER task. Two in_progress tasks for
    one agent corrupt poll's active-task resolution (the just-unblocked task
    has the freshest updated_at AND a stale ack_at, so poll would report
    "working" on it while the real session runs the other task). Reset the
    task to a claimable inbox state instead: the agent's normal poll-claim
    flow (or auto-dispatch) re-delivers it with a full prompt AFTER the
    current work finishes — no interrupt, no state corruption.
    """
    old_status = task.status
    task.status = "inbox"
    task.dispatched_at = None
    task.ack_at = None
    session.add(task)

    # Give the active-task lock back: update_agent_active_task (which runs
    # earlier in the PATCH flow) unconditionally repoints current_task_id to
    # the just-unblocked task, stealing the lock from the task the agent is
    # actually running. Restore it to the real in_progress task (or clear).
    if task.assigned_agent_id is not None:
        _assigned = await session.get(Agent, task.assigned_agent_id)
        if _assigned is not None and _assigned.current_task_id == task.id:
            _real_active = (await session.exec(
                select(Task)
                .where(
                    Task.assigned_agent_id == _assigned.id,
                    Task.status == "in_progress",
                    Task.id != task.id,
                )
                .limit(1)
            )).first()
            _assigned.current_task_id = _real_active.id if _real_active else None
            session.add(_assigned)

    await record_task_event(
        session, task.id, old_status, "inbox",
        changed_by="system", reason="unblock_requeue_agent_busy",
    )
    from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
    await clear_dispatch_attempt_id(
        session, task,
        caller="requeue_unblocked_task", reason="unblock_requeue_agent_busy",
    )
    await session.commit()
    await session.refresh(task)

    logger.info(
        "Unblock-Requeue: task=%s — Agent %s arbeitet gerade an einem anderen "
        "Task; entblockter Task geht zurueck in die Inbox (Claim nach der "
        "aktuellen Arbeit, kein Interrupt)",
        task.id, task.assigned_agent_id,
    )
    await emit_event(
        session,
        "task.unblock_requeued",
        f"Unblock-Requeue: Task \"{task.title}\" wartet in der Inbox — "
        f"zugewiesener Agent arbeitet gerade an einem anderen Task",
        board_id=board_id,
        task_id=task.id,
        agent_id=task.assigned_agent_id,
        severity="info",
        detail={"reason": "assigned_agent_busy_on_unblock"},
    )


async def redispatch_unblocked_task(
    session: AsyncSession,
    task: Task,
    board_id: uuid.UUID,
) -> None:
    """B2: dead-agent branch of resolve_unblock_action — reset the dispatch
    handshake and route through the normal auto_dispatch_task re-dispatch
    (targets the same agent if it revives, falls back to lead/others per
    dispatch's own logic if it stays dead)."""
    from app.services.dispatch import auto_dispatch_task
    from app.utils import create_tracked_task

    task.dispatched_at = None
    task.ack_at = None
    session.add(task)

    # Review fix B-3: reconcile the (dead) agent's active-task pointer. If
    # current_task_id still points at the task being redispatched, anything
    # reading it in the window until re-dispatch (poll active-preference,
    # mc delegate 409-guard, watchdog corroboration) would act on a stale
    # lock. The fresh dispatch sets it again on claim/ACK.
    if task.assigned_agent_id is not None:
        _assigned = await session.get(Agent, task.assigned_agent_id)
        if _assigned is not None and _assigned.current_task_id == task.id:
            _assigned.current_task_id = None
            session.add(_assigned)

    await session.commit()
    await session.refresh(task)

    create_tracked_task(
        auto_dispatch_task(task.id, board_id),
        name=f"unblock-redispatch:{task.id}",
    )
    logger.info(
        "Unblock-Redispatch: task=%s agent=%s war offline (stale last_seen_at) "
        "→ dispatched_at/ack_at zurueckgesetzt, auto_dispatch_task neu getriggert",
        task.id, task.assigned_agent_id,
    )
    await emit_event(
        session,
        "task.unblock_redispatched",
        f"Unblock-Redispatch: Task \"{task.title}\" — zugewiesener Agent war offline",
        board_id=board_id,
        task_id=task.id,
        agent_id=task.assigned_agent_id,
        severity="warning",
        detail={"reason": "assigned_agent_stale_on_unblock"},
    )


def trigger_auto_memory(
    task: Task,
    new_status: str,
    old_status: str,
) -> None:
    """Start background tasks for auto-memory on status changes.

    Fire-and-forget — errors are logged in the task callback.
    """
    if new_status not in ("done", "failed"):
        return
    if not task.board_id:
        return

    from app.services.auto_memory import record_task_completion, record_task_failure
    from app.utils import create_tracked_task

    if new_status == "done":
        create_tracked_task(
            record_task_completion(task.id, task.assigned_agent_id),
            name=f"auto_memory:completion:{task.id}",
        )
    else:
        create_tracked_task(
            record_task_failure(task.id, task.assigned_agent_id),
            name=f"auto_memory:failure:{task.id}",
        )


async def trigger_feedback_lesson(
    session: AsyncSession,
    task: Task,
    new_status: str,
    old_status: str,
) -> None:
    """Capture feedback lessons on review decisions.

    Approved (review → done) or rejected (review → in_progress).
    """
    if not task.board_id or old_status != "review":
        return

    from app.services.auto_memory import record_feedback_lesson
    from app.utils import create_tracked_task

    if new_status == "done":
        create_tracked_task(
            record_feedback_lesson(task.id, task.assigned_agent_id, "approved"),
            name=f"feedback:approved:{task.id}",
        )
    elif new_status == "in_progress":
        last_cmt = (await session.exec(
            select(TaskComment)
            .where(TaskComment.task_id == task.id)
            .order_by(TaskComment.created_at.desc())
            .limit(1)
        )).first()
        feedback_text = last_cmt.content if last_cmt else None
        create_tracked_task(
            record_feedback_lesson(
                task.id, task.assigned_agent_id, "rejected", feedback_text,
            ),
            name=f"feedback:rejected:{task.id}",
        )


# ── Completion Callback: notify Board Lead ──────────────
#
# Phase 29 / Gateway-Sunset: `select_lead_callback_session()` was removed.
# The helper picked the best session key for a lead nudge from a Gateway
# `sessions.list()` response (Telegram > Discord >
# Main). With the gateway sunset, the session list is gone entirely —
# delivery now happens via TaskComment + (optionally) a direct
# `telegram_bot.send_message()` call.


async def _notify_lead_on_completion(
    session_unused: AsyncSession,
    task: Task,
    board_id: uuid.UUID,
    reviewer_name: str,
) -> None:
    """Completion callback: Henry gets a mandatory report-back obligation.

    Stage 1 (immediate): Henry gets a callback with an open obligation
      → report_back_status = "pending"
      → Henry is expected to respond to the operator organically
    Stage 2 (fallback, 5 min): system safety net
      → ONLY if report_back_status is still "pending"
      → a terse system message to the operator via Telegram
    """
    from app.database import engine

    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            task = await session.get(Task, task.id)
            if not task:
                return

            # ── Collect evidence ──────────────────────────────
            evidence_cmt = (await session.exec(
                select(TaskComment)
                .where(
                    TaskComment.task_id == task.id,
                    TaskComment.comment_type.in_(["resolution", "progress", "review"]),
                )
                .order_by(TaskComment.created_at.desc())
                .limit(3)
            )).all()

            evidence_lines = []
            for cmt in reversed(evidence_cmt):
                label = cmt.comment_type.upper()
                content = cmt.content[:400]
                if len(cmt.content) > 400:
                    content += "\n[...gekuerzt]"
                evidence_lines.append(f"**{label}:** {content}")
            evidence_text = "\n\n".join(evidence_lines) if evidence_lines else "(Keine Evidence)"

            project_info = ""
            if task.project_id:
                project = await session.get(Project, task.project_id)
                if project:
                    project_info = f"**Projekt:** {project.name}\n"

            # ── Stage 1: callback routing (priority: callback_agent_id → owner if Board Lead → Board Lead) ────
            # Post Phase 29 / Gateway-Sunset: lead selection no longer uses a
            # gateway_agent_id filter; delivery happens via
            # TaskComment (runtime-agnostic — Board Lead's poll.sh / launchd
            # delivers via /agent/me/comments) + an optional direct Telegram
            # notice for the operator.
            lead = None
            if board_id:
                # Primary: explicit callback_agent_id
                if task.callback_agent_id:
                    cb_agent = await session.get(Agent, task.callback_agent_id)
                    if cb_agent:
                        lead = cb_agent

                # Secondary: owner_agent_id — but only if the owner is a Board Lead
                # (Planner as owner should NOT get the callback)
                if not lead and task.owner_agent_id:
                    owner = await session.get(Agent, task.owner_agent_id)
                    if owner and owner.is_board_lead:
                        lead = owner

                # Fallback: the board's Board Lead
                if not lead:
                    lead = (await session.exec(
                        select(Agent).where(
                            Agent.board_id == board_id,
                            Agent.is_board_lead == True,  # noqa: E712
                        )
                    )).first()

                if lead:
                    # Completion info for the Board Lead. The report-back to the operator
                    # is NOT handled by the lead — the executing agent already
                    # delivered directly to the operator's reports chat via `mc telegram`
                    # before `mc done` (a hard gate enforces this).
                    contract_block = (
                        f"\n## Naechster Schritt\n"
                        f"Klassifiziere das Ergebnis (Task only / Reusable Asset / Content Opportunity / Revenue Opportunity).\n"
                        f"Der ausfuehrende Agent hat dem Operator bereits via Reports-Chat geliefert.\n"
                    )

                    # Requester/Origin Info
                    requester_block = ""
                    if task.requester_channel and task.requester_id:
                        requester_block = (
                            f"**Rueckmeldung geht an:** {task.requester_channel} ({task.requester_id})\n"
                        )

                    notify_message = (
                        f"# ✅ TASK ERLEDIGT: {task.title}\n\n"
                        f"**Task-ID:** {task.id}\n"
                        f"{project_info}"
                        f"{requester_block}"
                        f"**Review:** Approved von {reviewer_name}\n"
                        f"{contract_block}\n"
                        f"## Evidence / Ergebnisse\n{evidence_text}\n"
                    )

                    # TaskComment is the runtime-agnostic delivery channel —
                    # the Board Lead's poll.sh / launchd host polls /agent/me/comments
                    # and pastes the content into their session.
                    session.add(TaskComment(
                        task_id=task.id,
                        author_type="system",
                        content=notify_message,
                        comment_type="system_notify",
                    ))
                    await session.commit()

                    # Direct Telegram notice to the operator (best-effort).
                    # Preferred channel from task context.
                    preferred = task.report_back_channel or task.requester_channel or None
                    if preferred == "telegram":
                        try:
                            from app.services.telegram_bot import telegram_bot
                            await telegram_bot.send_message(notify_message, parse_mode="Markdown")
                        except Exception as e:
                            logger.warning("Telegram completion notify failed: %s", e)

                    logger.info(
                        "Completion-Callback an %s via TaskComment fuer '%s'",
                        lead.name, task.title,
                    )

            # Log the owner-callback event
            if lead is not None:
                await emit_event(
                    session, "owner.completion_callback",
                    f"Completion-Callback an {lead.name} fuer '{task.title}'",
                    board_id=board_id, task_id=task.id, agent_id=lead.id,
                    detail={"owner_id": str(task.owner_agent_id), "callback_target": lead.name},
                )

        except Exception as e:
            logger.warning("Completion-Notification fehlgeschlagen: %s", e)


async def _existing_phase_approval_for_parent(
    session: AsyncSession, parent_id: uuid.UUID
) -> Task | None:
    """Check whether a phase_approval task already exists for this parent.

    Idempotency check: returns an existing open approval task (inbox/in_progress/review)
    so the push path (agent_scoped) + watchdog sweep (task_monitor) don't create duplicates.
    Done/failed are ignored (decision already made, a subsequent approval would be new).
    """
    existing = await session.exec(
        select(Task).where(
            Task.parent_task_id == parent_id,
            Task.delegation_type == "phase_approval",
            Task.status.in_(["inbox", "in_progress", "review"]),  # type: ignore[union-attr]
        )
    )
    return existing.first()


async def create_phase_approval_task(
    session: AsyncSession,
    parent: Task,
    board_lead: Agent | None,
) -> Task | None:
    """Create a Phase-Approval-Task for the Board Lead when all subtasks of parent are done.

    Returns the created Task, or None if board_lead is None (caller should
    fall back to legacy Rex-handoff behavior).

    The approval task has:
    - parent_task_id = parent.id (becomes a new child task of the parent)
    - assigned_agent_id = board_lead.id
    - delegation_type = "phase_approval"
    - status = "inbox" (will be auto-dispatched)

    When Board Lead resolves this task, handle_phase_approval_decision is called
    based on the comment_type of the last comment (phase_approved or phase_rewrite_request).
    """
    if board_lead is None:
        return None

    # Idempotency: duplicate protection. Two paths call us (agent_scoped push
    # on subtask-done + watchdog 30s sweep); without this check we got
    # duplicate approval tasks on 2026-04-22 that Boss had to process both of.
    existing = await _existing_phase_approval_for_parent(session, parent.id)
    if existing is not None:
        logger.info(
            "Phase-Approval existiert bereits fuer Parent '%s' (approval=%s, status=%s) — skip create",
            parent.title[:40], existing.id, existing.status,
        )
        return existing

    # Collect completed subtasks (exclude any existing phase_approval tasks)
    subtask_result = await session.exec(
        select(Task)
        .where(Task.parent_task_id == parent.id)
        .where(Task.status == "done")
    )
    subtasks = [s for s in subtask_result.all() if s.delegation_type != "phase_approval"]

    # Build a description summarizing each subtask
    subtask_lines = []
    for st in subtasks:
        subtask_lines.append(f"- **{st.title}** (`{st.id}`) — erledigt")

    subtask_block = "\n".join(subtask_lines)
    description = (
        f"## Phase Approval: {parent.title}\n\n"
        f"Alle {len(subtasks)} Subtasks dieser Phase sind abgeschlossen. Bitte pruefe:\n\n"
        f"{subtask_block}\n\n"
        f"## Entscheidung\n\n"
        f"**Option A — Alles ok:** Poste einen Kommentar mit `comment_type: phase_approved` "
        f"und setze diesen Task auf `done`. Der Parent-Task wird dann auf `review` gesetzt "
        f"und der Operator benachrichtigt.\n\n"
        f"**Option B — Subtask(s) muessen ueberarbeitet werden:** Poste einen Kommentar mit "
        f"`comment_type: phase_rewrite_request` und der Content enthaelt die Task-IDs und Gruende, "
        f"z.B. `subtask: <uuid>, grund: Deliverable fehlt`. Dann Task auf `done` setzen. "
        f"Die genannten Subtasks werden re-opened.\n\n"
        f"## Parent-Task\n\n`{parent.id}` — {parent.title}\n"
    )

    approval = Task(
        board_id=parent.board_id,
        title=f"Phase Approval: {parent.title}",
        description=description,
        status="inbox",
        priority="high",
        parent_task_id=parent.id,
        assigned_agent_id=board_lead.id,
        delegation_type="phase_approval",
        is_auto_created=True,
        project_id=parent.project_id,
    )

    session.add(approval)
    await session.commit()
    await session.refresh(approval)

    try:
        await emit_event(
            session,
            "task.phase_approval_created",
            f"Phase-Approval angelegt: '{parent.title}' → {board_lead.name}",
            board_id=parent.board_id,
            task_id=approval.id,
            agent_id=board_lead.id,
            detail={"parent_task_id": str(parent.id), "subtasks_count": len(subtasks)},
        )
    except Exception as e:
        logger.warning("Phase-approval event emission failed: %s", e)

    return approval


# Magic marker so the reminder system comment can be recognized again in
# the DB (idempotency check + escalation logic).
ORCH_CLOSE_REMINDER_MARKER = "[orch-close-reminder]"

# After N reminders with no result, the operator is notified via the reports bot.
# Deliberately generous: Boss has time to react before the operator is disturbed.
ORCH_CLOSE_ESCALATION_THRESHOLD = 3


async def _escalate_orch_close_to_mark(
    parent: Task,
    orchestrator: Agent,
    nudge_count: int,
) -> bool:
    """After N close reminders with no result: notify the operator via the reports bot.

    Idempotent per parent (Redis key `orch_close_escalated`, 48h TTL). Sent once
    and only again after expiry.

    Returns True if sent, False if skipped (not configured, already
    escalated, Redis error, or API error).
    """
    try:
        redis = await get_redis()
        escalated_key = f"mc:watchdog:orch_close_escalated:{parent.id}"
        if await redis.get(escalated_key):
            return False
    except Exception as e:
        logger.warning("Redis fuer Eskalation-Check nicht verfuegbar: %s", e)
        return False

    if not telegram_reports.configured:
        logger.debug(
            "Eskalation geskippt fuer Parent %s — Reports-Bot nicht konfiguriert",
            parent.id,
        )
        return False

    title_safe = (parent.title or "")[:80]
    message = (
        f"⚠️ <b>Orchestrator-Close-Eskalation</b>\n\n"
        f"<b>Parent:</b> {title_safe}\n"
        f"<b>Task-ID:</b> <code>{parent.id}</code>\n"
        f"<b>Orchestrator:</b> {orchestrator.name}\n"
        f"<b>Reminder gesendet:</b> {nudge_count}× ohne Reaktion\n\n"
        f"Phase wurde approved aber der Parent ist nicht abgeschlossen. "
        f"Bitte prüfe:\n"
        f"• Boss offline/stuck?\n"
        f"• Manuelles Eingreifen nötig (schließen/re-delegieren/cancellen)?"
    )

    try:
        result = await telegram_reports.send(message)
        if result and result.get("ok"):
            await redis.set(escalated_key, str(nudge_count), ex=48 * 3600)
            logger.info(
                "Eskalation an den Operator gesendet fuer Parent %s (count=%d)",
                parent.id, nudge_count,
            )
            return True
        return False
    except Exception as e:
        logger.warning("Eskalation send fehlgeschlagen fuer Parent %s: %s", parent.id, e)
        return False


async def _increment_close_nudge_count(parent_id: uuid.UUID) -> int:
    """Increments the nudge counter for this parent. Returns the new value
    or 0 if Redis is unavailable (fail-soft)."""
    try:
        redis = await get_redis()
        count_key = f"mc:watchdog:orch_close_nudge_count:{parent_id}"
        count = await redis.incr(count_key)
        if count == 1:
            await redis.expire(count_key, 48 * 3600)
        return int(count)
    except Exception as e:
        logger.warning("Redis fuer Nudge-Counter nicht verfuegbar: %s", e)
        return 0


async def _post_close_reminder_comment(
    session: AsyncSession,
    parent: Task,
    *,
    reason: Literal["phase_approved", "stuck_safety_net"],
    needs_report: bool,
    dedup_window_minutes: int | None = None,
) -> bool:
    """Post a system comment on the parent — delivered to the owner agent via
    /agent/me/poll (poll.sh pastes it into their tmux session).

    Runtime-agnostic push path: all runtimes (cli-bridge / host /
    claude-code) use poll.sh / launchd to consume TaskComments. Uses the
    existing mechanism `_collect_and_ack_new_comments` +
    `_DELIVER_SYSTEM_COMMENT_TYPES` (see routers/agents.py) —
    `comment_type="system"` is on the allowlist and gets paste-buffered
    into the Claude session.

    Idempotent: no second delivery if a reminder with the same marker
    already exists within the dedup_window.

    Default dedup (10 min) applies to the push path right after phase_approved.
    The watchdog safety-net path (reason=stuck_safety_net) uses its own
    Redis dedup (3-min TTL) — there we want a new reminder every 3 min
    until auto-close. Default for stuck_safety_net: 2 min (one tick
    shorter than the watchdog dedup).

    Returns True if a new comment was created.
    """
    from datetime import timedelta

    if dedup_window_minutes is None:
        dedup_window_minutes = 2 if reason == "stuck_safety_net" else 10

    cutoff = utcnow() - timedelta(minutes=dedup_window_minutes)
    existing = await session.exec(
        select(TaskComment)
        .where(TaskComment.task_id == parent.id)
        .where(TaskComment.comment_type == "system")
        .where(TaskComment.created_at >= cutoff)
    )
    for c in existing.all():
        if ORCH_CLOSE_REMINDER_MARKER in (c.content or ""):
            logger.debug(
                "Close-reminder skipped (parent=%s): existing reminder within %d min",
                parent.id, dedup_window_minutes,
            )
            return False

    if needs_report:
        steps = (
            "**Pflicht-Sequenz (Hard-Gate):**\n"
            "1. `mc deliverable --title \"Final-Report\" --type document --path "
            "<report.md>` — Report registrieren (gibt UUID zurueck)\n"
            f"2. `mc telegram \"Zusammenfassung-Caption\" --file <deliverable-uuid>` "
            "— Report-File an den Operator via Telegram senden\n"
            f"3. `mc done {parent.id}` — Task abschliessen\n\n"
            "Ohne Schritt 2 blockiert das Backend `mc done`."
        )
    else:
        steps = f"`mc done {parent.id}` wenn alles erledigt ist."

    if reason == "stuck_safety_net":
        intro = (
            "Phase wurde approved, aber dieser Parent ist seit 3+ Min nicht "
            "abgeschlossen. Niemand sonst macht das — DU bist dran."
        )
    else:
        intro = (
            "Du hast gerade `phase_approved` gesetzt. Der Parent bleibt "
            "`in_progress` (Trust-by-Default — kein dedicated Reviewer). Es "
            "gibt jetzt keinen Approval-Task mehr in deiner Sicht — der "
            "Parent selbst ist dein naechster Schritt."
        )

    content = (
        f"{ORCH_CLOSE_REMINDER_MARKER}\n\n"
        f"# Reminder: Parent-Task abschliessen\n\n"
        f"{intro}\n\n"
        f"{steps}"
    )

    comment = TaskComment(
        task_id=parent.id,
        author_type="system",
        comment_type="system",
        content=content,
    )
    session.add(comment)
    parent.updated_at = utcnow()
    session.add(parent)
    await session.commit()
    return True


async def send_orchestrator_close_nudge(
    session: AsyncSession,
    parent: Task,
    orchestrator: Agent,
    *,
    reason: Literal["phase_approved", "stuck_safety_net"] = "phase_approved",
) -> bool:
    """Active nudge to the orchestrator: 'you must close out this parent'.

    Trust-by-default boards leave the parent on `in_progress` instead of
    `review` after `phase_approved`. Without this nudge, the orchestrator
    no longer sees an open task in their session (the approval is done)
    and forgets the hard-gate sequence `mc telegram` + `mc done`.

    Post Phase 29 / Gateway-Sunset: ONLY path A (system comment + poll).
    All remaining runtimes (cli-bridge / host / claude-code) deliver
    system comments via /agent/me/comments through poll.sh / launchd into
    the orchestrator's tmux session. The gateway path that used to exist
    here (RPC chat-send with Telegram-session preference) is gone.

    Callers:
    - `handle_phase_approval_decision` right after `phase_approved`
    - watchdog `_check_stuck_orchestrator_close` as a safety net after 3 min

    Returns True if the message was delivered.
    """
    # The hard gate only applies to telegram-routed tasks (analogous to task_lifecycle.py:486).
    # Discord-routed tasks don't need an `mc telegram` hint.
    is_telegram_channel = (parent.report_back_channel or "telegram") == "telegram"
    needs_report = (
        bool(parent.report_back_required)
        and is_telegram_channel
        and not parent.report_sent_to_telegram
    )

    posted = await _post_close_reminder_comment(
        session, parent, reason=reason, needs_report=needs_report,
    )
    if posted:
        count = await _increment_close_nudge_count(parent.id)
        if count >= ORCH_CLOSE_ESCALATION_THRESHOLD:
            await _escalate_orch_close_to_mark(parent, orchestrator, count)
    return posted


def _extract_rewrite_reason(content: str, subtask_id: uuid.UUID) -> str:
    """Extract the per-subtask rewrite reason from a phase_rewrite_request comment.

    Board Lead's rewrite comments follow the documented pattern (see Phase-Approval
    Task description, Option B):

        subtask: <uuid>, grund: <text>

    A single comment may target multiple subtasks. We extract the block belonging
    to ``subtask_id`` so we can attach it as a per-subtask TaskComment (and not
    leak the cross-subtask gossip into each agent's context).

    Returns:
        The reason block for ``subtask_id`` (trimmed), or the full original
        content as fallback when no per-subtask block can be located. The
        fallback keeps backwards compatibility with free-form rewrite briefs
        that don't follow the structured pattern.
    """
    sid = str(subtask_id)
    # Match: "subtask: <sid>, grund: ...<text>..."
    # Terminate at the next "subtask: <uuid>" marker (any uuid) or EOF.
    pattern = re.compile(
        rf"subtask:\s*{re.escape(sid)}\s*,\s*grund:\s*(.+?)"
        r"(?=\n\s*subtask:\s*[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(content)
    if match:
        return match.group(1).strip()
    return content.strip()


async def handle_phase_approval_decision(
    session: AsyncSession,
    approval_task: Task,
    agent: Agent,
    comment_type: str,
    comment_content: str,
) -> dict:
    """Handle the Board Lead's decision on a phase-approval task.

    Called when Board Lead posts a phase_approved or phase_rewrite_request
    comment on a delegation_type=phase_approval task.

    Returns dict:
    - decision: "approved" | "rewrite" | "unknown"
    - reopened: list[uuid.UUID] — subtasks re-opened (empty for approved)
    - parent_promoted: bool — whether parent moved to review
    """
    import re as _re
    result: dict = {"decision": "unknown", "reopened": [], "parent_promoted": False}

    if approval_task.delegation_type != "phase_approval":
        logger.warning(
            "handle_phase_approval_decision called on non-approval task %s",
            approval_task.id,
        )
        return result

    parent = await session.get(Task, approval_task.parent_task_id) if approval_task.parent_task_id else None
    if parent is None:
        logger.warning("Approval task %s has no parent", approval_task.id)
        return result

    if comment_type == "phase_approved":
        result["decision"] = "approved"
        if parent.status == "in_progress":
            # Trust-by-default boards have no dedicated reviewer — `review`
            # would leave the parent hanging in limbo (bug 2, 2026-04-22:
            # parent stayed on review for 8 min). Instead: the parent stays
            # in_progress, the orchestrator closes it out themselves via the
            # hard gate (mc telegram → mc done).
            from app.models.board import Board
            _board = await session.get(Board, parent.board_id)
            _trust_by_default = _board is not None and not _board.require_review_before_done

            if _trust_by_default:
                logger.info(
                    "Phase-Approval: Parent '%s' bleibt in_progress (Trust-by-Default board) — "
                    "Orchestrator %s muss selbst mc telegram + mc done machen",
                    parent.title[:40], agent.name,
                )
                result["parent_promoted"] = False
                try:
                    await emit_event(
                        session,
                        "task.phase_approved",
                        f"Phase '{parent.title}' von {agent.name} approved → Orchestrator schliesst via mc done ab",
                        board_id=parent.board_id,
                        task_id=parent.id,
                        agent_id=agent.id,
                        severity="info",
                    )
                except Exception as e:
                    logger.warning("phase_approved event emission failed: %s", e)
                # Active re-dispatch nudge: without it the orchestrator easily
                # overlooks that the parent is still open (the approval task is
                # done, from their view nothing looks open anymore).
                try:
                    nudged = await send_orchestrator_close_nudge(
                        session, parent, agent, reason="phase_approved",
                    )
                    if nudged:
                        result["orchestrator_nudged"] = True
                except Exception as e:
                    logger.warning("Orchestrator close nudge after phase_approved failed: %s", e)
                return result

            # Classic review path for boards with require_review_before_done=true
            parent.status = "review"
            parent.updated_at = utcnow()
            session.add(parent)
            await session.commit()
            result["parent_promoted"] = True

            try:
                await emit_event(
                    session,
                    "task.phase_approved",
                    f"Phase '{parent.title}' von {agent.name} approved → Review beim Operator",
                    board_id=parent.board_id,
                    task_id=parent.id,
                    agent_id=agent.id,
                    severity="info",
                )
            except Exception as e:
                logger.warning("phase_approved event emission failed: %s", e)
        return result

    if comment_type == "phase_rewrite_request":
        result["decision"] = "rewrite"
        # Parse UUIDs from content
        uuid_pattern = _re.compile(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            _re.IGNORECASE,
        )
        mentioned_ids = set(uuid_pattern.findall(comment_content))

        # Find done subtasks to re-open (exclude approval task itself)
        subtask_result = await session.exec(
            select(Task)
            .where(Task.parent_task_id == parent.id)
            .where(Task.status == "done")
        )
        all_subtasks = subtask_result.all()

        # Lazy imports — match handle_review_handoff style (avoid top-level
        # cycles with dispatch.py).
        from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
        from app.services.dispatch import auto_dispatch_task

        # Pass 1: sammeln, wer reopened wird (fuer die Topologie-Entscheidung
        # muessen alle Reopen-Kandidaten VOR dem Statuswechsel bekannt sein).
        to_reopen = [
            st for st in all_subtasks
            if st.delegation_type != "phase_approval" and str(st.id) in mentioned_ids
        ]
        reopen_ids_set = {st.id for st in to_reopen}

        from app.models.task import TaskDependency

        reopened_ids: list[uuid.UUID] = []
        dispatch_now: list[uuid.UUID] = []
        for st in to_reopen:
            # ── Topologie-Ordnung (Fix C, Incident 2026-07-04) ─────────
            # Der alte parallele Re-Dispatch liess den Verifier VOR dem
            # Fix des Coders laufen → falscher Blocker + Operator-Approval.
            # Ein Subtask "wartet", wenn einer seiner Vorgaenger ebenfalls
            # reopened wird oder ohnehin nicht done ist.
            dep_rows = (await session.exec(
                select(TaskDependency).where(TaskDependency.task_id == st.id)
            )).all()
            waits_on_upstream = False
            for dep in dep_rows:
                if dep.depends_on_task_id in reopen_ids_set:
                    waits_on_upstream = True
                    break
                dep_task = await session.get(Task, dep.depends_on_task_id)
                if dep_task is not None and dep_task.status != "done":
                    waits_on_upstream = True
                    break

            # Alle reopneten Subtasks gehen auf `in_progress` — Marks Prod-DB
            # hat einen enforce_task_transition-Trigger, der aus `done` NUR
            # `in_progress` erlaubt (done→inbox = check_violation; von den
            # SQLite-Tests nicht abgedeckt!). Wartende Dependents bleiben
            # dispatched_at=NULL und werden NICHT dispatcht — die Done-Kaskade
            # weckt sie, sobald der Vorgaenger erneut done ist; der Watchdog
            # _check_undispatched_tasks ist das Safety-Net. Sofort startbare
            # Subtasks behalten den expliziten Re-Dispatch (Incident
            # 2026-05-20: ohne aktiven Dispatch hing ein reopened Subtask 1h).
            await record_task_event(
                session, st.id, st.status, "in_progress",
                changed_by="agent", agent_id=agent.id,
                reason="phase_rewrite_request",
            )
            st.status = "in_progress"
            st.completed_at = None
            st.dispatched_at = None
            st.ack_at = None
            clear_spawn_tracking(st)
            await clear_dispatch_attempt_id(
                session, st,
                caller="task_lifecycle.phase_rewrite",
                reason="phase_rewrite_request",
            )
            st.updated_at = utcnow()
            session.add(st)

            # Per-subtask rewrite directive — extracted from the
            # multi-subtask brief Board Lead may have posted.
            reason_text = _extract_rewrite_reason(comment_content, st.id)
            wait_notice = (
                "⚠️ **NICHT SOFORT STARTEN** — dein Vorgaenger-Task wird "
                "zuerst neu bearbeitet. Du wirst automatisch re-dispatcht, "
                "sobald er fertig ist. Bis dahin: nichts tun.\n\n"
            ) if waits_on_upstream else ""
            directive = TaskComment(
                task_id=st.id,
                author_type="agent",
                author_agent_id=agent.id,
                comment_type="feedback",
                content=(
                    f"**Rewrite-Auftrag von {agent.name}**\n\n"
                    f"{wait_notice}"
                    f"{reason_text}\n\n"
                    "_Dein Task wurde von der Phase-Approval-Review wieder "
                    "geoeffnet. Bitte arbeite die Punkte ab und schliesse "
                    "erneut mit `mc done` ab._"
                ),
            )
            session.add(directive)
            reopened_ids.append(st.id)
            if not waits_on_upstream:
                dispatch_now.append(st.id)

        await session.commit()
        result["reopened"] = reopened_ids

        # Re-dispatch ONLY subtasks whose dependencies are met — the rest is
        # woken by the done-cascade in topological order. auto_dispatch_task
        # additionally gates on dependencies_met() (defense in depth).
        for sid in dispatch_now:
            asyncio.create_task(auto_dispatch_task(sid, parent.board_id))
        if len(dispatch_now) < len(reopened_ids):
            logger.info(
                "Phase-Rewrite: %d/%d Subtasks warten auf Vorgaenger (Kaskaden-Dispatch)",
                len(reopened_ids) - len(dispatch_now), len(reopened_ids),
            )

        try:
            await emit_event(
                session,
                "task.phase_rewrite_requested",
                f"Phase '{parent.title}': {len(reopened_ids)} Subtasks re-opened von {agent.name}",
                board_id=parent.board_id,
                task_id=parent.id,
                agent_id=agent.id,
                severity="warning",
                detail={"reopened_count": len(reopened_ids), "subtask_ids": [str(i) for i in reopened_ids]},
            )
        except Exception as e:
            logger.warning("phase_rewrite event emission failed: %s", e)
        return result

    return result
