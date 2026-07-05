import logging
import sys
import uuid
from datetime import datetime

from app.utils import create_tracked_task, utcnow

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, field_validator
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.auth import Role, require_role, require_user
from app.database import get_session
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment, TaskDependency, TaskEvent
from app.models.tag import Tag, TagAssignment

logger = logging.getLogger(__name__)
from app.redis_client import RedisKeys
from app.services.activity import emit_event
from app.services.dispatch import auto_dispatch_task
from app.services.sse import broadcast, make_sse_response


# Single Source of Truth — imported from task_status.py
from app.task_status import VALID_TRANSITIONS, STATUS_LABELS, check_children_complete


async def _enforce_board_rules(
    session: AsyncSession,
    board_id: uuid.UUID,
    task: Task,
    new_status: str,
    *,
    agent: "Agent | None" = None,
):
    """Check board workflow rules + status transitions.

    ADR-023 Note: The mandatory reflection is NOT enforced here. This is the
    User/UI-auth variant — the operator can manually close a task via the UI
    (e.g. when taking over the work or cleaning up an aborted task).
    The mandatory reflection is an agent responsibility and is only checked in
    `agent_scoped._enforce_board_rules` (PATCH via agent token).
    If the operator manually closes a task, that's a deliberate opt-out of
    the learning loop.
    """
    board = await session.get(Board, board_id)
    if not board:
        return

    # Rule 1: check valid status transitions
    current = task.status
    allowed = VALID_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        from_label = STATUS_LABELS.get(current, current)
        to_label = STATUS_LABELS.get(new_status, new_status)
        raise HTTPException(
            status_code=400,
            detail=f"Ungültiger Status-Übergang: {from_label} → {to_label}",
        )

    # Rule 2: parent/child integrity — parent must not become done while children are open
    if new_status == "done":
        children_ok, children_detail = await check_children_complete(task.id, session)
        if not children_ok:
            raise HTTPException(status_code=400, detail=children_detail)

    # Rule 3: task must go through review before it can be set to done
    # Exception: parent tasks with all subtasks done (review happened at subtask level)
    if board.require_review_before_done:
        if new_status == "done" and task.status not in ("review", "user_test"):
            subtask_result = await session.exec(
                select(Task).where(Task.parent_task_id == task.id)
            )
            subtasks = subtask_result.all()
            is_completed_parent = subtasks and all(s.status == "done" for s in subtasks)
            if not is_completed_parent:
                raise HTTPException(
                    status_code=400,
                    detail="Task muss zuerst durch Review bevor es auf Done gesetzt werden kann",
                )

    # Rule 3: blocker approval guard — blocked → in_progress only if no approval pending
    if new_status == "in_progress" and task.status == "blocked":
        from app.models.approval import Approval
        pending_approval = (await session.exec(
            select(Approval).where(
                Approval.task_id == task.id,
                Approval.action_type == "blocker_decision",
                Approval.status == "pending",
            )
        )).first()
        if pending_approval:
            raise HTTPException(
                status_code=403,
                detail="Task hat ein offenes Blocker-Approval. Warte auf die Entscheidung des Operators.",
            )

    # Rule 4: only the board lead may change the status (only relevant for agents)
    if board.only_lead_can_change_status and agent is not None:
        if not agent.is_board_lead:
            raise HTTPException(
                status_code=403,
                detail="Nur der Board Lead darf den Task-Status aendern",
            )

router = APIRouter(prefix="/api/v1", tags=["tasks"])


class TaskCreate(BaseModel):
    title: str
    description: str | None = None
    status: str = "inbox"
    priority: str = "medium"
    task_type: str = "story"
    project_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None
    assigned_agent_id: uuid.UUID | None = None
    due_at: datetime | None = None
    # Pre-dispatch gating (Phase 1 systemic orchestration)
    dispatch_phase: Literal["planning", "ready"] | None = None
    # Delegation contract (Phase 1.5)
    delegation_type: str | None = None
    branch_name: str | None = None
    target_url: str | None = None
    acceptance_criteria: str | None = None
    requires_auth: bool = False
    source_task_id: uuid.UUID | None = None
    triggered_by_deliverable_id: uuid.UUID | None = None
    expected_content: str | None = None
    # Completion contract
    report_back_required: bool = False
    report_back_channel: str | None = None
    report_back_chat_id: str | None = None
    report_back_requirements: str | None = None
    # Credentials (plaintext in → stored encrypted)
    credentials: str | None = None
    credential_id: uuid.UUID | None = None
    # Requester / origin tracking
    requester_channel: str | None = None  # "telegram" | "discord" | "web" | "agent"
    requester_id: str | None = None       # chat ID, user ID, or agent UUID
    # Operator intake (Phase 2 — primarily for root/intake tasks)
    intake_mode: Literal["quick", "structured"] | None = None
    request_kind: Literal["code_change", "content_create", "research", "browser_task", "credential_task", "mixed"] | None = None
    desired_output: str | None = None
    scope_out: str | None = None
    risk_notes: str | None = None
    reference_urls: list[str] | None = None
    reference_notes: str | None = None
    approval_policy: Literal["never", "on_plan", "on_execution", "on_publish", "on_sensitive_action", "always"] | None = None
    autonomy_level: Literal["advise_only", "draft_only", "execute_low_risk", "execute_with_approval_on_risk", "manual_dispatch_required"] | None = None
    publish_allowed: bool | None = None
    needs_browser: bool | None = None
    credential_consent: bool | None = None
    e2e_test_required: bool | None = None
    # Fields restored after review FB-2 (2026-04-21) — they exist on
    # Task model but had been dropped from TaskCreate schema, so the UI
    # was sending them and pydantic silently discarded them.
    phase_id: uuid.UUID | None = None
    use_separate_repo: bool | None = None  # deprecated — repo_id (ADR-052)
    repo_id: uuid.UUID | None = None  # Registry-Repo für Ad-hoc-Tasks (ADR-052)
    defer_dispatch: bool = False
    # ADR-053: True wenn der Client nach dem Create noch Referenz-Dateien
    # hochlädt — Auto-Dispatch würde die Uploads sonst überholen und der
    # Agent-Brief bliebe ohne Referenzen. Client promotet danach explizit.
    # planner_mode was removed by Migration 0071; accept+ignore here so
    # older UI code doesn't trigger a 422 while the deprecation finishes.
    planner_mode: str | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    task_type: str | None = None
    project_id: uuid.UUID | None = None
    assigned_agent_id: uuid.UUID | None = None
    due_at: datetime | None = None
    sort_order: int | None = None


class TaskReorderItem(BaseModel):
    id: uuid.UUID
    sort_order: int
    status: str | None = None


class CommentCreate(BaseModel):
    content: str
    author_type: str = "user"
    author_agent_id: uuid.UUID | None = None
    # Bug 4 (2026-05-13): comment_type was not declared here — Pydantic
    # silently dropped the field and the DB default "message" took over, even
    # when the user explicitly wanted to send feedback/handoff/etc. The
    # validator uses the same SoT as the agent-scoped POST (REL-01 pattern).
    comment_type: str = "message"

    @field_validator("comment_type")
    @classmethod
    def _validate_comment_type(cls, v: str) -> str:
        from app.comment_types import ALL_COMMENT_TYPES

        if v not in ALL_COMMENT_TYPES:
            valid = sorted(ALL_COMMENT_TYPES)
            raise ValueError(f"Ungueltiger comment_type: '{v}'. Gueltig: {valid}")
        return v

    @field_validator("content")
    @classmethod
    def _validate_content(cls, v: str) -> str:
        # Defense-in-depth against JSON-envelope content (Bug 2026-05-17).
        # See app/comment_types.py:validate_comment_content for rationale.
        from app.comment_types import validate_comment_content

        return validate_comment_content(v)


# Priority ordering for task queries
PRIORITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}


