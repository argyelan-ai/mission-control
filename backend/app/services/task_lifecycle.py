"""
TaskLifecycleService — Zentrale Stelle fuer alle Status-Transition Side-Effects.

Eliminiert die Duplikation zwischen agent_scoped.py und tasks.py bei:
- Review-Handoff (in_progress → review)
- Review-Rejection (review → in_progress)
- Review-Decision (approve / request_changes / hold)
- Task-Completion/Failure Auto-Memory
- Feedback-Lesson Capture

Beide Router delegieren an diesen Service statt die Logik selbst zu implementieren.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Literal

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
    """Task-Status-Event loggen (Event Sourcing).

    Wird bei JEDER Statusaenderung aufgerufen — egal ob User, Agent, Watchdog oder System.
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
    # Kein separater commit — Caller committed zusammen mit dem Status-Update


async def reopen_parent_for_new_subtask(
    session: AsyncSession,
    parent_task_id: uuid.UUID,
    new_subtask_title: str | None = None,
) -> bool:
    """Parent-Task automatisch auf in_progress zuruecksetzen wenn neuer Subtask
    erstellt wird waehrend Parent bereits auf `review` oder `done` wartet.

    Grund: Phase-Approval setzt Parent auf review sobald alle bisherigen Subtasks
    done sind. Wenn der Board Lead danach noch einen Follow-up-Subtask erstellt
    (z.B. "Neues Konzept basierend auf Research"), wuerde der Parent auf review
    bleiben — der Operator sieht einen review-Task, obwohl unten eine neue Subarbeit
    laeuft. Das ist ein Deadlock fuer den Task-Lifecycle.

    Verhalten:
      - parent.status == 'review'  -> zurueck auf in_progress + Event + True
      - parent.status == 'done'    -> KEIN auto-reopen (Caller muss 422 raisen)
      - parent.status == 'failed'  -> KEIN auto-reopen
      - sonst                      -> kein Eingriff, False

    Returns: True wenn Parent reopened wurde, sonst False.
    Kein Commit — Caller commiteert zusammen mit dem Subtask-Insert.
    """
    parent = await session.get(Task, parent_task_id)
    if parent is None or parent.status != "review":
        return False

    old_status = parent.status
    parent.status = "in_progress"
    parent.updated_at = utcnow()
    parent.completed_at = None  # falls bereits gesetzt, ruecksetzen
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
    """Spawn-Session-IDs loeschen.

    Lifecycle:
    - done/failed/blocked -> Session ist beendet, IDs irrelevant
    - inbox (requeue) -> alter Dispatch ungueltig

    Post Phase 29 / Gateway-Sunset: spawn_session_key + spawn_run_id are
    gateway-only artifacts (cli-bridge / host runtimes don't use them).
    They linger on legacy rows; clearing them is a no-op cleanup. The
    Gateway-Session deletion call (`sessions.delete`) is dropped — there
    is no gateway anymore.

    # TODO Phase 30: drop spawn_session_key + spawn_run_id columns from Task.

    NICHT enthalten: dispatch_attempt_id clear. Caller die das brauchen
    rufen `dispatch_attempt_audit.clear_dispatch_attempt_id` mit eigenem
    caller/reason auf, damit der Audit-Trail in task_attempt_audit den
    Aufruf-Kontext kennt (siehe doppelter-dispatch incident 2026-05-15).
    """
    task.spawn_run_id = None
    task.spawn_session_key = None


# ── Terminal Unassign ───────────────────────────────────────────────────
# Schutz gegen die Cancel-Schleife im agent_poll: wenn ein Task auf failed
# oder blocked geht und assigned_agent_id stehen bleibt, prueft agent_poll
# als ERSTES ob ein failed Task fuer den Agent existiert → liefert
# state="cancelled" → poll.sh sendet ESC → naechster Poll: gleiche
# Antwort. Endlos. Neue Tasks werden NIE delivered weil der failed Task
# immer Vorrang hat.
#
# Loesung: assigned_agent_id beim Uebergang nach failed/blocked auf NULL
# setzen. Wer den Task wieder freigeben will (der Operator via Approval, manuelle
# Re-Assign, planner) muss ohnehin neu zuweisen — das ist konsistent mit
# der Tatsache dass failed/blocked Tasks menschliche Intervention brauchen
# und kein Worker-Polling mehr.
#
# Ausnahme: blocked mit blocked_by_task_id (Callback-Wait via help_request,
# delegate). Der Parent-Agent muss assigned bleiben damit der Resume nach
# Subtask-done zum richtigen Agent zurueckrouten kann.

