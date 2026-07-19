"""Agent-scoped comments router (REF-02).

Owns:
  - GET  /boards/{board_id}/tasks/{task_id}/comments
  - POST /boards/{board_id}/tasks/{task_id}/comments
          + auto-ACK on first assigned-agent comment (sensitive to subagent-dispatch flag)
          + reflection→memory pipeline (writes BoardMemory(memory_type='lesson'))
  - _extract_reflection_lesson regex helper
  - _post_subtask_completion_comment + _post_subtask_blocker_comment helpers
  - AgentCommentCreate Pydantic model

Auth:   Agent PBKDF2 token via require_scope on each endpoint
Scope:  TASKS_WRITE for POST; TASKS_READ for GET

Phase 4 REF-02 step 3 — extracted from agent_scoped.py.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.scopes import Scope, require_scope
from app.database import get_session
from app.models.agent import Agent
from app.models.memory import BoardMemory
from app.models.task import Task, TaskComment
from app.services.activity import emit_event
from app.utils import utcnow

# Single Source of Truth: app/comment_types.py (REL-01). Same alias as
# agent_scoped.py uses — preserves the historical name for tests.
from app.comment_types import ALL_COMMENT_TYPES as VALID_COMMENT_TYPES
from app.comment_types import validate_comment_content

logger = logging.getLogger("mc.agent_comments")

router = APIRouter(prefix="/api/v1/agent", tags=["agent-comments"])


# ─────────────────────────────────────────────────────────────────────
# Inter-task comment helpers (called from PATCH agent_update_task in
# agent_scoped.py — re-exported via shim so the call sites keep working).
# ─────────────────────────────────────────────────────────────────────
async def _post_subtask_blocker_comment(
    session: AsyncSession,
    task: Task,
    agent: Agent,
    blocker_type: str | None,
    blocker_question: str | None,
    blocker_description: str | None,
) -> None:
    """Post a 'blocker' comment on the parent when a subtask transitions to blocked.

    Live-stream for the parent owner (Boss/Orchestrator): without this comment
    the parent owner saw no indication in /poll that its subtask was blocked —
    it couldn't react and the parent stayed stuck (Bug 2026-04-23).

    Guard logic:
      - No-op for root tasks (parent_task_id is None)
      - No-op when the parent no longer exists
      - No-op when the subtask is waiting on another subtask
        (blocked_by_task_id set) — that's internal orchestration and
        needs no operator decision, hence no parent notify either
      - No-op on self-delegation (worker == parent owner) — otherwise echo loop

    Best-effort: caller wraps this in try/except so a failure here doesn't
    tip the actual PATCH into a 500.
    """
    if task.parent_task_id is None:
        return

    # Callback-wait: no operator decision, no parent notify (same logic
    # as the approval creation further down in the handler).
    if task.blocked_by_task_id is not None:
        return

    parent = await session.get(Task, task.parent_task_id)
    if parent is None:
        return

    # Avoid self-echo: if the worker happens to be the parent owner,
    # it doesn't write a comment to itself.
    if parent.assigned_agent_id == agent.id:
        return

    short_id = str(task.id)[:8]
    parts = [
        f"**Subtask blocked:** {task.title} (`{short_id}`)",
        f"**Agent:** {agent.name}",
    ]
    if blocker_type:
        parts.append(f"**Blocker-Typ:** `{blocker_type}`")
    if blocker_question:
        parts.append(f"**Frage:** {blocker_question}")
    if blocker_description:
        parts.append(f"**Kontext:** {blocker_description}")
    parts.append(
        "Bitte reagieren — entweder die Frage beantworten und den Subtask "
        "wieder freigeben (PATCH status: in_progress + Hilfs-Kommentar), oder "
        "den Parent eskalieren an den Operator via Telegram."
    )
    content = "\n".join(parts)

    comment = TaskComment(
        task_id=parent.id,
        author_type="agent",
        author_agent_id=agent.id,
        comment_type="blocker",
        content=content,
    )
    session.add(comment)
    await session.commit()


async def _post_subtask_completion_comment(
    session: AsyncSession,
    task: Task,
    agent: Agent,
) -> None:
    """Post a summary comment on the parent when a subtask transitions to done.

    Live-stream: gives Boss (parent assignee) real-time visibility into
    subtask completions. No-op for root tasks (parent_task_id is None).
    """
    if task.parent_task_id is None:
        return

    parent = await session.get(Task, task.parent_task_id)
    if parent is None:
        return

    # Get last reflection comment (contains summary) if exists
    reflection_result = await session.exec(
        select(TaskComment)
        .where(TaskComment.task_id == task.id)
        .where(TaskComment.comment_type == "reflection")
        .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
        .limit(1)
    )
    reflection = reflection_result.first()
    summary = ""
    if reflection and reflection.content:
        # Take first 300 chars of reflection as summary
        summary = reflection.content[:300]
        if len(reflection.content) > 300:
            summary += "..."

    content = (
        f"**Subtask abgeschlossen:** {task.title}\n"
        f"**Agent:** {agent.name}\n"
        f"**Task-ID:** `{task.id}`\n"
    )
    if summary:
        content += f"\n**Zusammenfassung:**\n{summary}"

    comment = TaskComment(
        task_id=parent.id,
        author_type="agent",
        author_agent_id=agent.id,
        comment_type="subtask_completed",
        content=content,
    )
    session.add(comment)
    await session.commit()

    await emit_event(
        session,
        "task.subtask_completed",
        f"Subtask '{task.title}' abgeschlossen (von {agent.name})",
        board_id=parent.board_id,
        task_id=parent.id,
        agent_id=agent.id,
        detail={"subtask_id": str(task.id)},
    )


# ─────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────
class AgentCommentCreate(BaseModel):
    content: str
    comment_type: str = "message"

    @field_validator("comment_type")
    @classmethod
    def _validate_comment_type(cls, v: str) -> str:
        if v not in VALID_COMMENT_TYPES:
            valid = ", ".join(sorted(VALID_COMMENT_TYPES))
            raise ValueError(f"Ungueltiger comment_type: '{v}'. Gueltig: {valid}")
        return v

    @field_validator("content")
    @classmethod
    def _validate_content(cls, v: str) -> str:
        # Defense-in-depth against JSON-envelope content (Bug 2026-05-17).
        # See app/comment_types.py:validate_comment_content for rationale.
        return validate_comment_content(v)


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────
@router.get("/boards/{board_id}/tasks/{task_id}/comments")
async def agent_list_comments(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    """Agent can read a task's comments."""
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

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
        agents_result = await session.exec(select(Agent).where(Agent.id.in_(agent_ids)))  # type: ignore[arg-type]
        agent_map = {a.id: (a.name, a.emoji or "🤖") for a in agents_result.all()}

    return [
        {**c.model_dump(), "author_agent_name": agent_map.get(c.author_agent_id, (None, None))[0],
         "author_agent_emoji": agent_map.get(c.author_agent_id, (None, None))[1]}
        if c.author_agent_id else c.model_dump()
        for c in comments
    ]


