"""What a chat turn is told about its agent's own task runs.

Round-1 user test t1 (22.10.2026, MUST-1): an omp agent with an ACP chat
runs every task in a SEPARATE session. Its chat session knew nothing about
that run: asked "how far are you?" it found the task with ``mc me``, looked
for the run's files in its own workspace (``deliverables/<id>/``) instead of
``/deliverables/<id>/``, found nothing and did the whole task a second time.
The day before, the same question right after a run had ended was answered
"0 % — no task is running".

So the operator's chat message gets a short context block in front of it —
sent to the model only, never shown as part of the operator's bubble (the
chat daemon writes the operator's own text to the chat transcript and puts
the block in front of it only on the way to omp, see
``docker/omp-bridge/acp_chat.py``). It names each task of this agent that a
run is working on right now, or finished in the last
``RECENT_WINDOW``: title, id, status, checklist, the run's folder, its
registered deliverables and its last note — and the rule: answer, do not
touch. The server enforces that rule on its own
(``chat_session_guard``); this block is what keeps the model from trying.

Only for agents whose chat and task runs are DIFFERENT sessions — the omp
container with the ACP chat daemon (``acp-docker``). Hermes runs its tasks
inside its chat session; a TUI agent's chat types into the run's own pane.
The caller decides that (``agent_chat.post_chat_input``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.agent import Agent
from app.models.deliverable import TaskDeliverable
from app.models.task import Task, TaskComment, TaskEvent

#: A run that ended this recently is still "the task" for a question like
#: "how far are you?" (the 22.10. case was asked seconds after the end).
RECENT_WINDOW = timedelta(minutes=30)
#: Statuses in which a run holds the task (twin of
#: ``chat_task_steps.LIVE_STATUSES``; inbox counts once it was delivered).
LIVE_STATUSES = ("in_progress", "waiting", "blocked", "user_test")
FINISHED_STATUSES = ("review", "done", "failed", "aborted")
#: Comment types that say where a run stands, newest first.
_NOTE_TYPES = ("progress", "resolution", "reflection", "blocker", "evidence")
_MAX_TASKS = 3
_MAX_DELIVERABLES = 5
_NOTE_MAX = 240

BEGIN = "[Mission Control context — not written by the operator]"
END = "[End of context — the operator's message follows]"


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def operator_zone() -> tuple[tzinfo, str]:
    """The operator's clock (``settings.usage_timezone``, an IANA name)
    and its name; UTC when unset or unknown. Round-2 user test t1 SHOULD-1:
    the block said "01:04 UTC", the chat repeated it, the UI said 03:03 — to
    the operator the agent looked two hours behind."""
    name = (settings.usage_timezone or "").strip() or "UTC"
    try:
        return ZoneInfo(name), name
    except Exception:  # noqa: BLE001 — a typo in the env must not break a chat turn
        return timezone.utc, "UTC"


def _hhmm(dt: datetime | None, *, now: datetime | None = None) -> str | None:
    """``HH:MM`` on the operator's clock, with ``DD.MM.`` in front when it is
    not the same local day as ``now``."""
    aware = _aware(dt)
    if aware is None:
        return None
    zone, _name = operator_zone()
    local = aware.astimezone(zone)
    ref = _aware(now)
    if ref is not None and ref.astimezone(zone).date() != local.date():
        return local.strftime("%d.%m. %H:%M")
    return local.strftime("%H:%M")


#: The status as a word the operator reads (round-2 t1 NICE-3: the chat
#: quoted "`in_progress`" / "Status `review`"). Same meaning as the chat's
#: task lines in the UI (frontend messages tasks.detail.line.task.*), in the
#: agent's ``operator_language``; unknown languages fall back to English.
_STATUS_WORDS: dict[str, dict[str, str]] = {
    "en": {
        "inbox": "delivered, not started yet",
        "in_progress": "in progress",
        "waiting": "waits for the operator",
        "blocked": "waits for the operator",
        "user_test": "waits for the operator's test",
        "review": "awaits the operator's review",
        "done": "done",
        "failed": "failed",
        "aborted": "aborted",
    },
    "de": {
        "inbox": "zugestellt, noch nicht begonnen",
        "in_progress": "in Arbeit",
        "waiting": "wartet auf den Operator",
        "blocked": "wartet auf den Operator",
        "user_test": "wartet auf den Test des Operators",
        "review": "wartet auf Prüfung",
        "done": "erledigt",
        "failed": "fehlgeschlagen",
        "aborted": "abgebrochen",
    },
}


def status_word(status: str, language: str | None) -> str:
    """``status`` as a plain word in ``language`` (``en`` fallback)."""
    words = _STATUS_WORDS.get((language or "en").strip().lower()[:2], _STATUS_WORDS["en"])
    return words.get(status) or _STATUS_WORDS["en"].get(status) or status.replace("_", " ")


def _one_line(text: str | None, limit: int = _NOTE_MAX) -> str | None:
    flat = " ".join(str(text or "").split())
    if not flat:
        return None
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def run_folder(agent: Agent, task_id: Any) -> str:
    """Where the run writes its files, as the agent's own tools see it — the
    same rule the dispatch prompt gives the run (dispatch_message_builder:
    host agents write under ~/.mc, containers under /deliverables)."""
    if getattr(agent, "agent_runtime", None) == "host":
        return f"~/.mc/deliverables/{task_id}/"
    return f"/deliverables/{task_id}/"


async def _live_tasks(session: AsyncSession, agent: Agent) -> list[Task]:
    stmt = (
        select(Task)
        .where(
            Task.assigned_agent_id == agent.id,
            or_(
                Task.status.in_(LIVE_STATUSES),
                and_(Task.status == "inbox", Task.dispatched_at.is_not(None)),
            ),
        )
        .order_by(func.coalesce(Task.started_at, Task.dispatched_at).desc())
        .limit(_MAX_TASKS)
    )
    return list((await session.exec(stmt)).all())


async def _recent_tasks(
    session: AsyncSession, agent: Agent, now: datetime, exclude: set[Any],
) -> list[tuple[Task, datetime]]:
    """Tasks this agent worked (an ``→ in_progress`` event with its id) that
    left ``in_progress`` within ``RECENT_WINDOW`` and are finished now —
    also after a human-review handoff cleared the assignee."""
    worked = select(TaskEvent.task_id).where(
        TaskEvent.agent_id == agent.id, TaskEvent.to_status == "in_progress",
    )
    left = (
        select(TaskEvent.task_id.label("task_id"), func.max(TaskEvent.created_at).label("left_at"))
        .where(TaskEvent.from_status == "in_progress")
        .group_by(TaskEvent.task_id)
        .subquery()
    )
    stmt = (
        select(Task, left.c.left_at)
        .join(left, left.c.task_id == Task.id)
        .where(
            Task.id.in_(worked),
            Task.status.in_(FINISHED_STATUSES),
            left.c.left_at >= now - RECENT_WINDOW,
        )
        .order_by(left.c.left_at.desc())
        .limit(_MAX_TASKS)
    )
    return [(t, at) for t, at in (await session.exec(stmt)).all() if t.id not in exclude]


async def _deliverables(session: AsyncSession, ids: list[Any]) -> dict[Any, list[TaskDeliverable]]:
    out: dict[Any, list[TaskDeliverable]] = {}
    if not ids:
        return out
    rows = (
        await session.exec(
            select(TaskDeliverable)
            .where(TaskDeliverable.task_id.in_(ids))
            .order_by(TaskDeliverable.created_at.desc())
        )
    ).all()
    for row in rows:
        bucket = out.setdefault(row.task_id, [])
        if len(bucket) < _MAX_DELIVERABLES:
            bucket.append(row)
    return out


async def _last_notes(session: AsyncSession, ids: list[Any]) -> dict[Any, TaskComment]:
    out: dict[Any, TaskComment] = {}
    if not ids:
        return out
    rows = (
        await session.exec(
            select(TaskComment)
            .where(TaskComment.task_id.in_(ids), TaskComment.comment_type.in_(_NOTE_TYPES))
            .order_by(TaskComment.created_at.desc())
        )
    ).all()
    for row in rows:
        out.setdefault(row.task_id, row)
    return out


def _task_lines(
    agent: Agent, task: Task, *, when: str | None, now: datetime,
    deliverables: list[TaskDeliverable], note: TaskComment | None,
) -> list[str]:
    word = status_word(task.status, getattr(agent, "operator_language", None))
    head = f'- "{task.title}" (id {task.id}) — status: {word}'
    if task.checklist_total:
        head += f", checklist {task.checklist_done}/{task.checklist_total}"
    if when:
        head += f", {when}"
    lines = [head, f"  Run folder: {run_folder(agent, task.id)} (the run writes here — not your chat workspace)"]
    if deliverables:
        lines.append("  Deliverables registered by the run:")
        for d in deliverables:
            where = d.path or "(inline content)"
            lines.append(f"    · {_one_line(d.title, 120)} — {where}")
    else:
        lines.append("  Deliverables registered by the run: none yet")
    if note is not None:
        text = _one_line(note.content)
        if text:
            stamp = _hhmm(note.created_at, now=now)
            lines.append(f"  Last {note.comment_type} note" + (f" ({stamp})" if stamp else "") + f": {text}")
    return lines


async def run_context(session: AsyncSession, agent: Agent, *, now: datetime | None = None) -> str | None:
    """The context block for one chat turn of ``agent``, or ``None`` when no
    run of it is working or has just finished (nothing to add then)."""
    now = _aware(now) or datetime.now(timezone.utc)
    live = await _live_tasks(session, agent)
    recent = await _recent_tasks(session, agent, now, {t.id for t in live})
    if not live and not recent:
        return None

    ids = [t.id for t in live] + [t.id for t, _ in recent]
    deliverables = await _deliverables(session, ids)
    notes = await _last_notes(session, ids)

    lines = [BEGIN]
    if live:
        lines.append("A task of yours is being worked RIGHT NOW in its own run session — not in this chat:")
        for task in live:
            started = _hhmm(task.started_at or task.dispatched_at, now=now)
            lines += _task_lines(
                agent, task, when=f"started {started}" if started else None, now=now,
                deliverables=deliverables.get(task.id, []), note=notes.get(task.id),
            )
    if recent:
        lines.append("A run of yours finished recently (its own session, not this chat):")
        for task, left_at in recent:
            ended = _hhmm(left_at, now=now)
            lines += _task_lines(
                agent, task, when=f"run ended {ended}" if ended else None, now=now,
                deliverables=deliverables.get(task.id, []), note=notes.get(task.id),
            )
    _zone, zone_name = operator_zone()
    lines += [
        f"State as of {_hhmm(now)} (the operator's local time, {zone_name}); every time "
        "above is on that clock — say times as given, do not convert them.",
        "Answer from this block first; read more only when the question needs it. "
        "Name statuses in plain words, never raw status codes.",
        "Rules for this reply: answer the operator from this state. You may READ the run's "
        "files and run `mc task-get <id>`. Do NOT work on these tasks — no files in their "
        "folders, no `mc comment progress|resolution|evidence|blocker|reflection`, no "
        "`mc checklist` / `mc deliverable` / `mc finish` / `mc ack` / `mc recover` / "
        "`mc ask --blocking` / `mc question` / `mc help` / `mc delegate` for them (the server "
        "refuses those calls from the chat). A plain `mc ask` without `--blocking` is fine. "
        "If the operator wants a change to such a task, tell them to make it in the task view.",
        END,
    ]
    return "\n".join(lines)