async def apply_terminal_unassign(
    session: AsyncSession,
    task: Task,
    new_status: str,
) -> bool:
    """assigned_agent_id beim Uebergang nach failed/blocked loeschen.

    Verhindert die stille Cancel-Schleife (siehe Modul-Doku oben). Wird
    von allen Pfaden aufgerufen die `task.status` auf failed/blocked
    setzen — User-PATCH, Worker-PATCH, Backend-Cleanup.

    Args:
        session: Aktive DB-Session (kein Commit hier — Caller commitet)
        task: Task mit BEREITS gesetztem neuem Status (oder vor dem Set;
              die Methode liest nur new_status fuer die Entscheidung)
        new_status: Der Ziel-Status nach dem Uebergang

    Returns:
        True wenn unassignt wurde, False wenn nichts geaendert wurde.

    Verhalten (nach Fix 2026-04-24):
        - new_status == "failed"   -> immer unassign (terminal)
        - new_status == "blocked"  -> NIE unassign (blocked ist IMMER temporaer —
          entweder Callback-Wait, mc blocked Clarification, oder manual stop.
          assigned_agent_id wird gebraucht damit der Worker beim Resume wieder
          den Task bekommt und Clarification-Resolution-Comments via poll
          delivered werden koennen.)
        - sonst -> kein Eingriff
        - assigned_agent_id bereits None -> kein Crash, returnt False

    Loescht zusaetzlich agent.current_task_id falls dieser Task referenziert
    wird (Lock freigeben). update_agent_active_task macht das auch, aber
    nur wenn old_status == "in_progress". Bei Pfaden wie inbox→failed oder
    review→failed greift das nicht — daher hier defensive in depth.

    Live-Lessons die zu dieser Logik fuehrten:
    - PR #107: stop_task_run ruft apply_terminal_unassign nicht mehr →
      Der Stop/Resume des Operators verliert nicht mehr den Agent.
    - PR #111 (hier): blocker_decision via mc blocked hat assigned_agent_id
      verloren → Resolution-Comment nicht mehr delivered → Worker orphaned,
      Task eskalierte zum Board-Lead als Workaround. Jetzt bleibt assignment
      bei blocked erhalten.
    """
    if new_status != "failed":
        # blocked ist immer temporaer — assigned_agent_id wird fuer Resume
        # und Resolution-Comment-Delivery gebraucht.
        if new_status == "blocked":
            # Lock-Strategie haengt vom Blocker-Typ ab:
            # - blocked_by_task_id gesetzt (Callback-Wait): Worker wartet AKTIV
            #   auf Subtask — current_task_id BEHALTEN (Worker macht nichts
            #   anderes parallel). Behavior wie vor PR #111.
            # - blocked_by_task_id None (mc blocked, Human-Wait): Worker ist
            #   effektiv idle waehrend der Operator antwortet — Lock freigeben damit
            #   Worker andere Tasks nehmen kann. Bei Resume kommt der Task
            #   via poll zurueck (assigned_agent_id intakt).
            if task.blocked_by_task_id is None and task.assigned_agent_id is not None:
                agent = await session.get(Agent, task.assigned_agent_id)
                if agent is not None and agent.current_task_id == task.id:
                    agent.current_task_id = None
                    if agent.run_state in ("running", None):
                        agent.run_state = "blocked"
                    session.add(agent)
            return False
        return False

    # Defensive: Watchdog/Cleanup hat den Task vielleicht schon unassignt
    if task.assigned_agent_id is None:
        return False

    # Agent-Lock freigeben falls dieser Task im current_task_id steht
    agent_id_to_clear = task.assigned_agent_id
    agent = await session.get(Agent, agent_id_to_clear)
    if agent is not None and agent.current_task_id == task.id:
        agent.current_task_id = None
        # run_state nur anpassen wenn er bisher diesen Task gespiegelt hat
        if agent.run_state in ("running", None):
            agent.run_state = "blocked" if new_status == "blocked" else "idle"
        session.add(agent)

    task.assigned_agent_id = None
    session.add(task)
    return True


# ── Active-Task Tracking ────────────────────────────────────────────────
# Ein Agent hat maximal einen aktiven Haupttask. current_task_id auf dem
# Agent-Objekt wird gesetzt/geloescht wenn ein Task in_progress wird oder
# diesen Zustand verlaesst. Dispatch prueft dieses Feld als Guard.
#
# HINWEIS: Bei use_subagent_dispatch=True haben Workers parallele Sessions.
# current_task_id kann nur EINEN Task tracken und ist fuer Workers daher
# nur ein Hinweis (kein Lock). Der Busy-Check im Dispatch wird uebersprungen.

