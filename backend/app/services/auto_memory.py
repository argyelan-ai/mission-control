"""
Auto-Memory Service — MC lernt automatisch aus System-Events.

Generiert BoardMemory-Eintraege bei Task-Completion, Task-Failure,
Phase-Completion und woechentlichen Digests. Alle Eintraege haben
source="system" und auto_generated=True.

Jede Funktion erstellt eigene DB-Session (Background-Task-Pattern).
Redis-Dedup verhindert doppelte Eintraege.
"""

import hashlib
import logging
import uuid
from datetime import datetime, timedelta

from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.agent import Agent
from app.models.board import Project
from app.models.memory import BoardMemory
from app.models.tag import Tag, TagAssignment
from app.models.task import Task, TaskComment
from app.redis_client import RedisKeys, get_redis
from app.utils import utcnow

logger = logging.getLogger("mc.auto_memory")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _format_duration(start: datetime | None, end: datetime | None) -> str:
    """Dauer zwischen zwei Zeitpunkten als lesbaren String formatieren."""
    if not start or not end:
        return "unbekannt"
    diff = end - start
    total_minutes = int(diff.total_seconds() / 60)
    return _format_minutes(total_minutes)


def _format_minutes(total_minutes: int) -> str:
    """Minuten als lesbaren String formatieren."""
    if total_minutes < 1:
        return "<1 Min"
    if total_minutes < 60:
        return f"{total_minutes} Min"
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}min"


async def _load_recent_comments(
    session: AsyncSession, task_id: uuid.UUID, limit: int = 2
) -> list[TaskComment]:
    """Letzte N Kommentare eines Tasks laden."""
    result = await session.exec(
        select(TaskComment)
        .where(TaskComment.task_id == task_id)
        .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
        .limit(limit)
    )
    return list(result.all())


async def _load_project_tags(session: AsyncSession, project_id: uuid.UUID | None) -> list[str]:
    """Laedt Projekt-Tags als String-Liste fuer Memory-Eintraege."""
    if not project_id:
        return []
    try:
        project = await session.get(Project, project_id)
        tag_result = await session.exec(
            select(Tag)
            .join(TagAssignment, TagAssignment.tag_id == Tag.id)
            .where(TagAssignment.project_id == project_id)
        )
        tag_names = [t.name for t in tag_result.all()]
        result = []
        if project:
            result.append(f"projekt:{project.name.lower().replace(' ', '-')}")
        result.extend(tag_names)
        return result
    except Exception:
        return []


async def _dedup_check(key: str, ttl: int = 3600) -> bool:
    """Redis-basierter Duplikat-Schutz. Gibt True zurueck wenn KEIN Duplikat."""
    try:
        redis = await get_redis()
        acquired = await redis.set(key, "1", nx=True, ex=ttl)
        return bool(acquired)
    except Exception:
        # Bei Redis-Fehler lieber ausfuehren als gar nicht
        return True


# ── Phase 5 MSY-01: Reflection-Fold Helpers ──────────────────────────────────


async def _load_reflections_for_task(
    session: AsyncSession, task_id: uuid.UUID
) -> list[TaskComment]:
    """Phase 5 MSY-01: alle Reflection-Comments eines Tasks, oldest first.

    Mirror der `_load_reflection_and_last_comments` Pattern aus
    `services/report_auto_draft.py:46-72`. Nur reflection-typed Comments,
    nicht alle.

    W4.2: excludes author_type='system' to avoid re-folding the auto-generated
    task-done TaskComments back into BoardMemory (would re-introduce the noise
    we are eliminating in W4).
    """
    result = await session.exec(
        select(TaskComment)
        .where(TaskComment.task_id == task_id)
        .where(TaskComment.comment_type == "reflection")
        .where(TaskComment.author_type != "system")  # W4.2: skip auto-generated summaries
        .order_by(TaskComment.created_at)  # type: ignore[union-attr]
    )
    return list(result.all())


