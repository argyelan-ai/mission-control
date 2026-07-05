"""Loop-Runner — Meta-Controller für ergebnisgesteuerte Task-Schleifen (ADR-051, L1).

Der Runner führt selbst NICHTS aus. Pro Runde erzeugt er einen normalen
Parent-Task (Board-Lead-first via create_task_internal, kein assigned_agent_id)
und beobachtet dessen Ausgang. Danach entscheidet er:

    Runde terminal → auswerten (LoopRound.outcome + Report)
      → Circuit-Breaker (N Fehlrunden in Folge → paused + loop_gate-Approval)
      → Stop-Bedingungen (max_rounds, max_duration, backlog_empty bei project)
      → Human-Gate (human_every_n_rounds → waiting_gate + loop_gate-Approval)
      → sonst: nächste Runde starten.

Leitplanken (Workspace-Praxis): jede Runde läuft durch die vollen Gates der
Task-Pipeline (Review-Pflicht, Watchdog, Approvals); der Runner startet keine
neue Runde, solange die letzte nicht terminal ist; 1 aktiver Loop pro Board.
"""

import asyncio
import logging
from datetime import timedelta

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.approval import Approval
from app.models.loop import ACTIVE_LOOP_STATUSES, Loop, LoopRound, TERMINAL_LOOP_STATUSES
from app.models.tag import Tag, TagAssignment
from app.models.task import Task, TaskComment
from app.redis_client import get_redis
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger("mc.loop_runner")

# "aborted" ist terminal: sonst hängt der Loop für immer an einer
# abgebrochenen Runde (Stop-Bedingungen werden nur an Rundengrenzen geprüft).
TERMINAL_TASK_STATUSES = ("done", "failed", "aborted")
REPORT_HISTORY_ROUNDS = 3  # wie viele Runden-Reports in den nächsten Brief wandern
TAG_BACKLOG_LIMIT = 20  # wie viele offene Tag-Tasks maximal in den Brief wandern

LOCK_KEY = "mc:loop_runner:cycle_lock"


class LoopAlreadyActiveError(Exception):
    """Auf dem Board läuft bereits ein anderer aktiver Loop (1 pro Board, ADR-051)."""

    def __init__(self, other_loop_name: str) -> None:
        self.other_loop_name = other_loop_name
        super().__init__(
            f"Auf diesem Board läuft bereits Loop '{other_loop_name}' — "
            "nur 1 aktiver Loop pro Board"
        )


async def active_loop_on_board(
    session: AsyncSession, board_id, exclude=None,
) -> Loop | None:
    query = select(Loop).where(
        Loop.board_id == board_id,
        Loop.status.in_(ACTIVE_LOOP_STATUSES),  # type: ignore[attr-defined]
    )
    if exclude:
        query = query.where(Loop.id != exclude)
    return (await session.exec(query)).first()


async def supersede_pending_gates(session: AsyncSession, loop_id) -> None:
    """Operator-Aktion (UI oder Scheduler-Trigger) ersetzt offene loop_gate-Approvals."""
    pending = (await session.exec(
        select(Approval).where(
            Approval.action_type == "loop_gate",
            Approval.status == "pending",
        )
    )).all()
    for a in pending:
        if (a.payload or {}).get("loop_id") == str(loop_id):
            a.status = "superseded"
            a.resolved_at = utcnow()
            session.add(a)


async def start_loop(session: AsyncSession, loop: Loop) -> Loop:
    """Startet einen Loop (draft/paused/waiting_gate → running).

    Geteilter Pfad für den Router-Endpoint (Operator-UI) UND den
    Scheduler-Trigger `start_loop`-Action (ADR-051 L2) — beide sollen exakt
    dasselbe Verhalten haben statt eine zweite Implementierung zu pflegen.
    Raises ValueError bei ungültigem Status, LoopAlreadyActiveError bei
    Board-Konflikt — der Aufrufer übersetzt das in HTTPException bzw.
    einen Job-Fehler.
    """
    if loop.status not in ("draft", "paused", "waiting_gate"):
        raise ValueError(f"Loop kann aus Status '{loop.status}' nicht gestartet werden")

    other = await active_loop_on_board(session, loop.board_id, exclude=loop.id)
    if other:
        raise LoopAlreadyActiveError(other.name)

    loop.status = "running"
    if loop.started_at is None:
        loop.started_at = utcnow()
    loop.consecutive_failed_rounds = 0
    loop.updated_at = utcnow()
    await supersede_pending_gates(session, loop.id)
    session.add(loop)
    await session.commit()
    await session.refresh(loop)
    await emit_event(
        session, "loop.started",
        f"Loop '{loop.name}' gestartet ({loop.rounds_completed}/{loop.max_rounds} Runden)",
        board_id=loop.board_id, detail={"loop_id": str(loop.id)},
    )
    return loop


