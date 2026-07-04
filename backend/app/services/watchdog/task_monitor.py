"""Task Monitor Mixin — phase completion, review/blocked/zombie checks, recovery.

Phase 29 (Gateway sunset): all Gateway RPC notifies replaced by TaskComment
writes (Pattern A from 29-PATTERNS.md). poll.sh / launchd-host deliver the
comments via /agent/me/comments. Legacy queue paths (_recover_aborted_tasks,
_process_task_queues, _process_pending_dispatches, _check_spawn_timeouts) were
removed — they all operated on Gateway concepts (spawn-session-key,
sessions-list). Stale-task ownership lies with task_runner._check_dispatch_ack.
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
    """Task-related watchdog checks."""

    async def _check_phase_completions(self, session: AsyncSession) -> None:
        """Check whether all subtasks of a phase are completed."""
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
            # Phase-approval tasks are internal and don't count as regular phase subtasks
            subtasks = [s for s in all_subtasks if s.delegation_type != "phase_approval"]

            if not subtasks:
                continue

            all_done = all(s.status == "done" for s in subtasks)
            if not all_done:
                continue

            # Idempotency: content hash of the child set + status.
            # Fires again when status changes (e.g. after rejection + rework).
            import hashlib
            child_fingerprint = hashlib.md5(
                ",".join(sorted(f"{s.id}:{s.status}" for s in subtasks)).encode()
            ).hexdigest()
            redis = await get_redis()
            dedup_key = f"mc:watchdog:phase_done:{parent.id}"
            stored = await redis.get(dedup_key)
            if stored and stored == child_fingerprint:
                continue  # Same child set, already fired (Redis cache)

            # DB-level dedup: Redis-restart-proof.
            # Check whether a phase_completed event with the same fingerprint already exists.
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
                # Phase completion already fired (DB fallback) — restore Redis key
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

            # Send screenshots from children to Telegram
            requirements = (parent.report_back_requirements or "").lower()
            if "screenshot" in requirements or "visual" in requirements or "before_after" in requirements:
                _parent_id = parent.id
                _child_ids = [s.id for s in subtasks]
                _create_background_task(
                    self._send_phase_screenshots_bg(_parent_id, _child_ids)
                )

            # Phase approval: instead of going straight to review + Rex handoff, a phase-approval
            # task is created for the Board Lead (Boss). Boss decides: approved (→ parent review
            # → operator) or rewrite (→ subtasks re-open). Falls back to Rex handoff if no
            # Board Lead exists.
            if parent.status == "in_progress":
                # Idempotency guard: phase was already approved AND the current
                # child set is exactly what was approved. Skip re-creation —
                # the stuck_orchestrator_close path nudges Boss to close instead.
                #
                # Important for rewrite-path correctness:
                # If Boss issued a `phase_rewrite_request` and workers
                # reworked the subtasks, the current child set is NOT
                # what was approved anymore. In that case a new approval
                # must be created (fingerprint-unchanged means: subtask IDs
                # + status unchanged since approval; if a rewrite happened,
                # the status flipped to inbox in between and back to done
                # → latest subtask.updated_at > approval.completed_at).
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
                    # Check: was ANY subtask modified AFTER the approval?
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
                    # else: child set changed (e.g. rewrite) → let it fall through,
                    # `_existing_phase_approval_for_parent` in the create call handles idempotency

                # Find Board Lead
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
                    # No Board Lead → legacy Rex handoff
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

            # Dedup with fingerprint — stays permanent until child set changes
            await redis.set(dedup_key, child_fingerprint)

            # Auto-advance: only start the next phase if the current phase is REALLY
            # complete (done). Not for review/user_test — the review/test gate
            # is still running there.
            if parent.status == "done":
                await self._auto_advance_next_phase(session, parent)

            if parent.project_id:
                await self._update_project_progress(session, parent.project_id)

    async def _send_phase_screenshots_bg(
        self, parent_id: "uuid.UUID", child_ids: list["uuid.UUID"]
    ) -> None:
        """Send screenshot artifacts from children to Telegram.

        Runs as a background task with its own DB session (the watchdog
        session may already be closed by this point).
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
                seen_paths: set[str] = set()  # Deduplicate: send the same path only once
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
        """Safety net + auto-close for stuck parents after phase_approved.

        Problem (2026-04-22 live tests x3): On trust-by-default boards, the
        parent stays `in_progress` after `phase_approved` until the orchestrator
        calls `mc done`. If it forgets or confuses task IDs (PATCH
        on subtask instead of parent), the task hangs permanently.

        Two-stage safety net:
        1. **Nudge phase** (starting 3 min after updated_at): active re-reminder via
           system comment (poll.sh) OR RPC. Max 2 nudges, 3 min apart.
        2. **Auto-close phase** (after 2 unresolved nudges ~= 6 min): backend
           sets the parent directly to `review`. Event + operator notification. After
           that, the operator can manually set `done` if the output is ok.

        Conditions (now more liberal than PR #59):
        - Root task (parent_task_id IS NULL), status=in_progress
        - At least 1 subtask with delegation_type=phase_approval + status=done
        - All non-approval subtasks done, no OPEN approval (no race with
          a currently running repeat phase approval)
        - updated_at > 3 min ago

        Removed filters (were too narrow):
        - `report_back_required=True` — previously, normal tasks without
          a Telegram hard gate were ignored. Now: all stuck parents.
        - Lead runtime filter (Phase 29-30, Gateway sunset) — previously
          host-runtime leads (Boss) were skipped. Now: all lead types get
          nudged (system-comment-based, runtime-agnostic).

        Idempotency: Redis counter mc:watchdog:stuck_orch_close_count:{id} (TTL 1h).
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

            # If there's an OPEN approval ALONGSIDE the done approval
            # (inbox/in_progress/review), Boss is in a new phase — skip
            # so we don't nudge in parallel to a running approval.
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

            # Nudge counter: how many times have we already nudged this parent?
            count_key = f"mc:watchdog:stuck_orch_close_count:{parent.id}"
            try:
                nudge_count = int(await redis.get(count_key) or 0)
            except Exception:
                nudge_count = 0

            # Dedup key per nudge iteration (prevents 2 nudges in the same watchdog loop)
            dedup_key = f"mc:watchdog:stuck_orch_close:{parent.id}"
            if await redis.get(dedup_key):
                continue

            # Find Board Lead (orchestrator)
            bl_result = await session.exec(
                select(Agent).where(
                    Agent.board_id == parent.board_id,
                    Agent.is_board_lead == True,  # noqa: E712
                )
            )
            lead = bl_result.first()
            if not lead:
                continue

            # ── Auto-close after 2 unresolved nudges ──
            # Logic: 1st nudge at iter 1 (t=3min), 2nd nudge at iter 2 (t=6min).
            # If we're now at count=2 and the parent is STILL stuck,
            # we auto-close (review). The operator makes the final decision (done).
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

                # Notify operator via reports bot (idempotent via escalation key)
                try:
                    from app.services.task_lifecycle import _escalate_orch_close_to_mark
                    await _escalate_orch_close_to_mark(parent, lead, nudge_count)
                except Exception as e:
                    logger.warning("Auto-close mark notification failed: %s", e)

                # Reset counter — auto-close ended the cycle.
                try:
                    await redis.delete(count_key)
                except Exception:
                    pass
                continue

            # ── Otherwise: normal nudge ──
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

            # Counter increment (TTL 1h — parent should be completed long before that)
            try:
                new_count = int(await redis.incr(count_key))
                if new_count == 1:
                    await redis.expire(count_key, 3600)
            except Exception:
                new_count = nudge_count + 1

            await redis.set(dedup_key, "1", ex=180)  # 3 min — matches watchdog interval
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
        """Notify Board Lead in a structured way: all subtasks done, decision needed.

        Phase 29: gateway-agent-id gate removed. Find lead via is_board_lead flag;
        notify via TaskComment (poll.sh / launchd-host deliver it).
        """
        if not parent_task.board_id:
            return

        # Find lead (assigned agent or Board Lead)
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

        # Build subtask summary + evidence
        subtask_lines = []
        evidence_lines = []
        for s in subtasks:
            agent = await session.get(Agent, s.assigned_agent_id) if s.assigned_agent_id else None
            agent_name = agent.name if agent else "unbekannt"
            subtask_lines.append(f"  - {s.title} ({agent_name}, {s.status})")
            # Collect evidence from resolution/review comments
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

        # Report-back is NO LONGER handled by the lead — the executing agent
        # must send `mc telegram` before `mc done` itself (hard gate in agent_scoped.py).
        # Lead only gets the completion info.
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

        # TaskComment as runtime-agnostic delivery (Pattern A 29-PATTERNS.md)
        session.add(TaskComment(
            task_id=parent_task.id,
            author_type="system",
            content=message,
            comment_type="watchdog_notify",
        ))
        # Set dispatched_at so _check_undispatched_tasks doesn't dispatch
        # the root again (race prevention).
        # Reset dispatch_attempt_id: Board Lead works in the
        # main session without an attempt header.
        parent_task.dispatched_at = utcnow()
        session.add(parent_task)
        await session.commit()

        from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
        await clear_dispatch_attempt_id(
            session, parent_task,
            caller="watchdog_phase_complete",
            reason="parent_redispatched_to_board_lead",
        )

        # Fallback timer removed (2026-04-22): the executing agent now sends
        # directly via `mc telegram` before `mc done` (hard gate enforces this).
        # Auto-draft on failed handles the failure case.

    async def _update_project_progress(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        """Update progress_pct based on completed subtasks."""
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
        """Check whether all phases of a project are complete → status review."""
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
        """Automatically start the next phase when the previous one is done."""
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

        # Dispatch subtasks of the new phase (always on auto-advance)
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

    # Phase 29: _recover_aborted_tasks removed — operated on
    # has_session check (Gateway sessions-list) and Gateway RPC chat-send.
    # Stale/aborted tasks are now caught via task_runner._check_dispatch_ack
    # and _recover_orphaned_tasks (heartbeat-based).

    # Phase 29: _process_task_queues + _process_pending_dispatches removed —
    # both were gateway-only (queue_length, sessions-list, Gateway RPC).
    # CLI-bridge agents have their own poll.sh queue; re-dispatch runs
    # via dispatch.auto_dispatch_task (runtime-aware).

    async def _check_blocked_tasks(self, session: AsyncSession) -> None:
        """Blocked-Task-Leiter (Fix A):

        1. Callback-Waits (blocked_by_task_id) sind Orchestrierung — skip.
        2. Pending Approval vorhanden → Operator ist dran; nur Reminder-Event.
        3. Kein Approval → Lead-Triage laeuft. Nach Ablauf des Board-
           Triage-Fensters eskaliert der Watchdog an den Operator
           (blocker_decision-Approval + Telegram).
        """
        from app.models.approval import Approval
        from app.models.board import Board
        from app.services.blocker_triage import escalate_blocker_to_operator

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
            # Orchestrierungs-Wait: Callback-Resume kuemmert sich, kein Fall
            # fuer Lead oder Operator.
            if task.blocked_by_task_id is not None:
                continue

            age_minutes = (now - task.updated_at).total_seconds() / 60

            # Offene Approvals (Blocker ODER Klaerungsfrage) → Fall liegt
            # bereits beim Operator; nur periodischer Reminder.
            pending_approval = (await session.exec(
                select(Approval).where(
                    Approval.task_id == task.id,
                    Approval.action_type.in_(  # type: ignore[union-attr]
                        ("blocker_decision", "clarification_question")
                    ),
                    Approval.status == "pending",
                )
            )).first()

            assigned_name = "unbekannt"
            if task.assigned_agent_id:
                assigned_agent = await session.get(Agent, task.assigned_agent_id)
                if assigned_agent:
                    assigned_name = assigned_agent.name

            if pending_approval:
                if age_minutes < 30:
                    continue
                dedup_key = f"mc:watchdog:blocked_remind:{task.id}"
                if await redis.get(dedup_key):
                    continue
                await redis.set(dedup_key, "1", ex=7200)  # 2h TTL
                await emit_event(
                    session, "task.blocked_reminder",
                    f"Blocked-Reminder: '{task.title}' ({assigned_name}) — {int(age_minutes)}min — Approval pending",
                    board_id=task.board_id, task_id=task.id,
                )
                logger.info(
                    "Blocked reminder (approval pending) for '%s' (%dmin)",
                    task.title, int(age_minutes),
                )
                continue

            # Lead-Triage laeuft: eskalieren, sobald das Board-Fenster um ist.
            # (Legacy-Bestand ohne Triage-Payload eskaliert genauso — der
            # Kommentar-Fallback in escalate_blocker_to_operator greift.)
            board = await session.get(Board, task.board_id)
            triage_minutes = getattr(board, "blocker_triage_minutes", 15) if board else 15
            if age_minutes < max(triage_minutes, 1):
                continue

            # Dedup: Eskalation nur einmal pro 2h anstossen (escalate selbst
            # ist zusaetzlich idempotent gegen existierende pending Approvals).
            dedup_key = f"mc:watchdog:blocker_escalated:{task.id}"
            if await redis.get(dedup_key):
                continue
            await redis.set(dedup_key, "1", ex=7200)

            approval = await escalate_blocker_to_operator(
                session, task=task, reason="triage_timeout",
            )
            if approval:
                logger.info(
                    "Blocker-Triage abgelaufen (%dmin > %dmin): '%s' → Operator",
                    int(age_minutes), triage_minutes, task.title,
                )

    async def _check_dependency_zombies(self, session: AsyncSession) -> None:
        """Find tasks waiting on impossible dependencies (zombie prevention).

        A task is a zombie if it is inbox/in_progress and a dependency
        is in a terminal failure status (failed, blocked). That dependency
        will never become done → the task waits forever.

        Action: create an approval so the operator can resolve the dependency.
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
            # Only check if the dependent task is still actively waiting
            task = await session.get(Task, dep.task_id)
            if not task or task.status not in ("inbox", "in_progress"):
                continue

            # Check dependency task
            dep_task = await session.get(Task, dep.depends_on_task_id)
            if not dep_task or dep_task.status not in ("failed", "blocked"):
                continue

            # Approval needs agent_id — skip if task isn't assigned to an agent
            if not task.assigned_agent_id:
                continue

            # Dedup: only once per dependency pair
            dedup_key = RedisKeys.recovery_attempt(str(task.id), "dependency_zombie")
            if await redis.get(dedup_key):
                continue

            # Check whether an approval already exists
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
        """Monitor review tasks: timeout escalation + decision-missing nudge.

        Timeout escalation (existing, with HOLD exception):
        1. 60 min without update → nudge reviewer (RPC push)
        2. 120 min → notify lead
        3. 180 min → create approval for the operator

        Decision-missing (new):
        Reviewer commented but made no decision → nudge after 15 min.
        """
        from app.models.task import TaskComment
        from app.models.board import Board
        from sqlalchemy import func

        # Archived boards are cleaned up — their tasks must never escalate.
        # Otherwise e.g. a demo-seed review task (no reviewer, board later
        # archived) would hang forever at >180min and fire a Discord warning every 2h.
        result = await session.exec(
            select(Task)
            .join(Board, Board.id == Task.board_id)
            .where(Task.status == "review", Board.is_archived == False)  # noqa: E712
        )
        review_tasks = result.all()

        if not review_tasks:
            return

        now = utcnow()
        redis = await get_redis()

        for task in review_tasks:
            if not task.board_id or not task.updated_at:
                continue

            # ── HOLD exception: deliberately paused → skip escalation entirely ──
            if task.review_decision == "hold":
                continue

            # ── Don't escalate operationally stopped tasks ──
            if task.run_control in ("stopped", "manual_hold"):
                continue

            # ── Decision-missing check ──────────────────────────────────────
            # If decision is missing and reviewer has already commented
            if task.review_decision is None and task.assigned_agent_id:
                await self._check_review_decision_missing(
                    session, task, now, redis,
                )

            # ── Existing timeout escalation ──────────────────────────────
            # Only if no decision has been made yet (approved/changes_requested
            # means the task shouldn't be in review anymore → inconsistency, ignore)
            if task.review_decision in ("approved", "changes_requested"):
                continue

            age_minutes = (now - task.updated_at).total_seconds() / 60
            if age_minutes < 60:
                continue

            # Dedup key per escalation level
            if age_minutes >= 180:
                escalation_level = "approval"
            elif age_minutes >= 120:
                escalation_level = "lead"
            else:
                escalation_level = "nudge"

            dedup_key = f"mc:watchdog:review_{escalation_level}:{task.id}"
            if await redis.get(dedup_key):
                continue

            # Get reviewer agent
            reviewer_name = "unbekannt"
            reviewer_agent = None
            if task.assigned_agent_id:
                reviewer_agent = await session.get(Agent, task.assigned_agent_id)
                if reviewer_agent:
                    reviewer_name = reviewer_agent.name

            if escalation_level == "nudge":
                # Level 1: nudge reviewer via TaskComment (Pattern A 29-PATTERNS.md)
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
                # Level 2: notify lead via TaskComment
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
                # Level 3: create approval for the operator
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
        """Reviewer commented but made no decision → nudge after 15 min.

        6 conditions to guard against false positives:
        1. Task is in review (caller checks)
        2. review_decision is None (caller checks)
        3. run_control not stopped/manual_hold (caller checks)
        4. Reviewer has commented
        5. Comment > 15 min old
        6. No operator comment afterward
        """
        from app.models.task import TaskComment
        from sqlalchemy import func

        # 4. Has the reviewer commented?
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
            return  # Reviewer hasn't commented yet → normal timeout applies

        # 5. Is the last reviewer comment > 15 min old?
        comment_age_min = (now - last_reviewer_comment.created_at).total_seconds() / 60
        if comment_age_min < 15:
            return  # Reviewer was just active

        # 6. Was there NO operator comment after the reviewer comment?
        operator_after = await session.exec(
            select(func.count()).select_from(TaskComment).where(
                TaskComment.task_id == task.id,
                TaskComment.author_type == "user",
                TaskComment.created_at > last_reviewer_comment.created_at,
            )
        )
        if operator_after.one() > 0:
            return  # Operator is actively involved

        # ── All conditions met → nudge ──────────────
        dedup_key = f"mc:watchdog:review_decision_missing:{task.id}"
        if await redis.get(dedup_key):
            return  # Already nudged, cooldown running

        # Nudge reviewer via TaskComment (Pattern A 29-PATTERNS.md)
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

        await redis.set(dedup_key, "1", ex=1800)  # 30 min cooldown

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
        """Find tasks that are assigned but were never dispatched, and re-dispatch them.

        IMPORTANT: Respects the dispatch_phase gate — tasks in "planning" are
        NOT dispatched here, only via the promote orchestrator or manual promote.

        Phase 29: Gateway branch removed. CLI-bridge path remains; other runtimes
        (host) are handled via dispatch.auto_dispatch_task (see TODO below).
        """
        from app.services.dispatch import _build_dispatch_message

        # Grace: don't touch tasks created/promoted in the last 5s.
        # Prevents a race with auto_dispatch_task (background task after promote).
        grace_cutoff = utcnow() - timedelta(seconds=5)
        result = await session.exec(
            select(Task).where(
                Task.assigned_agent_id.isnot(None),  # type: ignore[arg-type]
                Task.dispatched_at.is_(None),  # type: ignore[arg-type]
                Task.status.in_(["inbox", "in_progress"]),  # type: ignore[arg-type]
                Task.updated_at < grace_cutoff,  # type: ignore[operator]
                # Planning gate: do NOT dispatch tasks in the planning phase here
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

            # CLI-bridge agents: own dispatch path via bridge
            if agent_runtime in ("free-code-bridge", "cli-bridge"):
                # Busy check analogous to auto_dispatch_task: cli-bridge/host have
                # no isolated sessions — if the agent already has a task dispatched
                # or in_progress, DO NOT overwrite. Otherwise /clear → context loss.
                # (Bug 8 from 2026-04-22: undispatched recovery bypassed the push-dispatch
                # busy check and delivered task B even though task A wasn't acked yet.)
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
                    dispatched_agents.add(agent.id)  # Once per agent per tick
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
            # (runtime-aware path). Gateway branch (chat-send / chat-send-isolated /
            # spawn-session-key) was removed. Tests exist in dispatch suite.
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

    # Phase 29: _check_spawn_timeouts removed — operated on task.spawn-session-key
    # (gateway-only) and sessions-list (Gateway API). CLI-bridge agents have no
    # spawn-session-key concept. TODO Phase 31: cli-bridge task-queue timeouts.

    async def _recover_orphaned_tasks(self, session: AsyncSession) -> int:
        """Reset tasks stuck in 'in_progress' without an agent heartbeat back to 'inbox'.

        Conditions:
        - Task.status == "in_progress"
        - Task.updated_at > 30 minutes ago
        - Assigned agent hasn't sent a heartbeat in the last 30 minutes

        Returns the number of reset tasks.
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
                # Agent sent a heartbeat in the last 30 minutes → skip
                if (now - last_seen).total_seconds() < 1800:
                    continue

            # Reset task back to inbox
            old_status = task.status
            task.status = "inbox"
            task.updated_at = now
            session.add(task)
            recovered += 1

            # Log task event
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