def _reflection_dedup_key(task_id: uuid.UUID, reflection_text: str) -> str:
    """Phase 5 MSY-01 D-03: dedup key per (task_id, reflection_text-sha256).

    Per D-03: identical reflection on same task → silent skip; different
    reflection on same task → fresh emission.
    """
    h = hashlib.sha256(reflection_text.encode("utf-8")).hexdigest()[:16]
    return RedisKeys.auto_memory_reflection_fold(str(task_id), h)


async def _fold_reflections_into_memory(
    session: AsyncSession,
    task_id: uuid.UUID,
    task: Task,
    project_tags: list[str],
) -> int:
    """Phase 5 MSY-01 D-04: fold all un-folded reflections into BoardMemory.

    Runs OUTSIDE the top-level `auto_memory_task_done` dedup short-circuit so
    that legacy reflections (predating MSY-01) get picked up on the first
    post-MSY-01 invocation. Per-reflection dedup via
    `_reflection_dedup_key` provides idempotency.

    Pitfall 1 (05-PATTERNS.md): the existing `agent_comments.py:395-422`
    reflection→`lesson` pipeline coexists by design. This fold writes a
    `journal`-style BoardMemory at task-completion time, board-scoped;
    the existing pipeline writes a `lesson` BoardMemory at comment-post
    time, agent-scoped. Two distinct rows for the same reflection — by
    design.

    Returns count of newly-folded reflections.
    """
    # Lazy-Import (Pitfall 1 — IMPORT, do NOT reimplement)
    from app.routers.agent_comments import _extract_reflection_lesson

    reflections = await _load_reflections_for_task(session, task_id)
    folded = 0
    for refl in reflections:
        refl_text = refl.content or ""
        if not refl_text.strip():
            continue
        refl_key = _reflection_dedup_key(task_id, refl_text)
        if not await _dedup_check(refl_key, ttl=86400 * 30):  # 30 Tage TTL
            continue
        lesson_text = _extract_reflection_lesson(refl_text)
        if not lesson_text:
            lesson_text = refl_text[:500]
        refl_memory = BoardMemory(
            board_id=task.board_id,
            title=f"Reflection: {task.title[:80]}",
            content=lesson_text,
            memory_type="journal",
            source="system",
            auto_generated=True,
            tags=["auto", "reflection_fold"] + project_tags,
        )
        session.add(refl_memory)
        await session.commit()
        await session.refresh(refl_memory)
        try:
            from app.services.memory_indexing import index_memory
            await index_memory(refl_memory)
        except Exception as e:
            logger.warning("auto_memory reflection_fold index failed: %s", e)
        logger.info(
            "Auto-memory: reflection_fold recorded for task '%s' (refl_id=%s)",
            task.title, refl.id,
        )
        folded += 1
    return folded


# ── Task Completion ──────────────────────────────────────────────────────────