@router.get("/boards/{board_id}/tasks/{task_id}/hierarchy")
async def get_task_hierarchy(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Task hierarchy: parent, children, report-back, credentials, requester."""
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    # Parent
    parent = None
    if task.parent_task_id:
        p = await session.get(Task, task.parent_task_id)
        if p:
            parent = {
                "id": str(p.id),
                "title": p.title,
                "status": p.status,
                "priority": p.priority,
            }

    # Children
    children_result = await session.exec(
        select(Task).where(Task.parent_task_id == task_id).order_by(Task.sort_order)
    )
    children = [
        {
            "id": str(c.id),
            "title": c.title,
            "status": c.status,
            "priority": c.priority,
        }
        for c in children_result.all()
    ]

    # Report-Back
    report_back = None
    if task.report_back_required:
        report_back = {
            "required": task.report_back_required,
            "channel": task.report_back_channel,
            "status": task.report_back_status,
            "requirements": task.report_back_requirements,
        }

    # Requester
    requester = None
    if task.requester_channel:
        requester = {
            "channel": task.requester_channel,
            "id": task.requester_id,
        }

    return {
        "parent": parent,
        "children": children,
        "report_back": report_back,
        "has_credentials": task.credentials_encrypted is not None,
        "requester": requester,
    }


@router.get("/boards/{board_id}/tasks/pipeline")
async def get_pipeline(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Pipeline view: tasks grouped by status with agent info."""
    # Load all active tasks (not done)
    active_result = await session.exec(
        select(Task).where(
            Task.board_id == board_id,
            Task.status != "done",
        ).order_by(Task.updated_at.desc())
    )
    active_tasks = active_result.all()

    # Just count done
    done_result = await session.exec(
        select(Task).where(Task.board_id == board_id, Task.status == "done")
    )
    done_count = len(done_result.all())

    failed_count = sum(1 for t in active_tasks if t.status == "failed")

    # Build agent map
    agent_ids = {t.assigned_agent_id for t in active_tasks if t.assigned_agent_id}
    agent_map: dict[str, dict] = {}
    if agent_ids:
        agents_result = await session.exec(
            select(Agent).where(Agent.id.in_(agent_ids))  # type: ignore[attr-defined]
        )
        for a in agents_result.all():
            agent_map[str(a.id)] = {"name": a.name, "emoji": a.emoji or "🤖"}

    # Check dependencies: which tasks have unfulfilled deps?
    blocked_dep_tasks: set[str] = set()
    if active_tasks:
        from app.models.task import TaskDependency
        dep_result = await session.exec(
            select(TaskDependency).where(
                TaskDependency.task_id.in_([t.id for t in active_tasks])  # type: ignore[attr-defined]
            )
        )
        deps = dep_result.all()
        for dep in deps:
            dep_task = await session.get(Task, dep.depends_on_task_id)
            if dep_task and dep_task.status != "done":
                blocked_dep_tasks.add(str(dep.task_id))

    # Load tags for all active tasks
    tag_map: dict[str, list[dict]] = {}
    if active_tasks:
        tag_result = await session.exec(
            select(Tag.name, Tag.color, TagAssignment.task_id).join(
                TagAssignment, TagAssignment.tag_id == Tag.id
            ).where(
                TagAssignment.task_id.in_([t.id for t in active_tasks])  # type: ignore[union-attr]
            )
        )
        for row in tag_result.all():
            tid = str(row[2])
            tag_map.setdefault(tid, []).append({"name": row[0], "color": row[1]})

    # Group and sort by status (prio desc, updated desc)
    def sort_key(t: Task) -> tuple:
        return (-PRIORITY_ORDER.get(t.priority, 2), -(t.updated_at.timestamp() if t.updated_at else 0))

    pipeline: dict[str, list] = {"inbox": [], "in_progress": [], "review": [], "user_test": [], "blocked": [], "failed": [], "aborted": []}
    for t in sorted(active_tasks, key=sort_key):
        if t.status not in pipeline:
            continue
        entry = {
            "id": str(t.id),
            "title": t.title,
            "priority": t.priority,
            "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
            "agent": agent_map.get(str(t.assigned_agent_id)) if t.assigned_agent_id else None,
            "has_blocked_deps": str(t.id) in blocked_dep_tasks,
            "dispatched_at": t.dispatched_at.isoformat() if t.dispatched_at else None,
            "tags": tag_map.get(str(t.id), []),
            "review_decision": t.review_decision,
            "dispatch_phase": t.dispatch_phase,
        }
        pipeline[t.status].append(entry)

    return {
        "pipeline": pipeline,
        "done_count": done_count,
        "failed_count": failed_count,
    }


@router.get("/boards/{board_id}/tasks")
async def list_tasks(
    board_id: uuid.UUID,
    status: str | None = Query(None),
    agent_id: uuid.UUID | None = Query(None),
    project_id: uuid.UUID | None = Query(None),
    parent_task_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    query = select(Task).where(Task.board_id == board_id)
    if status:
        query = query.where(Task.status == status)
    if agent_id:
        query = query.where(Task.assigned_agent_id == agent_id)
    if project_id:
        query = query.where(Task.project_id == project_id)
    if parent_task_id:
        # Load subtasks of a specific task
        query = query.where(Task.parent_task_id == parent_task_id)
    query = query.order_by(Task.sort_order, Task.created_at)
    result = await session.exec(query)
    tasks = result.all()

    # Compute last_activity_at: max(started_at, updated_at, last comment)
    task_ids = [t.id for t in tasks if t.status == "in_progress"]
    last_comment_map: dict[uuid.UUID, datetime] = {}
    if task_ids:
        from sqlalchemy import func as sa_func
        comment_query = (
            select(TaskComment.task_id, sa_func.max(TaskComment.created_at).label("last_comment"))
            .where(TaskComment.task_id.in_(task_ids))  # type: ignore[union-attr]
            .group_by(TaskComment.task_id)
        )
        comment_result = await session.exec(comment_query)  # type: ignore[arg-type]
        for row in comment_result.all():
            last_comment_map[row[0]] = row[1]

    # Load tags for all tasks
    list_tag_map: dict[str, list[dict]] = {}
    if tasks:
        tag_result = await session.exec(
            select(Tag.name, Tag.color, TagAssignment.task_id).join(
                TagAssignment, TagAssignment.tag_id == Tag.id
            ).where(
                TagAssignment.task_id.in_([t.id for t in tasks])  # type: ignore[union-attr]
            )
        )
        for row in tag_result.all():
            tid = str(row[2])
            list_tag_map.setdefault(tid, []).append({"name": row[0], "color": row[1]})

    enriched = []
    for t in tasks:
        data = t.model_dump()
        if t.status == "in_progress":
            candidates = [ts for ts in [t.started_at, t.updated_at] if ts]
            last_comment = last_comment_map.get(t.id)
            if last_comment:
                candidates.append(last_comment)
            data["last_activity_at"] = max(candidates).isoformat() if candidates else None
        else:
            data["last_activity_at"] = None
        data["tags"] = list_tag_map.get(str(t.id), [])
        enriched.append(data)

    return enriched


@router.get("/boards/{board_id}/tasks/stream")
async def stream_tasks(board_id: uuid.UUID, current_user = Depends(require_user)):
    return make_sse_response([RedisKeys.board_events(str(board_id))])


@router.post("/boards/{board_id}/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(
    board_id: uuid.UUID,
    payload: TaskCreate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    if payload.task_type not in ("story", "bug", "revision", "chore"):
        raise HTTPException(status_code=422, detail=f"Invalid task_type: {payload.task_type}")

    # Closed-parent guard: no new children under a done/failed root.
    # A parent on review gets reopened (see below), not blocked.
    if payload.parent_task_id:
        parent_for_guard = await session.get(Task, payload.parent_task_id)
        if parent_for_guard and parent_for_guard.status in ("done", "failed"):
            raise HTTPException(
                422,
                f"Parent-Task ist bereits {parent_for_guard.status}. "
                "Neue Arbeit muss als eigener Task geplant werden.",
            )
        # Parent reopen: if the parent is waiting on review and a new subtask is
        # added, the parent must go back to in_progress — otherwise it stays stuck
        # in review while new work runs below (phase-approval deadlock).
        if parent_for_guard and parent_for_guard.status == "review":
            from app.services.task_lifecycle import reopen_parent_for_new_subtask
            await reopen_parent_for_new_subtask(
                session, payload.parent_task_id, new_subtask_title=payload.title,
            )

    # ── Delegation contract guard (Phase 1.5) — also applies to dashboard/API ──
    if payload.delegation_type:
        from app.services.delegation_contracts import validate_delegation_contract
        _inherited_creds_user = False
        if not payload.credentials and payload.parent_task_id:
            _parent_for_creds = await session.get(Task, payload.parent_task_id)
            _inherited_creds_user = bool(_parent_for_creds and _parent_for_creds.credentials_encrypted)
        contract_fields = {
            "branch_name": payload.branch_name,
            "target_url": payload.target_url,
            "acceptance_criteria": payload.acceptance_criteria,
            "credentials": payload.credentials or ("__inherited__" if _inherited_creds_user else None),
            "requires_auth": payload.requires_auth,
            "source_task_id": payload.source_task_id,
            "description": payload.description,
        }
        hard_errors, _warnings = validate_delegation_contract(payload.delegation_type, contract_fields)
        if hard_errors:
            raise HTTPException(
                status_code=422,
                detail=f"Delegation Contract '{payload.delegation_type}' nicht erfuellt: "
                       + "; ".join(hard_errors),
            )

    # Registry-Repo-Auswahl (ADR-052): existiert + aktiv?
    if payload.repo_id:
        from app.models.repo import Repo
        chosen_repo = await session.get(Repo, payload.repo_id)
        if not chosen_repo or not chosen_repo.is_active:
            raise HTTPException(
                status_code=400,
                detail="repo_id verweist auf kein aktives Registry-Repo",
            )

    # `planner_mode` is accepted for UI backward compat but has no
    # Task-model column (removed in Migration 0071). Strip before passing
    # to Task(). `use_separate_repo`/`phase_id` DO exist on the model
    # and flow through normally.
    task_data = payload.model_dump(exclude={"credentials", "credential_id", "planner_mode", "defer_dispatch"})

    # Encrypt credentials if present
    if payload.credentials:
        from app.services.encryption import encrypt
        task_data["credentials_encrypted"] = encrypt(payload.credentials)

    # Reference vault credential
    if payload.credential_id:
        task_data["credential_id"] = payload.credential_id

    # project_id assignment (priority: explicit > parent > board default)
    if not payload.project_id:
        if payload.parent_task_id:
            parent = await session.get(Task, payload.parent_task_id)
            if parent and parent.project_id:
                task_data["project_id"] = parent.project_id
        if not task_data.get("project_id"):
            board_obj_for_default = await session.get(Board, board_id)
            if board_obj_for_default and board_obj_for_default.default_project_id:
                task_data["project_id"] = board_obj_for_default.default_project_id

    task = Task(board_id=board_id, created_by_user_id=current_user.id, **task_data)

    # Pre-dispatch gating: executable child work items start in planning,
    # root/container tasks remain without active gating.
    from app.config import settings as _settings
    if _settings.enable_dispatch_gating:
        is_work_item = task.parent_task_id is not None and task.assigned_agent_id is not None
        if is_work_item:
            task.dispatch_phase = "planning"
        else:
            task.dispatch_phase = None

    session.add(task)
    await session.commit()
    await session.refresh(task)

    await emit_event(
        session,
        "task.created",
        f"Task created: {task.title}",
        board_id=board_id,
        task_id=task.id,
    )

    # Auto-dispatch: assign task and send via PUSH
    # Also for pre-assigned tasks (assigned_agent_id set) — so the push gets triggered
    skip_dispatch = _settings.enable_dispatch_gating and task.dispatch_phase == "planning"
    # ADR-053: Client lädt gleich noch Referenz-Dateien hoch — nicht
    # losrennen, sonst überholt der Dispatch die Uploads und der Brief
    # bleibt ohne Referenzen. Client ruft danach POST .../promote.
    if payload.defer_dispatch:
        skip_dispatch = True
    board_obj = await session.get(Board, board_id)
    if board_obj and board_obj.auto_dispatch_enabled and not skip_dispatch:
        create_tracked_task(auto_dispatch_task(task.id, board_id))

    return task


# ── Reorder (must come BEFORE {task_id} routes) ─────────────────────────────

@router.patch("/boards/{board_id}/tasks/reorder")
async def reorder_tasks(
    board_id: uuid.UUID,
    items: list[TaskReorderItem],
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    for item in items:
        task = await session.get(Task, item.id)
        if task and task.board_id == board_id:
            task.sort_order = item.sort_order
            if item.status and item.status != task.status:
                await _enforce_board_rules(session, board_id, task, item.status)
                from app.services.task_lifecycle import record_task_event
                await record_task_event(
                    session, task.id, task.status, item.status,
                    changed_by="user", reason="reorder",
                )
                task.status = item.status
            task.updated_at = utcnow()
            session.add(task)
    await session.commit()
    return {"updated": len(items)}


# ── Task Run Control (Stop/Resume) ──────────────────────────────────────────
# IMPORTANT: registered before the {task_id} catch-all (router ordering)


# ── Review Decision (User-scoped) ─────────────────────────────────────────


class ReviewDecisionBody(BaseModel):
    decision: str  # "approve" | "request_changes" | "hold"
    comment: str

    @classmethod
    def validate_decision(cls, v: str) -> str:
        if v not in ("approve", "request_changes", "hold"):
            raise ValueError("decision must be approve, request_changes, or hold")
        return v


@router.post("/boards/{board_id}/tasks/{task_id}/review")
async def user_review_decision(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    body: ReviewDecisionBody,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Explicit review decision by operator."""
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    if not body.comment or not body.comment.strip():
        raise HTTPException(status_code=422, detail="comment is required")
    if body.decision not in ("approve", "request_changes", "hold"):
        raise HTTPException(status_code=422, detail="decision must be approve, request_changes, or hold")

    from app.services.task_lifecycle import execute_review_decision
    await execute_review_decision(
        session, task, board_id, body.decision, body.comment,
        actor_user_id=current_user.id,
    )
    return {"status": "ok", "decision": body.decision}


# ── Pre-Dispatch Gating: Promote (User-only) ─────────────────────────────


@router.post("/boards/{board_id}/tasks/{task_id}/dispatch")
async def dispatch_deferred_task(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Dispatch nachholen für Tasks, die mit defer_dispatch erstellt wurden
    (ADR-053: erst Referenz-Uploads, dann losschicken)."""
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "inbox" or task.dispatched_at is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Task ist nicht mehr dispatchbar (Status '{task.status}')",
        )
    create_tracked_task(auto_dispatch_task(task.id, task.board_id))
    return {"status": "dispatch_triggered", "task_id": str(task.id)}


@router.post("/boards/{board_id}/tasks/{task_id}/promote")
async def promote_task(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Promote child task from planning to ready — triggers dispatch.

    User/Operator only. No Agent-Promote in Phase 1.
    Only executable work items (parent_task_id set, assigned_agent_id set).
    """
    from app.config import settings as _settings
    if not _settings.enable_dispatch_gating:
        raise HTTPException(status_code=409, detail="Dispatch gating is not enabled")

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.services.dispatch_gating import promote_task_to_ready
    task = await promote_task_to_ready(task, session)

    # Trigger dispatch
    create_tracked_task(auto_dispatch_task(task.id, task.board_id))

    return task


# ── Task Run Control (Stop/Resume) ────────────────────────────────────────


class StopRunPayload(BaseModel):
    reason: str = ""


@router.post("/boards/{board_id}/tasks/{task_id}/stop")
async def stop_task_run_endpoint(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: StopRunPayload = StopRunPayload(),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Stop an active task run. Only for tasks with an active run (admin only)."""
    from app.services.operations import stop_task_run
    task = await stop_task_run(session, task_id, str(current_user.id), payload.reason)
    return task


@router.post("/boards/{board_id}/tasks/{task_id}/resume")
async def resume_task_run_endpoint(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Release a stopped task again (admin only)."""
    from app.services.operations import resume_task_run
    task = await resume_task_run(session, task_id, str(current_user.id))
    return task


# ── Task Deliverables (User-facing) ──────────────────────────────────────────

@router.get("/boards/{board_id}/tasks/{task_id}/deliverables")
async def list_task_deliverables(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    include_subtasks: bool = False,
    depth: int = 2,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """User-facing: all deliverables of a task.

    Query params:
      include_subtasks: If true, deliverables of all descendant subtasks
          (recursive up to `depth`) are included — each gets
          `source_task_id`, `source_task_title`, `source_depth` for UI
          grouping. This lets orchestrator parent tasks see the whole
          tree at a glance.
      depth: Max subtask depth (1=direct children, 2=grandchildren). Default 2,
          max 5 as a response-size guard.
    """
    from app.models.deliverable import TaskDeliverable
    from sqlmodel import col as _col

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    effective_depth = max(1, min(int(depth or 2), 5))

    # task_id -> (title, depth) via BFS
    task_meta: dict[uuid.UUID, tuple[str, int]] = {task_id: (task.title or "", 0)}
    if include_subtasks:
        current_level = [task_id]
        for d in range(1, effective_depth + 1):
            if not current_level:
                break
            children_result = await session.exec(
                select(Task).where(_col(Task.parent_task_id).in_(current_level))
            )
            children = list(children_result.all())
            next_level: list[uuid.UUID] = []
            for child in children:
                if child.id not in task_meta:
                    task_meta[child.id] = (child.title or "", d)
                    next_level.append(child.id)
            current_level = next_level

    result = await session.exec(
        select(TaskDeliverable)
        .where(_col(TaskDeliverable.task_id).in_(list(task_meta.keys())))
        .order_by(TaskDeliverable.created_at.desc())  # type: ignore[union-attr]
    )
    deliverables = result.all()

    # Resolve agent names
    agent_ids = {d.agent_id for d in deliverables}
    agent_map: dict[str, str] = {}
    if agent_ids:
        from app.models.agent import Agent as AgentModel
        agents_result = await session.exec(
            select(AgentModel).where(AgentModel.id.in_(agent_ids))  # type: ignore[attr-defined]
        )
        for a in agents_result.all():
            agent_map[str(a.id)] = a.name

    def _serialize(d: TaskDeliverable) -> dict:
        source_title, source_depth = task_meta.get(d.task_id, ("", 0))
        row = {
            "id": str(d.id),
            "task_id": str(d.task_id),
            "agent_id": str(d.agent_id),
            "agent_name": agent_map.get(str(d.agent_id)),
            "deliverable_type": d.deliverable_type,
            "title": d.title,
            "path": d.path,
            "description": d.description,
            "content": d.content,
            "scope": d.scope,
            "tags": d.tags,
            "is_pinned": d.is_pinned,
            "is_reusable": d.is_reusable,
            "git_commit_hash": d.git_commit_hash,
            "created_at": str(d.created_at),
        }
        if include_subtasks:
            row["source_task_id"] = str(d.task_id)
            row["source_task_title"] = source_title
            row["source_depth"] = source_depth
        return row

    return [_serialize(d) for d in deliverables]


# ── Admin Deliverable Create (HERM-11/F4 — Plan 26-04) ───────────────────
#
# Mirrors agent_scoped.py:1260 (agent-scoped POST) but uses admin JWT auth
# so MCP tools (mc-mcp.py mc_register_deliverable) and the UI can register
# deliverables without an agent token. Same payload schema, same path-
# prefix validator (incl. HERM-14 host-form support), same response shape.
# agent_id is NULL for admin-created rows — the model column was made
# nullable in migration 0098 to allow this.

class AdminDeliverableCreate(BaseModel):
    """Admin-scoped deliverable create payload — superset of agent's DeliverableCreate."""
    deliverable_type: Literal["screenshot", "file", "url", "artifact", "document", "data", "video"]
    title: str
    path: str | None = None
    description: str | None = None
    content: str | None = None
    scope: str = "task"
    tags: list[str] | None = None
    is_pinned: bool = False
    is_reusable: bool = False
    git_commit: bool = False  # accepted for API symmetry; ignored on admin path (no agent context)


@router.post(
    "/boards/{board_id}/tasks/{task_id}/deliverables",
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_task_deliverable(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: AdminDeliverableCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Admin-scoped deliverable create. Mirrors agent_scoped.py POST but uses
    User auth (JWT) so MCP tools and UI can post deliverables without an
    agent token. agent_id is NULL on rows created here. Used by Hermes via
    MCP after Plan 26-04 (HERM-11/F4)."""
    import os as _os_admin
    from app.models.deliverable import TaskDeliverable

    # Title validation (mirrors agent-scoped DeliverableCreate.title_not_empty)
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title darf nicht leer sein")
    if len(title) > 500:
        raise HTTPException(status_code=422, detail="title max 500 Zeichen")

    if payload.scope not in ("task", "phase", "project"):
        raise HTTPException(status_code=422, detail="scope muss task | phase | project sein")

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found in board")

    if payload.deliverable_type in ("document", "data") and not (
        payload.content and payload.content.strip()
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"deliverable_type='{payload.deliverable_type}' requires inline 'content' "
                "(Markdown/Text)."
            ),
        )

    from app.services.deliverable_paths import validate_deliverable_path
    validate_deliverable_path(payload.path, payload.content, task_id)

    deliverable = TaskDeliverable(
        task_id=task_id,
        agent_id=None,  # Admin-created — no agent context (model nullable since 0098)
        deliverable_type=payload.deliverable_type,
        title=title,
        path=payload.path,
        description=payload.description,
        content=payload.content,
        scope=payload.scope,
        tags=payload.tags,
        is_pinned=payload.is_pinned,
        is_reusable=payload.is_reusable,
        git_commit_hash=None,
    )
    session.add(deliverable)
    await session.commit()
    await session.refresh(deliverable)

    logger.info(
        "Deliverable created (admin): task=%s user=%s type=%s title='%s' scope=%s",
        task_id, getattr(current_user, "email", "?"),
        payload.deliverable_type, title[:60], payload.scope,
    )

    # Phase A vault-as-brain: enqueue wrapper sync. Same pattern as the
    # agent-scoped create endpoint — best-effort, errors stay inside the
    # background task and never propagate to the admin caller.
    from app.routers.agent_scoped import _sync_deliverable_to_vault_bg as _sync_bg
    background_tasks.add_task(_sync_bg, deliverable.id)

    return {
        "id": str(deliverable.id),
        "task_id": str(deliverable.task_id),
        "agent_id": None,
        "deliverable_type": deliverable.deliverable_type,
        "title": deliverable.title,
        "path": deliverable.path,
        "scope": deliverable.scope,
        "is_pinned": deliverable.is_pinned,
        "created_at": str(deliverable.created_at),
        "git_commit_hash": None,
    }


async def _resolve_deliverable_fs_path(
    deliverable,
    session: AsyncSession,
    *,
    target: str = "container",
) -> str | None:
    """Thin wrapper → ``fs_service.resolve_deliverable``.

    The translation logic (runtime-aware slug injection, host-form mapping,
    sidecar handling) lives in the single ``fs_service`` resolver now. Kept as
    a module-local name so the deliverable endpoints below stay unchanged.
    """
    from app.services.fs_service import resolve_deliverable

    return await resolve_deliverable(deliverable, session, target=target)


@router.get("/boards/{board_id}/tasks/{task_id}/deliverables/{deliverable_id}/image")
async def get_deliverable_image(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Serve the image file of a screenshot deliverable."""
    import os
    from fastapi.responses import FileResponse
    from app.models.deliverable import TaskDeliverable

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    deliverable = await session.get(TaskDeliverable, deliverable_id)
    if not deliverable or deliverable.task_id != task_id:
        raise HTTPException(status_code=404, detail="Deliverable not found")

    if deliverable.deliverable_type != "screenshot":
        raise HTTPException(status_code=400, detail="Only screenshot deliverables have images")

    resolved = await _resolve_deliverable_fs_path(deliverable, session)
    if not resolved:
        raise HTTPException(status_code=404, detail="No path set or unresolvable")
    if ".." in deliverable.path:
        raise HTTPException(status_code=400, detail="Invalid path")

    clean_path = os.path.realpath(resolved)
    if not os.path.isfile(clean_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        clean_path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# Pydantic model for the open endpoint (used in Task 3)
class _OpenDeliverableBody(BaseModel):
    reveal: bool = False
    subpath: str | None = None


@router.get("/boards/{board_id}/tasks/{task_id}/deliverables/{deliverable_id}/file")
async def get_deliverable_file(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    subpath: str | None = None,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Serve any file of a deliverable (with optional subpath for dir deliverables)."""
    import mimetypes
    import os
    from fastapi.responses import FileResponse
    from app.models.deliverable import TaskDeliverable

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    deliverable = await session.get(TaskDeliverable, deliverable_id)
    if not deliverable or deliverable.task_id != task_id:
        raise HTTPException(status_code=404, detail="Deliverable not found")

    resolved = await _resolve_deliverable_fs_path(deliverable, session)
    if not resolved:
        raise HTTPException(status_code=404, detail="No path set or unresolvable")
    if ".." in deliverable.path:
        raise HTTPException(status_code=400, detail="Invalid path")

    root_real = os.path.realpath(resolved)

    if subpath is not None:
        if ".." in subpath:
            raise HTTPException(status_code=400, detail="Invalid subpath")
        target = os.path.realpath(os.path.join(root_real, subpath))
        if not (target == root_real or target.startswith(root_real + os.sep)):
            raise HTTPException(status_code=400, detail="Subpath escapes root")
    else:
        target = root_real

    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="File not found on disk")

    media_type, _ = mimetypes.guess_type(target)
    if not media_type:
        media_type = "application/octet-stream"

    return FileResponse(
        target,
        media_type=media_type,
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/boards/{board_id}/tasks/{task_id}/deliverables/{deliverable_id}/open")
async def open_deliverable(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    body: _OpenDeliverableBody,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Open a file or directory with the native macOS app (open -R = Finder reveal)."""
    import os
    import subprocess
    from app.models.deliverable import TaskDeliverable

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    deliverable = await session.get(TaskDeliverable, deliverable_id)
    if not deliverable or deliverable.task_id != task_id:
        raise HTTPException(status_code=404, detail="Deliverable not found")

    # Existence check in the backend container (where we can read).
    resolved_container = await _resolve_deliverable_fs_path(deliverable, session, target="container")
    if not resolved_container:
        raise HTTPException(status_code=404, detail="No path set or unresolvable")
    if ".." in deliverable.path:
        raise HTTPException(status_code=400, detail="Invalid path")

    root_real = os.path.realpath(resolved_container)

    if body.subpath is not None:
        if ".." in body.subpath:
            raise HTTPException(status_code=400, detail="Invalid subpath")
        target = os.path.realpath(os.path.join(root_real, body.subpath))
        if not (target == root_real or target.startswith(root_real + os.sep)):
            raise HTTPException(status_code=400, detail="Subpath escapes root")
    else:
        target = root_real

    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail="Path not found on disk")

    # For the host helper (macOS `open`) we need the REAL host path,
    # not the container mount path. Mapping /deliverables/ → ${HOME}/.mc-deliverables.
    resolved_host = await _resolve_deliverable_fs_path(deliverable, session, target="host")
    if not resolved_host:
        # Legacy/absolute host paths: fall back to container path
        resolved_host = resolved_container
    host_root_real = os.path.realpath(resolved_host) if resolved_host.startswith(("/Users/", "/tmp/", "/var/")) else resolved_host
    if body.subpath is not None:
        host_target = os.path.join(host_root_real, body.subpath)
    else:
        host_target = host_root_real

    # macOS `open` doesn't run inside the Docker container — call the host helper via host.docker.internal.
    # sys is imported at the end of the module (for test monkeypatching via patch("app.routers.tasks.sys.platform")).
    # IMPORTANT: send the host helper the HOST path, not the container mount path.
    in_docker = os.path.exists("/.dockerenv") or sys.platform.startswith("linux")
    if in_docker:
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    "http://host.docker.internal:8765/open",
                    json={"path": host_target, "reveal": body.reveal},
                    timeout=3.0,
                )
        except Exception:
            pass  # Helper unreachable — open silently fails
    else:
        cmd = ["open", "-R", host_target] if body.reveal else ["open", host_target]
        subprocess.Popen(cmd)
    return {"ok": True}


@router.get("/boards/{board_id}/tasks/{task_id}/deliverables/{deliverable_id}/directory")
async def list_deliverable_directory(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    subpath: str | None = None,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """List the directory contents of a deliverable (for the directory browser)."""
    import os
    from app.models.deliverable import TaskDeliverable

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    deliverable = await session.get(TaskDeliverable, deliverable_id)
    if not deliverable or deliverable.task_id != task_id:
        raise HTTPException(status_code=404, detail="Deliverable not found")

    resolved = await _resolve_deliverable_fs_path(deliverable, session)
    if not resolved:
        raise HTTPException(status_code=404, detail="No path set or unresolvable")
    if ".." in deliverable.path:
        raise HTTPException(status_code=400, detail="Invalid path")

    root_real = os.path.realpath(resolved)

    if not os.path.isdir(root_real):
        raise HTTPException(status_code=400, detail="Deliverable path is not a directory")

    if subpath is not None:
        if ".." in subpath:
            raise HTTPException(status_code=400, detail="Invalid subpath")
        target = os.path.realpath(os.path.join(root_real, subpath))
        if not (target == root_real or target.startswith(root_real + os.sep)):
            raise HTTPException(status_code=400, detail="Subpath escapes root")
    else:
        target = root_real

    if not os.path.isdir(target):
        raise HTTPException(status_code=404, detail="Directory not found")

    entries = []
    try:
        for entry in sorted(os.scandir(target), key=lambda e: (not e.is_dir(), e.name.lower())):
            size: int | None = None
            if entry.is_file():
                try:
                    size = entry.stat().st_size
                except OSError:
                    pass
            entries.append({
                "name": entry.name,
                "type": "directory" if entry.is_dir() else "file",
                "size": size,
            })
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    return {
        "root_path": root_real,
        "current_path": subpath if subpath is not None else "",
        "entries": entries,
    }


# ── Task Workspace (User-facing, read-only browser over task.workspace_path) ─

@router.get("/boards/{board_id}/tasks/{task_id}/workspace/list")
async def list_task_workspace(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    subpath: str = "",
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """List a task workspace directory for the Workspace tab.

    Returns ``exists: false`` (200, not 404) whenever ``workspace_path`` is
    unset or the directory has since vanished — the frontend renders a hint
    instead of an error in that case.
    """
    from dataclasses import asdict
    from app.services import task_workspace_files
    from app.services.fs_service import FsAccessError, FsNotFound

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        exists, entries = task_workspace_files.list_workspace(task.workspace_path, subpath)
    except FsAccessError:
        raise HTTPException(status_code=400, detail="Invalid path")
    except FsNotFound:
        raise HTTPException(status_code=404, detail="Not found")

    if not exists:
        return {"exists": False, "subpath": "", "entries": []}
    return {"exists": True, "subpath": subpath, "entries": [asdict(e) for e in entries]}


@router.get("/boards/{board_id}/tasks/{task_id}/workspace/content")
async def get_task_workspace_content(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    subpath: str,
    download: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Stream a single file out of a task's workspace (preview or download)."""
    import mimetypes
    from fastapi.responses import FileResponse
    from app.services import task_workspace_files
    from app.services.fs_service import FsAccessError, FsNotFound

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        target = task_workspace_files.resolve_workspace_file(task.workspace_path, subpath)
    except FsAccessError:
        raise HTTPException(status_code=400, detail="Invalid path")
    except FsNotFound:
        raise HTTPException(status_code=404, detail="Not found")

    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(
        path=str(target),
        media_type=mime,
        filename=target.name if download else None,
        headers={"X-Content-Type-Options": "nosniff"},
    )


# ── Checklist (User-facing, read-only) ───────────────────────────────────────

@router.get("/boards/{board_id}/tasks/{task_id}/checklist")
async def get_task_checklist(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """User reads a task's checklist (read-only, for UI)."""
    from app.models.checklist import TaskChecklistItem

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task nicht gefunden")

    result = await session.exec(
        select(TaskChecklistItem)
        .where(TaskChecklistItem.task_id == task_id)
        .order_by(TaskChecklistItem.sort_order)
    )
    return result.all()


@router.get("/boards/{board_id}/tasks/{task_id}/git-info")
async def get_task_git_info_endpoint(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Git status of a task for UI display."""
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task nicht gefunden")

    if not task.workspace_path:
        return {
            "branch": None,
            "last_commit": None,
            "uncommitted": False,
            "ahead": 0,
            "workspace_path": None,
        }

    from app.services.git_service import git_service
    info = await git_service.get_task_git_info(task.workspace_path, branch_name=task.branch_name)
    info["workspace_path"] = task.workspace_path

    # Attach repo metadata from the project
    if task.project_id:
        from app.models.board import Project as ProjectModel
        project = await session.get(ProjectModel, task.project_id)
        if project:
            info["repo_url"] = project.github_repo_url
            info["repo_name"] = project.github_repo_name

    return info


@router.get("/boards/{board_id}/tasks/{task_id}/git-diff")
async def get_task_git_diff_endpoint(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    commit: str,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Git diff for a single commit from the task workspace."""
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task nicht gefunden")

    if not task.workspace_path:
        raise HTTPException(status_code=404, detail="Kein Workspace gefunden")

    from app.services.git_service import git_service
    try:
        diff = await git_service.get_commit_diff(task.workspace_path, commit)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return diff


# ── Single Task CRUD ─────────────────────────────────────────────────────────

@router.get("/boards/{board_id}/tasks/{task_id}")
async def get_task(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/boards/{board_id}/tasks/{task_id}")
async def update_task(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: TaskUpdate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    old_status = task.status
    old_assigned = task.assigned_agent_id
    updates = payload.model_dump(exclude_none=True)

    if "task_type" in updates and updates["task_type"] not in ("story", "bug", "revision", "chore"):
        raise HTTPException(status_code=422, detail=f"Invalid task_type: {updates['task_type']}")

    # Check board rules before status is changed
    if "status" in updates:
        await _enforce_board_rules(session, board_id, task, updates["status"])

    for k, v in updates.items():
        setattr(task, k, v)

    # Auto-set timestamps on status transitions
    if "status" in updates:
        new_status = updates["status"]
        if new_status == "inbox":
            # Back to inbox = reset the dispatch cycle
            task.dispatched_at = None
            task.ack_at = None
            task.started_at = None
            # Reset run_control: otherwise an old 'stopped' flag stays stuck,
            # the task gets dispatched again, the agent works, but isn't
            # allowed to switch to review -> deadlock.
            task.run_control = None
        elif new_status == "in_progress" and old_status != "in_progress":
            # F2 fix (Plan 26-03): first-set-wins. Re-opens (review→in_progress,
            # blocked→in_progress) preserve the original started_at for accurate
            # Cycle Time analytics. Only set when currently NULL.
            if task.started_at is None:
                task.started_at = utcnow()
            task.ack_at = utcnow()  # ACK — manual or via UI
        elif new_status == "done" and old_status != "done":
            task.completed_at = utcnow()

    # Log task event (event sourcing)
    if "status" in updates:
        from app.services.task_lifecycle import record_task_event, clear_spawn_tracking
        await record_task_event(
            session, task.id, old_status, updates["status"],
            changed_by="user", reason="manual_update",
        )
        # Clear spawn tracking on terminal/inactive status
        if updates["status"] in ("done", "failed", "blocked", "inbox"):
            clear_spawn_tracking(task)
            from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
            await clear_dispatch_attempt_id(
                session, task,
                caller="user_task_update",
                reason=f"status_to_{updates['status']}",
            )

        # Free the port on done/failed
        if updates["status"] in ("done", "failed") and task.workspace_port:
            task.workspace_port = None

        # Auto-unassign on failed/blocked → prevents a cancel loop in
        # agent_poll. See apply_terminal_unassign() docs.
        from app.services.task_lifecycle import apply_terminal_unassign
        await apply_terminal_unassign(session, task, updates["status"])

    # ── Approval cleanup: supersede obsolete approvals ────
    if "status" in updates:
        from app.services.approval_cleanup import cleanup_obsolete_approvals
        await cleanup_obsolete_approvals(session, task.id, updates["status"], board_id)

    task.updated_at = utcnow()
    session.add(task)
    await session.commit()
    await session.refresh(task)

    # Vertical hooks (e.g. News-Studio pipeline-stage auto-advance) — no-op
    # if no vertical is registered (stripped public release).
    if "status" in updates and updates["status"] == "done" and task.pipeline_id:
        from app.verticals import hooks as vertical_hooks
        await vertical_hooks.run_task_done_hooks(session, task)
        await session.commit()
        await session.refresh(task)

    # Auto-trigger: notify agent when a task is assigned
    if "assigned_agent_id" in updates and task.assigned_agent_id != old_assigned:
        # Notify the old agent: task withdrawn (Phase 29: via TaskComment)
        if old_assigned:
            old_agent = await session.get(Agent, old_assigned)
            if old_agent:
                msg = (
                    f"REASSIGNED: Task \"{task.title}\" wurde dir entzogen.\n"
                    f"STOPPE sofort alle Arbeiten an diesem Task.\n"
                    f"Task-ID: {task.id}\n\n"
                    f"**Aktion:** Beende sofort alle Arbeiten an diesem Task. "
                    f"Lies deinen letzten Checkpoint-Kommentar fuer eventuelle andere offene Tasks."
                )
                session.add(TaskComment(
                    task_id=task.id,
                    author_type="system",
                    content=msg,
                    comment_type="reassignment_notice",
                ))

        # New agent = new dispatch cycle → reset old tracking data
        task.dispatched_at = None
        task.ack_at = None
        task.dispatch_intent = "manual_redispatch"
        from app.services.task_lifecycle import clear_spawn_tracking
        clear_spawn_tracking(task)
        from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
        await clear_dispatch_attempt_id(
            session, task,
            caller="user_task_update", reason="manual_reassign",
        )
        agent = await session.get(Agent, task.assigned_agent_id)

        # Operational controls guard
        from app.services.operations import check_dispatch_allowed
        _dispatch_allowed = True
        if agent:
            allowed, reason = await check_dispatch_allowed(task, agent, session)
            if not allowed:
                logger.info("Manual re-dispatch blocked: '%s' — %s", task.title, reason)
                _dispatch_allowed = False

        # Phase 29: Re-dispatch via auto_dispatch_task (cli-bridge / host / claude-code
        # runtimes). Gateway-only path removed.
        session.add(task)
        await session.commit()
        await session.refresh(task)
        if _dispatch_allowed and agent:
            create_tracked_task(auto_dispatch_task(task.id, board_id))

    event_type = "task.status_changed" if "status" in updates else "task.updated"
    await emit_event(
        session,
        event_type,
        f"Task {event_type}: {task.title}",
        board_id=board_id,
        task_id=task.id,
        detail={"old_status": old_status, "new_status": task.status} if "status" in updates else None,
    )

    # ── Status-transition side effects (via TaskLifecycleService) ────────
    if "status" in updates:
        from app.services.task_lifecycle import (
            handle_review_handoff, handle_review_rejection,
            trigger_auto_memory, trigger_feedback_lesson,
        )
        new_status = updates["status"]

        # User sets task to review → find reviewer and notify via push
        if new_status == "review" and old_status == "in_progress":
            await handle_review_handoff(session, task, board_id)

        # User rejects review → back to original developer
        # Also catch done→in_progress (re-open after accidental done)
        if new_status == "in_progress" and old_status in ("review", "done", "user_test"):
            await handle_review_rejection(session, task, board_id)

        # User test: notify the operator via Telegram (Phase 29: direct HTTPS path)
        if new_status == "user_test":
            from app.services.telegram_bot import telegram_bot
            from app.config import phone_test_url
            tailscale_url = phone_test_url()
            await telegram_bot.send_message(
                f"<b>Bereit zum Testen: {task.title}</b>\n\n"
                f"Bitte auf dem Handy testen:\n{tailscale_url}\n\n"
                f"Task-ID: {task.id}"
            )

        # User/operator unblocks task → notify assigned agent (Phase 29: TaskComment)
        if new_status == "in_progress" and old_status == "blocked":
            if task.assigned_agent_id:
                target = await session.get(Agent, task.assigned_agent_id)
                if target:
                    msg = (
                        f"UNBLOCKED: Dein Task \"{task.title}\" wurde entblockt.\n\n"
                        f"Task-ID: {task.id}\n\n"
                        f"**Aktion:** Lies deinen letzten Checkpoint-Kommentar "
                        f"(GET /api/v1/agent/boards/{board_id}/tasks/{task.id}/comments) "
                        f"und arbeite sofort an diesem Task weiter."
                    )
                    session.add(TaskComment(
                        task_id=task.id,
                        author_type="system",
                        content=msg,
                        comment_type="system_notify",
                    ))

        # Auto-memory + feedback lessons
        trigger_auto_memory(task, new_status, old_status)
        await trigger_feedback_lesson(session, task, new_status, old_status)

    # Phase start: when a parent task goes to in_progress → dispatch all inbox subtasks
    new_status = updates.get("status")
    if new_status == "in_progress" and task.parent_task_id is None:
        subtask_result = await session.exec(
            select(Task).where(
                Task.parent_task_id == task.id,
                Task.status == "inbox",
            )
        )
        subtasks = subtask_result.all()
        if subtasks:
            board = await session.get(Board, board_id)
            if board and board.auto_dispatch_enabled:
                for subtask in subtasks:
                    create_tracked_task(auto_dispatch_task(subtask.id, board_id))
                logger.info(
                    "Phase start: dispatching %d subtasks for '%s'",
                    len(subtasks),
                    task.title,
                )

    # Phase done → auto-advance to the next phase + project progress
    if new_status == "done" and task.parent_task_id is None and task.project_id:
        # Find and start the next phase
        next_phase = (await session.exec(
            select(Task).where(
                Task.project_id == task.project_id,
                Task.parent_task_id.is_(None),  # type: ignore[attr-defined]
                Task.status == "inbox",
                Task.sort_order > task.sort_order,
            ).order_by(Task.sort_order.asc()).limit(1)
        )).first()
        if next_phase:
            from app.services.task_lifecycle import record_task_event
            await record_task_event(
                session, next_phase.id, "inbox", "in_progress",
                changed_by="system", reason="phase_auto_advance",
            )
            next_phase.status = "in_progress"
            # F2 fix (Plan 26-03): first-set-wins on started_at.
            if next_phase.started_at is None:
                next_phase.started_at = utcnow()
            next_phase.updated_at = utcnow()
            session.add(next_phase)
            await session.commit()
            await emit_event(
                session, "task.phase_auto_started",
                f"Phase auto-gestartet: '{next_phase.title}'",
                board_id=board_id, task_id=next_phase.id,
            )
            # Dispatch subtasks of the new phase
            sub_result = await session.exec(
                select(Task).where(Task.parent_task_id == next_phase.id, Task.status == "inbox")
            )
            board = await session.get(Board, board_id)
            if board and board.auto_dispatch_enabled:
                for sub in sub_result.all():
                    create_tracked_task(auto_dispatch_task(sub.id, board_id))

    return task


@router.get("/boards/{board_id}/tasks/{task_id}/events")
async def get_task_events(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Task status event history (event sourcing) — chronological."""
    from app.models.task import TaskEvent

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    result = await session.exec(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.created_at.desc())
        .limit(limit)
    )
    events = result.all()

    # Enrich with agent names
    agent_ids = {e.agent_id for e in events if e.agent_id}
    agent_name_map: dict[uuid.UUID, str] = {}
    if agent_ids:
        agents_result = await session.exec(select(Agent).where(Agent.id.in_(agent_ids)))
        agent_name_map = {a.id: a.name for a in agents_result.all()}

    return [
        {
            **e.model_dump(),
            "agent_name": agent_name_map.get(e.agent_id) if e.agent_id else None,
        }
        for e in events
    ]


@router.get("/boards/{board_id}/tasks/{task_id}/dependencies")
async def get_task_dependencies(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Load a task's dependencies with the status of the dependent tasks."""
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    dep_result = await session.exec(
        select(TaskDependency).where(TaskDependency.task_id == task_id)
    )
    deps = dep_result.all()

    result = []
    for dep in deps:
        dep_task = await session.get(Task, dep.depends_on_task_id)
        if dep_task:
            result.append({
                "task_id": str(dep_task.id),
                "title": dep_task.title,
                "status": dep_task.status,
            })
    return result


@router.delete("/boards/{board_id}/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.models.approval import Approval
    from app.models.activity import ActivityEvent

    # 1. Subtasks: clear parent_task_id
    subtask_result = await session.exec(
        select(Task).where(Task.parent_task_id == task_id)
    )
    for sub in subtask_result.all():
        sub.parent_task_id = None
        session.add(sub)

    # 2. Delete comments
    comment_result = await session.exec(
        select(TaskComment).where(TaskComment.task_id == task_id)
    )
    for c in comment_result.all():
        await session.delete(c)

    # 3. Delete dependencies (both directions)
    dep_result = await session.exec(
        select(TaskDependency).where(
            (TaskDependency.task_id == task_id) | (TaskDependency.depends_on_task_id == task_id)
        )
    )
    for d in dep_result.all():
        await session.delete(d)

    # 4. Delete tag assignments
    tag_result = await session.exec(
        select(TagAssignment).where(TagAssignment.task_id == task_id)
    )
    for t in tag_result.all():
        await session.delete(t)

    # 5. Delete approvals
    approval_result = await session.exec(
        select(Approval).where(Approval.task_id == task_id)
    )
    for a in approval_result.all():
        await session.delete(a)

    # 6. Delete activity events
    event_result = await session.exec(
        select(ActivityEvent).where(ActivityEvent.task_id == task_id)
    )
    for e in event_result.all():
        await session.delete(e)

    # 6b. Delete task events (event sourcing)
    from app.models.task import TaskEvent
    task_event_result = await session.exec(
        select(TaskEvent).where(TaskEvent.task_id == task_id)
    )
    for te in task_event_result.all():
        await session.delete(te)

    # 6c. Delete task checkpoints (non-nullable FK — would otherwise block the delete)
    from app.models.checkpoint import TaskCheckpoint
    checkpoint_result = await session.exec(
        select(TaskCheckpoint).where(TaskCheckpoint.task_id == task_id)
    )
    for cp in checkpoint_result.all():
        await session.delete(cp)

    # 6d. Cost events: set task_id to null (nullable FK)
    from app.models.cost_event import CostEvent
    cost_result = await session.exec(
        select(CostEvent).where(CostEvent.task_id == task_id)
    )
    for ce in cost_result.all():
        ce.task_id = None
        session.add(ce)

    # 6d2. Model usage events (Token Harvester): set task_id to null —
    # nullable FK without ondelete (NO ACTION) would otherwise block the
    # delete as soon as the harvester attributes events to tasks.
    from app.models.model_usage import ModelUsageEvent
    usage_result = await session.exec(
        select(ModelUsageEvent).where(ModelUsageEvent.task_id == task_id)
    )
    for ue in usage_result.all():
        ue.task_id = None
        session.add(ue)

    # 6e. Delete task deliverables (non-nullable FK → RESTRICT)
    from app.models.deliverable import TaskDeliverable
    del_result = await session.exec(
        select(TaskDeliverable).where(TaskDeliverable.task_id == task_id)
    )
    for d in del_result.all():
        await session.delete(d)

    # 6f. Delete task checklist items (non-nullable FK → RESTRICT)
    from app.models.checklist import TaskChecklistItem
    checklist_result = await session.exec(
        select(TaskChecklistItem).where(TaskChecklistItem.task_id == task_id)
    )
    for item in checklist_result.all():
        await session.delete(item)

    # 6g. Loops (ADR-051): gelöschter Runden-Task = Fehlrunde (volle Wertung
    # inkl. Circuit-Breaker) + FK-Referenzen lösen. Blockiert den Delete nie.
    from app.services.loop_runner import handle_round_task_deleted
    await handle_round_task_deleted(session, task_id)

    # 6h. Referenz-Dateien (ADR-053): Rows + Dateien mitlöschen.
    from app.services.reference_cleanup import delete_references_for
    await delete_references_for(session, task_id=task_id)
    # E2E-Medien (Playwright-MCP-Videos/Screenshots) des Tasks miträumen —
    # best-effort, blockiert den Delete nie (Fund 05.07.).
    from app.services.mcp_media_cleanup import delete_mcp_media_for_task
    try:
        delete_mcp_media_for_task(task_id)
    except Exception:
        pass

    # 6i. file_index: task_id-Provenance lösen — der FK blockte sonst den
    # Task-Delete (Live-Smoke-Fund; betraf latent auch Deliverable-Captures).
    from app.models.file_index import FileIndexEntry
    for fi in (await session.exec(
        select(FileIndexEntry).where(FileIndexEntry.task_id == task_id)
    )).all():
        fi.task_id = None
        session.add(fi)

    # 7. Clear agent current_task_id
    agent_result = await session.exec(
        select(Agent).where(Agent.current_task_id == task_id)
    )
    for ag in agent_result.all():
        ag.current_task_id = None
        session.add(ag)

    # 8. Clean up Redis queue (remove task_id from agent queues)
    if task.assigned_agent_id:
        try:
            from app.redis_client import get_redis
            redis = await get_redis()
            queue_key = f"mc:agent:{task.assigned_agent_id}:task_queue"
            await redis.lrem(queue_key, 0, str(task_id))
        except Exception:
            logger.warning("Redis cleanup failed for task %s", task_id)

    # 8b. Kill FreeCode tmux session if the agent is a FreeCode agent
    if task.assigned_agent_id:
        try:
            assigned_agent = await session.get(Agent, task.assigned_agent_id)
            if assigned_agent and getattr(assigned_agent, "agent_runtime", "openclaw") in ("free-code-bridge", "cli-bridge"):
                from app.config import settings
                import urllib.request
                req = urllib.request.Request(
                    f"{settings.free_code_bridge_url}/sessions/{task_id}",
                    method="DELETE"
                )
                urllib.request.urlopen(req, timeout=3)
                logger.info("FreeCode tmux session killed for deleted task %s", task_id)
        except Exception:
            pass  # Bridge unreachable or session doesn't exist — not a problem

    # 9. Delete the task itself
    await session.delete(task)
    await session.commit()


# ── Usage / Cost Attribution (Token Harvester task_id join) ────────────────


@router.get("/tasks/{task_id}/usage")
async def get_task_usage(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Token/cost usage attributed to this task (model_usage_events.task_id).

    Events are attributed by the Token Harvester matching transcript cwd
    against the task's workspace_path — see services/token_harvester.py.
    """
    from sqlalchemy import func

    from app.models.model_usage import ModelUsageEvent

    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    result = await session.exec(
        select(
            func.count(ModelUsageEvent.id),
            func.coalesce(func.sum(ModelUsageEvent.input_tokens), 0),
            func.coalesce(func.sum(ModelUsageEvent.output_tokens), 0),
            func.coalesce(func.sum(ModelUsageEvent.cache_read_tokens), 0),
            func.coalesce(func.sum(ModelUsageEvent.cache_write_tokens), 0),
            func.coalesce(func.sum(ModelUsageEvent.cost_usd), 0.0),
        ).where(ModelUsageEvent.task_id == task_id)
    )
    event_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd = result.one()

    return {
        "task_id": str(task_id),
        "event_count": event_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": input_tokens + output_tokens + cache_read_tokens + cache_write_tokens,
        "cost_usd": round(cost_usd, 6),
    }


# ── Transcript ────────────────────────────────────────────────────────────────


@router.get("/tasks/{task_id}/transcript")
async def get_task_transcript(
    task_id: uuid.UUID,
    limit: int = Query(30, le=100),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Task transcript: agent session messages during processing."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    transcript_mode = "direct"
    session_role = None
    session_key = task.spawn_session_key

    if not session_key:
        # Reconstruction: check the last TaskEvent to derive the role
        transcript_mode = "reconstructed"
        last_event = (await session.exec(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.created_at.desc())
            .limit(1)
        )).first()

        # Phase 30: gateway_agent_id session-key reconstruction dropped.
        # The OpenClaw-session-history path is gone since Phase 29; the
        # canonical channel below (TaskComment rows) is runtime-agnostic.

    # Phase 29: Gateway chat_history removed. Transcript is now reconstructed
    # from TaskComment rows (runtime-agnostic canonical channel).
    comment_result = await session.exec(
        select(TaskComment)
        .where(TaskComment.task_id == task_id)
        .order_by(TaskComment.created_at.desc())
        .limit(limit)
    )
    rows = list(reversed(comment_result.all()))
    messages = [
        {
            "role": c.author_type or "agent",
            "content": c.content,
            "ts": c.created_at.isoformat() if c.created_at else None,
            "comment_type": c.comment_type,
        }
        for c in rows
    ]
    return {
        "transcript_mode": "taskcomment" if session_key else "reconstructed",
        "session_role": session_role,
        "session_key": session_key,
        "messages": messages,
    }


# ── Comments ─────────────────────────────────────────────────────────────────

@router.post("/boards/{board_id}/tasks/{task_id}/comments", status_code=status.HTTP_201_CREATED)
async def add_comment(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: CommentCreate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    comment = TaskComment(task_id=task_id, **payload.model_dump())
    session.add(comment)
    await session.commit()
    await session.refresh(comment)

    await emit_event(
        session,
        "task.commented",
        f"Comment on {task.title}",
        board_id=board_id,
        task_id=task_id,
    )

    # Phase 29: TaskComment is the canonical delivery channel for cli-bridge / host
    # / claude-code runtimes. poll.sh pulls new_comments[] on next iteration. No
    # additional gateway notify needed — the TaskComment write above is sufficient.

    return comment


@router.get("/boards/{board_id}/tasks/{task_id}/comments")
async def list_comments(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")
    result = await session.exec(
        select(TaskComment).where(TaskComment.task_id == task_id).order_by(TaskComment.created_at)
    )
    comments = result.all()

    # Enrich with agent names
    agent_ids = {c.author_agent_id for c in comments if c.author_agent_id}
    agent_map: dict[uuid.UUID, tuple[str, str]] = {}
    if agent_ids:
        from app.models.agent import Agent
        agents_result = await session.exec(select(Agent).where(Agent.id.in_(agent_ids)))  # type: ignore[arg-type]
        agent_map = {a.id: (a.name, a.emoji or "🤖") for a in agents_result.all()}

    return [
        {**c.model_dump(), "author_agent_name": agent_map.get(c.author_agent_id, (None, None))[0],
         "author_agent_emoji": agent_map.get(c.author_agent_id, (None, None))[1]}
        if c.author_agent_id else c.model_dump()
        for c in comments
    ]