async def update_agent_active_task(
    session: AsyncSession,
    agent_id: uuid.UUID,
    task: Task,
    new_status: str,
    old_status: str,
) -> None:
    """current_task_id auf dem Agent setzen/loeschen bei Status-Wechseln.

    Setzt current_task_id wenn:
    - Task wechselt zu in_progress UND Agent hat keinen aktiven Task
      (oder der aktive Task ist dieser hier)

    Loescht current_task_id wenn:
    - Task verlaesst in_progress (review, done, blocked, failed, inbox)
      UND current_task_id == task.id

    Bei use_subagent_dispatch: Workers ueberspringen current_task_id Tracking
    (parallele Sessions → Feld kann nur einen Task abbilden).
    """
    from app.config import settings

    # Spawn-Tracking loeschen wenn Task einen terminalen/inaktiven Zustand erreicht.
    # Bei in_progress bleiben die IDs erhalten (Session laeuft noch).
    # Bei Handoff/Rejection/Dispatch werden sie vom Caller ueberschrieben.
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

    # Workers mit isolierten Sessions: current_task_id nicht setzen/loeschen
    # (parallele Tasks → Feld waere sofort inkonsistent)
    # run_state trotzdem aktualisieren fuer UI-Anzeige.
    if settings.use_subagent_dispatch and not agent.is_board_lead:
        if new_status == "in_progress" and old_status != "in_progress":
            agent.run_state = "running"
            session.add(agent)
        elif old_status == "in_progress" and new_status != "in_progress":
            # run_state nur auf idle wenn kein anderer Task mehr in_progress ist
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
        # Task wird aktiv — Agent-Lock setzen
        if agent.current_task_id is None or agent.current_task_id == task.id:
            agent.current_task_id = task.id
            agent.run_state = "running"
            session.add(agent)
        else:
            # Agent hat schon einen anderen aktiven Task — loggen aber nicht blockieren
            # (der Busy-Check im Dispatch sollte das verhindern)
            logger.warning(
                "Agent %s hat bereits aktiven Task %s, neuer Task %s wird trotzdem in_progress",
                agent.name, agent.current_task_id, task.id,
            )
            agent.current_task_id = task.id
            agent.run_state = "running"
            session.add(agent)

    elif old_status == "in_progress" and new_status != "in_progress":
        # Task verlaesst in_progress — Agent-Lock freigeben
        if agent.current_task_id == task.id:
            agent.current_task_id = None
            # run_state basierend auf neuem Status
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
    """PR mergen wenn vorhanden (best effort). Extrahiert aus agent_scoped.py."""
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

        # Workspace finden (Reviewer oder Fallback)
        workspace = actor_agent.workspace_path if actor_agent else None
        if workspace:
            reviewer_dir = os.path.join(workspace, project_slug)
            if os.path.isdir(reviewer_dir):
                await git_service.merge_pr(reviewer_dir, pr_number)
                logger.info("PR #%d gemerged fuer Task '%s'", pr_number, task.title)
                return

        # Fallback: gh CLI global
        if project.github_repo_name:
            await git_service._run_cmd(
                "gh", "pr", "merge", str(pr_number),
                "--repo", project.github_repo_name,
                "--squash", "--delete-branch",
            )
            logger.info("PR #%d gemerged (global) fuer Task '%s'", pr_number, task.title)
    except Exception as e:
        logger.warning("PR-Merge fehlgeschlagen: %s", e)