async def record_task_completion(task_id: uuid.UUID, agent_id: uuid.UUID | None) -> None:
    """Zeichnet eine Lesson auf wenn ein Task auf 'done' gesetzt wird.

    Phase 5 MSY-01 (D-04): reflections are folded UNCONDITIONALLY on every
    call (per-reflection dedup via `_reflection_dedup_key` provides
    idempotency). The existing `auto_memory_task_done` short-circuit then
    gates only the legacy journal-summary INSERT — preserving its
    at-most-once semantics.

    This shape ensures legacy reflections (created before MSY-01) get folded
    on the first post-MSY-01 invocation, even though the top-level dedup key
    was already written at original completion time.
    """
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            task = await session.get(Task, task_id)
            if not task:
                return

            project_tags = await _load_project_tags(session, task.project_id)

            # Phase 5 MSY-01 (D-04): fold reflections on EVERY call.
            # Idempotency comes from per-reflection dedup keys — NOT from
            # the top-level auto_memory_task_done key. Legacy reflections
            # (predating MSY-01) get picked up on first post-MSY-01 call
            # because the top-level key being already-set doesn't gate this.
            await _fold_reflections_into_memory(session, task_id, task, project_tags)

            # Existing top-level dedup short-circuit for the journal-summary
            # INSERT (Pitfall 1 — behaviour-preserving, byte-identical to
            # pre-MSY-01 for the journal-summary path).
            dedup_key = RedisKeys.auto_memory_task_done(str(task_id))
            if not await _dedup_check(dedup_key):
                return

            agent_name = "Unbekannt"
            if agent_id:
                agent = await session.get(Agent, agent_id)
                if agent:
                    agent_name = f"{agent.emoji or ''} {agent.name}".strip()

            duration = _format_duration(task.started_at, task.completed_at)
            comments = await _load_recent_comments(session, task_id, limit=2)

            # Inhalt zusammenbauen
            lines = [
                f"**Task erledigt:** {task.title}",
                f"**Agent:** {agent_name}",
                f"**Dauer:** {duration}",
                f"**Prioritaet:** {task.priority}",
            ]
            if comments:
                lines.append("\n**Letzte Kommentare:**")
                for c in reversed(comments):  # chronologisch
                    preview = c.content[:150].replace("\n", " ")
                    lines.append(f"- {preview}")

            # W4.2 — Redirect: write a TaskComment instead of a BoardMemory.
            # Auto-generated task-done summaries are telemetry, not knowledge;
            # they polluted the vault (74% of all 881 notes). A TaskComment
            # with comment_type='reflection' keeps the audit trail attached to
            # the task without flooding the vault or board memory.
            comment = TaskComment(
                task_id=task_id,
                author_type="system",
                comment_type="reflection",
                content="\n".join(lines),
            )
            session.add(comment)
            await session.commit()

            logger.info("Auto-memory: task_done reflection comment written for '%s'", task.title)
        except Exception:
            logger.exception("Auto-memory: failed to record task_done for %s", task_id)


# ── Task Failure ─────────────────────────────────────────────────────────────


async def record_task_failure(task_id: uuid.UUID, agent_id: uuid.UUID | None) -> None:
    """Zeichnet eine Lesson auf wenn ein Task auf 'failed' gesetzt wird."""
    dedup_key = RedisKeys.auto_memory_task_failed(str(task_id))
    if not await _dedup_check(dedup_key):
        return

    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            task = await session.get(Task, task_id)
            if not task:
                return

            agent_name = "Unbekannt"
            if agent_id:
                agent = await session.get(Agent, agent_id)
                if agent:
                    agent_name = f"{agent.emoji or ''} {agent.name}".strip()

            duration = _format_duration(task.started_at, utcnow())
            comments = await _load_recent_comments(session, task_id, limit=1)

            # Fehlergrund aus letztem Kommentar
            error_reason = "Kein Fehlergrund angegeben"
            if comments:
                error_reason = comments[0].content[:200].replace("\n", " ")

            lines = [
                f"**Task fehlgeschlagen:** {task.title}",
                f"**Agent:** {agent_name}",
                f"**Dauer bis Abbruch:** {duration}",
                f"**Prioritaet:** {task.priority}",
                f"\n**Fehlergrund:** {error_reason}",
            ]

            memory = BoardMemory(
                board_id=task.board_id,
                agent_id=agent_id,
                title=f"Task fehlgeschlagen: {task.title[:70]}",
                content="\n".join(lines),
                memory_type="lesson",
                source="system",
                auto_generated=True,
                tags=["auto", "task_failed"],
            )
            session.add(memory)
            await session.commit()
            await session.refresh(memory)
            try:
                from app.services.memory_indexing import index_memory
                await index_memory(memory)
            except Exception as e:
                logger.warning("auto_memory task_failed index failed: %s", e)

            logger.info("Auto-memory: task_failed recorded for '%s'", task.title)
        except Exception:
            logger.exception("Auto-memory: failed to record task_failed for %s", task_id)


# ── Phase Completion ─────────────────────────────────────────────────────────