def _short(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class LoopRunnerService:
    """Singleton nach Watchdog-Muster: Intervall-Tick mit Per-Cycle-Redis-Lock."""

    def __init__(self, interval: int = 30) -> None:
        self.interval = interval
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Loop-Runner gestartet (Intervall %ss)", self.interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Loop-Runner gestoppt")

    async def _run_loop(self) -> None:
        await asyncio.sleep(15)  # Boot-Grace, DB/Redis hochfahren lassen
        while self._running:
            try:
                redis = await get_redis()
                got_lock = await redis.set(
                    LOCK_KEY, "1", nx=True, ex=self.interval * 3
                )
                if got_lock:
                    async with AsyncSession(engine, expire_on_commit=False) as session:
                        await self.tick(session)
                    await redis.delete(LOCK_KEY)
            except Exception:  # noqa: BLE001 — ein Fehler darf den Runner nie killen
                logger.exception("Loop-Runner-Tick fehlgeschlagen")
            await asyncio.sleep(self.interval)

    # ── Kern-Tick (separat aufrufbar für Tests) ─────────────────────────

    async def tick(self, session: AsyncSession) -> None:
        result = await session.exec(select(Loop).where(Loop.status == "running"))
        for loop in list(result.all()):
            try:
                await self._advance(session, loop)
            except Exception:  # noqa: BLE001
                logger.exception("Loop %s: advance fehlgeschlagen", loop.id)
                # Session sauber halten — sonst failen alle Folgeloops
                # dieses Ticks an der abgebrochenen Transaktion (Review M2).
                try:
                    await session.rollback()
                except Exception:  # noqa: BLE001
                    pass

    async def _advance(self, session: AsyncSession, loop: Loop) -> None:
        if loop.current_task_id is None:
            # Frisch gestartet oder nach Gate/Resume: nächste Runde fällig.
            await self._start_round(session, loop)
            return

        task = await session.get(Task, loop.current_task_id)
        if task is None:
            # Runden-Task wurde gelöscht → als Fehlrunde werten.
            await self._complete_round(session, loop, outcome="failed",
                                       note="Runden-Task wurde gelöscht")
            return
        if task.status not in TERMINAL_TASK_STATUSES:
            return  # Runde läuft — volle Gates der Task-Pipeline gelten.

        await self._complete_round(session, loop, outcome=task.status, task=task)

    # ── Runde starten ───────────────────────────────────────────────────

    async def _start_round(self, session: AsyncSession, loop: Loop) -> None:
        from app.services.task_create import create_task_internal

        round_no = loop.current_round_no + 1
        brief = await self._build_round_brief(session, loop, round_no)
        title = f"Loop round {round_no}/{loop.max_rounds}: {loop.name}"

        task = await create_task_internal(
            session,
            board_id=loop.board_id,
            title=title,
            description=brief,
            project_id=loop.project_id,
            is_auto_created=True,
            auto_reason=f"loop:{loop.id}:round:{round_no}",
            # KEIN assigned_agent_id → Board-Lead-first-Dispatch greift.
        )

        session.add(LoopRound(
            loop_id=loop.id, round_no=round_no, task_id=task.id,
            started_at=utcnow(),
        ))
        loop.current_round_no = round_no
        loop.current_task_id = task.id
        loop.updated_at = utcnow()
        session.add(loop)
        await session.commit()

        await emit_event(
            session, "loop.round_started",
            f"Loop '{loop.name}': Runde {round_no}/{loop.max_rounds} gestartet",
            board_id=loop.board_id, task_id=task.id,
            detail={"loop_id": str(loop.id), "round_no": round_no},
        )

    async def _build_round_brief(
        self, session: AsyncSession, loop: Loop, round_no: int,
    ) -> str:
        parts = [
            f"# Loop: {loop.name} — Runde {round_no}/{loop.max_rounds}",
            "",
            "## Ziel des Loops",
            loop.goal.strip(),
        ]

        # Backlog-Quelle
        if loop.backlog_source == "markdown" and loop.backlog_md:
            parts += ["", "## Backlog",
                      loop.backlog_md.strip(),
                      "",
                      "Nimm das NÄCHSTE noch offene Item aus dem Backlog. "
                      "Genau EIN Item pro Runde — nicht mehrere."]
        elif loop.backlog_source == "project":
            parts += ["", "## Backlog",
                      "Das Backlog sind die offenen Tasks dieses Projekts. "
                      "Nimm den wichtigsten offenen Task als Runden-Thema "
                      "(genau EINEN)."]
        elif loop.backlog_source == "tag":
            tag_tasks = await self._open_tag_backlog(session, loop)
            if tag_tasks:
                listing = "\n".join(f"- {title} (`{tid}`)" for tid, title in tag_tasks)
                parts += ["", "## Backlog",
                          f"Offene Tasks mit Tag `{loop.backlog_tag}` auf diesem Board:",
                          listing, "",
                          "Nimm GENAU EINEN dieser Tasks als Runden-Thema."]
            else:
                parts += ["", "## Backlog",
                          f"Kein offener Task mit Tag `{loop.backlog_tag}` gefunden. "
                          "Prüfe, ob das Tag-Backlog vollständig abgearbeitet ist — "
                          "falls ja, schreibe »BACKLOG LEER« in die Abschluss-Reflexion."]
        else:  # open_ended
            parts += ["", "## Backlog",
                      "Open-ended: Finde selbst das nächste sinnvollste Item "
                      "im Sinne des Loop-Ziels (z.B. den nächsten Bug, die "
                      "nächste Verbesserung). Genau EIN Item pro Runde."]

        if loop.round_brief:
            parts += ["", "## Runden-Anweisungen", loop.round_brief.strip()]

        # Kontinuität: Reports der letzten N Runden
        reports = (await session.exec(
            select(LoopRound)
            .where(LoopRound.loop_id == loop.id, LoopRound.report != None)  # noqa: E711
            .order_by(LoopRound.round_no.desc())  # type: ignore[union-attr]
            .limit(REPORT_HISTORY_ROUNDS)
        )).all()
        if reports:
            parts += ["", f"## Reports der letzten {len(reports)} Runden"]
            for r in sorted(reports, key=lambda x: x.round_no):
                parts += [f"### Runde {r.round_no}", (r.report or "").strip()]

        parts += [
            "",
            "## Loop-Kontrakt (BINDEND)",
            "- Diese Runde ist Teil einer autonomen Schleife. Arbeite GENAU ein "
            "Backlog-Item ab — kein Scope-Creep.",
            "- Ist das Backlog vollständig abgearbeitet oder das Loop-Ziel "
            "erreicht, schreibe das EXPLIZIT in die Abschluss-Reflexion "
            "(»BACKLOG LEER« bzw. »ZIEL ERREICHT«).",
            "- Merges/destruktive Aktionen laufen über die normalen Gates.",
        ]
        return "\n".join(parts)

    # ── Runde auswerten ──────────────────────────────────────────────────

    async def _complete_round(
        self, session: AsyncSession, loop: Loop, *,
        outcome: str, task: Task | None = None, note: str = "",
    ) -> None:
        round_row = (await session.exec(
            select(LoopRound).where(
                LoopRound.loop_id == loop.id,
                LoopRound.round_no == loop.current_round_no,
            )
        )).first()

        report = await self._build_round_report(session, loop, outcome, task, note)
        goal_reached = False
        reflection = None
        if task is not None:
            reflection = await self._last_reflection(session, task)
            up = (reflection or "").upper()
            goal_reached = "BACKLOG LEER" in up or "ZIEL ERREICHT" in up

        if round_row:
            round_row.outcome = outcome
            round_row.report = report
            round_row.finished_at = utcnow()
            session.add(round_row)

        loop.rounds_completed += 1
        loop.current_task_id = None
        if outcome == "done":
            loop.consecutive_failed_rounds = 0
        else:
            loop.consecutive_failed_rounds += 1
        loop.updated_at = utcnow()
        session.add(loop)
        await session.commit()

        await emit_event(
            session, "loop.round_completed",
            f"Loop '{loop.name}': Runde {loop.current_round_no} → {outcome}",
            board_id=loop.board_id, task_id=task.id if task else None,
            severity="info" if outcome == "done" else "warning",
            detail={"loop_id": str(loop.id), "round_no": loop.current_round_no,
                    "outcome": outcome},
        )

        if loop.telegram_reports:
            await self._send_round_telegram_report(
                loop, round_no=loop.current_round_no, outcome=outcome,
                reflection=reflection, note=note,
            )

        # 1) Circuit-Breaker: N Fehlrunden in Folge → Pause + Eskalation.
        if loop.consecutive_failed_rounds >= max(loop.pause_on_failed_rounds, 1):
            await self._pause_with_gate(
                session, loop,
                reason="circuit_breaker",
                description=(
                    f"Loop '{loop.name}' pausiert: "
                    f"{loop.consecutive_failed_rounds} Fehlrunden in Folge"
                ),
            )
            return

        # 2) Stop-Bedingungen (an Rundengrenzen geprüft).
        stop_reason = None
        if goal_reached and loop.stop_on_backlog_empty:
            stop_reason = "backlog_empty"
        elif loop.rounds_completed >= loop.max_rounds:
            stop_reason = "max_rounds"
        elif (
            loop.max_duration_minutes and loop.started_at
            and utcnow() - loop.started_at >= timedelta(minutes=loop.max_duration_minutes)
        ):
            stop_reason = "max_duration"
        elif loop.budget_usd is not None or loop.budget_tokens is not None:
            # L3: Budget an der Rundengrenze — Summe der task-attribuierten
            # Usage-Events aller Runden-Tasks. Ohne Attribution ist die
            # Summe 0 (Budget greift dann schlicht nie, kein False-Stop).
            used_tokens, used_usd = await self._loop_usage(session, loop)
            if loop.budget_usd is not None and used_usd >= loop.budget_usd:
                logger.info("Loop '%s': Budget erreicht (%.2f/%.2f USD)",
                            loop.name, used_usd, loop.budget_usd)
                stop_reason = "budget_exceeded"
            elif loop.budget_tokens is not None and used_tokens >= loop.budget_tokens:
                logger.info("Loop '%s': Budget erreicht (%d/%d Tokens)",
                            loop.name, used_tokens, loop.budget_tokens)
                stop_reason = "budget_exceeded"
        if stop_reason:
            await self._finish(session, loop, reason=stop_reason)
            return

        # 3) Human-Gate nach Zeitplan (Default 0 = nie; Marks Entscheid:
        #    Gates nur bei Problemen/Merges — Merges gated die Task-Pipeline).
        if (
            loop.human_every_n_rounds > 0
            and loop.rounds_completed % loop.human_every_n_rounds == 0
        ):
            await self._wait_for_gate(session, loop)
            return

        # 4) Weiter: nächste Runde sofort.
        await self._start_round(session, loop)

    async def _build_round_report(
        self, session: AsyncSession, loop: Loop,
        outcome: str, task: Task | None, note: str,
    ) -> str:
        lines = [f"**Outcome:** {outcome}"]
        if note:
            lines.append(f"**Hinweis:** {note}")
        if task is not None:
            lines.append(f"**Task:** {task.title} (`{task.id}`)")
            from app.models.deliverable import TaskDeliverable
            deliverables = (await session.exec(
                select(TaskDeliverable).where(TaskDeliverable.task_id == task.id)
            )).all()
            if deliverables:
                lines.append(
                    "**Deliverables:** "
                    + "; ".join(_short(d.title, 80) for d in deliverables[:5])
                )
            reflection = await self._last_reflection(session, task)
            if reflection:
                lines.append(f"**Reflexion:** {_short(reflection)}")
        return "\n".join(lines)

    async def _last_reflection(self, session: AsyncSession, task: Task) -> str | None:
        comment = (await session.exec(
            select(TaskComment)
            .where(
                TaskComment.task_id == task.id,
                TaskComment.comment_type.in_(["reflection", "progress"]),  # type: ignore[attr-defined]
            )
            .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
            .limit(1)
        )).first()
        return comment.content if comment else None

    async def _open_tag_backlog(
        self, session: AsyncSession, loop: Loop,
    ) -> list[tuple]:
        """Offene, nicht dispatchte Board-Tasks mit `loop.backlog_tag` (L2)."""
        if not loop.backlog_tag:
            return []
        rows = (await session.exec(
            select(Task.id, Task.title)
            .join(TagAssignment, TagAssignment.task_id == Task.id)
            .join(Tag, Tag.id == TagAssignment.tag_id)
            .where(
                Tag.slug == loop.backlog_tag,
                Task.board_id == loop.board_id,
                Task.status == "inbox",
                Task.dispatched_at.is_(None),  # type: ignore[union-attr]
            )
            .limit(TAG_BACKLOG_LIMIT)
        )).all()
        return list(rows)

    async def _send_round_telegram_report(
        self, loop: Loop, *, round_no: int, outcome: str,
        reflection: str | None, note: str,
    ) -> None:
        """Kompakter Telegram-Report nach jeder Runde (L2, Opt-out via
        `loop.telegram_reports`). Fehler dürfen den Runner nie stören."""
        try:
            from app.services.telegram_reports import telegram_reports
            if not telegram_reports.configured:
                return
            lines = [
                f"🔁 <b>{loop.name}</b> — Runde {round_no}/{loop.max_rounds}: "
                f"<b>{outcome.upper()}</b>",
            ]
            excerpt = _short(reflection or note, 220)
            if excerpt:
                lines.append(excerpt)
            if loop.max_rounds:
                remaining = max(loop.max_rounds - loop.rounds_completed, 0)
                lines.append(
                    f"Verbleibend: {remaining} Runde{'n' if remaining != 1 else ''}"
                )
            await telegram_reports.send("\n".join(lines))
        except Exception as e:  # noqa: BLE001
            logger.warning("Loop-Runden-Telegram-Report fehlgeschlagen: %s", e)

    # ── Gates / Pause / Finish ───────────────────────────────────────────

    async def _create_gate_approval(
        self, session: AsyncSession, loop: Loop, *, reason: str, description: str,
    ) -> Approval:
        approval = Approval(
            board_id=loop.board_id,
            task_id=None,
            agent_id=None,
            action_type="loop_gate",
            description=description,
            payload={
                "loop_id": str(loop.id),
                "loop_name": loop.name,
                "round_no": loop.current_round_no,
                "rounds_completed": loop.rounds_completed,
                "max_rounds": loop.max_rounds,
                "reason": reason,
                "consecutive_failed_rounds": loop.consecutive_failed_rounds,
            },
            expires_at=utcnow() + timedelta(hours=24),
        )
        session.add(approval)
        await session.commit()
        await session.refresh(approval)

        try:
            from app.services.telegram_bot import telegram_bot
            await telegram_bot.send_approval_telegram(
                approval.id, f"Loop '{loop.name}'", description,
                f"Runde {loop.rounds_completed}/{loop.max_rounds} — "
                f"Approve = weiterlaufen, Reject = pausiert lassen.",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Loop-Gate-Telegram fehlgeschlagen: %s", e)
        return approval

    async def _pause_with_gate(
        self, session: AsyncSession, loop: Loop, *, reason: str, description: str,
    ) -> None:
        loop.status = "paused"
        loop.updated_at = utcnow()
        session.add(loop)
        await session.commit()
        await self._create_gate_approval(
            session, loop, reason=reason, description=description,
        )
        await emit_event(
            session, "loop.paused", description,
            board_id=loop.board_id, severity="warning",
            detail={"loop_id": str(loop.id), "reason": reason},
        )

    async def _wait_for_gate(self, session: AsyncSession, loop: Loop) -> None:
        loop.status = "waiting_gate"
        loop.updated_at = utcnow()
        session.add(loop)
        await session.commit()
        await self._create_gate_approval(
            session, loop,
            reason="scheduled_gate",
            description=(
                f"Loop '{loop.name}': Gate nach Runde {loop.rounds_completed} — "
                "weiterlaufen?"
            ),
        )
        await emit_event(
            session, "loop.gate_requested",
            f"Loop '{loop.name}' wartet auf dein Go (Runde {loop.rounds_completed})",
            board_id=loop.board_id, severity="info",
            detail={"loop_id": str(loop.id)},
        )

    async def _loop_usage(self, session, loop) -> tuple[int, float]:
        """Summe (input+output tokens, cost_usd) der Events aller Runden-Tasks.

        Cache-Tokens zählen bewusst NICHT in budget_tokens (sie würden das
        Budget um Größenordnungen verzerren); cost_usd enthält sie über die
        Preisberechnung ohnehin korrekt gewichtet.
        """
        from sqlalchemy import func as sa_func

        from app.models.loop import LoopRound
        from app.models.model_usage import ModelUsageEvent

        task_ids = (await session.exec(
            select(LoopRound.task_id).where(
                LoopRound.loop_id == loop.id, LoopRound.task_id != None,  # noqa: E711
            )
        )).all()
        if not task_ids:
            return 0, 0.0
        row = (await session.exec(
            select(
                sa_func.coalesce(sa_func.sum(
                    ModelUsageEvent.input_tokens + ModelUsageEvent.output_tokens), 0),
                sa_func.coalesce(sa_func.sum(ModelUsageEvent.cost_usd), 0.0),
            ).where(ModelUsageEvent.task_id.in_(task_ids))  # type: ignore[union-attr]
        )).one()
        return int(row[0]), float(row[1])

    async def _finish(
        self, session: AsyncSession, loop: Loop, *, reason: str,
    ) -> None:
        loop.status = "done"
        loop.finished_at = utcnow()
        loop.last_error = None
        loop.updated_at = utcnow()
        session.add(loop)
        await session.commit()
        await emit_event(
            session, "loop.finished",
            f"Loop '{loop.name}' abgeschlossen ({reason}) — "
            f"{loop.rounds_completed} Runden",
            board_id=loop.board_id,
            detail={"loop_id": str(loop.id), "reason": reason},
        )


async def handle_round_task_deleted(session: AsyncSession, task_id) -> None:
    """Von den Task-Delete-Endpoints gerufen, BEVOR der Task gelöscht wird.

    Ein gelöschter Runden-Task ist eine Fehlrunde (ADR-051 §Leitplanken) —
    volle Wertung inkl. Circuit-Breaker/Stop/Gate-Entscheidung. Ohne diesen
    Hook würde das blosse FK-Nullen den Fail-Pfad des Runners umgehen
    (Review-Fund M1). Fehler dürfen den Delete nie blockieren.
    """
    runner = LoopRunnerService()
    loops = (await session.exec(
        select(Loop).where(Loop.current_task_id == task_id))
    ).all()
    for loop in loops:
        try:
            if loop.status == "running":
                await runner._complete_round(
                    session, loop, outcome="failed",
                    note="Runden-Task wurde gelöscht",
                )
            else:
                loop.current_task_id = None
                loop.updated_at = utcnow()
                session.add(loop)
        except Exception:  # noqa: BLE001
            logger.exception("Loop %s: Delete-Wertung fehlgeschlagen — FK wird nur gelöst", loop.id)
            await session.rollback()
            fresh = await session.get(Loop, loop.id)
            if fresh and fresh.current_task_id == task_id:
                fresh.current_task_id = None
                session.add(fresh)

    rounds = (await session.exec(
        select(LoopRound).where(LoopRound.task_id == task_id))
    ).all()
    for lr in rounds:
        lr.task_id = None
        session.add(lr)


async def apply_loop_gate_decision(
    session: AsyncSession, approval: Approval, decision: str,
) -> None:
    """Wendet eine loop_gate-Entscheidung an — geteilter Pfad für
    resolve_approval (UI) UND Telegram-Quick-Resolve.

    approved → Loop läuft weiter (Fehlerserie zurückgesetzt, Runner startet
    die nächste Runde im nächsten Tick); rejected → bleibt pausiert.
    """
    import uuid as _uuid

    _loop_id = (approval.payload or {}).get("loop_id")
    if not _loop_id:
        return
    try:
        loop = await session.get(Loop, _uuid.UUID(str(_loop_id)))
    except (ValueError, TypeError):
        logger.warning("loop_gate mit kaputter loop_id im Payload: %r", _loop_id)
        return
    if loop is None or loop.status not in ("paused", "waiting_gate"):
        return  # Loop weg oder inzwischen anders weitergeschaltet — no-op.

    if decision == "approved":
        loop.status = "running"
        loop.consecutive_failed_rounds = 0
    else:
        loop.status = "paused"
    loop.updated_at = utcnow()
    session.add(loop)
    await session.commit()
    await emit_event(
        session, "loop.gate_resolved",
        f"Loop '{loop.name}': Gate {decision} — "
        + ("läuft weiter" if decision == "approved" else "bleibt pausiert"),
        board_id=loop.board_id,
        detail={"loop_id": str(loop.id), "decision": decision},
    )


loop_runner = LoopRunnerService()
