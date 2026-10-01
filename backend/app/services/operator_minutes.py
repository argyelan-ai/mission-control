"""Operator minutes per job — an ESTIMATE of how much of the operator's time
one job took (ROADMAP E1 acceptance: "operator minutes <= 15 per job", and
the old-vs-new comparison in operator minutes, follow-up questions and
manual rescues of heads).

MC has no clock on the operator. What it does record is every time the
operator's side touched a job. This module turns those touches into minutes
with one simple, explainable rule — nothing more precise is claimed.

What counts as a touch (operator side, per task):
  status_change    task_events with changed_by='user' (the same signal as
                   digest M4 — manual moves, stop/resume, approval-driven moves)
  comment          task_comments with author_type='user'
  thread_reply     messages with sender_type='user' in the task's threads
  approval_answer  approvals of the task resolved as approved/rejected
                   (at resolved_at; superseded/expired are closed by the system)
  head_start       task_events reason 'head_start' ("Run as head" clicked)
  head_restart     task_events reason 'head_restart' ("restart with …")
  night_mark       task_events reason 'night_shift_start' — the operator marked
                   the card for the night shift. The mark time itself is not in
                   the DB once the night is over; the night start stands in for
                   it (alone at night it forms its own sitting, so it counts
                   MIN_SESSION — the cost of marking it).
  head_stop        task_events reason 'head_stopped' (a head ends as "stopped"
                   only after a stop request)

Active minutes: touches of one job (the task and all its subtasks) sorted by
time; touches no more than SESSION_GAP apart form one sitting. A sitting
counts its span (first to last touch), but at least MIN_SESSION.
  - SESSION_GAP = 10 min: between two clicks of one sitting the operator reads
    the card, the log or the PR; ten minutes of quiet means they left.
  - MIN_SESSION = 3 min: even a single click needs the card opened and read
    first. A lone click is never "0 minutes".
Not seen at all: reading without clicking, work in a terminal, reviewing or
merging the PR on GitHub. Seen but not separable: a session acting with the
operator's own login (e.g. the operator's coding session calling the API)
counts as the operator — MC cannot tell the two apart.

Counts next to the minutes:
  follow_up_questions   the job asked the operator: head 'needs you'
                        (task_events reason 'head_needs_you'), approvals of
                        FOLLOW_UP_APPROVAL_TYPES (any status — it was asked),
                        agent comments of type 'needs_decision'
  manual_status_changes user status changes with a reason in
                        MANUAL_FIX_REASONS (hand moves, stop/resume, drag on
                        the board, no reason given) — answers to approvals are
                        not counted here, they are follow-ups
  head_rescues          a head restarted after it failed or was stopped, plus
                        every head the operator stopped
  rescues               manual_status_changes + head_rescues

A finished job is a top-level task (no parent) that is done (at completed_at)
or whose head passed with an open PR (at its last 'head_passed' event) — a
head job ends at "PR open", merging is the operator's acceptance. Its path is
"head" when a head ever ran on it, else "fleet": the old-vs-new comparison.
"""
from __future__ import annotations

import statistics
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlmodel import or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.approval import Approval
from app.models.task import Task, TaskComment, TaskEvent
from app.models.thread import Message, Thread
from app.utils import ensure_aware, utcnow

SESSION_GAP = timedelta(minutes=10)
MIN_SESSION = timedelta(minutes=3)
#: ROADMAP E1 acceptance
TARGET_MINUTES = 15

HEAD_TOUCH_REASONS = {
    "head_start": "head_start",
    "head_restart": "head_restart",
    "night_shift_start": "night_mark",
    "head_stopped": "head_stop",
}
HEAD_OUTCOME_REASONS = ("head_passed", "head_failed", "head_stopped", "head_needs_you")
HEAD_PATH_REASONS = ("head_start", "head_restart", "night_shift_start")
HEAD_RESCUE_AFTER = ("head_failed", "head_stopped")
ANSWERED_APPROVAL_STATUSES = ("approved", "rejected")
FOLLOW_UP_APPROVAL_TYPES = ("clarification_question", "blocker_decision", "question")
FOLLOW_UP_COMMENT_TYPES = ("needs_decision",)
MANUAL_FIX_REASONS = ("manual_update", "manual_stop", "manual_resume", "reorder", None)
#: subtask levels walked below a job (epics are 1–2 deep; a guard, not a rule)
MAX_DEPTH = 5