async def record_phase_completion(
    parent_task_id: uuid.UUID, subtask_ids: list[uuid.UUID]
) -> None:
    """Zeichnet Knowledge auf wenn alle Subtasks einer Phase abgeschlossen sind."""
    dedup_key = RedisKeys.auto_memory_phase_done(str(parent_task_id))
    if not await _dedup_check(dedup_key):
        return

    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            parent = await session.get(Task, parent_task_id)
            if not parent:
                return

            # Subtasks laden
            subtasks = []
            for sid in subtask_ids:
                st = await session.get(Task, sid)
                if st:
                    subtasks.append(st)

            # Beteiligte Agents sammeln
            agent_ids = {s.assigned_agent_id for s in subtasks if s.assigned_agent_id}
            agent_names = []
            for aid in agent_ids:
                agent = await session.get(Agent, aid)
                if agent:
                    agent_names.append(f"{agent.emoji or ''} {agent.name}".strip())

            # Gesamtdauer: fruehester Start → spaetestes Ende
            starts = [s.started_at for s in subtasks if s.started_at]
            ends = [s.completed_at for s in subtasks if s.completed_at]
            total_duration = "unbekannt"
            if starts and ends:
                earliest = min(starts)
                latest = max(ends)
                total_duration = _format_duration(earliest, latest)

            lines = [
                f"**Phase abgeschlossen:** {parent.title}",
                f"**Subtasks:** {len(subtasks)}",
                f"**Beteiligte Agents:** {', '.join(agent_names) or 'keine'}",
                f"**Gesamtdauer:** {total_duration}",
            ]

            # Subtask-Uebersicht
            lines.append("\n**Tasks:**")
            for s in subtasks:
                status_icon = "done" if s.status == "done" else s.status
                lines.append(f"- [{status_icon}] {s.title}")

            memory = BoardMemory(
                board_id=parent.board_id,
                title=f"Phase erledigt: {parent.title[:80]}",
                content="\n".join(lines),
                memory_type="knowledge",
                source="system",
                auto_generated=True,
                tags=["auto", "phase_done"],
            )
            session.add(memory)
            await session.commit()
            await session.refresh(memory)
            try:
                from app.services.memory_indexing import index_memory
                await index_memory(memory)
            except Exception as e:
                logger.warning("auto_memory phase_done index failed: %s", e)

            logger.info(
                "Auto-memory: phase_done recorded for '%s' (%d subtasks)",
                parent.title, len(subtasks),
            )
        except Exception:
            logger.exception("Auto-memory: failed to record phase_done for %s", parent_task_id)


# ── Weekly Digest ────────────────────────────────────────────────────────────