@router.post("/boards/{board_id}/tasks/{task_id}/comments", status_code=status.HTTP_201_CREATED)
async def agent_add_comment(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: AgentCommentCreate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_WRITE)),
):
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    # ── Auto-ACK: first comment from the assigned agent → set ack_at ──
    # Shared handshake (§3.3) — same implementation as the Message channel.
    from app.services.task_lifecycle import apply_ack_handshake
    apply_ack_handshake(session, task, agent)

    comment = TaskComment(
        task_id=task_id,
        author_type="agent",
        author_agent_id=agent.id,
        comment_type=payload.comment_type,
        content=payload.content,
    )
    session.add(comment)
    agent.last_task_activity_at = utcnow()
    session.add(agent)

    # ── Resolution Auto-Promote ──────────────────────────────────────────
    # When an agent writes a "resolution" comment but the task is still
    # in_progress:
    # → subtasks go straight to done (review runs at the phase level)
    # → root tasks go to review (agent forgot to send PATCH)
    auto_promoted = False
    # Phase 8 BUG-01: agent.auto_promote_on_resolution=False suppresses both
    # auto-promote paths (here + task_runner.py:771). Default True preserves
    # single-step worker safety-net (Cody/Rex/Sparky); deployer is False per
    # Migration 0092 data step.
    if (
        payload.comment_type == "resolution"
        and task.status == "in_progress"
        and (task.assigned_agent_id == agent.id or agent.is_board_lead)
        and agent.auto_promote_on_resolution
    ):
        old_status = task.status
        # Subtasks go straight to done (review runs at the phase level)
        if task.parent_task_id is not None:
            task.status = "done"
            task.completed_at = utcnow()
            # See task_lifecycle.execute_review_decision for why "done"
            # resets the sticky dispatch_intent label.
            task.dispatch_intent = "root"
        else:
            task.status = "review"
        # Prevent stale dispatch_attempt_id (audit trail).
        from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
        await clear_dispatch_attempt_id(
            session, task,
            caller="agent_comment",
            reason="resolution_auto_promote",
        )
        task.updated_at = utcnow()
        session.add(task)
        auto_promoted = True
        logger.info(
            "Resolution-Auto-Promote: %s schrieb resolution-Kommentar → Task '%s' in_progress→%s",
            agent.name, task.title[:60], task.status,
        )

    # ── Fulfill report-back contract ──────────────────────────────
    # When the Board Lead posts a report_back comment → contract fulfilled
    if (
        payload.comment_type == "report_back"
        and agent.is_board_lead
        and task.report_back_required
        and task.report_back_status == "pending"
    ):
        task.report_back_status = "sent"
        task.updated_at = utcnow()
        session.add(task)
        logger.info(
            "Report-back Contract erfuellt von %s fuer Task '%s'",
            agent.name, task.title[:60],
        )

    await session.commit()
    await session.refresh(comment)

    # Side effects after auto-promote (outside the transaction)
    if auto_promoted:
        new_status = task.status  # "done" for subtasks, "review" for root tasks
        # Activity Event
        await emit_event(
            session, "task.status_changed",
            f"Auto-Promote: {agent.name} resolution-Kommentar → {new_status}",
            board_id=board_id, task_id=task.id, agent_id=agent.id,
            detail={"old_status": "in_progress", "new_status": new_status, "auto_promoted": True},
        )

        # Active-Task Tracking
        from app.services.task_lifecycle import update_agent_active_task
        await update_agent_active_task(session, agent.id, task, new_status, "in_progress")

        # Review handoff: only for review (subtasks go straight to done).
        # Mirror the PATCH routers' human_review_required gate (tasks.py,
        # agent_task_status.py) — unconditionally calling handle_review_handoff
        # here dispatched an agent reviewer even for tasks routed to Mark /
        # a vertical review-hook (e.g. bench_studio: burns frontier tokens on
        # a review nobody wants, and skips the task_review_hooks that would
        # otherwise finalize the task straight to done — review-hook fix,
        # 2026-07-15).
        if new_status == "review":
            if not getattr(task, "human_review_required", None):
                from app.services.task_lifecycle import handle_review_handoff
                await handle_review_handoff(session, task, board_id, developer=agent)
            else:
                from app.services.task_lifecycle import handle_human_review_handoff
                await handle_human_review_handoff(session, task, board_id, developer=agent)

    # ── Lead-Eskalation (Fix A): Lead entscheidet, dass der Blocker ein
    # Operator-Fall ist → sofort Stufe 2 (Approval + Telegram), Triage-Frist
    # nicht abwarten.
    if (
        payload.comment_type == "escalate_to_operator"
        and task.status == "blocked"
    ):
        from app.services.blocker_triage import escalate_blocker_to_operator, is_lead_agent
        if not is_lead_agent(agent):
            logger.info(
                "escalate_to_operator ignoriert: %s ist kein Lead", agent.name,
            )
        else:
            try:
                await escalate_blocker_to_operator(
                    session, task=task, reason="lead_escalated",
                )
            except Exception as e:
                logger.warning(
                    "Lead-Eskalation fehlgeschlagen fuer Task %s: %s", task.id, e,
                )

    # Phase Approval Workflow: phase_approved / phase_rewrite_request trigger parent action
    if (
        payload.comment_type in ("phase_approved", "phase_rewrite_request")
        and task.delegation_type == "phase_approval"
        and agent.is_board_lead
    ):
        from app.services.task_lifecycle import handle_phase_approval_decision
        try:
            await handle_phase_approval_decision(
                session, task, agent,
                comment_type=payload.comment_type,
                comment_content=payload.content,
            )
        except Exception as e:
            logger.warning(
                "Phase approval decision failed for task %s: %s",
                task.id, e,
            )

    # Phase 29: RPC notification to the assigned agent removed.
    # The comment is already persisted via session.add(comment); the target
    # agent's cli-bridge poll.sh picks it up on the next tick via
    # GET /agent/me/comments. This makes delivery runtime-agnostic.

    # ── Reflection → Agent-Memory Pipeline (Phase B, 2026-04-11) ─────────
    # When an agent posts a reflection comment → automatically store the
    # lesson part as BoardMemory(type=lesson, agent_id=self) and index it
    # in Qdrant. Closes the learning loop: reflections automatically land
    # in the agent-memory layer and are retrievable via vector search on
    # the next dispatch.
    if payload.comment_type == "reflection":
        try:
            lesson_text = _extract_reflection_lesson(payload.content or "")
            if lesson_text and len(lesson_text) >= 20:
                lesson_memory = BoardMemory(
                    board_id=task.board_id,
                    agent_id=agent.id,  # agent-scoped → agent layer in Qdrant
                    title=f"Lesson: {task.title[:60]}",
                    content=lesson_text,
                    memory_type="lesson",
                    source=agent.name,
                    tags=["auto", "reflection", "task_done"],
                    auto_generated=True,
                )
                session.add(lesson_memory)
                await session.commit()
                await session.refresh(lesson_memory)
                try:
                    from app.services.memory_indexing import index_memory
                    await index_memory(lesson_memory)
                except Exception as _e:
                    logger.warning("Reflection memory index failed: %s", _e)
                logger.info(
                    "Reflection → Agent-Memory: lesson gespeichert fuer %s (task %s)",
                    agent.name, task.id,
                )
        except Exception as e:
            logger.warning("Reflection pipeline failed for task %s: %s", task.id, e)

    # Bug 9 (2026-05-13): when an agent posts a default `message` comment on
    # a task assigned to someone else (e.g. Boss writes a briefing to
    # Sparky), we warn — `message` is NOT in DELIVERABLE_SYSTEM_TYPES, so
    # the worker never sees it via /me/poll. Instead of failing silently we
    # return a `delivery_hint` in the response — the `mc` CLI renders it.
    from fastapi.encoders import jsonable_encoder
    response = jsonable_encoder(comment)
    if (
        payload.comment_type == "message"
        and task.assigned_agent_id
        and task.assigned_agent_id != agent.id
    ):
        response["delivery_hint"] = (
            "Worker bekommt diesen `message`-Comment nicht via /me/poll — "
            "er ist als Routine-Notiz/Audit klassifiziert. Fuer Briefings "
            "auf existierende Tasks nutze `mc comment handoff \"...\"`, "
            "fuer neue Sub-Aufgaben `mc delegate`."
        )
    return response