def active_minutes(timestamps: Iterable[datetime]) -> tuple[float, int]:
    """(minutes, sittings) for a set of touch times — the rule above."""
    times = sorted(ensure_aware(t) for t in timestamps)
    if not times:
        return 0.0, 0
    total = timedelta()
    sittings = 0
    start = prev = times[0]
    for t in times[1:]:
        if t - prev > SESSION_GAP:
            total += max(prev - start, MIN_SESSION)
            sittings += 1
            start = t
        prev = t
    total += max(prev - start, MIN_SESSION)
    sittings += 1
    return total.total_seconds() / 60, sittings


async def _descendants(session: AsyncSession, root_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """task id -> its job (root) id, for the roots and all their subtasks."""
    owner = {r: r for r in root_ids}
    frontier = list(root_ids)
    for _ in range(MAX_DEPTH):
        if not frontier:
            break
        rows = (await session.exec(
            select(Task.id, Task.parent_task_id).where(Task.parent_task_id.in_(frontier))
        )).all()
        frontier = []
        for child_id, parent_id in rows:
            if child_id not in owner:
                owner[child_id] = owner[parent_id]
                frontier.append(child_id)
    return owner


def _empty_job() -> dict[str, Any]:
    return {"touch_times": [], "kinds": Counter(), "follow_up_questions": 0,
            "manual_status_changes": 0, "head_rescues": 0, "head": False}


async def _collect(session: AsyncSession, root_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    owner = await _descendants(session, root_ids)
    ids = list(owner)
    jobs = {r: _empty_job() for r in root_ids}
    if not ids:
        return jobs

    def touch(task_id, ts, kind):
        job = jobs[owner[task_id]]
        job["touch_times"].append(ts)
        job["kinds"][kind] += 1

    events = (await session.exec(
        select(TaskEvent).where(
            TaskEvent.task_id.in_(ids),
            or_(TaskEvent.changed_by == "user",
                TaskEvent.reason.in_([*HEAD_TOUCH_REASONS, *HEAD_OUTCOME_REASONS])),
        ).order_by(TaskEvent.created_at)
    )).all()
    last_head_outcome: dict[uuid.UUID, str] = {}
    for e in events:
        job = jobs[owner[e.task_id]]
        if e.changed_by == "user":
            touch(e.task_id, e.created_at, "status_change")
            if e.reason in MANUAL_FIX_REASONS:
                job["manual_status_changes"] += 1
            continue
        if e.reason in HEAD_PATH_REASONS:
            job["head"] = True
        if e.reason == "head_needs_you":
            job["follow_up_questions"] += 1
        if e.reason == "head_restart" and last_head_outcome.get(e.task_id) in HEAD_RESCUE_AFTER:
            job["head_rescues"] += 1
        if e.reason == "head_stopped":
            job["head_rescues"] += 1
        if e.reason in HEAD_OUTCOME_REASONS:
            last_head_outcome[e.task_id] = e.reason
        if e.reason in HEAD_TOUCH_REASONS:
            touch(e.task_id, e.created_at, HEAD_TOUCH_REASONS[e.reason])

    comments = (await session.exec(
        select(TaskComment.task_id, TaskComment.created_at, TaskComment.author_type,
               TaskComment.comment_type).where(
            TaskComment.task_id.in_(ids),
            or_(TaskComment.author_type == "user",
                TaskComment.comment_type.in_(FOLLOW_UP_COMMENT_TYPES)),
        )
    )).all()
    for task_id, ts, author_type, comment_type in comments:
        if author_type == "user":
            touch(task_id, ts, "comment")
        elif author_type == "agent" and comment_type in FOLLOW_UP_COMMENT_TYPES:
            jobs[owner[task_id]]["follow_up_questions"] += 1

    replies = (await session.exec(
        select(Thread.task_id, Message.created_at)
        .join(Message, Message.thread_id == Thread.id)
        .where(Thread.task_id.in_(ids), Message.sender_type == "user")
    )).all()
    for task_id, ts in replies:
        touch(task_id, ts, "thread_reply")

    approvals = (await session.exec(select(Approval).where(Approval.task_id.in_(ids)))).all()
    for a in approvals:
        if a.action_type in FOLLOW_UP_APPROVAL_TYPES:
            jobs[owner[a.task_id]]["follow_up_questions"] += 1
        if a.status in ANSWERED_APPROVAL_STATUSES and a.resolved_at is not None:
            touch(a.task_id, a.resolved_at, "approval_answer")
    return jobs


def _summary(job: dict) -> dict[str, Any]:
    minutes, sittings = active_minutes(job["touch_times"])
    return {
        "estimate": True,
        "active_minutes": round(minutes),
        "sessions": sittings,
        "touches": len(job["touch_times"]),
        "touches_by_kind": dict(job["kinds"]),
        "follow_up_questions": job["follow_up_questions"],
        "manual_status_changes": job["manual_status_changes"],
        "head_rescues": job["head_rescues"],
        "rescues": job["manual_status_changes"] + job["head_rescues"],
        "path": "head" if job["head"] else "fleet",
    }


async def operator_minutes_for_task(session: AsyncSession, task_id: uuid.UUID) -> dict[str, Any]:
    """The estimate for one job: this task plus all its subtasks."""
    return _summary((await _collect(session, [task_id]))[task_id])


async def finished_jobs(session: AsyncSession, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Top-level jobs finished in [start, end) with their estimate and
    ``finished_at`` (see the module docstring for "finished")."""
    done = (await session.exec(
        select(Task.id, Task.completed_at).where(
            Task.parent_task_id.is_(None), Task.status == "done",
            Task.completed_at >= start, Task.completed_at < end,
        )
    )).all()
    finished = {tid: ensure_aware(ts) for tid, ts in done}
    passed = (await session.exec(
        select(TaskEvent.task_id, TaskEvent.created_at)
        .join(Task, Task.id == TaskEvent.task_id)
        .where(TaskEvent.reason == "head_passed", Task.parent_task_id.is_(None), Task.status != "done")
    )).all()
    last_pass: dict[uuid.UUID, datetime] = {}
    for tid, ts in passed:
        ts = ensure_aware(ts)
        last_pass[tid] = max(ts, last_pass.get(tid, ts))
    for tid, ts in last_pass.items():
        if start <= ts < end:
            finished[tid] = ts
    if not finished:
        return []
    collected = await _collect(session, list(finished))
    return [{"task_id": str(tid), "finished_at": finished[tid].isoformat(), **_summary(collected[tid])}
            for tid in finished]


def _median(values: list[int]) -> float | None:
    return statistics.median(values) if values else None


def aggregate(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    minutes = [j["active_minutes"] for j in jobs]
    return {
        "jobs": len(jobs),
        "median_minutes": _median(minutes),
        "total_minutes": sum(minutes),
        "jobs_over_target": sum(1 for m in minutes if m > TARGET_MINUTES),
        "follow_up_questions": sum(j["follow_up_questions"] for j in jobs),
        "manual_status_changes": sum(j["manual_status_changes"] for j in jobs),
        "head_rescues": sum(j["head_rescues"] for j in jobs),
        "rescues": sum(j["rescues"] for j in jobs),
    }


def rules() -> dict[str, Any]:
    return {
        "session_gap_minutes": int(SESSION_GAP.total_seconds() // 60),
        "min_session_minutes": int(MIN_SESSION.total_seconds() // 60),
        "target_minutes": TARGET_MINUTES,
    }


async def operator_minutes_window(session: AsyncSession, *, days: int = 7, now=None) -> dict[str, Any]:
    """Aggregate over the jobs finished in the last ``days`` days."""
    now = ensure_aware(now) if now is not None else utcnow()
    return {"estimate": True, "days": days,
            **aggregate(await finished_jobs(session, now - timedelta(days=days), now))}


async def operator_minutes_by_week(
    session: AsyncSession, *, weeks: int = 6, now=None, tz: str | None = None,
) -> dict[str, Any]:
    """Per ISO week (in ``tz``, default UTC): finished jobs, median minutes,
    counts — overall and split by path (head vs. fleet)."""
    from app.services.usage_baseline import MAX_WEEKS, iso_week_label, week_starts, zone

    tzinfo = zone(tz)
    now = ensure_aware(now) if now is not None else utcnow()
    weeks = max(1, min(weeks, MAX_WEEKS))
    mondays = week_starts(weeks, now, tzinfo)
    out = []
    for i, monday in enumerate(mondays):
        start = datetime.combine(monday, datetime.min.time(), tzinfo)
        end = (datetime.combine(mondays[i + 1], datetime.min.time(), tzinfo)
               if i + 1 < len(mondays) else now)
        jobs = await finished_jobs(session, start, end)
        out.append({
            "week": iso_week_label(monday),
            "week_start": monday.isoformat(),
            "partial": i == len(mondays) - 1,
            **aggregate(jobs),
            "by_path": {path: aggregate([j for j in jobs if j["path"] == path])
                        for path in ("head", "fleet")},
        })
    return {"estimate": True, "generated_at": now.isoformat(), "tz": tz or "UTC",
            "target_minutes": TARGET_MINUTES, "rules": rules(), "weeks": out}