async def generate_weekly_digest() -> None:
    """Generiert einen woechentlichen Review — max 1x pro Woche (Redis-Dedup, 6 Tage TTL)."""
    dedup_key = RedisKeys.auto_memory_weekly_digest()
    if not await _dedup_check(dedup_key, ttl=518400):  # 6 Tage
        return

    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            week_ago = utcnow() - timedelta(days=7)

            # Tasks done letzte 7 Tage
            done_result = await session.exec(
                select(func.count()).where(
                    Task.status == "done",
                    Task.completed_at >= week_ago,  # type: ignore[operator]
                )
            )
            done_count = done_result.one()

            # Tasks failed letzte 7 Tage
            failed_result = await session.exec(
                select(func.count()).where(
                    Task.status == "failed",
                    Task.updated_at >= week_ago,  # type: ignore[operator]
                )
            )
            failed_count = failed_result.one()

            # Top-3 aktivste Agents (nach abgeschlossenen Tasks)
            top_agents_result = await session.exec(
                select(
                    Task.assigned_agent_id,
                    func.count().label("cnt"),
                )
                .where(
                    Task.status == "done",
                    Task.completed_at >= week_ago,  # type: ignore[operator]
                    Task.assigned_agent_id.isnot(None),  # type: ignore[attr-defined]
                )
                .group_by(Task.assigned_agent_id)
                .order_by(func.count().desc())
                .limit(3)
            )
            top_agents = top_agents_result.all()

            agent_lines = []
            for agent_id, cnt in top_agents:
                agent = await session.get(Agent, agent_id)
                if agent:
                    name = f"{agent.emoji or ''} {agent.name}".strip()
                    agent_lines.append(f"- {name}: {cnt} Tasks")

            # Durchschnittsdauer (nur fuer done Tasks mit started_at + completed_at)
            avg_result = await session.exec(
                select(
                    func.avg(
                        func.extract("epoch", Task.completed_at)  # type: ignore[arg-type]
                        - func.extract("epoch", Task.started_at)  # type: ignore[arg-type]
                    )
                ).where(
                    Task.status == "done",
                    Task.completed_at >= week_ago,  # type: ignore[operator]
                    Task.started_at.isnot(None),  # type: ignore[attr-defined]
                    Task.completed_at.isnot(None),  # type: ignore[attr-defined]
                )
            )
            avg_seconds = avg_result.one()
            avg_duration = "keine Daten"
            if avg_seconds:
                avg_duration = _format_minutes(int(avg_seconds / 60))

            lines = [
                f"**Woechentlicher Review** ({week_ago.strftime('%d.%m.')} - {utcnow().strftime('%d.%m.%Y')})",
                f"\n**Tasks erledigt:** {done_count}",
                f"**Tasks fehlgeschlagen:** {failed_count}",
                f"**Durchschnittliche Dauer:** {avg_duration}",
            ]

            if agent_lines:
                lines.append("\n**Aktivste Agents:**")
                lines.extend(agent_lines)

            memory = BoardMemory(
                board_id=None,
                agent_id=None,
                title=f"Weekly Review {utcnow().strftime('%d.%m.%Y')}",
                content="\n".join(lines),
                memory_type="weekly_review",
                source="system",
                auto_generated=True,
                tags=["auto", "weekly_review"],
            )
            session.add(memory)
            await session.commit()
            await session.refresh(memory)
            try:
                from app.services.memory_indexing import index_memory
                await index_memory(memory)
            except Exception as e:
                logger.warning("auto_memory weekly_digest index failed: %s", e)

            logger.info(
                "Auto-memory: weekly_digest generated (done=%d, failed=%d)",
                done_count, failed_count,
            )
        except Exception:
            logger.exception("Auto-memory: failed to generate weekly_digest")


# ── Fetch Recent Lessons (fuer Dispatch-Enhancement) ─────────────────────────


async def fetch_recent_lessons(
    session: AsyncSession, board_id: uuid.UUID | None, limit: int = 3
) -> list[BoardMemory]:
    """Laedt die neuesten auto-generierten Lessons fuer ein Board."""
    if not board_id:
        return []

    result = await session.exec(
        select(BoardMemory)
        .where(
            BoardMemory.auto_generated == True,  # noqa: E712
            BoardMemory.memory_type == "lesson",
            BoardMemory.board_id == board_id,
        )
        .order_by(BoardMemory.created_at.desc())  # type: ignore[union-attr]
        .limit(limit)
    )
    return list(result.all())


# ── Fetch Agent Lessons ──────────────────────────────────────────────────────


async def fetch_agent_lessons(
    session: AsyncSession, agent_id: uuid.UUID, limit: int = 3
) -> list[BoardMemory]:
    """Laedt die neuesten Lessons die ein Agent geschrieben hat (agent-scoped)."""
    result = await session.exec(
        select(BoardMemory)
        .where(
            BoardMemory.agent_id == agent_id,
            BoardMemory.memory_type == "lesson",
        )
        .order_by(BoardMemory.created_at.desc())  # type: ignore[union-attr]
        .limit(limit)
    )
    return list(result.all())


# ── Fetch Relevant Lessons (Keyword-Matching) ───────────────────────────────