def _extract_reflection_lesson(content: str) -> str:
    """Extracts the lesson part from a reflection comment.

    The reflection format's single source of truth is `app.constants`:
    `REFLECTION_REQUIRED_FIELDS`. The last entry is the lesson field —
    the first "meaningful" word variant of it (plus a few synonyms) is
    used as a regex anchor. If the operator ever renames it
    ("Lektion"/"Erkenntnis"/...), the extraction script follows
    automatically. The fallback remains "last 40% of the text".
    """
    import re as _re
    if not content:
        return ""
    from app.constants import REFLECTION_REQUIRED_FIELDS
    # Keyword pool: first word of the lesson field + English synonyms,
    # case-insensitive.
    _last_field = REFLECTION_REQUIRED_FIELDS[-1] if REFLECTION_REQUIRED_FIELDS else "Lesson"
    _primary = _re.sub(r"[^\wäöüÄÖÜ].*$", "", _last_field) or "Lesson"
    _keywords = sorted({_primary.lower(), "lesson", "erkenntnis", "learning", "lektion"})
    _kw_pattern = "|".join(_re.escape(k) for k in _keywords)

    # Case 1: Markdown section with a lesson header
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if _re.match(rf"^\s*#+\s*({_kw_pattern})", line, _re.I):
            tail_lines = []
            for tl in lines[i + 1:]:
                if _re.match(r"^\s*#+\s", tl):
                    break
                tail_lines.append(tl)
            return "\n".join(tail_lines).strip()
    # Case 2: free text with "Lesson:" or equivalent
    m = _re.search(rf"(?i)\*{{0,2}}({_kw_pattern})[:\s]+\*{{0,2}}([^\n#][\s\S]*?)(?:\n#|\Z)", content)
    if m:
        return m.group(2).strip()
    # Case 3: fallback — take the last 40% as the likely lesson
    cut = int(len(content) * 0.6)
    tail = content[cut:].strip()
    return tail if len(tail) >= 20 else ""
