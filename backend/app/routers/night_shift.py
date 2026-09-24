"""Night shift API (ROADMAP E2) — mark tasks "run tonight", list tonight, settings.

  GET    /api/v1/night-shift/config            viewer    window settings + the next window
  PUT    /api/v1/night-shift/config            admin     change window / zone / cloud share / on-off
  GET    /api/v1/night-shift/tonight           viewer    marked tasks + their state, last report
  GET    /api/v1/night-shift/tasks/{task_id}   viewer    the mark of one task (or null)
  PUT    /api/v1/night-shift/tasks/{task_id}   operator  mark / change the pair
  DELETE /api/v1/night-shift/tasks/{task_id}   operator  unmark (before it started)

Only a card nobody else works on can be marked: an inbox card nothing holds
(the mark holds it), or a card on ``manual_hold`` (``night.markable``).
Mark and unmark hold the per-task mark lock the worker's start holds too, so
a start and an operator change never cross (``night_store.mark_lock``).

The start itself happens in mc-worker (``services/heads/night_shift.py``)
through the head launcher's start path. Errors carry a ``code`` for i18n.
Behind ``settings.heads_enabled`` like the head launcher.
"""
from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.config import settings
from app.database import get_session
from app.models.task import Task
from app.routers.heads import _hold_after_failed_start, run_view
from app.services.heads import box_guard, files, night, night_store, pairs
from app.services.heads.night_store import HOLD_REASON, NightMark
from app.services.heads.start import active_run_for_task

router = APIRouter(prefix="/api/v1/night-shift", tags=["night-shift"])

#: A pair that cannot start right now for these reasons may still be marked —
#: the engine can be up and the box free by tonight.
MARKABLE_BLOCKS = frozenset({"engine_not_ready", "box_busy"})
FINISHED = night.FINISHED_TASK_STATUSES


def _enabled() -> None:
    if not settings.heads_enabled:
        raise HTTPException(status_code=404, detail={"code": "heads_disabled"})


def _err(status: int, code: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, **extra})


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts else None


async def _config_view(session: AsyncSession) -> dict:
    cfg = await night_store.load_config(session)
    now = datetime.now(UTC)
    current = night.current_window(now, cfg)
    return {
        **asdict(cfg),
        "active": current is not None,
        "window": night.next_window(now, cfg).to_dict(),
    }


def mark_view(mark: NightMark, task: Task, runs: dict, now: float) -> dict:
    run = runs.get(mark.run_id) if mark.run_id else None
    if mark.run_id:
        state = "started"
    elif mark.gave_up:
        state = "skipped"
    elif mark.last_error:
        state = "waiting"
    else:
        state = "queued"
    return {
        "task_id": mark.task_id,
        "title": task.title,
        "task_status": task.status,
        "harness": mark.harness,
        "runtime_slug": mark.runtime_slug,
        "locality": mark.locality,
        "marked_at": _iso(mark.marked_at),
        "night": mark.night,
        "state": state,
        "reason": mark.gave_up or mark.last_error,
        "run": run_view(run, now) if run is not None else None,
    }


# ── Settings ────────────────────────────────────────────────────────────────


@router.get("/config", dependencies=[Depends(require_role(Role.VIEWER))])
async def get_config(session: AsyncSession = Depends(get_session)):
    _enabled()
    return await _config_view(session)


class ConfigBody(BaseModel):
    enabled: bool | None = None
    start: str | None = Field(default=None, max_length=5)
    end: str | None = Field(default=None, max_length=5)
    timezone: str | None = Field(default=None, max_length=64)
    cloud_share: int | None = None


@router.put("/config", dependencies=[Depends(require_role(Role.ADMIN))])
async def put_config(body: ConfigBody, session: AsyncSession = Depends(get_session)):
    _enabled()
    updates = body.model_dump(exclude_none=True)
    try:
        await night_store.save_config(session, updates)
    except ValueError as exc:
        fields = exc.args[0] if exc.args and isinstance(exc.args[0], list) else []
        raise _err(422, "invalid_config", fields=fields)
    return await _config_view(session)


# ── Tonight ────────────────────────────────────────────────────────────────


@router.get("/tonight", dependencies=[Depends(require_role(Role.VIEWER))])
async def get_tonight(session: AsyncSession = Depends(get_session)):
    _enabled()
    marks = night_store.list_marks()
    tasks: dict[str, Task] = {}
    if marks:
        ids = [uuid.UUID(m.task_id) for m in marks]
        tasks = {str(t.id): t for t in (await session.exec(select(Task).where(Task.id.in_(ids)))).all()}
    now = time.time()
    runs = {r.run_id: r for r in files.list_runs()} if any(m.run_id for m in marks) else {}
    entries = [mark_view(m, tasks[m.task_id], runs, now) for m in marks if m.task_id in tasks]
    report = night_store.latest_report()
    if report:
        report = {k: report.get(k) for k in ("night", "sent_at", "delivered", "entries")}
    return {"config": await _config_view(session), "entries": entries, "last_report": report}


