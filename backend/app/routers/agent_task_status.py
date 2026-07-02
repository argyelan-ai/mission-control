"""Agent-scoped task status transitions router (REF-02 step 4).

Owns:
  - PATCH /boards/{board_id}/tasks/{task_id}      (the 600-line state-machine endpoint)
  - GET   /boards/{board_id}/tasks/next            (task-claim / pull dispatch)
  - GET   /boards/{board_id}/tasks                 (list)
  - POST  /boards/{board_id}/tasks                 (creation by agent)
  - GET   /boards/{board_id}/tasks/{id}/detail
  - DELETE /boards/{board_id}/tasks/{id}
  - GET   /boards/{board_id}/tasks/pipeline
  - GET   /boards/{board_id}/tasks/{id}/events
  - PATCH /boards/{board_id}/tasks/{id}/report-back
  - POST  /boards/{board_id}/tasks/{id}/review
  - POST  /boards/{board_id}/tasks/{id}/checkpoint (410 — deprecated shim)
  - GET   /boards/{board_id}/tasks/{id}/checkpoint
  - Private helpers: _handle_help_request_resume, _handle_callback_resume,
    _handle_phase_completion_push, dispatch_callback_to_parent, dispatch_resume_to_agent

Auth:   Agent PBKDF2 token via require_scope on each endpoint
Scope:  TASKS_WRITE for state changes, TASKS_READ for GETs

Phase 4 REF-02 step 4 — extracted verbatim from agent_scoped.py.
Calls validators in services/work_context.py (Plan 04-04).
Calls git handlers in routers/agent_git.py (Plan 04-05).
Calls comment helpers re-exported via agent_scoped (Plan 04-06).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, field_validator
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, or_, and_

from app.auth import require_agent
from app.scopes import Scope, require_scope
from app.database import get_session
from app.models.agent import Agent
from app.models.approval import Approval
from app.models.board import Board, Project
from app.models.task import Task, TaskComment, TaskDependency
from app.services.activity import emit_event
from app.services.work_context import (
    enforce_board_rules_agent as _enforce_board_rules_agent,
    VALID_BLOCKER_TYPES,
)
from app.routers.agent_git import (
    handle_review_pr_creation,
    handle_done_pr_merge,
    handle_worktree_cleanup,
)
from app.utils import utcnow

logger = logging.getLogger("mc.agent_task_status")

router = APIRouter(prefix="/api/v1/agent", tags=["agent-status"])


# ─────────────────────────────────────────────────────────────────────
# Cross-task callback / help-request helpers (verbatim from agent_scoped.py
# lines 67-402 pre-04-07). Tests import these via re-export shim — Pattern S1.
# ─────────────────────────────────────────────────────────────────────


async def _handle_help_request_resume(session: AsyncSession, subtask):
    """Auto-Resume: Wenn ein Help-Request-Subtask done geht, Eltern-Task fortsetzen."""
    from app.models.task import Task
    from app.services.activity import emit_event

    if not subtask.help_request_from:
        return

    if subtask.status != "done":
        if subtask.status == "failed":
            await emit_event(
                session,
                event_type="task.help_request.failed",
                title=f"Help Request fehlgeschlagen: {subtask.title}",
                severity="warning",
                board_id=subtask.board_id,
                task_id=subtask.parent_task_id,
                agent_id=subtask.help_request_from,
                detail={"help_task_id": str(subtask.id)},
            )
        return

    parent = await session.get(Task, subtask.parent_task_id)
    if not parent or parent.blocked_by_task_id != subtask.id:
        return

    parent.status = "in_progress"
    parent.blocked_by_task_id = None
    session.add(parent)
    await session.commit()

    import asyncio as _aio
    _aio.create_task(dispatch_resume_to_agent(subtask))

    await emit_event(
        session,
        event_type="task.help_request.resolved",
        title=f"Help Request erledigt: {subtask.title}",
        severity="info",
        board_id=subtask.board_id,
        task_id=parent.id,
        agent_id=subtask.help_request_from,
        detail={
            "help_task_id": str(subtask.id),
            "helper_agent_id": str(subtask.assigned_agent_id),
        },
    )

    logger.info("Help Request resolved: subtask %s done → parent %s resumed", subtask.id, parent.id)


async def _handle_callback_resume(session: AsyncSession, subtask):
    """Auto-Resume fuer Boss-Callback-Pattern: Wenn ein Subtask done geht und
    ein Parent via blocked_by_task_id auf ihn verweist (ohne help_request_from),
    Parent auf in_progress zurueck + Callback-Event.

    Primaerpfad: Agent nutzt `mc delegate` (setzt blocked_by_task_id atomar).
    Fallback: wenn Agent `mc blocked` ohne blocked_by_task_id nutzt aber der
    Subtask einen parent_task_id-Link und callback_agent_id hat, finden wir den
    Parent ueber parent_task_id — Safety-Net gegen vergessenen Link.
    """
    from app.models.task import Task
    from sqlmodel import select
    from app.services.activity import emit_event

    if subtask.status not in ("done", "failed"):
        return

    # Primaerpfad: Parent ueber blocked_by_task_id finden
    parent_result = await session.exec(
        select(Task).where(Task.blocked_by_task_id == subtask.id)
    )
    parents = list(parent_result.all())

    # Fallback: wenn kein Parent direkt zeigt, aber Subtask hat parent_task_id +
    # callback_agent_id → der Parent koennte vergessen haben den Link zu setzen.
    # Schutz: wenn ein pending blocker_decision-Approval auf dem Parent existiert,
    # ist das ein echter Operator-Blocker und kein Callback-Wait — NICHT resumen.
    if not parents and subtask.parent_task_id and subtask.callback_agent_id:
        candidate = await session.get(Task, subtask.parent_task_id)
        if (
            candidate is not None
            and candidate.status == "blocked"
            and candidate.blocked_by_task_id is None
        ):
            from app.models.approval import Approval
            pending_approval = (
                await session.exec(
                    select(Approval).where(
                        Approval.task_id == candidate.id,
                        Approval.action_type == "blocker_decision",
                        Approval.status == "pending",
                    )
                )
            ).first()
            if pending_approval is not None:
                logger.info(
                    "Callback-Fallback skipped: Parent %s hat pending blocker_decision-Approval",
                    candidate.id,
                )
            else:
                logger.info(
                    "Callback-Fallback: Parent %s hat blocked_by_task_id=NULL, resume via parent_task_id",
                    candidate.id,
                )
                parents = [candidate]

    if not parents:
        return

    for parent in parents:
        if parent.status != "blocked":
            continue
        parent.status = "in_progress"
        parent.blocked_by_task_id = None
        session.add(parent)
        await session.commit()

        await emit_event(
            session,
            event_type="task.callback_received",
            title=f"Callback: Subtask {subtask.title} abgeschlossen",
            severity="info" if subtask.status == "done" else "warning",
            board_id=subtask.board_id,
            task_id=parent.id,
            agent_id=parent.callback_agent_id or parent.assigned_agent_id,
            detail={
                "subtask_id": str(subtask.id),
                "subtask_status": subtask.status,
            },
        )
        logger.info(
            "Callback resume: subtask %s %s → parent %s in_progress",
            subtask.id, subtask.status, parent.id,
        )

        # Callback-Pfad (kein help_request_from) → eigener Dispatcher der den
        # Parent-Agent via parent.callback_agent_id / assigned_agent_id findet.
        # dispatch_resume_to_agent() nutzt subtask.help_request_from und wuerde
        # hier early-returnen.
        try:
            import asyncio as _aio
            _aio.create_task(dispatch_callback_to_parent(parent.id, subtask.id))
        except Exception as e:
            logger.warning("dispatch_callback_to_parent failed: %s", e)


async def _handle_phase_completion_push(session: AsyncSession, completed_subtask) -> None:
    """Push-Callback: sobald der letzte Subtask einer Phase done/failed ist, Phase-Approval
    sofort erzeugen — ohne auf den Watchdog-Sweep (30s) zu warten.

    Der Watchdog bleibt als Safety-Net: falls dieser Push wegen eines Fehlers nicht
    durchläuft, erkennt der periodische Sweep die Phase beim nächsten Zyklus.

    Idempotent: wenn bereits ein phase_approval Task existiert, wird nichts getan.
    Bei failed-Subtasks zählen wir done + failed beide als "abgeschlossen" — Board Lead
    entscheidet beim Review ob rewrite nötig ist.
    """
    from app.models.task import Task
    from app.models.agent import Agent as _Agent
    from sqlmodel import select, and_

    if not completed_subtask.parent_task_id:
        return
    if completed_subtask.delegation_type == "phase_approval":
        return

    parent = await session.get(Task, completed_subtask.parent_task_id)
    if not parent or parent.status != "in_progress":
        return

    siblings_result = await session.exec(
        select(Task).where(Task.parent_task_id == parent.id)
    )
    siblings = [s for s in siblings_result.all() if s.delegation_type != "phase_approval"]
    if not siblings:
        return
    if not all(s.status in ("done", "failed") for s in siblings):
        return

    existing_result = await session.exec(
        select(Task).where(
            and_(
                Task.parent_task_id == parent.id,
                Task.delegation_type == "phase_approval",
            )
        )
    )
    if existing_result.first() is not None:
        return

    bl_result = await session.exec(
        select(_Agent).where(
            _Agent.board_id == parent.board_id,
            _Agent.is_board_lead == True,  # noqa: E712
        )
    )
    board_lead = bl_result.first()
    if board_lead is None:
        logger.warning(
            "Phase-completion push: no Board Lead on board %s — watchdog fallback übernimmt",
            parent.board_id,
        )
        return

    try:
        from app.services.task_lifecycle import create_phase_approval_task
        approval = await create_phase_approval_task(session, parent, board_lead)
        if approval is not None:
            logger.info(
                "Phase-completion push: '%s' → approval %s für %s erstellt",
                parent.title[:50], approval.id, board_lead.name,
            )
    except Exception as e:
        logger.warning(
            "Phase-completion push failed for parent %s: %s — watchdog fallback übernimmt",
            parent.id, e,
        )


async def dispatch_callback_to_parent(parent_task_id, subtask_id):
    """Schickt eine Resume-Message an den Parent-Agent im Boss-Callback-Flow.

    Anders als dispatch_resume_to_agent (Help-Request-Pfad): wir nutzen den
    explizit gespeicherten callback_agent_id ODER den assigned_agent_id des
    Parent-Tasks als Empfaenger — nicht subtask.help_request_from.
    """
    from sqlmodel.ext.asyncio.session import AsyncSession as _AS
    from app.database import engine
    from app.models.task import Task, TaskComment
    from app.models.agent import Agent
    from sqlmodel import select

    async with _AS(engine, expire_on_commit=False) as session:
        parent = await session.get(Task, parent_task_id)
        subtask = await session.get(Task, subtask_id)
        if not parent or not subtask:
            return

        target_agent_id = parent.callback_agent_id or parent.assigned_agent_id
        if not target_agent_id:
            logger.info("Callback: Parent %s hat keinen Ziel-Agent, skip message", parent.id)
            return
        target_agent = await session.get(Agent, target_agent_id)
        if not target_agent:
            logger.info("Callback: Ziel-Agent %s nicht gefunden, skip message", target_agent_id)
            return

        last_comment = (
            await session.exec(
                select(TaskComment)
                .where(TaskComment.task_id == subtask.id)
                .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
                .limit(1)
            )
        ).first()

        parts = [
            f"## Callback: Subtask '{subtask.title}' abgeschlossen ({subtask.status})\n",
            f"Du hattest auf dieses Subtask gewartet. Dein Parent-Task '{parent.title}' ist wieder auf `in_progress`.\n",
        ]
        if last_comment:
            parts.append(f"### Letzte Nachricht vom Subtask\n{last_comment.content[:2000]}\n")
        parts.append("**Mach weiter mit deiner urspruenglichen Aufgabe.**")
        message = "\n".join(parts)

        # Phase 29: Delivery via TaskComment (runtime-agnostic — poll.sh picks
        # it up on cli-bridge + host). Gateway-RPC chat_send_isolated entfernt.
        session.add(TaskComment(
            task_id=parent.id,
            author_type="system",
            content=message,
            comment_type="callback_resume",
        ))
        await session.commit()
        logger.info(
            "Callback-Resume: TaskComment fuer %s (parent %s) geschrieben",
            target_agent.name, parent.id,
        )


async def dispatch_resume_to_agent(subtask):
    """Schickt eine Resume-Message an den wartenden Agent mit dem Ergebnis."""
    from sqlmodel.ext.asyncio.session import AsyncSession as _AS
    from app.database import engine
    from app.models.task import Task, TaskComment
    from app.models.agent import Agent
    from app.models.deliverable import TaskDeliverable
    from sqlmodel import select

    async with _AS(engine, expire_on_commit=False) as session:
        parent = await session.get(Task, subtask.parent_task_id)
        if not parent:
            return

        waiting_agent = await session.get(Agent, subtask.help_request_from)
        if not waiting_agent:
            return

        comment_q = (
            select(TaskComment)
            .where(TaskComment.task_id == subtask.id)
            .order_by(TaskComment.created_at.desc())
            .limit(1)
        )
        last_comment = (await session.exec(comment_q)).first()

        deliv_q = (
            select(TaskDeliverable)
            .where(TaskDeliverable.task_id == subtask.id)
            .order_by(TaskDeliverable.created_at.desc())
            .limit(1)
        )
        last_deliverable = (await session.exec(deliv_q)).first()

        parts = [
            f"## Help Request erledigt: {subtask.title}\n",
        ]
        if subtask.assigned_agent_id:
            helper = await session.get(Agent, subtask.assigned_agent_id)
            parts.append(f"Dein Help Request wurde von {helper.name if helper else 'unbekannt'} bearbeitet.\n")
        if last_comment:
            parts.append(f"### Zusammenfassung\n{last_comment.content}\n")
        if last_deliverable:
            content_preview = (last_deliverable.content or "")[:2000]
            parts.append(f"### Deliverable: {last_deliverable.title}\n{content_preview}\n")
        parts.append("**Mach weiter mit deiner urspruenglichen Aufgabe.**")

        message = "\n".join(parts)

        # Phase 29: Delivery via TaskComment (runtime-agnostic — poll.sh picks
        # it up on cli-bridge + host). Gateway-RPC chat_send_isolated entfernt.
        session.add(TaskComment(
            task_id=parent.id,
            author_type="system",
            content=message,
            comment_type="help_request_resume",
        ))
        await session.commit()
        logger.info("Resume TaskComment fuer %s (task %s) geschrieben", waiting_agent.name, parent.id)


# ─────────────────────────────────────────────────────────────────────
# Pydantic models for status endpoints (verbatim from agent_scoped.py)
# ─────────────────────────────────────────────────────────────────────


class AgentTaskCreate(BaseModel):
    title: str
    description: str | None = None
    status: str = "inbox"
    priority: str = "medium"
    task_type: str = "story"  # story | bug | revision | chore
    project_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None
    assigned_agent_id: uuid.UUID | None = None  # Explizite Agent-Zuweisung (fuer Orchestrator)
    depends_on: list[uuid.UUID] = []  # Task-IDs auf die dieser Task wartet
    is_auto_created: bool = True
    auto_reason: str | None = None
    # Pre-Dispatch Gating (Phase 1) — Agent-Input wird bei Work Items serverseitig ueberschrieben
    dispatch_phase: Literal["planning", "ready"] | None = None
    # Orchestrator-Steuerfelder (Phase 4A) — Board Lead darf auf Root-Tasks setzen
    request_kind: Literal["code_change", "content_create", "research", "browser_task", "credential_task", "mixed"] | None = None
    approval_policy: Literal["never", "on_plan", "on_execution", "on_publish", "on_sensitive_action", "always"] | None = None
    autonomy_level: Literal["advise_only", "draft_only", "execute_low_risk", "execute_with_approval_on_risk", "manual_dispatch_required"] | None = None
    needs_browser: bool | None = None
    credential_consent: bool | None = None
    # Delegation Contract (Phase 1.5)
    delegation_type: str | None = None    # code_change | visual_proof | credential_bound | review
    branch_name: str | None = None
    target_url: str | None = None
    acceptance_criteria: str | None = None
    requires_auth: bool = False
    source_task_id: uuid.UUID | None = None  # Fuer review: Referenz zum reviewten Task
    expected_content: str | None = None  # Fuer visual_proof: erwarteter sichtbarer Inhalt
    # Completion Contract
    report_back_required: bool = False
    report_back_channel: str | None = None       # "telegram" | "discord"
    report_back_chat_id: str | None = None       # Telegram chat_id
    report_back_requirements: str | None = None  # "summary,screenshot,before_after"
    # Credentials (Klartext rein → wird verschluesselt gespeichert)
    credentials: str | None = None
    # Requester / Origin Tracking
    requester_channel: str | None = None  # "telegram" | "discord" | "web" | "agent"
    requester_id: str | None = None       # Chat-ID, User-ID, oder Agent-UUID
    # Project System — Phase-Kontext
    phase_id: uuid.UUID | None = None
    triggered_by_deliverable_id: uuid.UUID | None = None
    # Expliziter Callback-Target — überschreibt auto-set Board Lead
    callback_agent_id: uuid.UUID | None = None


class AgentTaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    project_id: uuid.UUID | None = None
    # Structured Blocker (optional — nur relevant bei status: blocked)
    blocker_type: str | None = None       # missing_info | technical_problem | decision_needed | permission_needed | dependency_blocked | other
    blocker_description: str | None = None  # Was ist das Problem?
    blocker_question: str | None = None     # Konkrete Frage an den Operator
    # Callback-Wait (Boss-Pattern): zeigt auf Subtask auf den gewartet wird
    blocked_by_task_id: uuid.UUID | None = None


class ReviewDecisionBody(BaseModel):
    decision: str  # "approve" | "request_changes" | "hold"
    comment: str

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, v: str) -> str:
        if v not in ("approve", "request_changes", "hold"):
            raise ValueError("decision must be approve, request_changes, or hold")
        return v

    @field_validator("comment")
    @classmethod
    def validate_comment(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("comment is required")
        return v


class ReportBackUpdate(BaseModel):
    status: str  # "sent"
    summary: str | None = None


class CheckpointCreate(BaseModel):
    """Agent schreibt Checkpoint — knapper Arbeitsstand + optionale strukturierte Daten."""
    state_summary: str  # max ~500 Zeichen Freitext
    checkpoint_type: Literal["auto", "manual"] = "manual"
    context_data: dict | None = None  # erledigte_schritte, naechste_schritte, artefakte

    @field_validator("state_summary")
    @classmethod
    def summary_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("state_summary darf nicht leer sein")
        if len(v) > 2000:
            raise ValueError("state_summary max 2000 Zeichen")
        return v

    @field_validator("context_data")
    @classmethod
    def no_secrets_in_context(cls, v: dict | None) -> dict | None:
        """Grober Check: keine offensichtlichen Secrets in context_data."""
        if v is None:
            return v
        import json
        dump = json.dumps(v, default=str).lower()
        blocked = ["password", "passwort", "secret_key", "api_key", "token"]
        for keyword in blocked:
            if keyword in dump and any(
                c in dump for c in ["=", ":", "bearer"]
            ):
                raise ValueError(f"context_data darf keine Secrets enthalten (gefunden: {keyword})")
        return v


# Lazy import: AgentCommentCreate, _post_subtask_blocker_comment,
# _post_subtask_completion_comment live in agent_comments.py and are
# imported INSIDE the PATCH endpoint body (Plan 04-06 boundary).
# Pattern S2 / cycle-break: agent_task_status → agent_comments would be a
# horizontal router-router import; keep it lazy.


# Priority ordering fuer Pull-Dispatch
_PRIO_CASE = {"critical": 4, "high": 3, "medium": 2, "low": 1}


async def _dependencies_met(session: AsyncSession, task: Task) -> bool:
    """Wrapper — delegiert an dispatch.dependencies_met()."""
    from app.services.dispatch import dependencies_met
    return await dependencies_met(session, task)


# ─────────────────────────────────────────────────────────────────────
# Endpoints (verbatim from agent_scoped.py — same line order as source)
# ─────────────────────────────────────────────────────────────────────


@router.get("/boards/{board_id}/tasks/next")
async def get_next_task(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    """Pull-basierter Task-Dispatch: Agent holt naechsten verfuegbaren Task.

    Returns 200 mit Task + Kontext wenn Arbeit verfuegbar.
    Returns 204 wenn Agent beschaeftigt oder keine Arbeit.
    """
    from starlette.responses import Response

    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    # 1. Agent beschaeftigt? (hat aktiven in_progress Task)
    active = (await session.exec(
        select(Task).where(
            Task.assigned_agent_id == agent.id,
            Task.status == "in_progress",
        )
    )).first()
    if active:
        return Response(status_code=204)

    # 2. Zugewiesene inbox-Tasks laden
    # dispatch_phase Guard: Tasks mit "planning" sind NICHT verfuegbar (muessen erst promoted werden)
    candidates = (await session.exec(
        select(Task).where(
            Task.assigned_agent_id == agent.id,
            Task.status == "inbox",
            or_(
                Task.dispatch_phase.is_(None),  # type: ignore[union-attr]
                Task.dispatch_phase == "ready",
            ),
        ).order_by(Task.created_at.asc())
    )).all()

    # Python-sort nach Prioritaet (SQL CASE geht nicht portabel)
    candidates = sorted(candidates, key=lambda t: -_PRIO_CASE.get(t.priority, 2))

    # 3. Dependencies pruefen + Root-Guard + ersten verfuegbaren Task nehmen
    for task in candidates:
        if not await _dependencies_met(session, task):
            continue

        # Root-Tasks (parent_task_id=NULL) duerfen nur von Board Lead gepullt werden
        if task.parent_task_id is None and not agent.is_board_lead:
            continue

        # Task aktivieren
        task.status = "in_progress"
        # F2 fix (Plan 26-03): first-set-wins on started_at — preserves
        # original "work began" timestamp on re-opens. Pull-dispatch normally
        # picks fresh inbox tasks (started_at=NULL), but re-queued tasks may
        # have a prior started_at that must not be reset.
        if task.started_at is None:
            task.started_at = utcnow()
        task.ack_at = utcnow()  # Pull = impliziter ACK
        task.dispatch_phase = None  # Gate resetten (wie Push-Dispatch)
        task.updated_at = utcnow()
        # Active-Task Lock setzen (nur Board Lead — Workers haben parallele Sessions)
        from app.config import settings as _pull_settings
        if not (_pull_settings.use_subagent_dispatch and not agent.is_board_lead):
            agent.current_task_id = task.id
        session.add(task)
        session.add(agent)
        await session.commit()
        await session.refresh(task)

        # Task-Event loggen (wie Push-Dispatch)
        from app.services.task_lifecycle import record_task_event
        await record_task_event(
            session, task.id, "inbox", "in_progress",
            changed_by="agent", agent_id=agent.id, reason="pull_dispatch",
        )

        await emit_event(
            session, "task.pull_dispatched",
            f"{agent.emoji or '🤖'} {agent.name} hat Task uebernommen: '{task.title}'",
            board_id=board_id, task_id=task.id, agent_id=agent.id,
        )

        # Kontext aufbauen
        from app.services.dispatch import _build_dispatch_message
        context = await _build_dispatch_message(task, agent, session)

        # Credentials entschluesseln fuer den assigned Agent
        credentials = None
        if task.credentials_encrypted:
            from app.services.encryption import safe_decrypt
            credentials = safe_decrypt(task.credentials_encrypted)

        return {"task": task, "context": context, "credentials": credentials}

    # 4. Kein Task verfuegbar
    return Response(status_code=204)


@router.get("/boards/{board_id}/tasks")
async def agent_list_tasks(
    board_id: uuid.UUID,
    status_filter: str | None = Query(None, alias="status"),
    assigned_agent_id: uuid.UUID | None = Query(None),
    parent_task_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    """Tasks eines Boards auflisten — mit optionalen Filtern."""
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    query = select(Task).where(Task.board_id == board_id)
    if status_filter:
        query = query.where(Task.status == status_filter)
    if assigned_agent_id:
        query = query.where(Task.assigned_agent_id == assigned_agent_id)
    if parent_task_id:
        query = query.where(Task.parent_task_id == parent_task_id)
    query = query.order_by(Task.created_at.desc()).limit(limit)

    result = await session.exec(query)
    tasks = result.all()

    # Agent-Namen aufloesen
    agent_ids = {t.assigned_agent_id for t in tasks if t.assigned_agent_id}
    agent_map: dict[str, str] = {}
    if agent_ids:
        agents_result = await session.exec(
            select(Agent).where(Agent.id.in_(agent_ids))  # type: ignore[attr-defined]
        )
        agent_map = {str(a.id): a.name for a in agents_result.all()}

    return [
        {
            "id": str(t.id),
            "title": t.title,
            "status": t.status,
            "priority": t.priority,
            "task_type": t.task_type,
            "assigned_agent_id": str(t.assigned_agent_id) if t.assigned_agent_id else None,
            "assigned_agent_name": agent_map.get(str(t.assigned_agent_id)) if t.assigned_agent_id else None,
            "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            "dispatched_at": t.dispatched_at.isoformat() if t.dispatched_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        }
        for t in tasks
    ]


@router.get("/boards/{board_id}/tasks/{task_id}/detail")
async def agent_get_task_detail(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    """Einzelnen Task mit allen Details lesen."""
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    # Agent-Name aufloesen
    assigned_name = None
    if task.assigned_agent_id:
        assigned = await session.get(Agent, task.assigned_agent_id)
        assigned_name = assigned.name if assigned else None

    data = task.model_dump()
    data["assigned_agent_name"] = assigned_name
    return data


@router.delete(
    "/boards/{board_id}/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_scope(Scope.TASKS_MANAGE))],
)
async def agent_delete_task(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_agent),
):
    """Task loeschen — nur mit tasks:manage Scope (Board Leads).

    Loescht Task + alle zugehoerigen Kommentare, Dependencies, Events,
    Checkpoints, Deliverables, Checklist-Items.
    """
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.models.approval import Approval
    from app.models.activity import ActivityEvent
    from app.models.task import TaskEvent
    from app.models.checkpoint import TaskCheckpoint
    from app.models.deliverable import TaskDeliverable
    from app.models.checklist import TaskChecklistItem

    title = task.title

    # Subtasks: parent_task_id loesen
    for sub in (await session.exec(select(Task).where(Task.parent_task_id == task_id))).all():
        sub.parent_task_id = None
        session.add(sub)

    # Abhaengige Daten loeschen
    for model, fk in [
        (TaskComment, TaskComment.task_id),
        (TaskDependency, TaskDependency.task_id),
        (Approval, Approval.task_id),
        (ActivityEvent, ActivityEvent.task_id),
        (TaskEvent, TaskEvent.task_id),
        (TaskCheckpoint, TaskCheckpoint.task_id),
        (TaskDeliverable, TaskDeliverable.task_id),
        (TaskChecklistItem, TaskChecklistItem.task_id),
    ]:
        for row in (await session.exec(select(model).where(fk == task_id))).all():
            await session.delete(row)

    # Reverse Dependencies
    for dep in (await session.exec(
        select(TaskDependency).where(TaskDependency.depends_on_task_id == task_id)
    )).all():
        await session.delete(dep)

    # Agent current_task_id loesen
    for ag in (await session.exec(select(Agent).where(Agent.current_task_id == task_id))).all():
        ag.current_task_id = None
        session.add(ag)

    await session.delete(task)
    await session.commit()

    logger.info("Agent %s deleted task '%s' (%s)", agent.name, title[:60], task_id)
    await emit_event(
        session, "task.deleted",
        f"{agent.name} hat Task '{title[:60]}' geloescht",
        agent_id=agent.id, board_id=board_id,
        detail={"task_id": str(task_id), "title": title},
    )


@router.get("/boards/{board_id}/tasks/pipeline")
async def agent_get_pipeline(
    board_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    """Pipeline-Ansicht: aktive Tasks gruppiert nach Status."""
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    result = await session.exec(
        select(Task).where(Task.board_id == board_id).order_by(Task.updated_at.desc())
    )
    tasks = result.all()

    # Agent-Map
    agent_ids = {t.assigned_agent_id for t in tasks if t.assigned_agent_id}
    agent_map: dict[str, str] = {}
    if agent_ids:
        agents_result = await session.exec(
            select(Agent).where(Agent.id.in_(agent_ids))  # type: ignore[attr-defined]
        )
        agent_map = {str(a.id): a.name for a in agents_result.all()}

    pipeline: dict[str, list] = {"inbox": [], "in_progress": [], "review": [], "blocked": [], "done": [], "failed": []}
    for t in tasks:
        bucket = pipeline.get(t.status, pipeline.get("inbox"))
        if bucket is not None:
            bucket.append({
                "id": str(t.id),
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "assigned_agent_name": agent_map.get(str(t.assigned_agent_id)),
                "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            })

    return {
        "pipeline": pipeline,
        "counts": {k: len(v) for k, v in pipeline.items()},
        "total": len(tasks),
    }


# NOTE: GET /{task_id} must come AFTER static paths like /pipeline and /next
# (Router Ordering Rule — CLAUDE.md). Static segments must be declared first.
@router.get("/boards/{board_id}/tasks/{task_id}")
async def agent_get_task(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    """Einzelnen Task lesen — Kurzform ohne /detail-Suffix."""
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")
    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")
    assigned_name = None
    if task.assigned_agent_id:
        assigned = await session.get(Agent, task.assigned_agent_id)
        assigned_name = assigned.name if assigned else None
    data = task.model_dump()
    data["assigned_agent_name"] = assigned_name
    return data


@router.post("/boards/{board_id}/tasks", status_code=status.HTTP_201_CREATED)
async def agent_create_task(
    board_id: uuid.UUID,
    payload: AgentTaskCreate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_CREATE)),
):
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    # ── Duplicate Root Guard (Idempotency) ────────────────────────
    # Verhindert doppelte Root-Tasks durch Retry/Unsicherheit.
    # Nur Roots (kein parent_task_id), nur im 60s Fenster, nur gleicher Kanal+Absender.
    if not payload.parent_task_id:
        import re as _re
        from datetime import timedelta

        def _normalize_title(t: str) -> str:
            return _re.sub(r"\s+", " ", (t or "").strip().lower())[:50]

        _new_title = _normalize_title(payload.title if hasattr(payload, "title") else "")
        if _new_title:
            _dup_query = select(Task).where(
                Task.parent_task_id.is_(None),  # type: ignore[union-attr]
                Task.board_id == board_id,
                Task.owner_agent_id == agent.id,
                Task.created_at > utcnow() - timedelta(seconds=60),
            )
            # Requester-Filter nur wenn gesetzt
            if getattr(payload, "requester_channel", None):
                _dup_query = _dup_query.where(Task.requester_channel == payload.requester_channel)
            if getattr(payload, "requester_id", None):
                _dup_query = _dup_query.where(Task.requester_id == payload.requester_id)

            _dup_result = await session.exec(_dup_query)
            for _existing in _dup_result.all():
                if _normalize_title(_existing.title) != _new_title:
                    continue
                # inbox → immer dedupen
                if _existing.status == "inbox":
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "duplicate_root",
                            "existing_task_id": str(_existing.id),
                            "message": f"Aehnlicher Root-Task existiert bereits (erstellt vor "
                                       f"{int((utcnow() - _existing.created_at).total_seconds())}s)",
                        },
                    )
                # in_progress → nur wenn sehr frisch UND keine Children
                if _existing.status == "in_progress":
                    _children = await session.exec(
                        select(Task.id).where(Task.parent_task_id == _existing.id).limit(1)
                    )
                    if not _children.first():
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "error": "duplicate_root",
                                "existing_task_id": str(_existing.id),
                                "message": f"Aehnlicher Root-Task in Bearbeitung (erstellt vor "
                                           f"{int((utcnow() - _existing.created_at).total_seconds())}s, noch keine Children)",
                            },
                        )

    # Board Lead Delegation: description ist Pflicht wenn an anderen Agent delegiert wird
    if agent.is_board_lead and payload.assigned_agent_id and payload.assigned_agent_id != agent.id:
        if not payload.description or len(payload.description.strip()) < 50:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Delegation braucht eine ausfuehrliche Beschreibung (mind. 50 Zeichen). "
                    "Pflichtpunkte: Ziel, Kontext, Guardrails, erwarteter Output, Definition of Done. "
                    "Wenn Credentials noetig sind: im credentials-Feld setzen oder den Operator fragen."
                ),
            )

    # ── Board Lead Self-Task Guard ─────────────────────────────
    # Board Lead darf keine self-assigned Child-Tasks anlegen.
    # Self-Tasks bleiben technisch in inbox haengen (kein Dispatch-Pfad).
    # Nicht-codierende Eigenarbeit soll direkt auf dem Root-Task dokumentiert werden.
    if (agent.is_board_lead
            and payload.parent_task_id
            and payload.assigned_agent_id == agent.id):
        raise HTTPException(
            status_code=422,
            detail=(
                "Board Lead darf keine self-assigned Child-Tasks anlegen. "
                "Nicht-codierende Eigenarbeit (pruefen, bewerten, zusammenfassen) "
                "direkt auf dem Root-Task als Kommentar dokumentieren."
            ),
        )

    # Planner-Delegation Guard entfernt (Phase 6, 2026-04-11).
    # Boss plant selbst via openclaude-Subagents — kein Planner-Intermediate,
    # keine with_planner-Blockade, keine Planning-Sibling-Race.

    # ── Delegation Contract Guard (Phase 1.5) ─────────────────
    # Vererbung: branch_name und requires_auth vom Parent
    if payload.parent_task_id:
        parent_for_inherit = await session.get(Task, payload.parent_task_id)
        if parent_for_inherit:
            if not payload.branch_name and parent_for_inherit.branch_name:
                payload.branch_name = parent_for_inherit.branch_name
            if not payload.requires_auth and parent_for_inherit.requires_auth:
                payload.requires_auth = parent_for_inherit.requires_auth
            # credentials werden weiter unten separat vererbt (existierender Code)

    # Contract-Validierung (gilt fuer ALLE Task-Ersteller, nicht nur Board Lead)
    # Root-Credentials zaehlen als gueltige Erfuellung fuer auth-beduerftige Children.
    from app.services.delegation_contracts import validate_delegation_contract
    _inherited_creds = False
    if not payload.credentials and payload.parent_task_id and parent_for_inherit:
        _inherited_creds = bool(parent_for_inherit.credentials_encrypted)
    contract_fields = {
        "branch_name": payload.branch_name,
        "target_url": payload.target_url,
        "acceptance_criteria": payload.acceptance_criteria,
        "credentials": payload.credentials or ("__inherited__" if _inherited_creds else None),
        "requires_auth": payload.requires_auth,
        "source_task_id": payload.source_task_id,
        "expected_content": payload.expected_content,
        "description": payload.description,
    }
    hard_errors, warnings = validate_delegation_contract(payload.delegation_type, contract_fields)

    if hard_errors:
        raise HTTPException(
            status_code=422,
            detail=f"Delegation Contract '{payload.delegation_type}' nicht erfuellt: "
                   + "; ".join(hard_errors),
        )

    # Orchestrator-Steuerfelder: nur Board Lead darf sie setzen
    # Workers duerfen request_kind/approval_policy/autonomy_level/needs_browser nicht setzen
    _ORCHESTRATOR_FIELDS = {"request_kind", "approval_policy", "autonomy_level", "needs_browser"}
    if not agent.is_board_lead:
        for field in _ORCHESTRATOR_FIELDS:
            if getattr(payload, field, None) is not None:
                setattr(payload, field, None)  # Still ignorieren, kein 403

    # assigned_agent_id: wenn explizit gesetzt → diese ID nutzen, sonst erstellenden Agent
    effective_assignee = payload.assigned_agent_id or agent.id

    # Archived-Agent-Guard: archivierte Agents duerfen keine neuen Tasks bekommen.
    # Offline ist erlaubt (Pending-Queue / Re-Dispatch greift spaeter).
    if payload.assigned_agent_id and payload.assigned_agent_id != agent.id:
        from app.models.agent import Agent as AgentModel
        target_agent = await session.get(AgentModel, payload.assigned_agent_id)
        if target_agent and target_agent.status == "archived":
            raise HTTPException(
                422,
                f"Agent '{target_agent.name}' ist archiviert und kann keine neuen Tasks erhalten.",
            )

    # Closed-Parent-Guard: keine neuen Children unter done/failed Root.
    # Parent auf review wird reopened (siehe unten), nicht blockiert.
    if payload.parent_task_id:
        parent_for_guard = await session.get(Task, payload.parent_task_id)
        if parent_for_guard and parent_for_guard.status in ("done", "failed"):
            raise HTTPException(
                422,
                f"Parent-Task '{parent_for_guard.title[:50]}' ist bereits {parent_for_guard.status}. "
                "Neue Arbeit muss als eigener Root-Task geplant werden.",
            )
        # Parent-Reopen: Phase-Approval setzt Parent auf review sobald alle bisherigen
        # Subtasks done sind. Ein nachtraeglich erstellter Subtask (z.B. Boss delegiert
        # Follow-up an Davinci) haette den Parent sonst haengen lassen in review.
        if parent_for_guard and parent_for_guard.status == "review":
            from app.services.task_lifecycle import reopen_parent_for_new_subtask
            await reopen_parent_for_new_subtask(
                session, payload.parent_task_id,
                new_subtask_title=getattr(payload, "title", None),
            )

    task_data = payload.model_dump(exclude={"assigned_agent_id", "depends_on", "credentials", "source_task_id", "callback_agent_id"})
    # phase_id und triggered_by_deliverable_id werden automatisch via model_dump eingeschlossen

    # source_task_id separat setzen (nicht im exclude weil es ein FK ist)
    if payload.source_task_id:
        task_data["source_task_id"] = payload.source_task_id

    # Credentials verschluesseln wenn vorhanden
    if payload.credentials:
        from app.services.encryption import encrypt
        task_data["credentials_encrypted"] = encrypt(payload.credentials)

    # project_id Zuweisung (Prioritaet: explizit > Parent > Board-Default)
    if not payload.project_id:
        if payload.parent_task_id:
            parent = await session.get(Task, payload.parent_task_id)
            if parent and parent.project_id:
                task_data["project_id"] = parent.project_id
        if not task_data.get("project_id"):
            board_for_default = await session.get(Board, board_id)
            if board_for_default and board_for_default.default_project_id:
                task_data["project_id"] = board_for_default.default_project_id

    # ── Duplicate Child Guard (PRE-COMMIT) ─────────────────
    # Verhindert dass ein Agent zwei aktive Subtasks unter demselben Parent bekommt.
    # Kriterien: gleicher Parent + gleicher zugewiesener Agent + aktiver Status.
    if payload.parent_task_id and effective_assignee:
        existing_children = await session.exec(
            select(Task).where(
                Task.parent_task_id == payload.parent_task_id,
                Task.assigned_agent_id == effective_assignee,
                Task.status.notin_(["done", "failed"]),  # type: ignore[union-attr]
            )
        )
        active_sibling = existing_children.first()
        if active_sibling:
            target_name = (await session.get(Agent, effective_assignee))
            agent_name = target_name.name if target_name else str(effective_assignee)
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Duplicate Child blockiert: {agent_name} hat bereits einen aktiven "
                    f"Subtask unter diesem Parent: \"{active_sibling.title}\" "
                    f"(status: {active_sibling.status}). "
                    f"Bitte den bestehenden Task abschliessen, abbrechen oder neu zuweisen "
                    f"bevor ein neuer erstellt wird."
                ),
            )

    # Soft-Validation: Description-Qualität prüfen
    if payload.description and len(payload.description) > 100:
        has_markdown = any(signal in payload.description for signal in ("##", "**", "\n\n", "- ", "1. "))
        if not has_markdown:
            logger.warning(
                "description ohne Markdown-Signale (agent=%s, task=%s, len=%d)",
                agent.id, payload.title, len(payload.description),
            )

    task = Task(board_id=board_id, assigned_agent_id=effective_assignee, **task_data)
    # dispatch_intent: Agent-erstellte Tasks sind immer Subtasks
    task.dispatch_intent = "subtask"
    # owner_agent_id: Der erstellende Agent ist Owner (immutable, aendert sich nie)
    task.owner_agent_id = agent.id

    # callback_agent_id: explizit oder auto-set
    # Expliziter Wert aus Payload hat Vorrang
    if payload.callback_agent_id is not None:
        task.callback_agent_id = payload.callback_agent_id
    elif not agent.is_board_lead:
        # Nicht-Board-Lead erstellt Task → Callback an Board Lead setzen.
        # Phase 30: gateway_agent_id filter dropped — Board Lead lookup is
        # purely by board + role flag now.
        board_lead = (await session.exec(
            select(Agent).where(
                Agent.board_id == board_id,
                Agent.is_board_lead == True,  # noqa: E712
            )
        )).first()
        if board_lead:
            task.callback_agent_id = board_lead.id
    # Board Lead erstellt Task → callback_agent_id bleibt null
    # (owner_agent_id = Board Lead → bestehender Fallback greift korrekt)

    # Pre-Dispatch Gating: Agent-Bypass schliessen
    # Ausfuehrbare Work Items (Child + assigned an anderen Agent) → erzwungen "planning"
    # Root-Tasks und Self-Assigned → kein Gating (null)
    from app.config import settings as _settings
    if _settings.enable_dispatch_gating:
        is_executable_work_item = (
            task.parent_task_id is not None
            and task.assigned_agent_id is not None
            and task.assigned_agent_id != agent.id
        )
        if is_executable_work_item:
            task.dispatch_phase = "planning"
        else:
            task.dispatch_phase = None

    session.add(task)
    await session.commit()
    await session.refresh(task)

    # Dependencies erstellen
    if payload.depends_on:
        for dep_id in payload.depends_on:
            session.add(TaskDependency(task_id=task.id, depends_on_task_id=dep_id))
        await session.commit()

    agent.last_task_activity_at = utcnow()
    session.add(agent)
    await session.commit()

    await emit_event(
        session, "task.created", f"Agent {agent.name} created task: {task.title}",
        board_id=board_id, task_id=task.id, agent_id=agent.id,
    )

    # ── Delegation Contract Warnings als Activity Events ────
    for warning in warnings:
        await emit_event(
            session, "delegation.warning", warning,
            severity="warning",
            board_id=board_id, task_id=task.id, agent_id=agent.id,
        )

    # Pre-Dispatch Gating: Sofort auto-promoten wenn moeglich, sonst auf Watchdog warten
    _skip_dispatch = False
    if _settings.enable_dispatch_gating and task.dispatch_phase == "planning":
        from app.services.dispatch_gating import (
            evaluate_promote_decision, promote_task_to_ready, AUTO_PROMOTE, HIGH_RISK_TAGS,
        )
        try:
            # Board-Lead-delegierte Tasks: autonomy_level inline setzen (wie Watchdog es tut)
            if task.owner_agent_id and not task.autonomy_level:
                _owner = await session.get(Agent, task.owner_agent_id)
                # Phase 30: gateway_agent_id slug-match dropped (was a legacy
                # planner detection path). Name-substring match covers it.
                _is_authorized = _owner and (
                    _owner.is_board_lead
                    or "planner" in (_owner.name or "").lower()
                )
                if _is_authorized:
                    _tags: set[str] = set()
                    _tags_raw = getattr(task, "tags", None)
                    if isinstance(_tags_raw, list):
                        for _t in _tags_raw:
                            if isinstance(_t, str):
                                _tags.add(_t.lower())
                            elif isinstance(_t, dict) and "name" in _t:
                                _tags.add(_t["name"].lower())
                    if not (_tags & HIGH_RISK_TAGS) and not getattr(task, "requires_auth", False):
                        task.autonomy_level = "execute_low_risk"
                        session.add(task)
                        await session.commit()
                        await session.refresh(task)

            # Parent laden fuer Kontext-Vererbung (wie Watchdog)
            _parent_for_promote = None
            if task.parent_task_id:
                _parent_for_promote = await session.get(Task, task.parent_task_id)

            decision, _promote_reason = evaluate_promote_decision(task, parent_task=_parent_for_promote)
            if decision == AUTO_PROMOTE:
                task = await promote_task_to_ready(task, session)
                await session.refresh(task)
                logger.info(
                    "Inline auto-promote: '%s' → ready (%s)", task.title[:50], _promote_reason
                )
                # Dispatch laeuft unten normal weiter
            else:
                _skip_dispatch = True
                dispatch_info = {"status": "planning", "reason": _promote_reason}
        except Exception as _promote_err:
            logger.warning("Inline auto-promote failed for '%s': %s", task.title, _promote_err)
            _skip_dispatch = True
            dispatch_info = {"status": "planning", "reason": "dispatch_gating_active"}

    # Operational Controls Guard
    from app.services.operations import check_dispatch_allowed

    # Dependency-Gate — Task bleibt inbox wenn Vorgaenger nicht 'done' sind.
    # Der direct-dispatch path unten (CLI-Bridge + Gateway) umgeht sonst die
    # dependencies_met()-Pruefung die auto_dispatch/watchdog/task_lifecycle
    # haben. Recovery ist automatisch: bei status=done eines Vorgaengers
    # scannt derselbe Router (ganz unten, ~Zeile 2504) die Dependents und
    # dispatcht dann nach.
    if not _skip_dispatch:
        from app.services.dispatch import dependencies_met
        if not await dependencies_met(session, task):
            _skip_dispatch = True
            dispatch_info = {
                "status": "waiting_for_deps",
                "reason": "dependencies not done",
            }
            logger.info(
                "Task '%s' not dispatched — waiting on dependencies",
                task.title[:60],
            )

    # Explizite Zuweisung an anderen Agent → pruefen ob busy, dann dispatchen oder queuen
    if not _skip_dispatch:
        dispatch_info = None
    target_agent = None
    if _skip_dispatch:
        pass  # dispatch_info already set above
    elif payload.assigned_agent_id and payload.assigned_agent_id != agent.id:
        target_agent = await session.get(Agent, payload.assigned_agent_id)

        if target_agent:
            # Guard: darf an diesen Agent dispatcht werden? (gilt fuer alle Runtimes)
            allowed, reason = await check_dispatch_allowed(task, target_agent, session)
            if not allowed:
                logger.info("Agent-subtask dispatch blocked: '%s' — %s", task.title, reason)
                dispatch_info = {"status": "blocked", "reason": reason}
                target_agent = None  # Skip dispatch below

        _target_runtime = getattr(target_agent, "agent_runtime", "openclaw") if target_agent else None

        if target_agent and _target_runtime == "cli-bridge":
            # ── CLI-Bridge: direkt dispatchen, kein OpenClaw-Umweg ──
            import uuid as _uuid
            from app.services.dispatch import _build_dispatch_message
            from app.services.cli_bridge_runner import dispatch_to_cli_bridge
            from app.services.dispatch_attempt_audit import set_dispatch_attempt_id
            try:
                await set_dispatch_attempt_id(
                    session, task, str(_uuid.uuid4()),
                    caller="agent_subtask_create",
                    reason="new_subtask_direct_dispatch",
                )
                message = await _build_dispatch_message(task, target_agent, session)
                started = await dispatch_to_cli_bridge(target_agent, task, message, session)
                if started:
                    task.dispatched_at = utcnow()
                    task.updated_at = utcnow()
                    target_agent.run_state = "running"
                    session.add(task)
                    session.add(target_agent)
                    await session.commit()
                    dispatch_info = {"status": "dispatched", "target_agent": target_agent.name}
                    logger.info("CLI bridge direct dispatch: '%s' -> %s", task.title, target_agent.name)
                else:
                    dispatch_info = {"status": "not_dispatched", "reason": "cli_bridge_failed", "target_agent": target_agent.name}
            except Exception as e:
                logger.warning("CLI bridge dispatch failed for '%s': %s", task.title, e)
                dispatch_info = {"status": "not_dispatched", "reason": "cli_bridge_error", "target_agent": target_agent.name}
        elif target_agent:
            dispatch_info = {"status": "not_dispatched", "reason": "agent_not_provisioned", "target_agent": target_agent.name}
        else:
            dispatch_info = {"status": "not_dispatched", "reason": "agent_not_found"}

    # Board Lead Implicit ACK: Subtask erstellen = Parent-Task bestaetigen
    if payload.parent_task_id:
        parent = await session.get(Task, payload.parent_task_id)
        if (parent and parent.status == "inbox"
                and parent.assigned_agent_id == agent.id):
            parent.status = "in_progress"
            # F2 fix (Plan 26-03): first-set-wins on started_at.
            if parent.started_at is None:
                parent.started_at = utcnow()
            parent.ack_at = utcnow()
            parent.updated_at = utcnow()
            session.add(parent)
            await session.commit()
            logger.info("Implicit ACK: parent task '%s' by %s (subtask created)", parent.title, agent.name)

    # Response mit Dispatch-Feedback enrichen
    result = task.model_dump()
    result["assigned_agent_name"] = target_agent.name if target_agent else None
    if dispatch_info:
        result["dispatch"] = dispatch_info
    return result


@router.patch("/boards/{board_id}/tasks/{task_id}")
async def agent_update_task(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: AgentTaskUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_WRITE)),
):
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    # ── Run-Control Guard ──────────────────────────────────────────
    # Gestoppte/gehaltene Tasks akzeptieren KEINE Agent-Updates mehr.
    # In-flight Claude-Calls können nach sessions.reset() noch ankommen.
    if task.run_control in ("stopped", "manual_hold"):
        logger.warning(
            "Late update rejected: Agent %s tried PATCH on stopped task '%s' "
            "(run_control=%s, attempted status=%s)",
            agent.name, task.title, task.run_control,
            payload.status if hasattr(payload, "status") else "n/a",
        )
        await emit_event(
            session, "task.late_update_rejected",
            f"Agent {agent.name} Update auf gestoppten Task '{task.title}' abgelehnt",
            severity="warning",
            board_id=board_id, task_id=task.id, agent_id=agent.id,
            detail={"run_control": task.run_control, "attempted": payload.model_dump(exclude_none=True)},
        )
        raise HTTPException(
            status_code=409,
            detail=f"Task run_control={task.run_control} — Updates nicht erlaubt"
        )

    # ── Dispatch Attempt Guard ────────────────────────────────────
    # Schuetzt gegen stale Updates von alten Runs nach Stop/Requeue.
    # Phase A: warn only. Phase B (ENFORCE_DISPATCH_ATTEMPT_ID=true): hard 409.
    #
    # Zwei Fehlerklassen werden unterschieden:
    # 1. Header fehlt komplett (received=None) → "missing_dispatch_attempt_id"
    # 2. Header mit altem Wert (received != expected) → "stale_dispatch_attempt_id"
    # Agent braucht beide Unterscheidung um korrekt zu reagieren.
    _req_attempt_id = request.headers.get("X-Dispatch-Attempt-Id")
    if task.dispatch_attempt_id and _req_attempt_id != task.dispatch_attempt_id:
        from app.config import settings as _settings
        _header_missing = _req_attempt_id is None
        _detail = {
            "expected": task.dispatch_attempt_id,
            "received": _req_attempt_id,
            "reason": "missing_header" if _header_missing else "stale_value",
            "attempted": payload.model_dump(exclude_none=True),
        }

        if _header_missing:
            _error_detail = (
                f"Fehlender X-Dispatch-Attempt-Id Header. Nutze fuer Status-Aenderungen "
                f"die mc-CLI (`mc ack` / `mc review` / `mc done` / `mc blocked` / `mc failed`) "
                f"oder `mc comment` — die setzt den Header automatisch aus /tmp/mc-context.env. "
                f"Wenn du wirklich raw curl brauchst, sende '-H \"X-Dispatch-Attempt-Id: "
                f"{task.dispatch_attempt_id}\"' mit."
            )
            _event_type = "task.missing_dispatch_attempt_id"
            _event_msg = f"{agent.name}: X-Dispatch-Attempt-Id Header fehlt bei Update auf '{task.title}'"
            _log_msg = "Missing header REJECTED: Agent %s sent no X-Dispatch-Attempt-Id, expected=%s for task '%s'"
            _log_args = (agent.name, task.dispatch_attempt_id, task.title)
        else:
            _error_detail = (
                f"Stale dispatch_attempt_id — dein Run ist veraltet. "
                f"Erwartet: {task.dispatch_attempt_id}, gesendet: {_req_attempt_id}. "
                f"Der Task wurde neu dispatcht — dein Update stammt von einem alten Run."
            )
            _event_type = "task.stale_update_rejected"
            _event_msg = f"Stale Update von {agent.name} auf '{task.title}' abgelehnt (falsche dispatch_attempt_id)"
            _log_msg = "Stale update REJECTED: Agent %s sent attempt_id=%s, expected=%s for task '%s'"
            _log_args = (agent.name, _req_attempt_id, task.dispatch_attempt_id, task.title)

        if _settings.enforce_dispatch_attempt_id:
            logger.warning(_log_msg, *_log_args)
            # Differenzierte Event-Emission (2026-05-18):
            # - missing_header = Agent-Briefing/Tool-Wahl-Problem (z.B. raw curl
            #   statt `mc done`). Der 409-HTTP-Response erzieht den Agent zur
            #   Korrektur und produziert die Selbst-Recovery, die wir in den
            #   Logs sehen. Discord-Noise auf jedem Dispatch ist Mehrwert =
            #   null. Nur log.warning, kein emit_event.
            # - stale_value = ECHTER Run-Konflikt (alter Run schreibt nach
            #   Stop/Requeue). Bleibt Event mit severity=warning, weil es
            #   Aufschluss ueber kaputte Run-Lifecycles gibt.
            if not _header_missing:
                await emit_event(
                    session, _event_type, _event_msg,
                    severity="warning",
                    board_id=board_id, task_id=task.id, agent_id=agent.id,
                    detail=_detail,
                )
            raise HTTPException(status_code=409, detail=_error_detail)
        elif _req_attempt_id is not None:
            # Header gesendet aber falsch → immer warnen (auch in Phase A)
            logger.warning(_log_msg, *_log_args)
            await emit_event(
                session, "task.stale_update_warning",
                f"Stale Update von {agent.name} auf '{task.title}' (falsche dispatch_attempt_id, Phase A: durchgelassen)",
                severity="info",
                board_id=board_id, task_id=task.id, agent_id=agent.id,
                detail=_detail,
            )

    # Reviewer-Erkennung (frueher definieren fuer spaetere Guards)
    is_reviewer = False
    if agent.role:
        from app.scopes import AgentRole
        try:
            is_reviewer = AgentRole(agent.role) == AgentRole.REVIEWER
        except ValueError:
            pass
    elif agent.name and ("rex" in agent.name.lower() or "review" in agent.name.lower()):
        is_reviewer = True

    # Ownership-Check: Agent darf nur eigene Tasks aendern (mit Ausnahmen)
    if task.assigned_agent_id != agent.id:
        is_lead = agent.is_board_lead
        allowed = is_lead or (is_reviewer and task.status == "review")
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail="Agent darf nur eigene Tasks aendern",
            )

    old_status = task.status
    updates = payload.model_dump(exclude_none=True)

    # ── Review-Safeguard: Widerspruch erkennen ──────────────────────────
    # Wenn Reviewer "in_progress" setzt aber sein letzter Kommentar "Approved" sagt
    # → automatisch zu "done" korrigieren (GLM-5 verwechselt die Status-Werte)
    if (
        old_status == "review"
        and updates.get("status") == "in_progress"
        and agent.role
    ):
        from app.scopes import AgentRole
        try:
            is_reviewer = AgentRole(agent.role) == AgentRole.REVIEWER
        except ValueError:
            is_reviewer = False

        if is_reviewer:
            recent_cmt = (await session.exec(
                select(TaskComment)
                .where(
                    TaskComment.task_id == task.id,
                    TaskComment.author_agent_id == agent.id,
                )
                .order_by(TaskComment.created_at.desc())
                .limit(1)
            )).first()
            if recent_cmt:
                content_lower = recent_cmt.content.lower()
                approval_signals = ["approved", "bestanden", "approve", "lgtm"]
                if any(signal in content_lower for signal in approval_signals):
                    logger.warning(
                        "Review-Safeguard: %s schrieb '%s' aber setzte in_progress — korrigiere zu done",
                        agent.name, recent_cmt.content[:80],
                    )
                    updates["status"] = "done"

    # ── Fallback: review_decision automatisch setzen bei altem PATCH-Weg ──
    # NUR done → approved. NICHT in_progress → changes_requested!
    # Reviewer-ACK (review→in_progress) ist Arbeitsbeginn, keine Entscheidung.
    # Fuer changes_requested muss der explizite POST /review Endpoint genutzt werden.
    if "status" in updates and old_status == "review" and task.review_decision is None:
        if updates["status"] == "done":
            task.review_decision = "approved"
            task.review_decided_at = utcnow()

    # ── Konsistenz-Guard: review_decision ↔ letzter Kommentar ──
    # Wenn Reviewer status=done setzt aber letzter Kommentar "not ship-ready" enthaelt → warnen
    if "status" in updates and old_status == "review" and is_reviewer:
        recent_cmt = (await session.exec(
            select(TaskComment)
            .where(TaskComment.task_id == task.id, TaskComment.author_agent_id == agent.id)
            .order_by(TaskComment.created_at.desc())
            .limit(1)
        )).first()
        if recent_cmt:
            cmt_lower = recent_cmt.content.lower()
            if updates["status"] == "done" and "not ship-ready" in cmt_lower:
                logger.warning(
                    "Review-Konsistenz: %s setzte done aber Kommentar enthaelt 'not ship-ready' — korrigiere zu in_progress",
                    agent.name,
                )
                updates["status"] = "in_progress"
                task.review_decision = "changes_requested"
                task.review_decided_at = utcnow()

    # Board Rules pruefen bevor Status geaendert wird
    if "status" in updates:
        # ── Subtask Auto-Korrektur: review → done ─────────────────
        # Subtasks (mit parent_task_id) sollen direkt done gehen.
        # Falls ein Worker trotzdem "review" setzt (alte SOUL/Session),
        # korrigiert das Backend automatisch zu "done".
        if (updates["status"] == "review"
                and task.parent_task_id is not None
                and not agent.is_board_lead):
            updates["status"] = "done"
            logger.info(
                "Subtask auto-correct: review → done fuer '%s' (Worker soll done setzen)",
                task.title[:40],
            )

        await _enforce_board_rules_agent(session, board_id, task, updates["status"], agent)

        # ── Blocker-Approval Guard (PRE-COMMIT) ─────────────────
        # Muss VOR setattr/commit stehen, sonst driftet DB-Zustand
        # von API-Response auseinander.
        new_status_check = updates["status"]
        if new_status_check == "in_progress" and task.status == "blocked":
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

    # Soft-Validation: Warning wenn description kein Markdown-Signal enthält
    _desc_update = updates.get("description")
    if _desc_update and len(_desc_update) > 100:
        has_markdown = any(signal in _desc_update for signal in ("##", "**", "\n\n", "- ", "1. "))
        if not has_markdown:
            logger.warning(
                "description ohne Markdown-Signale (agent=%s, task_id=%s, len=%d)",
                agent.id, task_id, len(_desc_update),
            )

    # ── Blocked-by-Task-ID Validierung (Approval-Bypass-Schutz) ───────
    # Ohne diesen Guard koennte jeder Agent mit tasks:write eine beliebige UUID
    # in blocked_by_task_id setzen und damit den Operator-Approval-Pfad umgehen.
    # Akzeptiert wird die Referenz nur wenn:
    #   - Subtask existiert
    #   - auf demselben Board
    #   - entweder parent_task_id == self.id (echte Hierarchie)
    #   - oder callback_agent_id == agent.id (der Agent hat selbst delegiert)
    if updates.get("blocked_by_task_id") is not None:
        _blocked_by = updates["blocked_by_task_id"]
        _sub = await session.get(Task, _blocked_by)
        if _sub is None:
            raise HTTPException(
                status_code=422,
                detail="blocked_by_task_id: referenzierter Subtask existiert nicht.",
            )
        if _sub.board_id != board_id:
            raise HTTPException(
                status_code=422,
                detail="blocked_by_task_id: Subtask gehoert nicht zu diesem Board.",
            )
        _is_own_subtask = (
            _sub.parent_task_id == task.id
            or _sub.callback_agent_id == agent.id
        )
        if not _is_own_subtask:
            raise HTTPException(
                status_code=422,
                detail=(
                    "blocked_by_task_id: kein passender Subtask — "
                    "nutze `mc delegate` fuer atomare Delegation mit Callback."
                ),
            )

    # ── Blocker-Pflichtfeld-Validierung ───────────────────────────────
    _BLOCKER_ONLY_FIELDS = {"blocker_type", "blocker_description", "blocker_question"}
    if updates.get("status") == "blocked":
        # Callback-Wait-Fall: wenn blocked_by_task_id gesetzt ist (bereits oben
        # validiert) wartet der Agent auf einen Subtask — keine Operator-Approval
        # noetig, daher auch keine blocker_type/question-Pflicht.
        _is_callback_wait = (
            updates.get("blocked_by_task_id") is not None
            or task.blocked_by_task_id is not None
        )
        if not _is_callback_wait:
            if not updates.get("blocker_type"):
                raise HTTPException(status_code=422, detail="blocker_type ist Pflichtfeld bei status=blocked")
            if updates["blocker_type"] not in VALID_BLOCKER_TYPES:
                raise HTTPException(
                    status_code=422,
                    detail=f"blocker_type ungueltig. Erlaubt: {', '.join(sorted(VALID_BLOCKER_TYPES))}",
                )
            if not updates.get("blocker_question"):
                raise HTTPException(status_code=422, detail="blocker_question ist Pflichtfeld bei status=blocked")
            # Felder auf Maxlaenge trimmen
            if updates.get("blocker_description"):
                updates["blocker_description"] = updates["blocker_description"][:300]
            updates["blocker_question"] = updates["blocker_question"][:150]

    # ── Report-Back Hard-Gate bei status=done ─────────────────────────
    # Gilt nur fuer Telegram-Kanal Root-Tasks. Discord-Delivery + Subtasks bypassed.
    # Gilt NUR fuer agent-scoped PATCH (dieser Handler), nicht fuer User-Auth.
    _telegram_delivery = (task.report_back_channel or "telegram") == "telegram"
    if (
        updates.get("status") == "done"
        and task.report_back_required
        and task.parent_task_id is None
        and _telegram_delivery
        and not task.report_sent_to_telegram
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Task hat report_back_required=true (telegram) aber es wurde noch kein "
                "Report via `mc telegram` gesendet. Schicke zuerst eine Zusammenfassung "
                "an den Reports-Chat des Operators, dann `mc done`. "
                "(Format-Template siehe deine SOUL.md unter 'Telegram-Reports an den Operator'.)"
            ),
        )

    # ── Report-Back Auto-Draft bei status=failed ──────────────────────
    # Nur fuer Telegram-Root-Tasks. Atomic Flag-Claim verhindert Double-Send bei
    # parallelen PATCH-Requests (C4 Race-Fix).
    if (
        updates.get("status") == "failed"
        and task.report_back_required
        and task.parent_task_id is None
        and _telegram_delivery
        and not task.report_sent_to_telegram
    ):
        from sqlalchemy import update as _sa_update
        # Atomic Claim: nur ein Request bekommt rowcount=1, andere sehen Flag=true
        _claim = await session.exec(
            _sa_update(Task)
            .where(Task.id == task.id, Task.report_sent_to_telegram == False)  # noqa: E712
            .values(report_sent_to_telegram=True)
        )
        await session.commit()
        _claimed = _claim.rowcount == 1

        if _claimed:
            from app.services.report_auto_draft import render_and_send_failure_draft
            try:
                sent = await render_and_send_failure_draft(session, task, agent)
                if sent:
                    logger.info(
                        "Auto-Draft-Report gesendet fuer failed Task '%s' (Agent %s)",
                        task.title, agent.name,
                    )
                else:
                    # Send fehlgeschlagen (z.B. Bot unconfigured) — Flag zuruecknehmen
                    # damit nachfolgende manueller `mc telegram` Call noch das Flag setzen kann
                    await session.exec(
                        _sa_update(Task)
                        .where(Task.id == task.id)
                        .values(report_sent_to_telegram=False)
                    )
                    await session.commit()
            except Exception as e:
                # Auto-Draft darf den Status-Wechsel nicht blockieren.
                logger.warning(
                    "Auto-Draft-Send fehlgeschlagen fuer Task %s: %s — failed-Transition trotzdem zugelassen",
                    task.id, e,
                )
                # Bei Exception: Flag zuruecknehmen
                try:
                    await session.exec(
                        _sa_update(Task)
                        .where(Task.id == task.id)
                        .values(report_sent_to_telegram=False)
                    )
                    await session.commit()
                except Exception:
                    pass  # Best-effort, nicht kritisch
        # Nach dem commit oben ist die Session wieder frisch; Task muss erneut geladen
        # werden fuer nachfolgende setattr/update Aktionen.
        await session.refresh(task)

    # Blocker-spezifische Felder nicht auf das Task-Model setzen (nur fuer Approval-Payload)
    for k, v in updates.items():
        if k not in _BLOCKER_ONLY_FIELDS:
            setattr(task, k, v)

    if "status" in updates:
        new_status = updates["status"]
        if new_status == "in_progress":
            if old_status != "in_progress":
                # F2 fix (Plan 26-03): first-set-wins. Re-opens
                # (review→in_progress, blocked→in_progress) preserve the
                # original "work began" timestamp for Cycle Time analytics.
                if task.started_at is None:
                    task.started_at = utcnow()
            # ACK setzen — auch wenn Task schon in_progress war (z.B. von
            # next-task oder dispatch gesetzt bevor Agent ACKen konnte).
            # ack_at = "Agent hat den Task explizit bestaetigt".
            if task.ack_at is None:
                task.ack_at = utcnow()
            # Active-Task-Lock mitsetzen. Pull-Dispatch macht das ebenfalls
            # (agent_scoped.py:1294). Ohne diesen Schritt bleibt Boss bei Push-
            # Dispatch mit agent.current_task_id=None — `mc delegate`, `mc help`
            # etc. antworten dann 409 "Kein aktiver Task" obwohl der Agent
            # gerade an genau diesem Task arbeitet.
            # Live-Bug 2026-04-24 DGX-Spark Task: Boss musste direct POST /tasks
            # als Workaround nutzen. Skip-Bedingung identisch zu Pull-Dispatch:
            # im Subagent-Dispatch-Modus haben Workers parallele Sessions, der
            # Lock bleibt bei Board Leads.
            from app.config import settings as _ack_settings
            if not (_ack_settings.use_subagent_dispatch and not agent.is_board_lead):
                if agent.current_task_id != task.id:
                    agent.current_task_id = task.id
        elif new_status in ("review", "done") and task.ack_at is None:
            # Retroaktiver ACK: Agent hat Task bearbeitet ohne explizit zu ACKen.
            # Besser spaet als nie — Analytics brauchen den Timestamp.
            task.ack_at = utcnow()
            if task.started_at is None:
                task.started_at = task.ack_at
        if new_status == "done" and old_status != "done":
            task.completed_at = utcnow()
            agent.total_tasks_completed += 1
            # Lock freigeben bei Terminal-Status (wie bei failed in
            # task_lifecycle.apply_terminal_unassign)
            if agent.current_task_id == task.id:
                agent.current_task_id = None

    # Task-Event loggen (Event Sourcing)
    if "status" in updates:
        from app.services.task_lifecycle import record_task_event
        await record_task_event(
            session, task.id, old_status, updates["status"],
            changed_by="agent", agent_id=agent.id,
            reason="review_safeguard_correction" if updates.get("status") != payload.status else None,
        )

    task.updated_at = utcnow()
    agent.last_task_activity_at = utcnow()
    session.add(task)
    session.add(agent)
    await session.commit()
    await session.refresh(task)

    # Phase Approval Workflow: subtask → done triggers live-stream comment on parent
    # Guards:
    #   - old_status != "done": idempotent re-PATCH (done → done) darf keinen
    #     doppelten Kommentar posten.
    #   - try/except: Hook ist Best-Effort (Live-Stream-Kommentar). Fehler
    #     (Redis-Hiccup, Integrity-Error) duerfen die PATCH-Response nicht auf 500
    #     kippen — sonst retried der Agent und triggert trotz Idempotenz-Guard
    #     einen zweiten Durchlauf (Race window minimal, aber moeglich).
    new_status_for_hook = updates.get("status")
    if (
        new_status_for_hook == "done"
        and old_status != "done"
        and task.parent_task_id is not None
    ):
        try:
            # Lazy import: agent_comments → agent_task_status would create
            # a horizontal router-router cycle if hoisted to module top.
            from app.routers.agent_comments import _post_subtask_completion_comment
            await _post_subtask_completion_comment(session, task, agent)
        except Exception as e:
            logger.warning(
                "Subtask completion comment failed for task %s: %s",
                task.id, e,
            )

    # ── Subtask blocked → Parent-Notify (Bug 2026-04-23) ──────────────
    # Wenn ein Worker einen Subtask auf blocked setzt, muss der Parent-Owner
    # (Boss/Orchestrator) sofort einen sichtbaren Hinweis bekommen — sonst
    # bleibt der Parent stuck weil keine Aktion getriggert wird.
    # Best-Effort: Fehler hier (z.B. Redis-Hiccup) duerfen die PATCH-Response
    # nicht auf 500 kippen, sonst retried der Agent.
    # Idempotenz: old_status != "blocked" verhindert doppelte Comments bei
    # Re-PATCH (z.B. blocked → blocked durch Replay).
    if (
        new_status_for_hook == "blocked"
        and old_status != "blocked"
        and task.parent_task_id is not None
    ):
        try:
            from app.routers.agent_comments import _post_subtask_blocker_comment
            await _post_subtask_blocker_comment(
                session,
                task,
                agent,
                blocker_type=updates.get("blocker_type"),
                blocker_question=updates.get("blocker_question"),
                blocker_description=updates.get("blocker_description"),
            )
        except Exception as e:
            logger.warning(
                "Subtask blocker comment failed for task %s: %s",
                task.id, e,
            )

    # Active-Task Tracking: current_task_id auf Agent setzen/loeschen
    if "status" in updates and task.assigned_agent_id:
        from app.services.task_lifecycle import update_agent_active_task
        await update_agent_active_task(
            session, task.assigned_agent_id, task, updates["status"], old_status,
        )

    # Auto-Unassign bei failed/blocked → verhindert Cancel-Schleife im
    # agent_poll. Callback-Wait (blocked + blocked_by_task_id) wird vom
    # Helper ausgelassen, daher safe nach delegate/help_request.
    if "status" in updates:
        from app.services.task_lifecycle import apply_terminal_unassign
        if await apply_terminal_unassign(session, task, updates["status"]):
            await session.commit()
            await session.refresh(task)

    # ── Approval Cleanup: Obsolete Approvals superseden ────
    if "status" in updates:
        from app.services.approval_cleanup import cleanup_obsolete_approvals
        await cleanup_obsolete_approvals(session, task.id, updates["status"], board_id)

    # Vertical-Hooks (z.B. News-Studio Pipeline-Stage auto-advance) — no-op
    # wenn kein Vertical registriert ist (gestrippter Public-Release).
    if updates.get("status") == "done" and task.pipeline_id:
        from app.verticals import hooks as vertical_hooks
        await vertical_hooks.run_task_done_hooks(session, task)
        await session.commit()

    # Auto-Trigger: Abhaengige Tasks dispatchen wenn deren Dependencies jetzt erfuellt
    if updates.get("status") == "done":
        from app.services.dispatch import dependencies_met, auto_dispatch_task
        dep_result = await session.exec(
            select(TaskDependency).where(TaskDependency.depends_on_task_id == task.id)
        )
        for dep in dep_result.all():
            dependent_task = await session.get(Task, dep.task_id)
            if (dependent_task
                    and dependent_task.status == "inbox"
                    and not dependent_task.dispatched_at
                    and await dependencies_met(session, dependent_task)):
                import asyncio as _aio
                _aio.create_task(auto_dispatch_task(dependent_task.id, dependent_task.board_id))
                logger.info("Auto-trigger: '%s' dispatched (dependency '%s' done)",
                            dependent_task.title, task.title)

    # ── Help Request Auto-Resume ────────────────────────────
    if updates.get("status") in ("done", "failed") and task.help_request_from:
        await _handle_help_request_resume(session, task)

    # ── Boss Callback Auto-Resume (non-help-request) ────────
    # Parent-Tasks die via blocked_by_task_id auf dieses Subtask warten werden
    # auf in_progress zurueckgesetzt (Boss-Callback-Pattern fuer Research-Waits)
    if updates.get("status") in ("done", "failed") and not task.help_request_from:
        await _handle_callback_resume(session, task)

    # ── Phase-Completion Push ───────────────────────────────
    # Sobald ein Subtask done/failed ist und alle Geschwister ebenfalls fertig
    # sind, wird der Phase-Approval-Task sofort erzeugt (statt erst 30s später
    # via Watchdog-Sweep). Der Watchdog bleibt als Safety-Net aktiv.
    if updates.get("status") in ("done", "failed") and task.parent_task_id:
        await _handle_phase_completion_push(session, task)

    # ── Ephemeral Agent Cleanup (Phase 2, 2026-04-11) ────────
    # Wenn der transitionierende Agent 'ephemeral' im skills-Tag hat UND
    # einen Root-Task auf done/failed gesetzt hat → Agent nach Delay loeschen.
    # Nutzt create_tracked_task fuer sauberes shutdown (I6 aus Review).
    if (
        updates.get("status") in ("done", "failed")
        and task.parent_task_id is None
        and agent.skills and "ephemeral" in agent.skills
    ):
        import asyncio as _aio
        from app.utils import create_tracked_task

        async def _ephemeral_delete(agent_id_to_delete: uuid.UUID, trigger_task_id: uuid.UUID):
            # Kurze Delay damit Status-Change Events sauber rausgehen
            await _aio.sleep(5)
            from sqlmodel.ext.asyncio.session import AsyncSession as _AS
            from app.database import engine as _engine
            async with _AS(_engine, expire_on_commit=False) as _s:
                a = await _s.get(Agent, agent_id_to_delete)
                if not a:
                    return
                # Check: keine offenen Tasks mehr
                open_tasks = await _s.exec(
                    select(Task).where(
                        Task.assigned_agent_id == agent_id_to_delete,
                        Task.status.in_(["inbox", "in_progress", "review", "blocked"]),  # type: ignore[union-attr]
                    )
                )
                if open_tasks.first():
                    logger.info("Ephemeral cleanup skipped fuer %s — noch offene Tasks", a.name)
                    return
                _name = a.name
                await _s.delete(a)
                await _s.commit()
                logger.info("Ephemeral agent %s (%s) nach Task-Done geloescht", _name, agent_id_to_delete)
                await emit_event(
                    _s,
                    event_type="agent.ephemeral_deleted",
                    title=f"Ephemeral Agent '{_name}' automatisch geloescht",
                    severity="info",
                    detail={"agent_id": str(agent_id_to_delete), "trigger_task_id": str(trigger_task_id)},
                )

        create_tracked_task(
            _ephemeral_delete(agent.id, task.id),
            name=f"ephemeral-cleanup-{agent.id}",
        )

    # ── Phase Auto-Advance bei done (nach Review + Test Gate) ────
    # Wenn ein Root-Task (Phase) auf done geht UND ein Projekt hat,
    # naechste Phase automatisch starten.
    if (updates.get("status") == "done"
            and task.parent_task_id is None
            and task.project_id):
        from app.services.watchdog.task_monitor import TaskMonitorMixin
        _monitor = TaskMonitorMixin()
        await _monitor._auto_advance_next_phase(session, task)

    # ── Port freigeben bei done/failed ──────────────────────────
    if updates.get("status") in ("done", "failed") and task.workspace_port:
        task.workspace_port = None

    # ── Worktree Cleanup bei done/failed (Bundle 4 — REF-02: agent_git.py) ──
    if updates.get("status") in ("done", "failed"):
        await handle_worktree_cleanup(session, task, agent, updates["status"])

    await emit_event(
        session, "task.status_changed",
        f"Agent {agent.name} updated task: {task.title}",
        board_id=board_id, task_id=task.id, agent_id=agent.id,
        detail={"old_status": old_status, "new_status": task.status},
    )

    # report_back.delivered Event wenn Task done + Report-Flag war gesetzt (Happy-Path)
    if (
        updates.get("status") == "done"
        and task.report_sent_to_telegram
        and task.report_back_required
        and task.parent_task_id is None
    ):
        await emit_event(
            session, "report_back.delivered",
            f"Report-back fuer '{task.title}' geliefert (Agent {agent.name}, via `mc telegram`)",
            board_id=board_id, task_id=task.id, agent_id=agent.id,
        )

    # Auto-Memory + Feedback-Lessons (via TaskLifecycleService)
    if updates.get("status") and task.board_id:
        from app.services.task_lifecycle import trigger_auto_memory, trigger_feedback_lesson
        trigger_auto_memory(task, updates["status"], old_status)
        await trigger_feedback_lesson(session, task, updates["status"], old_status)

    # ── Auto Review-Handoff ──────────────────────────────────────────────
    if "status" in updates:
        new_status = updates["status"]

        # ── Evidence-Guard: Mind. 1 progress/resolution Kommentar vor Review ──
        if new_status == "review" and old_status == "in_progress":
            evidence_result = await session.exec(
                select(TaskComment).where(
                    TaskComment.task_id == task.id,
                    TaskComment.author_agent_id == agent.id,
                    TaskComment.comment_type.in_(["progress", "resolution", "checkpoint"]),  # type: ignore[union-attr]
                )
            )
            evidence_comments = evidence_result.all()
            if not evidence_comments:
                raise HTTPException(
                    status_code=409,
                    detail="Evidence erforderlich vor Review: Mindestens 1 progress/resolution/checkpoint Kommentar noetig. "
                           "Bitte dokumentiere was getan wurde bevor du auf Review setzt.",
                )

            # ── Visual-Proof Evidence-Haertung (Phase 5B) ──
            if task.delegation_type == "visual_proof":
                from app.services.visual_proof import validate_visual_proof_evidence
                vp_valid, vp_issues = validate_visual_proof_evidence(
                    evidence_comments,
                    expected_content=getattr(task, "expected_content", None),
                    target_url=getattr(task, "target_url", None),
                )
                if not vp_valid:
                    raise HTTPException(
                        status_code=409,
                        detail="Visual-Proof Evidence unzureichend: "
                               + "; ".join(vp_issues[:3])
                               + " — Bitte Screenshot mit MEDIA:-Pfad als Kommentar posten.",
                    )

        # Developer → review: Git push + PR erstellen (REF-02: agent_git.py)
        # CRITICAL call order: PR creation MUST happen BEFORE handle_review_handoff
        # (Pitfall H: marker comment written before reviewer is notified).
        if new_status == "review" and old_status == "in_progress":
            await handle_review_pr_creation(session, task, agent)
            from app.services.task_lifecycle import handle_review_handoff
            await handle_review_handoff(session, task, board_id, developer=agent)

        # PR mergen bei echtem Abschluss (nach Review/Test Gate — REF-02: agent_git.py)
        if new_status == "done" and old_status in ("review", "user_test"):
            await handle_done_pr_merge(session, task, agent)

        # Reviewer rejects → zurueck an Original-Developer (via TaskLifecycleService)
        # Auch done→in_progress abfangen (Rex setzt manchmal versehentlich done statt in_progress)
        # NICHT wenn der zugewiesene Reviewer selbst ACKt (review → in_progress = "Ich starte Review")
        if new_status == "in_progress" and old_status in ("review", "done", "user_test"):
            is_reviewer_ack = (
                old_status == "review"
                and task.assigned_agent_id == agent.id
                and agent.role == "reviewer"
            )
            if not is_reviewer_ack:
                from app.services.task_lifecycle import handle_review_rejection
                await handle_review_rejection(session, task, board_id, rejecting_agent=agent)

        # User Test: den Operator via Telegram benachrichtigen (Phase 29: direct HTTPS path)
        if new_status == "user_test":
            from app.services.telegram_bot import telegram_bot
            from app.config import phone_test_url
            tailscale_url = phone_test_url()
            await telegram_bot.send_message(
                f"<b>Bereit zum Testen: {task.title}</b>\n\n"
                f"Bitte auf dem Handy testen:\n{tailscale_url}\n\n"
                f"Task-ID: {task.id}"
            )

        # Agent blocked → Approval fuer den Operator erstellen + Lead informieren (ohne Handlungsoptionen)
        if new_status == "blocked":
            from datetime import timedelta
            from app.services.dispatch import find_agent_by_role
            from app.scopes import AgentRole

            blocker_cmt = (await session.exec(
                select(TaskComment)
                .where(TaskComment.task_id == task.id, TaskComment.author_agent_id == agent.id)
                .order_by(TaskComment.created_at.desc())
                .limit(1)
            )).first()
            blocker_text = blocker_cmt.content[:2000] if blocker_cmt else "Kein Blocker-Kommentar"

            # Guard 1 (Primary): blocked_by_task_id gesetzt (via mc delegate --callback)
            #   → Callback-Wait, keine Operator-Decision, KEINE Approval.
            # Guard 2 (Fallback): Agent hat mindestens einen Child-Subtask (parent_task_id=task.id)
            #   mit callback_agent_id=agent.id in non-terminal Status (inbox/in_progress/review/blocked).
            #   Das passiert wenn Agent NICHT `mc delegate` nutzt sondern raw curl — blocked_by_task_id
            #   bleibt NULL aber trotzdem ist das orchestration-wait, kein Operator-Decision-Wait.
            #   Incident-Context 2026-04-23: Boss ohne mc CLI auf Host machte manual curl POST
            #   /tasks + PATCH status=blocked. Ohne diesen Guard entstand blocker_decision-Approval
            #   → Operator-Inbox-Spam + Watchdog-Callback-Fallback wurde durch pending-approval blockiert
            #   → Parent ewig stuck. Dieser Guard ist der strukturelle Fix.
            _is_orchestration_wait = task.blocked_by_task_id is not None
            _fallback_reason = None
            if not _is_orchestration_wait:
                # Suche nach aktiven Child-Subtasks mit callback auf den blockenden Agent
                active_child = (await session.exec(
                    select(Task)
                    .where(
                        Task.parent_task_id == task.id,
                        Task.callback_agent_id == agent.id,
                        Task.status.in_(("inbox", "in_progress", "review", "blocked")),  # type: ignore[union-attr]
                    )
                    .limit(1)
                )).first()
                if active_child is not None:
                    _is_orchestration_wait = True
                    _fallback_reason = f"active child-subtask {active_child.id} with callback"

            if _is_orchestration_wait:
                blocked_by = task.blocked_by_task_id or (active_child.id if _fallback_reason else None)
                logger.info(
                    "Blocked-on-subtask ohne Approval: task %s wartet auf %s (%s)",
                    task.id, blocked_by,
                    "blocked_by_task_id" if task.blocked_by_task_id else _fallback_reason,
                )
                await emit_event(
                    session,
                    event_type="task.blocked_on_subtask",
                    title=f"{agent.name} wartet auf Subtask (kein Operator-Input noetig)",
                    severity="info",
                    board_id=board_id,
                    task_id=task.id,
                    agent_id=agent.id,
                    detail={
                        "blocked_by_task_id": str(blocked_by) if blocked_by else None,
                        "callback_detection": "explicit" if task.blocked_by_task_id else "fallback_via_parent_link",
                    },
                )
                # Skip Approval-Erstellung + Lead-Notification — reine Orchestration.
                # Callback-Auto-Resume greift wenn Subtask done geht (siehe _handle_callback_resume).
            else:
                blocker_type = payload.blocker_type  # bereits validiert oben
                blocker_description = (payload.blocker_description or "")[:1000]
                blocker_question = payload.blocker_question[:1000]

                # Projekt-Name laden
                project_name = None
                if task.project_id:
                    from app.models.board import Project
                    _project = await session.get(Project, task.project_id)
                    project_name = _project.name if _project else None

                # 1. Approval fuer den Operator erstellen
                approval = Approval(
                    board_id=board_id,
                    task_id=task.id,
                    agent_id=agent.id,
                    action_type="blocker_decision",
                    description=f"{agent.name} ist blockiert bei \"{task.title}\"",
                    payload={
                        "blocked_agent_id": str(agent.id),
                        "blocked_agent_name": agent.name,
                        "task_title": task.title,
                        "project_name": project_name,
                        "blocker_type": blocker_type,
                        "description": blocker_description,
                        "question": blocker_question,
                        "blocker_comment": blocker_text,  # Freitext-Fallback
                    },
                    expires_at=utcnow() + timedelta(hours=24),
                )
                session.add(approval)
                await session.commit()

                # 2. Telegram Approval Buttons
                try:
                    from app.services.telegram_bot import telegram_bot
                    await telegram_bot.send_approval_telegram(
                        approval.id, agent.name, task.title, blocker_text,
                    )
                except Exception as e:
                    logger.warning("Telegram approval failed: %s", e)

                # 3. Activity Event (severity=warning → Inbox-Badge)
                await emit_event(
                    session,
                    "approval.created",
                    f"Blocker-Approval: {agent.name} blockiert bei \"{task.title}\"",
                    board_id=board_id,
                    task_id=task.id,
                    agent_id=agent.id,
                    severity="warning",
                )

                # 4. Lead informieren ueber TaskComment (Phase 29 — Gateway entfernt).
                # poll.sh des Lead picks die Nachricht auf den naechsten Tick.
                lead = await find_agent_by_role(session, board_id, AgentRole.LEAD)
                if lead and lead.id != agent.id:
                    msg = (
                        f"BLOCKER: {agent.name} bei \"{task.title}\"\n\n"
                        f"{blocker_text}\n\n"
                        f"Task-ID: {task.id}\n\n"
                        f"Ein Approval wurde fuer den Operator erstellt. Der Operator entscheidet ueber die Loesung.\n"
                        f"Du kannst hilfreiche Infos als Kommentar posten."
                    )
                    session.add(TaskComment(
                        task_id=task.id,
                        author_type="system",
                        content=msg,
                        comment_type="blocker_lead_notify",
                    ))
                    await session.commit()

        # Agent entblockt Task → assigned Agent benachrichtigen (TaskComment)
        if new_status == "in_progress" and old_status == "blocked":
            if task.assigned_agent_id and task.assigned_agent_id != agent.id:
                target = await session.get(Agent, task.assigned_agent_id)
                if target:
                    hint_cmt = (await session.exec(
                        select(TaskComment)
                        .where(TaskComment.task_id == task.id)
                        .order_by(TaskComment.created_at.desc())
                        .limit(1)
                    )).first()
                    hint_text = hint_cmt.content[:500] if hint_cmt else ""
                    msg = (
                        f"UNBLOCKED: Dein Task \"{task.title}\" wurde von {agent.name} entblockt.\n\n"
                        f"{hint_text}\n\n"
                        f"Task-ID: {task.id}\n\n"
                        f"**Aktion:** Lies deinen letzten Checkpoint-Kommentar "
                        f"(GET /api/v1/agent/boards/{board_id}/tasks/{task.id}/comments) "
                        f"und arbeite sofort an diesem Task weiter."
                    )
                    session.add(TaskComment(
                        task_id=task.id,
                        author_type="system",
                        content=msg,
                        comment_type="unblock_notify",
                    ))
                    await session.commit()

    return task


@router.get("/boards/{board_id}/tasks/{task_id}/events")
async def agent_get_task_events(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    limit: int = Query(20, le=100),
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    """Task-Event-History fuer Agents — zeigt Statusaenderungen chronologisch."""
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
    return [e.model_dump() for e in result.all()]


@router.patch("/boards/{board_id}/tasks/{task_id}/report-back")
async def agent_report_back(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: ReportBackUpdate,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_WRITE)),
):
    """Agent meldet: report_back an den Operator wurde geliefert.

    Lifecycle: none → pending (automatisch bei Completion) → sent (Agent) → delivered (Operator bestätigt)
    Fallback: pending → fallback_sent (System, 10min Timer) → failed
    """
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    # Nur Board Leads oder der Owner duerfen report_back setzen
    if not agent.is_board_lead and task.owner_agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Nur Board Lead oder Task-Owner darf report_back setzen")

    valid_transitions = {
        "pending": {"sent", "failed"},
        "sent": {"delivered"},
    }
    current = task.report_back_status or "none"
    allowed = valid_transitions.get(current, set())
    if payload.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Ungültiger report_back Übergang: {current} → {payload.status}",
        )

    task.report_back_status = payload.status
    task.updated_at = utcnow()
    session.add(task)
    await session.commit()

    await emit_event(
        session, f"report_back.{payload.status}",
        f"Report-back für '{task.title}' auf '{payload.status}' gesetzt von {agent.name}",
        board_id=board_id, task_id=task.id, agent_id=agent.id,
    )

    logger.info("Report-back %s → %s für Task '%s' von %s", current, payload.status, task.title, agent.name)
    return {"status": payload.status, "task_id": str(task.id)}


@router.post("/boards/{board_id}/tasks/{task_id}/review")
async def agent_review_decision(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    body: ReviewDecisionBody,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_WRITE)),
):
    """Explizite Review-Entscheidung: approve, request_changes, oder hold."""
    if agent.board_id != board_id:
        raise HTTPException(status_code=403, detail="Agent not assigned to this board")

    task = await session.get(Task, task_id)
    if not task or task.board_id != board_id:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.services.task_lifecycle import execute_review_decision
    await execute_review_decision(
        session, task, board_id, body.decision, body.comment,
        actor_agent=agent,
    )
    return {"status": "ok", "decision": body.decision}


# ── Checkpoint Endpoints ───────────────────────────────────────────────────


@router.post(
    "/boards/{board_id}/tasks/{task_id}/checkpoint",
    status_code=status.HTTP_410_GONE,
    deprecated=True,
)
async def agent_save_checkpoint(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
):
    """Deprecated — use `mc checklist` + `mc comment progress`.

    Workstream A4 consolidated progress tracking around TaskChecklistItem.
    The checkpoint write path is gone; the GET endpoint below still serves
    historical reads for audit. Migration 0082 moved all prior checkpoint-
    typed comments to `progress`. Route stays registered for 2 releases so
    legacy Sparky SOUL snippets get a clear error instead of 404.
    """
    raise HTTPException(
        status_code=410,
        detail=(
            "POST /checkpoint is gone. Use `mc checklist add/done` for "
            "progress tracking and `mc comment progress \"...\"` for notes."
        ),
    )


@router.get("/boards/{board_id}/tasks/{task_id}/checkpoint")
async def agent_get_latest_checkpoint(
    board_id: uuid.UUID,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.TASKS_READ)),
):
    """Letzten Checkpoint fuer einen Task lesen."""
    from app.models.checkpoint import TaskCheckpoint

    result = await session.exec(
        select(TaskCheckpoint)
        .where(TaskCheckpoint.task_id == task_id)
        .order_by(TaskCheckpoint.created_at.desc())  # type: ignore[union-attr]
        .limit(1)
    )
    checkpoint = result.first()
    if not checkpoint:
        raise HTTPException(status_code=404, detail="Kein Checkpoint fuer diesen Task")

    return {
        "id": str(checkpoint.id),
        "task_id": str(checkpoint.task_id),
        "agent_id": str(checkpoint.agent_id),
        "checkpoint_type": checkpoint.checkpoint_type,
        "state_summary": checkpoint.state_summary,
        "context_data": checkpoint.context_data,
        "created_at": str(checkpoint.created_at),
    }
