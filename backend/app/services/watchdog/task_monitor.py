"""Task Monitor Mixin — Phasen-Completion, Review-/Blocked-/Zombie-Checks, Recovery.

Phase 29 (Gateway-Sunset): alle Gateway-RPC-Notifies durch TaskComment-Writes
ersetzt (Pattern A aus 29-PATTERNS.md). poll.sh / launchd-host liefern die
Kommentare via /agent/me/comments aus. Legacy-Queue-Pfade (_recover_aborted_tasks,
_process_task_queues, _process_pending_dispatches, _check_spawn_timeouts) sind
entfallen — sie operierten alle auf Gateway-Konzepten (spawn-session-key,
sessions-list). Stale-task ownership liegt bei task_runner._check_dispatch_ack.
"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy import or_, and_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.task import Task, TaskComment
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger("mc.watchdog")


class TaskMonitorMixin:
    """Task-bezogene Watchdog-Checks."""

    async def _check_phase_completions(self, session: AsyncSession) -> None:
        """Pruefen ob alle Subtasks einer Phase abgeschlossen sind."""
        result = await session.exec(
            select(Task).where(
                Task.status == "in_progress",
                Task.parent_task_id.is_(None),  # type: ignore[attr-defined]
            )
        )
        parent_tasks = result.all()

        for parent in parent_tasks:
            subtask_result = await session.exec(
                select(Task).where(Task.parent_task_id == parent.id)
            )
            all_subtasks = subtask_result.all()
            # Phase-Approval-Tasks sind intern und zaehlen nicht als regulaere Phase-Subtasks
            subtasks = [s for s in all_subtasks if s.delegation_type != "phase_approval"]

            if not subtasks:
                continue

            all_done = all(s.status == "done" for s in subtasks)
            if not all_done:
                continue

            # Idempotenz: Content-Hash des Child-Sets + Status.
            # Feuert erneut wenn sich der Status aendert (z.B. nach Rejection + Rework).
            import hashlib
            child_fingerprint = hashlib.md5(
                ",".join(sorted(f"{s.id}:{s.status}" for s in subtasks)).encode()
            ).hexdigest()
            redis = await get_redis()
            dedup_key = f"mc:watchdog:phase_done:{parent.id}"
            stored = await redis.get(dedup_key)
            if stored and stored == child_fingerprint:
                continue  # Gleiches Child-Set, bereits gefeuert (Redis-Cache)

            # DB-Level Dedup: Redis-restart-proof.
            # Pruefen ob ein phase_completed Event mit demselben Fingerprint bereits existiert.
            from app.models.activity import ActivityEvent
            from datetime import timedelta
            recent_cutoff = utcnow() - timedelta(hours=24)
            existing_evt = await session.exec(
                select(ActivityEvent).where(
                    ActivityEvent.task_id == parent.id,
                    ActivityEvent.event_type == "task.phase_completed",
                    ActivityEvent.created_at >= recent_cutoff,
                )
            )
            if existing_evt.first():
                # Phase-Completion bereits gefeuert (DB-Fallback) — Redis-Key wiederherstellen
                await redis.set(dedup_key, child_fingerprint)
                continue

            logger.info(
                "Phase completion: All %d subtasks of '%s' done — notifying lead",
                len(subtasks),
                parent.title,
            )

            await emit_event(
                session,
                "task.phase_completed",
                f"Phase abgeschlossen: '{parent.title}' ({len(subtasks)} Tasks erledigt)",
                board_id=parent.board_id,
                task_id=parent.id,
                detail={
                    "subtasks_count": len(subtasks),
                },
            )

            from app.services.auto_memory import record_phase_completion
            from app.services.watchdog.core import _create_background_task
            _create_background_task(record_phase_completion(parent.id, [s.id for s in subtasks]))

            # Screenshots aus Children an Telegram senden
            requirements = (parent.report_back_requirements or "").lower()
            if "screenshot" in requirements or "visual" in requirements or "before_after" in requirements:
                _parent_id = parent.id
                _child_ids = [s.id for s in subtasks]
                _create_background_task(
                    self._send_phase_screenshots_bg(_parent_id, _child_ids)
                )

            # Phase-Approval: statt direkt review + Rex-Handoff wird ein Phase-Approval-Task
            # fuer den Board Lead (Boss) angelegt. Boss entscheidet: approved (→ Parent review
            # → Operator) oder rewrite (→ Subtasks re-open). Fallback auf Rex-Handoff wenn kein
            # Board Lead existiert.
            if parent.status == "in_progress":
                # Idempotenz-Schutz: Phase wurde bereits approved UND das aktuelle
                # Child-Set ist genau das was approved wurde. Skip Re-Creation —
                # der stuck_orchestrator_close-Pfad nudged stattdessen Boss zum Close.
                #
                # Wichtig fuer Rewrite-Pfad-Korrektheit:
                # Wenn Boss `phase_rewrite_request` gemacht hat und Worker die
                # Subtasks neu bearbeitet haben, ist das aktuelle Child-Set NICHT
                # mehr das was approved wurde. Dann muss ein neuer Approval
                # erstellt werden (Fingerprint-unverändert heisst: subtasks-IDs
                # + status unverändert seit approval; wenn rewrite passiert hat
                # sich der status zwischendurch auf inbox gedreht + wieder done
                # → letzter subtask.updated_at > approval.completed_at).
                latest_done_approval = (await session.exec(
                    select(Task)
                    .where(
                        Task.parent_task_id == parent.id,
                        Task.delegation_type == "phase_approval",
                        Task.status == "done",
                    )
                    .order_by(Task.completed_at.desc())  # type: ignore[attr-defined]
                )).first()

                open_approval_exists = (await session.exec(
                    select(Task).where(
                        Task.parent_task_id == parent.id,
                        Task.delegation_type == "phase_approval",
                        Task.status.in_(["inbox", "in_progress", "review"]),  # type: ignore[union-attr]
                    )
                )).first()

                if latest_done_approval is not None and open_approval_exists is None:
                    # Prüfe: wurde EIN subtask NACH dem approval modifiziert?
                    approval_done_at = latest_done_approval.completed_at or latest_done_approval.updated_at
                    latest_subtask_mod = max(
                        (s.updated_at for s in subtasks if s.updated_at is not None),
                        default=None,
                    )
                    phase_unchanged_since_approval = (
                        approval_done_at is not None
                        and latest_subtask_mod is not None
                        and latest_subtask_mod <= approval_done_at
                    )
                    if phase_unchanged_since_approval:
                        logger.info(
                            "Phase already approved for '%s' (approval=%s done, child-set unchanged) — skip re-create, stuck-check handles close",
                            parent.title[:40], latest_done_approval.id,
                        )
                        await redis.set(dedup_key, child_fingerprint)
                        continue
                    # else: child-set changed (z.B. rewrite) → lass durchlaufen,
                    # `_existing_phase_approval_for_parent` im create-call regelt Idempotenz

                # Board Lead finden
                bl_result = await session.exec(
                    select(Agent).where(
                        Agent.board_id == parent.board_id,
                        Agent.is_board_lead == True,  # noqa: E712
                    )
                )
                board_lead = bl_result.first()

                if board_lead is not None:
                    from app.services.task_lifecycle import create_phase_approval_task
                    try:
                        approval = await create_phase_approval_task(session, parent, board_lead)
                        logger.info(
                            "Phase-Approval created for '%s' → %s (approval_id=%s)",
                            parent.title[:40], board_lead.name, approval.id,
                        )
                    except Exception as e:
                        logger.warning(
                            "Phase-Approval creation failed for '%s': %s — notifying lead",
                            parent.title[:40], e,
                        )
                        await self._redispatch_parent_to_lead(session, parent, subtasks)
                else:
                    # Kein Board Lead → legacy Rex-Handoff
                    logger.warning(
                        "Phase complete but no Board Lead on board %s — fallback to Rex handoff",
                        parent.board_id,
                    )
                    parent.status = "review"
                    parent.updated_at = utcnow()
                    session.add(parent)
                    await session.commit()
                    try:
                        from app.services.task_lifecycle import handle_review_handoff
                        await handle_review_handoff(session, parent, parent.board_id)
                    except Exception as e:
                        logger.warning("Fallback review handoff failed: %s", e)
                        await self._redispatch_parent_to_lead(session, parent, subtasks)

            # Dedup mit Fingerprint — bleibt permanent bis Child-Set sich aendert
            await redis.set(dedup_key, child_fingerprint)

            # Auto-Advance: naechste Phase NUR starten wenn aktuelle Phase WIRKLICH
            # abgeschlossen ist (done). Nicht bei review/user_test — dort laeuft
            # noch das Review-/Test-Gate.
            if parent.status == "done":
                await self._auto_advance_next_phase(session, parent)

            if parent.project_id:
                await self._update_project_progress(session, parent.project_id)

    async def _send_phase_screenshots_bg(
        self, parent_id: "uuid.UUID", child_ids: list["uuid.UUID"]
    ) -> None:
        """Screenshot-Artefakte aus Children an Telegram senden.

        Laeuft als Background-Task mit eigener DB-Session (Watchdog-Session
        ist zu diesem Zeitpunkt moeglicherweise schon geschlossen).
        """
        import re
        import uuid
        from app.models.task import Task, TaskComment
        from app.services.telegram_bot import telegram_bot
        from app.database import engine

        if not telegram_bot.configured:
            logger.info("Phase screenshots: Telegram not configured")
            return

        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                parent = await session.get(Task, parent_id)
                if not parent:
                    return
                parent_title = parent.title[:60]

                sent = 0
                seen_paths: set[str] = set()  # Deduplizieren: gleicher Pfad nur 1x senden
                for child_id in child_ids:
                    child = await session.get(Task, child_id)
                    if not child:
                        continue
                    child_title = child.title[:60]

                    comments = (await session.exec(
                        select(TaskComment)
                        .where(TaskComment.task_id == child_id)
                        .order_by(TaskComment.created_at.desc())
                        .limit(5)
                    )).all()

                    for cmt in comments:
                        paths = re.findall(
                            r'(/Users/[^\s;,)]+\.(?:png|jpg|jpeg))',
                            cmt.content or "",
                        )
                        for p in paths:
                            if p in seen_paths:
                                continue
                            seen_paths.add(p)
                            if sent >= 4:
                                logger.info("Phase screenshots: sent %d, limit reached", sent)
                                return
                            mid = await telegram_bot.send_photo(
                                p, caption=f"{parent_title} — {child_title}"
                            )
                            if mid:
                                sent += 1
                                logger.info("Phase screenshot sent: %s", p)

                if sent == 0:
                    logger.info("Phase screenshots: no image paths found in child comments")
                else:
                    logger.info("Phase screenshots: %d image(s) sent to Telegram", sent)
        except Exception as e:
            logger.warning("Phase screenshots failed: %s", e)

    async def _check_stuck_orchestrator_close(self, session: AsyncSession) -> None:
        """Safety-Net + Auto-Close fuer stuck Parents nach phase_approved.

        Problem (2026-04-22 Live-Tests x3): Auf Trust-by-Default-Boards bleibt
        der Parent nach `phase_approved` auf `in_progress` bis der Orchestrator
        `mc done` ruft. Wenn er das vergisst oder verwechselt Task-IDs (PATCH
        auf Subtask statt Parent), haengt der Task dauerhaft.

        Zweistufiger Safety-Net:
        1. **Nudge-Phase** (ab 3 min nach updated_at): Active re-reminder via
           System-Comment (poll.sh) ODER RPC. Max 2 Nudges im Abstand von 3 min.
        2. **Auto-Close-Phase** (nach 2 ergebnislosen Nudges ~= 6 min): Backend
           setzt Parent direkt auf `review`. Event + Operator-Notification. Danach
           kann der Operator manuell `done` setzen wenn Output ok ist.

        Bedingungen (jetzt liberaler als PR #59):
        - Root-Task (parent_task_id IS NULL), status=in_progress
        - Mindestens 1 Subtask mit delegation_type=phase_approval + status=done
        - Alle non-approval Subtasks done, kein OPEN approval (keine Race mit
          laufender nochmaliger Phase-Approval)
        - updated_at > 3 min ago

        Entfernte Filter (waren zu eng):
        - `report_back_required=True` — vorher wurden normale Tasks ohne
          Telegram-Hard-Gate ignoriert. Jetzt: alle stuck Parents.
        - Lead-Runtime-Filter (Phase 29-30, Gateway-Sunset) — vorher wurden
          Host-Runtime-Leads (Boss) geskipped. Jetzt: alle Lead-Typen werden
          ge-nudged (System-Comment-basiert, runtime-agnostisch).

        Idempotenz: Redis-Counter mc:watchdog:stuck_orch_close_count:{id} (TTL 1h).
        """
        cutoff = utcnow() - timedelta(minutes=3)
        result = await session.exec(
            select(Task).where(
                Task.status == "in_progress",
                Task.parent_task_id.is_(None),  # type: ignore[attr-defined]
                Task.updated_at < cutoff,
            )
        )
        candidates = result.all()
        if not candidates:
            return

        redis = await get_redis()

        for parent in candidates:
            subtask_result = await session.exec(
                select(Task).where(Task.parent_task_id == parent.id)
            )
            subtasks = subtask_result.all()
            if not subtasks:
                continue

            approval_done = [
                s for s in subtasks
                if s.delegation_type == "phase_approval" and s.status == "done"
            ]
            if not approval_done:
                continue

            # Wenn NEBEN dem done-Approval ein OFFENER Approval existiert
            # (inbox/in_progress/review), ist Boss in einer neuen Phase — skip
            # damit wir nicht parallel zu einem laufenden Approval nudgen.
            open_approval = [
                s for s in subtasks
                if s.delegation_type == "phase_approval"
                and s.status in ("inbox", "in_progress", "review")
            ]
            if open_approval:
                continue

            non_approval = [s for s in subtasks if s.delegation_type != "phase_approval"]
            if non_approval and not all(s.status == "done" for s in non_approval):
                continue

            # Nudge-Counter: wie oft haben wir diesen Parent schon angestupst?
            count_key = f"mc:watchdog:stuck_orch_close_count:{parent.id}"
            try:
                nudge_count = int(await redis.get(count_key) or 0)
            except Exception:
                nudge_count = 0

            # Dedup-Key pro Nudge-Iteration (verhindert 2 Nudges im selben Watchdog-Loop)
            dedup_key = f"mc:watchdog:stuck_orch_close:{parent.id}"
            if await redis.get(dedup_key):
                continue

            # Board Lead (Orchestrator) finden
            bl_result = await session.exec(
                select(Agent).where(
                    Agent.board_id == parent.board_id,
                    Agent.is_board_lead == True,  # noqa: E712
                )
            )
            lead = bl_result.first()
            if not lead:
                continue

            # ── Auto-Close nach 2 ergebnislosen Nudges ──
            # Logik: 1. Nudge bei iter 1 (t=3min), 2. Nudge bei iter 2 (t=6min).
            # Wenn wir jetzt bei count=2 sind und der Parent IMMER NOCH stuck ist,
            # schliessen wir automatisch ab (review). Der Operator entscheidet final (done).
            if nudge_count >= 2:
                logger.warning(
                    "Auto-close stuck parent '%s' (id=%s): %d nudges ohne Reaktion",
                    (parent.title or "")[:60], parent.id, nudge_count,
                )
                parent.status = "review"
                parent.updated_at = utcnow()
                session.add(parent)
                await session.commit()

                try:
                    await emit_event(
                        session,
                        "task.auto_closed_stuck",
                        f"Parent '{(parent.title or '')[:60]}' automatisch auf review "
                        f"gesetzt nach {nudge_count} ergebnislosen Nudges.",
                        board_id=parent.board_id,
                        task_id=parent.id,
                        agent_id=lead.id,
                        severity="warning",
                    )
                except Exception as e:
                    logger.debug("auto_closed_stuck event emit failed: %s", e)

                # Operator via Reports-Bot informieren (idempotent via escalation key)
                try:
                    from app.services.task_lifecycle import _escalate_orch_close_to_mark
                    await _escalate_orch_close_to_mark(parent, lead, nudge_count)
                except Exception as e:
                    logger.warning("Auto-close mark notification failed: %s", e)

                # Counter resetten — auto-close hat den Zyklus beendet.
                try:
                    await redis.delete(count_key)
                except Exception:
                    pass
                continue

            # ── Sonst: normaler Nudge ──
            try:
                from app.services.task_lifecycle import send_orchestrator_close_nudge
                nudged = await send_orchestrator_close_nudge(
                    session, parent, lead, reason="stuck_safety_net",
                )
            except Exception as e:
                logger.warning(
                    "Stuck orchestrator close nudge for parent %s failed: %s",
                    parent.id, e,
                )
                continue

            if not nudged:
                continue

            # Counter increment (TTL 1h — Parent sollte lange vorher abgeschlossen sein)
            try:
                new_count = int(await redis.incr(count_key))
                if new_count == 1:
                    await redis.expire(count_key, 3600)
            except Exception:
                new_count = nudge_count + 1

            await redis.set(dedup_key, "1", ex=180)  # 3 min — passt zu Watchdog-Interval
            try:
                await emit_event(
                    session,
                    "task.stuck_awaiting_orchestrator_close",
                    f"Parent '{(parent.title or '')[:60]}' wartet 3+ Min nach phase_approved — "
                    f"Nudge {new_count}/2 an {lead.name}",
                    board_id=parent.board_id,
                    task_id=parent.id,
                    agent_id=lead.id,
                    severity="warning",
                )
            except Exception as e:
                logger.debug("stuck_awaiting_orchestrator_close event emit failed: %s", e)

    async def _redispatch_parent_to_lead(
        self, session: AsyncSession, parent_task: Task, subtasks: list[Task]
    ) -> None:
        """Board Lead strukturiert benachrichtigen: alle Subtasks done, Entscheidung noetig.

        Phase 29: gateway-agent-id gate entfaellt. Lead per is_board_lead flag
        suchen; Notify per TaskComment (poll.sh / launchd-host liefern aus).
        """
        if not parent_task.board_id:
            return

        # Lead finden (assigned Agent oder Board Lead)
        lead = None
        if parent_task.assigned_agent_id:
            lead = await session.get(Agent, parent_task.assigned_agent_id)
        if not lead:
            result = await session.exec(
                select(Agent).where(
                    Agent.board_id == parent_task.board_id,
                    Agent.is_board_lead == True,  # noqa: E712
                )
            )
            lead = result.first()

        if not lead:
            return

        # Subtask-Zusammenfassung + Evidence bauen
        subtask_lines = []
        evidence_lines = []
        for s in subtasks:
            agent = await session.get(Agent, s.assigned_agent_id) if s.assigned_agent_id else None
            agent_name = agent.name if agent else "unbekannt"
            subtask_lines.append(f"  - {s.title} ({agent_name}, {s.status})")
            # Evidence aus Resolution/Review-Kommentaren sammeln
            last_cmt = (await session.exec(
                select(TaskComment)
                .where(
                    TaskComment.task_id == s.id,
                    TaskComment.comment_type.in_(["resolution", "review"]),
                )
                .order_by(TaskComment.created_at.desc())
                .limit(1)
            )).first()
            if last_cmt:
                evidence_lines.append(f"**{s.title}:** {last_cmt.content[:300]}")

        subtask_summary = "\n".join(subtask_lines)
        evidence_text = "\n\n".join(evidence_lines) if evidence_lines else "(Keine Evidence)"

        # Report-Back wird NICHT mehr vom Lead erledigt — der ausfuehrende Agent
        # muss `mc telegram` vor `mc done` selbst senden (Hard-Gate in agent_scoped.py).
        # Lead bekommt nur die Completion-Info.
        report_back_hint = ""
        if parent_task.report_back_required:
            report_back_hint = (
                f"\n**Hinweis:** Dieser Task hat `report_back_required=true`. "
                f"Der ausfuehrende Agent muss `mc telegram` VOR `mc done` aufrufen — "
                f"das Backend blockiert `done` sonst (Hard-Gate).\n"
            )

        message = (
            f"# AUFTRAG ABGESCHLOSSEN: {parent_task.title}\n\n"
            f"**Task-ID:** {parent_task.id}\n\n"
            f"Alle {len(subtasks)} Subtasks sind erledigt:\n{subtask_summary}\n"
            f"{report_back_hint}\n"
            f"## Evidence aus Subtasks\n{evidence_text}\n\n"
            f"## Deine Entscheidung\n"
            f"1. **Task abschliessen** → PATCH status: done "
            f"(bei report_back_required: erst `mc telegram` senden)\n"
            f"2. **Weitere Subtasks erstellen**\n"
        )

        # TaskComment als runtime-agnostische Delivery (Pattern A 29-PATTERNS.md)
        session.add(TaskComment(
            task_id=parent_task.id,
            author_type="system",
            content=message,
            comment_type="watchdog_notify",
        ))
        # dispatched_at setzen damit _check_undispatched_tasks den Root
        # nicht nochmal dispatched (Race-Prevention).
        # dispatch_attempt_id zuruecksetzen: Board Lead arbeitet in der
        # Haupt-Session ohne Attempt-Header.
        parent_task.dispatched_at = utcnow()
        session.add(parent_task)
        await session.commit()

        from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
        await clear_dispatch_attempt_id(
            session, parent_task,
            caller="watchdog_phase_complete",
            reason="parent_redispatched_to_board_lead",
        )

        # Fallback-Timer entfernt (2026-04-22): der ausfuehrende Agent sendet jetzt
        # direkt via `mc telegram` vor `mc done` (Hard-Gate erzwingt das).
        # Auto-Draft bei failed uebernimmt Failure-Case.

    async def _update_project_progress(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        """progress_pct aktualisieren basierend auf erledigten Subtasks."""
        from app.models.board import Project
        project = await session.get(Project, project_id)
        if not project:
            return

        all_result = await session.exec(
            select(Task).where(
                Task.project_id == project_id,
                Task.parent_task_id.isnot(None),  # type: ignore[attr-defined]
            )
        )
        all_subtasks = all_result.all()
        if not all_subtasks:
            return

        done_count = sum(1 for t in all_subtasks if t.status == "done")
        project.progress_pct = int((done_count / len(all_subtasks)) * 100)
        project.updated_at = utcnow()
        session.add(project)
        await session.commit()

        await self._check_project_completion(session, project)

    async def _check_project_completion(self, session: AsyncSession, project) -> None:
        """Pruefen ob alle Phasen eines Projekts abgeschlossen sind → Status review."""
        if project.status not in ("active", "planning"):
            return

        phases_result = await session.exec(
            select(Task).where(
                Task.project_id == project.id,
                Task.parent_task_id.is_(None),  # type: ignore[attr-defined]
            )
        )
        phases = phases_result.all()
        if not phases:
            return

        if all(p.status == "done" for p in phases):
            project.status = "review"
            project.updated_at = utcnow()
            session.add(project)
            await session.commit()

            await emit_event(
                session, "project.review",
                f"Projekt bereit: {project.name} — alle Phasen abgeschlossen",
                board_id=project.board_id,
                detail={"project_id": str(project.id), "progress_pct": 100},
            )
            logger.info("Project '%s' moved to review (all phases done)", project.name)

    async def _auto_advance_next_phase(self, session: AsyncSession, completed_parent: Task) -> None:
        """Naechste Phase automatisch starten wenn vorherige done ist."""
        if not completed_parent.project_id:
            return

        next_phase = (await session.exec(
            select(Task).where(
                Task.project_id == completed_parent.project_id,
                Task.parent_task_id.is_(None),  # type: ignore[attr-defined]
                Task.status == "inbox",
                Task.sort_order > completed_parent.sort_order,
            ).order_by(Task.sort_order.asc()).limit(1)
        )).first()

        if not next_phase:
            return

        from app.services.task_lifecycle import record_task_event
        await record_task_event(
            session, next_phase.id, "inbox", "in_progress",
            changed_by="watchdog", reason="auto_advance_phase",
        )

        next_phase.status = "in_progress"
        next_phase.started_at = utcnow()
        next_phase.updated_at = utcnow()
        session.add(next_phase)
        await session.commit()

        logger.info("Auto-advance: Phase '%s' started", next_phase.title)

        await emit_event(
            session, "task.phase_auto_started",
            f"Phase auto-gestartet: '{next_phase.title}'",
            board_id=next_phase.board_id, task_id=next_phase.id,
        )

        # Subtasks der neuen Phase dispatchen (immer bei Auto-Advance)
        subtask_result = await session.exec(
            select(Task).where(
                Task.parent_task_id == next_phase.id,
                Task.status == "inbox",
            )
        )
        subtasks = subtask_result.all()
        if subtasks:
            from app.services.dispatch import auto_dispatch_task, dependencies_met
            from app.services.watchdog.core import _create_background_task
            for subtask in subtasks:
                if await dependencies_met(session, subtask):
                    _create_background_task(auto_dispatch_task(subtask.id, next_phase.board_id))

    # Phase 29: _recover_aborted_tasks entfernt — operierte auf
    # has_session-Check (Gateway sessions-list) und Gateway-RPC chat-send.
    # Stale/aborted Tasks werden jetzt ueber task_runner._check_dispatch_ack
    # und _recover_orphaned_tasks (heartbeat-basiert) abgefangen.

    # Phase 29: _process_task_queues + _process_pending_dispatches entfernt —
    # beide waren gateway-only (queue_length, sessions-list, Gateway-RPC).
    # CLI-bridge agents haben ihre eigene poll.sh-Queue; Re-dispatch laeuft
    # ueber dispatch.auto_dispatch_task (runtime-aware).

    async def _check_blocked_tasks(self, session: AsyncSession) -> None:
        """Safety-Net: Blocked-Tasks pruefen. Wenn Approval existiert → Operator erinnern, sonst Lead."""
        from app.models.approval import Approval

        result = await session.exec(
            select(Task).where(Task.status == "blocked")
        )
        blocked_tasks = result.all()

        if not blocked_tasks:
            return

        now = utcnow()
        redis = await get_redis()

        for task in blocked_tasks:
            if not task.board_id or not task.updated_at:
                continue

            # Grace Period: 30 Minuten
            age_minutes = (now - task.updated_at).total_seconds() / 60
            if age_minutes < 30:
                continue

            # Redis-Dedup: nur alle 2h erinnern
            dedup_key = f"mc:watchdog:blocked_remind:{task.id}"
            if await redis.get(dedup_key):
                continue

            # Agent-Name holen
            assigned_name = "unbekannt"
            if task.assigned_agent_id:
                assigned_agent = await session.get(Agent, task.assigned_agent_id)
                if assigned_agent:
                    assigned_name = assigned_agent.name

            # Pruefen ob blocker_decision Approval existiert
            pending_approval = (await session.exec(
                select(Approval).where(
                    Approval.task_id == task.id,
                    Approval.action_type == "blocker_decision",
                    Approval.status == "pending",
                )
            )).first()

            if pending_approval:
                # Approval existiert → Operator sieht es im Inbox, nur loggen
                await redis.set(dedup_key, "1", ex=7200)  # 2h TTL
                await emit_event(
                    session, "task.blocked_reminder",
                    f"Blocked-Reminder: '{task.title}' ({assigned_name}) — {int(age_minutes)}min — Approval pending",
                    board_id=task.board_id, task_id=task.id,
                )
                logger.info("Blocked reminder (approval pending) for '%s' (%dmin)", task.title, int(age_minutes))
            else:
                # Kein Approval (Altlast?) → Lead via TaskComment erinnern
                lead_result = await session.exec(
                    select(Agent).where(
                        Agent.board_id == task.board_id,
                        Agent.is_board_lead == True,  # noqa: E712
                    )
                )
                lead = lead_result.first()
                if not lead:
                    continue

                msg = (
                    f"REMINDER: Task \"{task.title}\" ist seit {int(age_minutes)} Min blockiert.\n"
                    f"Zugewiesen an: {assigned_name}\n"
                    f"Task-ID: {task.id}\n"
                    f"Bitte pruefen und Blocker loesen."
                )
                # Pattern A (29-PATTERNS.md): TaskComment-Notify ersetzt Gateway-RPC.
                session.add(TaskComment(
                    task_id=task.id,
                    author_type="system",
                    content=msg,
                    comment_type="watchdog_notify",
                ))
                await session.commit()
                await redis.set(dedup_key, "1", ex=7200)  # 2h TTL

                await emit_event(
                    session, "task.blocked_reminder",
                    f"Blocked-Reminder: '{task.title}' ({assigned_name}) — {int(age_minutes)}min",
                    board_id=task.board_id, task_id=task.id,
                )
                logger.info("Blocked reminder posted for '%s' (%dmin)", task.title, int(age_minutes))

    async def _check_dependency_zombies(self, session: AsyncSession) -> None:
        """Tasks finden die auf impossible Dependencies warten (Zombie-Praevention).

        Ein Task ist ein Zombie wenn er inbox/in_progress ist und eine Dependency
        in einem terminalen Fehl-Status steht (failed, blocked). Diese Dependency
        wird nie done → Task wartet ewig.

        Aktion: Approval erstellen damit der Operator die Dependency aufloesen kann.
        """
        from app.models.approval import Approval
        from app.models.task import TaskDependency

        result = await session.exec(
            select(TaskDependency)
        )
        all_deps = result.all()
        if not all_deps:
            return

        redis = await get_redis()

        for dep in all_deps:
            # Nur pruefen wenn der abhaengige Task noch aktiv wartet
            task = await session.get(Task, dep.task_id)
            if not task or task.status not in ("inbox", "in_progress"):
                continue

            # Dependency-Task pruefen
            dep_task = await session.get(Task, dep.depends_on_task_id)
            if not dep_task or dep_task.status not in ("failed", "blocked"):
                continue

            # Approval braucht agent_id — skip wenn Task keinem Agent zugewiesen
            if not task.assigned_agent_id:
                continue

            # Dedup: nur einmal pro Dependency-Paar
            dedup_key = RedisKeys.recovery_attempt(str(task.id), "dependency_zombie")
            if await redis.get(dedup_key):
                continue

            # Pruefen ob schon ein Approval existiert
            existing = (await session.exec(
                select(Approval).where(
                    Approval.task_id == task.id,
                    Approval.action_type == "dependency_zombie",
                    Approval.status == "pending",
                )
            )).first()

            if not existing:
                approval = Approval(
                    board_id=task.board_id,
                    task_id=task.id,
                    agent_id=task.assigned_agent_id,
                    action_type="dependency_zombie",
                    description=(
                        f"Task '{task.title}' wartet auf '{dep_task.title}' "
                        f"die im Status '{dep_task.status}' steht. "
                        f"Dependency wird nie erfuellt — manuelle Aufloesung noetig."
                    ),
                )
                session.add(approval)
                await session.commit()

            await redis.set(dedup_key, "1", ex=14400)  # 4h TTL

            await emit_event(
                session, "task.dependency_zombie",
                f"Zombie-Dependency: '{task.title}' wartet auf '{dep_task.title}' ({dep_task.status})",
                board_id=task.board_id, task_id=task.id,
                severity="warning",
            )
            logger.warning(
                "Dependency zombie: '%s' waits on '%s' (status=%s)",
                task.title, dep_task.title, dep_task.status,
            )

    async def _check_review_tasks(self, session: AsyncSession) -> None:
        """Review-Tasks ueberwachen: Timeout-Eskalation + Decision-Missing-Nudge.

        Timeout-Eskalation (bestehend, mit HOLD-Ausnahme):
        1. 60 Min ohne Update → Reviewer nudgen (RPC Push)
        2. 120 Min → Lead informieren
        3. 180 Min → Approval erstellen fuer den Operator

        Decision-Missing (neu):
        Reviewer hat kommentiert aber keine Entscheidung getroffen → nach 15 Min nudgen.
        """
        from app.models.task import TaskComment
        from sqlalchemy import func

        result = await session.exec(
            select(Task).where(Task.status == "review")
        )
        review_tasks = result.all()

        if not review_tasks:
            return

        now = utcnow()
        redis = await get_redis()

        for task in review_tasks:
            if not task.board_id or not task.updated_at:
                continue

            # ── HOLD-Ausnahme: bewusst angehalten → komplette Eskalation ueberspringen ──
            if task.review_decision == "hold":
                continue

            # ── Operativ gestoppte Tasks nicht eskalieren ──
            if task.run_control in ("stopped", "manual_hold"):
                continue

            # ── Decision-Missing Check ──────────────────────────────────────
            # Wenn Entscheidung fehlt und Reviewer schon kommentiert hat
            if task.review_decision is None and task.assigned_agent_id:
                await self._check_review_decision_missing(
                    session, task, now, redis,
                )

            # ── Bestehende Timeout-Eskalation ──────────────────────────────
            # Nur wenn noch keine Entscheidung getroffen (approved/changes_requested
            # bedeutet Task sollte nicht mehr in review sein → Inkonsistenz, ignorieren)
            if task.review_decision in ("approved", "changes_requested"):
                continue

            age_minutes = (now - task.updated_at).total_seconds() / 60
            if age_minutes < 60:
                continue

            # Dedup Key pro Eskalationsstufe
            if age_minutes >= 180:
                escalation_level = "approval"
            elif age_minutes >= 120:
                escalation_level = "lead"
            else:
                escalation_level = "nudge"

            dedup_key = f"mc:watchdog:review_{escalation_level}:{task.id}"
            if await redis.get(dedup_key):
                continue

            # Reviewer-Agent holen
            reviewer_name = "unbekannt"
            reviewer_agent = None
            if task.assigned_agent_id:
                reviewer_agent = await session.get(Agent, task.assigned_agent_id)
                if reviewer_agent:
                    reviewer_name = reviewer_agent.name

            if escalation_level == "nudge":
                # Stufe 1: Reviewer nudgen via TaskComment (Pattern A 29-PATTERNS.md)
                if reviewer_agent:
                    msg = (
                        f"REVIEW-REMINDER: Task \"{task.title}\" wartet seit "
                        f"{int(age_minutes)} Min auf dein Review.\n"
                        f"Task-ID: {task.id}\n"
                        f"Board-ID: {task.board_id}\n\n"
                        f"Bitte jetzt den Review-Endpoint nutzen:\n"
                        f"POST .../review {{\"decision\": \"approve\", \"comment\": \"...\"}}\n"
                        f"POST .../review {{\"decision\": \"request_changes\", \"comment\": \"...\"}}"
                    )
                    session.add(TaskComment(
                        task_id=task.id,
                        author_type="system",
                        content=msg,
                        comment_type="watchdog_notify",
                    ))
                    await session.commit()

                await redis.set(dedup_key, "1", ex=3600)  # 1h TTL
                await emit_event(
                    session, "task.review_nudge",
                    f"Review-Nudge: '{task.title}' ({reviewer_name}) — {int(age_minutes)}min",
                    board_id=task.board_id, task_id=task.id,
                )
                logger.info("Review nudge posted for '%s' (%dmin)", task.title, int(age_minutes))

            elif escalation_level == "lead":
                # Stufe 2: Lead informieren via TaskComment
                lead_result = await session.exec(
                    select(Agent).where(
                        Agent.board_id == task.board_id,
                        Agent.is_board_lead == True,  # noqa: E712
                    )
                )
                lead = lead_result.first()
                if lead:
                    msg = (
                        f"REVIEW STUCK: Task \"{task.title}\" ist seit {int(age_minutes)} Min im Review.\n"
                        f"Reviewer: {reviewer_name}\n"
                        f"Task-ID: {task.id}\n\n"
                        f"Bitte pruefen ob Reviewer aktiv ist."
                    )
                    session.add(TaskComment(
                        task_id=task.id,
                        author_type="system",
                        content=msg,
                        comment_type="watchdog_notify",
                    ))
                    await session.commit()

                await redis.set(dedup_key, "1", ex=3600)
                await emit_event(
                    session, "task.review_escalation",
                    f"Review-Eskalation: '{task.title}' ({reviewer_name}) — {int(age_minutes)}min — Lead informiert",
                    board_id=task.board_id, task_id=task.id,
                    severity="warning",
                )
                logger.info("Review escalation to lead posted for '%s' (%dmin)", task.title, int(age_minutes))

            elif escalation_level == "approval":
                # Stufe 3: Approval fuer den Operator erstellen
                from app.models.approval import Approval
                from datetime import timedelta

                existing = (await session.exec(
                    select(Approval).where(
                        Approval.task_id == task.id,
                        Approval.action_type == "review_stuck",
                        Approval.status == "pending",
                    )
                )).first()

                if not existing:
                    approval = Approval(
                        board_id=task.board_id,
                        task_id=task.id,
                        agent_id=task.assigned_agent_id,
                        action_type="review_stuck",
                        description=(
                            f"Review fuer '{task.title}' haengt seit {int(age_minutes)} Min. "
                            f"Reviewer: {reviewer_name}. Manuelle Pruefung noetig."
                        ),
                        expires_at=now + timedelta(hours=24),
                    )
                    session.add(approval)
                    await session.commit()

                await redis.set(dedup_key, "1", ex=7200)
                await emit_event(
                    session, "task.review_stuck",
                    f"Review STUCK: '{task.title}' ({reviewer_name}) — {int(age_minutes)}min — Approval erstellt",
                    board_id=task.board_id, task_id=task.id,
                    severity="warning",
                )
                logger.warning("Review stuck approval for '%s' (%dmin)", task.title, int(age_minutes))

    async def _check_review_decision_missing(
        self,
        session: AsyncSession,
        task: Task,
        now,
        redis,
    ) -> None:
        """Reviewer hat kommentiert aber keine Entscheidung getroffen → Nudge nach 15 Min.

        6 Bedingungen gegen False Positives:
        1. Task ist in review (Caller prueft)
        2. review_decision is None (Caller prueft)
        3. run_control nicht stopped/manual_hold (Caller prueft)
        4. Reviewer hat kommentiert
        5. Kommentar > 15 Min alt
        6. Kein Operator-Kommentar danach
        """
        from app.models.task import TaskComment
        from sqlalchemy import func

        # 4. Reviewer hat kommentiert?
        reviewer_comments = await session.exec(
            select(TaskComment)
            .where(
                TaskComment.task_id == task.id,
                TaskComment.author_agent_id == task.assigned_agent_id,
            )
            .order_by(TaskComment.created_at.desc())
            .limit(1)
        )
        last_reviewer_comment = reviewer_comments.first()
        if not last_reviewer_comment:
            return  # Reviewer hat noch nicht kommentiert → normaler Timeout greift

        # 5. Letzter Reviewer-Kommentar > 15 Min alt?
        comment_age_min = (now - last_reviewer_comment.created_at).total_seconds() / 60
        if comment_age_min < 15:
            return  # Reviewer war gerade erst aktiv

        # 6. Nach dem Reviewer-Kommentar gab es KEINEN Operator-Kommentar?
        operator_after = await session.exec(
            select(func.count()).select_from(TaskComment).where(
                TaskComment.task_id == task.id,
                TaskComment.author_type == "user",
                TaskComment.created_at > last_reviewer_comment.created_at,
            )
        )
        if operator_after.one() > 0:
            return  # Operator ist aktiv involviert

        # ── Alle Bedingungen erfuellt → Nudge ──────────────
        dedup_key = f"mc:watchdog:review_decision_missing:{task.id}"
        if await redis.get(dedup_key):
            return  # Bereits genudged, Cooldown laeuft

        # Reviewer via TaskComment nudgen (Pattern A 29-PATTERNS.md)
        reviewer_agent = await session.get(Agent, task.assigned_agent_id)
        if reviewer_agent:
            review_path = f"/api/v1/agent/boards/{task.board_id}/tasks/{task.id}/review"
            msg = (
                f"REVIEW-ENTSCHEIDUNG FEHLT: Du hast Task \"{task.title}\" kommentiert "
                f"aber keine Review-Entscheidung getroffen.\n\n"
                f"Bitte jetzt entscheiden:\n"
                f"POST {review_path} {{\"decision\": \"approve\", \"comment\": \"...\"}}\n"
                f"POST {review_path} {{\"decision\": \"request_changes\", \"comment\": \"...\"}}\n\n"
                f"Ein Kommentar allein schliesst kein Review ab."
            )
            session.add(TaskComment(
                task_id=task.id,
                author_type="system",
                content=msg,
                comment_type="watchdog_notify",
            ))
            await session.commit()

        await redis.set(dedup_key, "1", ex=1800)  # 30 Min Cooldown

        await emit_event(
            session, "task.review_decision_missing",
            f"Review-Entscheidung fehlt: '{task.title}' ({reviewer_agent.name if reviewer_agent else 'unbekannt'}) — Kommentar vor {int(comment_age_min)}min",
            board_id=task.board_id, task_id=task.id,
            severity="warning",
        )
        logger.info(
            "Review decision missing nudge for '%s' (comment %dmin ago)",
            task.title, int(comment_age_min),
        )

    async def _check_undispatched_tasks(self, session: AsyncSession) -> None:
        """Tasks finden die assigned aber nie dispatcht wurden und nachdispatchen.

        WICHTIG: Respektiert dispatch_phase Gate — Tasks in "planning" werden
        NICHT hier dispatcht, sondern nur via Promote-Orchestrator oder manuelles Promote.

        Phase 29: Gateway-Branch entfernt. CLI-bridge Pfad bleibt; andere Runtimes
        (host) werden ueber dispatch.auto_dispatch_task abgewickelt (siehe TODO unten).
        """
        from app.services.dispatch import _build_dispatch_message

        # Grace: Tasks die in den letzten 5s erstellt/promoted wurden nicht anfassen.
        # Verhindert Race mit auto_dispatch_task (Background-Task nach Promote).
        grace_cutoff = utcnow() - timedelta(seconds=5)
        result = await session.exec(
            select(Task).where(
                Task.assigned_agent_id.isnot(None),  # type: ignore[arg-type]
                Task.dispatched_at.is_(None),  # type: ignore[arg-type]
                Task.status.in_(["inbox", "in_progress"]),  # type: ignore[arg-type]
                Task.updated_at < grace_cutoff,  # type: ignore[operator]
                # Planning-Gate: Tasks in planning-Phase NICHT hier dispatchen
                or_(
                    Task.dispatch_phase.is_(None),  # type: ignore[union-attr]
                    Task.dispatch_phase == "ready",
                ),
            )
        )
        tasks = result.all()
        if not tasks:
            return

        dispatched_agents: set[uuid.UUID] = set()

        for task in tasks:
            if not task.assigned_agent_id:
                continue

            if task.assigned_agent_id in dispatched_agents:
                continue

            agent = await session.get(Agent, task.assigned_agent_id)
            if not agent:
                continue

            agent_runtime = getattr(agent, "agent_runtime", "openclaw")

            # CLI-Bridge Agents: eigener Dispatch-Pfad via Bridge
            if agent_runtime in ("free-code-bridge", "cli-bridge"):
                # Busy-Check analog zu auto_dispatch_task: cli-bridge/host haben
                # keine isolierten Sessions — wenn Agent schon einen Task dispatched
                # oder in_progress hat, NICHT ueberschreiben. Sonst /clear → Context-Loss.
                # (Bug 8 vom 2026-04-22: Undispatched-Recovery umging den Push-Dispatch
                # Busy-Check und lieferte Task B aus obwohl Task A noch nicht ack'd war.)
                existing_busy = await session.exec(
                    select(Task).where(
                        Task.assigned_agent_id == agent.id,
                        Task.id != task.id,
                        or_(
                            Task.status == "in_progress",
                            (Task.status == "inbox") & (Task.dispatched_at.isnot(None)),  # type: ignore[union-attr,arg-type]
                        ),
                    )
                )
                if existing_busy.first():
                    logger.info(
                        "Undispatched recovery skipped (CLI bridge busy): '%s' -> %s",
                        task.title, agent.name,
                    )
                    dispatched_agents.add(agent.id)  # Einmal pro Agent pro Tick
                    continue

                try:
                    message = await _build_dispatch_message(task, agent, session)
                    from app.services.cli_bridge_runner import dispatch_to_cli_bridge
                    started = await dispatch_to_cli_bridge(agent, task, message, session)
                    if started:
                        task.dispatched_at = utcnow()
                        task.updated_at = utcnow()
                        agent.run_state = "running"
                        session.add(task)
                        session.add(agent)
                        await session.commit()
                        dispatched_agents.add(agent.id)
                        logger.info("Undispatched recovery (CLI bridge): '%s' -> %s", task.title, agent.name)
                except Exception as e:
                    logger.warning("Undispatched recovery failed (CLI bridge) '%s': %s", task.title, e)
                continue

            # Phase 29: host/manual/claude-code etc. — delegate to auto_dispatch_task
            # (runtime-aware path). Gateway-Branch (chat-send / chat-send-isolated /
            # spawn-session-key) wurde entfernt. Tests vorhanden in dispatch suite.
            busy_result = await session.exec(
                select(Task).where(
                    Task.assigned_agent_id == agent.id,
                    Task.id != task.id,
                    or_(
                        Task.status == "in_progress",
                        and_(Task.status == "inbox", Task.dispatched_at.isnot(None)),  # type: ignore[arg-type]
                    ),
                )
            )
            if busy_result.first():
                continue

            try:
                from app.services.dispatch import auto_dispatch_task
                import asyncio as _asyncio
                _asyncio.create_task(auto_dispatch_task(task.id, task.board_id))
                dispatched_agents.add(agent.id)
                logger.info(
                    "Undispatched recovery (%s): '%s' -> %s via auto_dispatch_task",
                    agent_runtime, task.title, agent.name,
                )
                await emit_event(
                    session, "task.undispatched_recovery",
                    f"Nachdispatcht: '{task.title}' -> {agent.name} (war {task.status} ohne dispatch)",
                    board_id=task.board_id, task_id=task.id, agent_id=agent.id,
                )
            except Exception as e:
                logger.warning("Undispatched recovery failed for '%s' -> %s: %s", task.title, agent.name, e)

    # Phase 29: _check_spawn_timeouts entfernt — operierte auf task.spawn-session-key
    # (gateway-only) und sessions-list (Gateway-API). Cli-bridge agents haben kein
    # spawn-session-key Konzept. TODO Phase 31: cli-bridge task-queue timeouts.

    async def _recover_orphaned_tasks(self, session: AsyncSession) -> int:
        """Tasks die in 'in_progress' feststecken ohne Agent-Heartbeat zurueck auf 'inbox' setzen.

        Bedingungen:
        - Task.status == "in_progress"
        - Task.updated_at > 30 Minuten her
        - Zugewiesener Agent hat keinen Heartbeat in den letzten 30 Minuten gesendet

        Gibt die Anzahl zurueckgesetzter Tasks zurueck.
        """
        from app.utils import ensure_aware

        cutoff = utcnow() - timedelta(minutes=30)

        result = await session.exec(
            select(Task).where(
                Task.status == "in_progress",
                Task.updated_at < cutoff,  # type: ignore[operator]
                Task.assigned_agent_id.isnot(None),  # type: ignore[arg-type]
            )
        )
        stuck_tasks = result.all()

        if not stuck_tasks:
            return 0

        recovered = 0
        now = utcnow()

        for task in stuck_tasks:
            if not task.assigned_agent_id:
                continue

            agent = await session.get(Agent, task.assigned_agent_id)
            if agent and agent.last_seen_at:
                last_seen = ensure_aware(agent.last_seen_at)
                # Agent hat in den letzten 30 Minuten einen Heartbeat gesendet → ueberspringen
                if (now - last_seen).total_seconds() < 1800:
                    continue

            # Task zurueck auf inbox setzen
            old_status = task.status
            task.status = "inbox"
            task.updated_at = now
            session.add(task)
            recovered += 1

            # Task-Event loggen
            try:
                from app.services.task_lifecycle import record_task_event
                await record_task_event(
                    session, task.id, old_status, "inbox",
                    changed_by="watchdog", reason="orphan_recovery",
                )
            except Exception as e:
                logger.debug("record_task_event failed for orphan recovery: %s", e)

            await emit_event(
                session, "task.orphan_recovered",
                f"Orphan-Recovery: '{task.title}' → inbox (Agent inaktiv)",
                board_id=task.board_id, task_id=task.id, agent_id=task.assigned_agent_id,
                severity="warning",
            )
            logger.info(
                "Orphan recovery: '%s' reset to inbox (agent=%s, stuck=%dmin)",
                task.title,
                agent.name if agent else "unknown",
                int((now - ensure_aware(task.updated_at)).total_seconds() / 60) if task.updated_at else 0,
            )

        if recovered > 0:
            await session.commit()

        return recovered