# ── One task ───────────────────────────────────────────────────────────────


async def _task(session: AsyncSession, task_id: uuid.UUID) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise _err(404, "task_not_found")
    return task


@router.get("/tasks/{task_id}", dependencies=[Depends(require_role(Role.VIEWER))])
async def get_task_mark(task_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    _enabled()
    task = await _task(session, task_id)
    mark = night_store.load_mark(str(task_id))
    if mark is None:
        return {"mark": None}
    runs = {r.run_id: r for r in files.list_runs()} if mark.run_id else {}
    return {"mark": mark_view(mark, task, runs, time.time())}


class MarkBody(BaseModel):
    harness: str = Field(max_length=32)
    runtime_slug: str = Field(max_length=64)
    #: "Queue for tonight" in New task created this inbox card only for a
    #: night head. If marking fails, hold the card so the fleet never picks
    #: it up later (the same rule as "Run as head", ``hold_on_failure``).
    hold_on_failure: bool = False


@router.put("/tasks/{task_id}")
async def put_task_mark(
    task_id: uuid.UUID,
    body: MarkBody,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_role(Role.OPERATOR)),
):
    _enabled()
    try:
        async with _locked(task_id):
            return await _mark(session, task_id, body, user)
    except HTTPException:
        if body.hold_on_failure:
            await session.rollback()
            await _hold_after_failed_start(session, task_id)
        raise


@contextlib.asynccontextmanager
async def _locked(task_id: uuid.UUID) -> AsyncIterator[None]:
    """The mark lock, as a 409 ``night_busy`` when the worker holds it too long."""
    try:
        async with night_store.mark_lock(str(task_id)):
            yield
    except night_store.MarkBusy:
        raise _err(409, "night_busy")


async def _mark(session: AsyncSession, task_id: uuid.UUID, body: MarkBody, user) -> dict:
    task = await _task(session, task_id)
    if task.status in FINISHED:
        raise _err(409, "task_finished")
    if not task.repo_id:
        raise _err(422, "repo_required")
    existing = night_store.load_mark(str(task_id))
    if existing is not None and existing.run_id:
        raise _err(409, "night_started")
    if active_run_for_task(str(task_id), time.time()) is not None:
        raise _err(409, "head_active")
    if not night.markable(task.status, task.run_control):
        # someone works on it (fleet agent, operator) — a night head would be a second worker
        raise _err(409, "task_busy")

    listing = await pairs.list_pairs(session, box_guard.occupancy())
    pair = next((p for p in listing["pairs"]
                 if p["harness"] == body.harness and p["runtime_slug"] == body.runtime_slug), None)
    if pair is None:
        raise _err(422, "pair_blocked", reason_code="runtime_not_offered")
    if pair["status"] == "blocked" and pair["reason_code"] not in MARKABLE_BLOCKS:
        raise _err(422, "pair_blocked", reason_code=pair["reason_code"])

    # Read again right before writing: the worker may have started it while
    # the pairs were probed (it holds the same lock for the start itself).
    existing = night_store.load_mark(str(task_id))
    if existing is not None and existing.run_id:
        raise _err(409, "night_started")
    if existing is not None:
        mark = existing
        mark.harness, mark.runtime_slug, mark.locality = body.harness, body.runtime_slug, pair["locality"]
        mark.last_error, mark.gave_up = None, None
    else:
        uid = getattr(user, "id", None)
        mark = NightMark(task_id=str(task_id), harness=body.harness, runtime_slug=body.runtime_slug,
                         locality=pair["locality"], marked_at=time.time(), marked_by=str(uid) if uid else None)
    # An inbox card waits for the night: hold it so nothing else picks it up.
    if task.status == "inbox" and task.run_control is None:
        task.run_control = "manual_hold"
        task.hold_reason = HOLD_REASON
        mark.held = True
        session.add(task)
        await session.commit()
    night_store.save_mark(mark)
    return {"mark": mark_view(mark, task, {}, time.time())}


@router.delete("/tasks/{task_id}", dependencies=[Depends(require_role(Role.OPERATOR))])
async def delete_task_mark(task_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    _enabled()
    async with _locked(task_id):
        mark = night_store.load_mark(str(task_id))
        if mark is None:
            return {"mark": None}
        if mark.run_id:
            # The head is working (or done): stop it on the task; its outcome
            # still belongs into the morning report.
            raise _err(409, "night_started")
        task = await session.get(Task, task_id)
        if (
            task is not None and mark.held and task.status == "inbox"
            and task.run_control == "manual_hold" and task.hold_reason == HOLD_REASON
        ):
            task.run_control = None
            task.hold_reason = None
            session.add(task)
            await session.commit()
        night_store.delete_mark(str(task_id))
    return {"mark": None}