async def fetch_relevant_lessons(
    session: AsyncSession,
    task_title: str,
    task_description: str | None,
    board_id: uuid.UUID | None,
    limit: int = 3,
) -> list[BoardMemory]:
    """Keyword-basierte Suche nach relevanten Lessons fuer einen Task.

    Einfaches Matching: Woerter aus dem Task-Titel werden gegen
    BoardMemory.content und .title gesucht (ILIKE).
    Keine Vektordatenbank noetig.
    """
    from sqlmodel import or_

    if not board_id:
        return []

    # Stoppwoerter rausfiltern, nur Woerter mit 4+ Zeichen
    text = f"{task_title} {task_description or ''}"
    keywords = [w.strip(".,!?:;()[]{}\"'") for w in text.split() if len(w) >= 4]
    keywords = list(set(keywords))[:5]  # Max 5 Keywords

    if not keywords:
        return []

    # OR-Suche: mindestens ein Keyword muss matchen
    conditions = []
    for kw in keywords:
        pattern = f"%{kw}%"
        conditions.append(BoardMemory.content.ilike(pattern))  # type: ignore[union-attr]
        conditions.append(BoardMemory.title.ilike(pattern))  # type: ignore[union-attr]

    result = await session.exec(
        select(BoardMemory)
        .where(
            BoardMemory.board_id == board_id,
            BoardMemory.memory_type == "lesson",
            or_(*conditions),
        )
        .order_by(BoardMemory.created_at.desc())  # type: ignore[union-attr]
        .limit(limit)
    )
    return list(result.all())


# ── Feedback Capture ─────────────────────────────────────────────────────────


async def record_feedback_lesson(
    task_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    feedback_type: str,
    comment: str | None = None,
) -> None:
    """Zeichnet eine Lesson auf wenn der Operator einen Task genehmigt oder ablehnt.

    feedback_type: "approved" oder "rejected"
    """
    dedup_key = RedisKeys.auto_memory_feedback(str(task_id), feedback_type)
    if not await _dedup_check(dedup_key):
        return

    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            task = await session.get(Task, task_id)
            if not task:
                return

            agent_name = "Unbekannt"
            if agent_id:
                agent = await session.get(Agent, agent_id)
                if agent:
                    agent_name = f"{agent.emoji or ''} {agent.name}".strip()

            if feedback_type == "approved":
                title = f"Genehmigt: {task.title[:80]}"
                content_lines = [
                    f"**Task genehmigt:** {task.title}",
                    f"**Agent:** {agent_name}",
                    "**Ergebnis:** Der Operator hat diesen Task akzeptiert.",
                ]
            else:
                title = f"Abgelehnt: {task.title[:80]}"
                content_lines = [
                    f"**Task abgelehnt:** {task.title}",
                    f"**Agent:** {agent_name}",
                    "**Ergebnis:** Der Operator hat diesen Task zurueckgewiesen.",
                ]
                if comment:
                    content_lines.append(f"\n**Feedback:** {comment}")

            project_tags = await _load_project_tags(session, task.project_id)
            # "approved" = Journal (Bestaetigungslog), "rejected" = Lesson (daraus lernen)
            mem_type = "journal" if feedback_type == "approved" else "lesson"
            memory = BoardMemory(
                board_id=task.board_id,
                title=title,
                content="\n".join(content_lines),
                memory_type=mem_type,
                source="system",
                auto_generated=True,
                tags=["auto", f"feedback_{feedback_type}"] + project_tags,
            )
            session.add(memory)
            await session.commit()
            await session.refresh(memory)
            try:
                from app.services.memory_indexing import index_memory
                await index_memory(memory)
            except Exception as e:
                logger.warning("auto_memory feedback_lesson index failed: %s", e)

            logger.info(
                "Auto-memory: feedback_%s recorded for '%s'",
                feedback_type, task.title,
            )
        except Exception:
            logger.exception(
                "Auto-memory: failed to record feedback for %s", task_id
            )