async def execute_review_decision(
    session: AsyncSession,
    task: Task,
    board_id: uuid.UUID,
    decision: Literal["approve", "request_changes", "hold"],
    comment_text: str,
    actor_agent: Agent | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Einzige Wahrheit fuer Review-Entscheidungen.

    Macht alles atomar: Kommentar + Decision + Status-Transition + Events.
    Drei Ausgaenge: approve (→done), request_changes (→in_progress), hold (bleibt review).
    """
    # ── Guards ──────────────────────────────────────────────
    if task.status != "review":
        raise HTTPException(409, "Task ist nicht im Review")
    if task.run_control in ("stopped", "manual_hold"):
        raise HTTPException(409, f"Task run_control={task.run_control}")

    # Parent/Child Guard: Bei Approve pruefen ob Children alle done sind
    if decision == "approve":
        from app.task_status import check_children_complete
        children_ok, children_detail = await check_children_complete(task.id, session)
        if not children_ok:
            raise HTTPException(400, children_detail)

    # Self-Review Guard: Agent der am Task GEARBEITET hat darf nicht approven.
    # Reviewer-ACK (review → in_progress durch Reviewer) zaehlt NICHT als Arbeit.
    if decision == "approve" and actor_agent:
        from app.models.task import TaskEvent
        from app.models.agent import Agent as AgentModel
        events_result = await session.exec(
            select(TaskEvent).where(
                TaskEvent.task_id == task.id,
                TaskEvent.to_status.in_(["in_progress", "review"]),  # type: ignore[union-attr]
                TaskEvent.agent_id.isnot(None),  # type: ignore[union-attr]
            )
        )
        worker_agent_ids: set[uuid.UUID] = set()
        for event in events_result.all():
            if not event.agent_id:
                continue
            # Reviewer-Transitions ausfiltern: Review-Arbeit ist kein Implementierungsakt.
            # Reviewer-ACK (review → in_progress) und Review-Abschluss (in_progress → review)
            # durch einen Reviewer zaehlen NICHT als Developer-Arbeit.
            is_review_transition = (
                (event.from_status == "review" and event.to_status == "in_progress") or
                (event.from_status == "in_progress" and event.to_status == "review")
            )
            if is_review_transition:
                event_agent = await session.get(AgentModel, event.agent_id)
                if event_agent and event_agent.role == "reviewer":
                    continue  # Review-Arbeit, nicht als Worker zaehlen
            worker_agent_ids.add(event.agent_id)

        if actor_agent.id in worker_agent_ids:
            if not actor_agent.is_board_lead:
                # Self-Review blockiert → an Board Lead eskalieren statt hart blocken
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
                    return  # Return ohne approve — Board Lead muss entscheiden
                else:
                    raise HTTPException(
                        409,
                        f"Self-review not allowed: Agent '{actor_agent.name}' war als Bearbeiter beteiligt. "
                        f"Kein Board Lead fuer Eskalation verfuegbar.",
                    )

    # ── Konsistenz-Guard: ship-ready ↔ review_decision ────
    # "ship-ready" im Kommentar + request_changes/hold = Widerspruch
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

    # ── 1. Kommentar (immer, atomar mit Entscheidung) ──────
    comment = TaskComment(
        task_id=task.id,
        author_type="agent" if actor_agent else "user",
        author_agent_id=actor_agent.id if actor_agent else None,
        comment_type="review",
        content=comment_text,
    )
    session.add(comment)

    # ── 2. Decision-Felder setzen ──────────────────────────
    decision_map = {
        "approve": "approved",
        "request_changes": "changes_requested",
        "hold": "hold",
    }
    task.review_decision = decision_map[decision]
    task.review_decided_at = utcnow()

    # ── 3. Reviewer-Agent freigeben ──────────────────────────
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

    # ── 4. Status-Transition (decision-abhaengig) ───────────
    if decision == "approve":
        # Phase-Parents mit Subtasks → user_test Gate statt direkt done
        # Alles andere (Einzeltasks, Subtasks) → direkt done
        _has_children = False
        if task.parent_task_id is None:  # Root-Level
            _children_result = await session.exec(
                select(Task.id).where(Task.parent_task_id == task.id).limit(1)
            )
            _has_children = _children_result.first() is not None

        # user_test nur bei Browser-relevanten Phase-Tasks
        _needs_test = _has_children and (
            getattr(task, "needs_browser", None)
            or getattr(task, "delegation_type", None) == "visual_proof"
        )

        if _needs_test:
            task.status = "user_test"
            logger.info("Review approved → user_test Gate: '%s'", task.title[:40])
        else:
            # Report-Back Hard-Gate (analog zu PATCH-Handler in agent_scoped.py):
            # /review mit decision=approve darf keinen Root-Task mit offener
            # report_back_required-Pflicht schliessen. Reviewer muss Developer
            # bitten zuerst `mc telegram` zu senden, oder den Operator direkt fragen.
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

        # PR merge — nur bei echtem done, NICHT bei user_test (Test Gate vor Merge)
        if task.status == "done":
            await _merge_pr_if_exists(session, task, actor_agent)

        # Test-Handoff: Tester-Agent fuer user_test dispatchen (wenn vorhanden)
        if task.status == "user_test":
            try:
                tester = await handle_test_handoff(session, task, board_id)
                if tester:
                    logger.info("Test-Handoff: '%s' → %s", task.title[:40], tester.name)
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

        # Abhaengige Tasks dispatchen
        from app.models.task import TaskDependency
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
                from app.utils import create_tracked_task
                create_tracked_task(auto_dispatch_task(dependent_task.id, dependent_task.board_id))

        # ── Board Lead Completion-Callback ──────────────────────
        # Informiert Henry (Board Lead), damit er dem Operator antworten kann
        from app.utils import create_tracked_task
        create_tracked_task(
            _notify_lead_on_completion(session, task, board_id, actor_name)
        )

    elif decision == "request_changes":
        # Status vorlaeufig auf in_progress (Fallback wenn kein Developer gefunden)
        task.status = "in_progress"

        # handle_review_rejection ueberschreibt auf inbox + dispatched_at
        # wenn Rework-Dispatch erfolgreich → ACK-Check des Task-Runners greift
        await handle_review_rejection(
            session, task, board_id, rejecting_agent=actor_agent,
        )

    elif decision == "hold":
        # Kein Status-Wechsel, kein Dispatch. Task bleibt in review.
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
    """Review-Handoff: Task an Reviewer uebergeben + Push-Benachrichtigung.

    Shared zwischen agent_scoped.py (Agent setzt review) und tasks.py (User setzt review).
    Git/PR-Erstellung bleibt im jeweiligen Router (agent-spezifisch).

    Returns: Reviewer-Agent oder None.
    """
    from app.routers.agent_scoped import _find_reviewer

    # ── Dedupe: Wenn Task schon an einen Reviewer zugewiesen ist, kein zweiter Handoff
    if task.assigned_agent_id and task.dispatch_intent == "review_handoff":
        existing_reviewer = await session.get(Agent, task.assigned_agent_id)
        if existing_reviewer and existing_reviewer.role == "reviewer":
            logger.info("Review-Handoff dedupe: '%s' bereits bei %s", task.title, existing_reviewer.name)
            return existing_reviewer

    reviewer = await _find_reviewer(session, board_id)
    if not reviewer:
        return None
    if developer and reviewer.id == developer.id:
        return None  # Reviewer darf nicht gleicher Agent sein

    # dispatch_intent setzen + Operational Controls Guard
    task.dispatch_intent = "review_handoff"
    from app.services.operations import check_dispatch_allowed
    allowed, reason = await check_dispatch_allowed(task, reviewer, session)
    if not allowed:
        logger.info("Review-Handoff blocked: '%s' — %s", task.title, reason)
        return None

    # Developer-Lock freigeben (current_task_id loeschen)
    if developer and developer.current_task_id == task.id:
        developer.current_task_id = None
        session.add(developer)

    # Reviewer-Zuweisung committen (OHNE dispatched_at — das kommt erst nach RPC-Erfolg)
    task.assigned_agent_id = reviewer.id
    task.ack_at = None
    task.completed_at = None  # Reset falls aus vorherigem Zyklus gesetzt
    task.review_decision = None  # Neue Review-Runde startet sauber
    task.review_decided_at = None
    task.updated_at = utcnow()
    clear_spawn_tracking(task)  # Alte Spawn-Session-IDs loeschen
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

    # Push-Benachrichtigung an Reviewer
    # Post Phase 29 / Gateway-Sunset: kein gateway_agent_id-Gate, kein RPC.
    # Re-Dispatch via auto_dispatch_task → der Dispatcher waehlt die richtige
    # Runtime-Delivery (cli-bridge / host / claude-code) und der Reviewer
    # bekommt die Review-Message ueber sein poll.sh / launchd.
    # Wir setzen dispatched_at hier NICHT — auto_dispatch_task setzt es nach
    # erfolgreicher Auslieferung selbst.
    from app.services.dispatch import auto_dispatch_task
    task.dispatched_at = None
    task.ack_at = None
    session.add(task)
    await session.commit()
    asyncio.create_task(auto_dispatch_task(task.id, board_id))

    return reviewer


async def handle_test_handoff(
    session: AsyncSession,
    task: Task,
    board_id: uuid.UUID,
) -> Agent | None:
    """Test-Handoff: Task an Tester uebergeben bei user_test.

    Analog zu handle_review_handoff, aber fuer QA/User-Testing.
    Tester prueft via Browser ob das Ergebnis aus User-Sicht funktioniert.
    """
    from app.services.dispatch import find_agent_by_role

    tester = await find_agent_by_role(session, board_id, "tester")
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

    # Dispatch an Tester
    # Post Phase 29 / Gateway-Sunset: Re-Dispatch via auto_dispatch_task.
    # Der Dispatcher waehlt die richtige Runtime-Delivery (cli-bridge / host /
    # claude-code) und schickt die Test-Message ueber poll.sh / launchd.
    from app.services.dispatch import auto_dispatch_task
    task.dispatched_at = None
    task.ack_at = None
    session.add(task)
    await session.commit()
    asyncio.create_task(auto_dispatch_task(task.id, board_id))

    return tester


async def handle_review_rejection(
    session: AsyncSession,
    task: Task,
    board_id: uuid.UUID,
    rejecting_agent: Agent | None = None,
) -> Agent | None:
    """Review-Rejection: Task zurueck an Original-Developer.

    Shared zwischen agent_scoped.py und tasks.py.
    Beinhaltet Busy-Check, Queue-Fallback und Rejection-Counter.

    Returns: Original-Developer oder None.
    """
    from app.routers.agent_scoped import _find_last_developer

    # Rejection Counter (nur bei Agent-Rejection)
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

    # Rejection-Routing: bevorzuge IMMER den Original-Developer (Context-
    # Preservation). Board Lead ist nur Fallback wenn kein Developer
    # ermittelbar ist. Geänderte Reihenfolge gegenüber früher: vorher gingen
    # Root-Tasks (parent_task_id=NULL) zuerst an den Board Lead — das hat
    # spezifisch dispatched Tasks (z.B. argyelan-viral-shorts an Davinci) bei
    # Rejection an Boss umgeleitet, der den Context nie hatte. Siehe
    # 2026-05-08 viral-shorts E2E-Run: Davinci hatte P1-P6 fertig, der
    # "change"-Klick des Operators schickte den Task an Boss statt zurück an Davinci.
    original_dev = await _find_last_developer(session, task)

    # Fallback: Board Lead für Root-Tasks ohne identifizierbaren Developer
    # (z.B. Tasks die direkt vom Operator erstellt wurden ohne dass jemand sie
    # je in_progress → review gesetzt hat).
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
        return None

    if rejecting_agent and original_dev.id == rejecting_agent.id:
        return None  # Gleicher Agent, kein Re-Dispatch noetig

    # dispatch_intent setzen + Operational Controls Guard
    task.dispatch_intent = "review_rework"
    from app.services.operations import check_dispatch_allowed
    allowed, reason = await check_dispatch_allowed(task, original_dev, session)
    if not allowed:
        logger.info("Review-Rejection re-dispatch blocked: '%s' — %s", task.title, reason)
        return None

    # Reviewer-Lock freigeben
    if rejecting_agent and rejecting_agent.current_task_id == task.id:
        rejecting_agent.current_task_id = None
        session.add(rejecting_agent)

    task.assigned_agent_id = original_dev.id
    task.updated_at = utcnow()
    task.dispatched_at = None
    task.ack_at = None

    # Busy-Check: Hat der Developer schon einen aktiven Task?
    # Mit isolierten Sessions entfaellt der Busy-Check fuer Workers
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

    old_status = task.status  # review oder in_progress

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
        # Status auf inbox setzen — Agent muss ACKen (PATCH status: in_progress)
        # Das stellt sicher dass der Task-Runner ACK-Timeout erkennt
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
        # Re-Dispatch nach Review-Rejection.
        # Post Phase 29 / Gateway-Sunset: kein gateway_agent_id-Gate, kein RPC.
        # Re-Dispatch via auto_dispatch_task — der Dispatcher haendelt die
        # Runtime-spezifische Auslieferung (cli-bridge / host / claude-code).
        # Kontext-Erhalt: TaskComment-history bleibt erhalten — poll.sh
        # liefert die history (including review feedback) ueber /agent/me/poll.
        from app.services.dispatch import auto_dispatch_task
        task.dispatched_at = None
        task.ack_at = None
        session.add(task)
        await session.commit()
        asyncio.create_task(auto_dispatch_task(task.id, board_id))

    return original_dev


def trigger_auto_memory(
    task: Task,
    new_status: str,
    old_status: str,
) -> None:
    """Background-Tasks fuer Auto-Memory bei Status-Wechseln starten.

    Fire-and-forget — Fehler werden im Task-Callback geloggt.
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
    """Feedback-Lessons bei Review-Entscheidungen erfassen.

    Approved (review → done) oder Rejected (review → in_progress).
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


# ── Completion Callback: Board Lead benachrichtigen ──────────────
#
# Phase 29 / Gateway-Sunset: `select_lead_callback_session()` wurde entfernt.
# Der Helper diente dazu, aus einer Gateway-`sessions.list()`-Antwort die
# beste Session-Key fuer einen Lead-Nudge zu waehlen (Telegram > Discord >
# Main). Mit dem Gateway-Sunset entfaellt die Session-Liste komplett —
# Lieferung erfolgt jetzt ueber TaskComment + (optional) direkten
# `telegram_bot.send_message()` Aufruf.


async def _notify_lead_on_completion(
    session_unused: AsyncSession,
    task: Task,
    board_id: uuid.UUID,
    reviewer_name: str,
) -> None:
    """Completion-Callback: Henry bekommt verpflichtenden Rueckmeldeauftrag.

    Stufe 1 (sofort): Henry bekommt Callback mit offener Verpflichtung
      → report_back_status = "pending"
      → Henry soll dem Operator organisch antworten
    Stufe 2 (Fallback, 5 min): System-Sicherheitsnetz
      → NUR wenn report_back_status noch "pending"
      → Knappe System-Nachricht an den Operator via Telegram
    """
    from app.database import engine

    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            task = await session.get(Task, task.id)
            if not task:
                return

            # ── Evidence sammeln ──────────────────────────────
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

            # ── Stufe 1: Callback-Routing (Prio: callback_agent_id → owner falls Board Lead → Board Lead) ────
            # Post Phase 29 / Gateway-Sunset: Lead-Auswahl nutzt keine
            # gateway_agent_id-Filter mehr; Auslieferung erfolgt via
            # TaskComment (runtime-agnostic — Board Lead's poll.sh / launchd
            # liefert ueber /agent/me/comments) + optional direkter Telegram-
            # Hinweis fuer den Operator.
            lead = None
            if board_id:
                # Primaer: expliziter callback_agent_id
                if task.callback_agent_id:
                    cb_agent = await session.get(Agent, task.callback_agent_id)
                    if cb_agent:
                        lead = cb_agent

                # Sekundaer: owner_agent_id — aber nur wenn der Owner ein Board Lead ist
                # (Planner als Owner soll NICHT den Callback bekommen)
                if not lead and task.owner_agent_id:
                    owner = await session.get(Agent, task.owner_agent_id)
                    if owner and owner.is_board_lead:
                        lead = owner

                # Fallback: Board Lead des Boards
                if not lead:
                    lead = (await session.exec(
                        select(Agent).where(
                            Agent.board_id == board_id,
                            Agent.is_board_lead == True,  # noqa: E712
                        )
                    )).first()

                if lead:
                    # Completion-Info fuer Board Lead. Der Report-Back an den Operator
                    # wird NICHT durch den Lead erledigt — der ausfuehrende Agent hat
                    # bereits via `mc telegram` vor `mc done` direkt an den Reports-Chat des Operators
                    # geliefert (Hard-Gate erzwingt das).
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

                    # TaskComment ist der runtime-agnostische Lieferkanal —
                    # Board Lead's poll.sh / launchd-host pollt /agent/me/comments
                    # und pasted den Inhalt in dessen Session.
                    session.add(TaskComment(
                        task_id=task.id,
                        author_type="system",
                        content=notify_message,
                        comment_type="system_notify",
                    ))
                    await session.commit()

                    # Direkter Telegram-Hinweis an den Operator (best-effort).
                    # Bevorzugter Kanal aus Task-Kontext.
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

            # Owner-Callback Event loggen
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
    """Pruefen ob bereits ein phase_approval-Task fuer diesen Parent existiert.

    Idempotenz-Check: gibt bestehenden open Approval-Task zurueck (inbox/in_progress/review)
    damit Push-Pfad (agent_scoped) + Watchdog-Sweep (task_monitor) keine Duplikate erzeugen.
    Done/failed werden ignoriert (Decision bereits getroffen, nachfolgender Approval waere neu).
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

    # Idempotenz: Duplikat-Schutz. Zwei Pfade rufen uns auf (agent_scoped push
    # bei Subtask-done + Watchdog 30s-Sweep), ohne diesen Check kam es am
    # 2026-04-22 zu Doppel-Approval-Tasks die Boss beide bearbeiten musste.
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


# Magic-Marker damit der Reminder-System-Comment in der DB wiedererkannt
# werden kann (Idempotenz-Check + Eskalations-Logik).
ORCH_CLOSE_REMINDER_MARKER = "[orch-close-reminder]"

# Nach N ergebnislosen Reminders wird der Operator via Reports-Bot benachrichtigt.
# Gewollt großzügig: Boss hat Zeit zu reagieren bevor der Operator gestört wird.
ORCH_CLOSE_ESCALATION_THRESHOLD = 3


async def _escalate_orch_close_to_mark(
    parent: Task,
    orchestrator: Agent,
    nudge_count: int,
) -> bool:
    """Nach N ergebnislosen Close-Reminders: den Operator via Reports-Bot benachrichtigen.

    Idempotent pro Parent (Redis-Key `orch_close_escalated`, 48h TTL). Wird einmal
    gesendet und dann erst nach Expiry wieder.

    Returns True wenn gesendet, False wenn geskippt (nicht-konfiguriert, bereits
    eskaliert, Redis-Fehler, oder API-Error).
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
    """Incrementiert den Nudge-Counter fuer diesen Parent. Returns neuer Wert
    oder 0 wenn Redis nicht verfuegbar (fail-soft)."""
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
    """System-Comment auf Parent posten — wird via /agent/me/poll an den
    Owner-Agent zugestellt (poll.sh pasted ihn in dessen tmux-Session).

    Runtime-agnostischer Push-Pfad: alle Runtimes (cli-bridge / host /
    claude-code) verwenden poll.sh / launchd, um TaskComments zu
    konsumieren. Nutzt den bestehenden Mechanismus
    `_collect_and_ack_new_comments` + `_DELIVER_SYSTEM_COMMENT_TYPES`
    (siehe routers/agents.py) — `comment_type="system"` ist auf der
    Allowlist und wird in der Claude-Session paste-buffert.

    Idempotent: keine zweite Lieferung wenn ein Reminder mit demselben
    Marker bereits im dedup_window existiert.

    Default-Dedup (10 min) greift fuer den Push-Pfad direkt nach phase_approved.
    Der Watchdog-Safety-Net-Pfad (reason=stuck_safety_net) nutzt seinen
    eigenen Redis-Dedup (3-min-TTL) — dort wollen wir alle 3 min einen neuen
    Reminder bis Auto-Close. Default fuer stuck_safety_net: 2 min (ein Tick
    weniger als Watchdog-Dedup).

    Returns True wenn ein neuer Comment angelegt wurde.
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
    """Aktiver Nudge an Orchestrator: 'du musst diesen Parent abschliessen'.

    Trust-by-Default-Boards lassen den Parent nach `phase_approved` auf
    `in_progress` statt auf `review`. Ohne diesen Nudge sieht der
    Orchestrator in seiner Session keinen offenen Task mehr (Approval
    ist done) und vergisst die Hard-Gate-Sequenz `mc telegram` + `mc done`.

    Post Phase 29 / Gateway-Sunset: NUR Pfad A (System-Comment + Poll).
    Alle verbleibenden Runtimes (cli-bridge / host / claude-code) liefern
    System-Comments via /agent/me/comments ueber poll.sh / launchd in die
    tmux-Session des Orchestrators. Der frueher hier vorhandene Gateway-
    Pfad (RPC chat-send mit Telegram-Session-Bevorzugung) ist entfallen.

    Aufrufer:
    - `handle_phase_approval_decision` direkt nach `phase_approved`
    - Watchdog `_check_stuck_orchestrator_close` als Safety-Net nach 3 Min

    Returns True wenn die Message zugestellt wurde.
    """
    # Hard-Gate gilt nur fuer telegram-routed Tasks (analog task_lifecycle.py:486).
    # Discord-routed Tasks brauchen keinen `mc telegram`-Hinweis.
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
            # Trust-by-Default-Boards haben keinen dedicated Reviewer — `review`
            # wuerde den Parent ins Leere haengen lassen (Bug 2, 2026-04-22:
            # Parent blieb 8 Min auf review). Stattdessen: Parent bleibt
            # in_progress, Orchestrator schliesst via Hard-Gate (mc telegram
            # → mc done) selbst ab.
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
                # Aktiver Re-Dispatch-Nudge: ohne den uebersieht der Orchestrator
                # leicht, dass der Parent noch offen ist (Approval-Task ist done,
                # in seiner Sicht erscheint nichts mehr offenes).
                try:
                    nudged = await send_orchestrator_close_nudge(
                        session, parent, agent, reason="phase_approved",
                    )
                    if nudged:
                        result["orchestrator_nudged"] = True
                except Exception as e:
                    logger.warning("Orchestrator close nudge after phase_approved failed: %s", e)
                return result

            # Klassischer Review-Pfad fuer Boards mit require_review_before_done=true
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

        reopened_ids: list[uuid.UUID] = []
        for st in all_subtasks:
            if st.delegation_type == "phase_approval":
                continue
            if str(st.id) in mentioned_ids:
                # done → in_progress (DB-CHECK-Constraint laesst done → inbox
                # nicht zu — war vor 2026-04-23 ein hart bekannter Crash).
                #
                # Full dispatch-state reset — analog handle_review_handoff
                # (Z.687-762). Old behavior preserved dispatched_at/ack_at
                # because the watchdog would have re-dispatched the subtask
                # on its own. But the watchdog never did, and the agent's
                # poll.sh / launchd saw the subtask as "already delivered",
                # so the agent never received a wakeup. We now re-dispatch
                # explicitly via auto_dispatch_task() and post a per-subtask
                # rewrite-directive TaskComment so the agent sees WHAT to
                # fix in its next dispatch context. Incident 2026-05-20:
                # Researcher-Subtask 6a65a509 hing 1h still nach rewrite-
                # Anfrage — Live-Beweis für die Lücke.
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
                directive = TaskComment(
                    task_id=st.id,
                    author_type="agent",
                    author_agent_id=agent.id,
                    comment_type="feedback",
                    content=(
                        f"**Rewrite-Auftrag von {agent.name}**\n\n"
                        f"{reason_text}\n\n"
                        "_Dein Task wurde von der Phase-Approval-Review wieder "
                        "geoeffnet. Bitte arbeite die Punkte ab und schliesse "
                        "erneut mit `mc done` ab._"
                    ),
                )
                session.add(directive)
                reopened_ids.append(st.id)

        await session.commit()
        result["reopened"] = reopened_ids

        # Re-dispatch each re-opened subtask. auto_dispatch_task picks the
        # right runtime (cli-bridge poll.sh / host launchd) and the agent
        # will receive a fresh dispatch message; the feedback-comment posted
        # above is included in the recovery-recap so the agent sees WHY.
        for sid in reopened_ids:
            asyncio.create_task(auto_dispatch_task(sid, parent.board_id))

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
