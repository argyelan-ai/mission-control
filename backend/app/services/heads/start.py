"""Start one head for a task — the single start path (docs/specs/head-launcher.md §6.3, §6.6).

Used by ``POST /api/v1/heads`` and by the night shift (``night_shift.py``), so
an unattended start goes through exactly the same checks as a click: task
lock, one active run per task, pair status (engine live, box free), run
folder, task ``inbox → in_progress`` + ``manual_hold`` in one transaction,
spool request. Errors are ``HeadStartError`` with an HTTP status and a code;
the router turns them into ``HTTPException``.
"""
from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.repo import Repo
from app.models.task import Task
from app.services.heads import box_guard, files, launcher, pairs
from app.services.heads.files import write_backend_file
from app.services.heads.mirror import move_task
from app.services.heads.state import ACTIVE_STATES, derive_for_run


class HeadStartError(Exception):
    """A start was refused. ``status`` is the HTTP status the API answers with."""

    def __init__(self, status: int, code: str, **extra) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra


@contextlib.contextmanager
def task_lock(task_id: str) -> Iterator[None]:
    try:
        marker = launcher.acquire_task_lock(task_id)
    except launcher.TaskStartBusy:
        raise HeadStartError(409, "head_active")
    try:
        yield
    finally:
        launcher.release_task_lock(marker)


def active_run_for_task(task_id: str, now: float):
    for run in reversed(files.list_runs()):
        if run.task_id == task_id and derive_for_run(run, now)["state"] in ACTIVE_STATES:
            return run
    return None


async def checked_pair(session: AsyncSession, harness: str, runtime_slug: str, ignore_run_id: str | None = None):
    occ = box_guard.occupancy()
    pair, runtime = await pairs.resolve_pair(session, harness, runtime_slug, occ, ignore_run_id=ignore_run_id)
    if pair is None or runtime is None:
        if runtime is None:
            code = "runtime_not_found"
        elif harness not in pairs.OFFERED_HARNESSES:
            code = "harness_not_supported"
        else:
            # the runtime row exists but is not offered: a second row for the
            # same engine + model (the slot row stands for it), or no model /
            # protocol a head can use
            code = "runtime_not_offered"
        raise HeadStartError(422, "pair_blocked", reason_code=code)
    if pair.status == "blocked":
        if pair.reason_code == "engine_not_ready":
            raise HeadStartError(409, "engine_not_ready")
        if pair.reason_code == "box_busy":
            raise HeadStartError(409, "box_busy", busy_by=pair.busy_by)
        raise HeadStartError(422, "pair_blocked", reason_code=pair.reason_code)
    return pair, runtime


async def hold_and_move(session: AsyncSession, task: Task, run_id: str, reason: str) -> None:
    """Hold the task and walk it to in_progress BEFORE the spool goes out.

    move_task flushes every hop, so a refused transition surfaces here — and
    then the run folder is removed: the host must never start a run whose
    task change did not happen (live: HTTP 500 after the spool, head ran).
    Any failure becomes 409 ``task_move_failed`` (``hold_on_failure`` in the
    API still holds the card; the night shift skips the mark for the night).
    """
    try:
        task.run_control = "manual_hold"
        moved = await move_task(session, task, "in_progress", reason=reason)
        if not moved and str(task.status) != "in_progress":
            raise RuntimeError(f"no valid path {task.status} → in_progress")
        session.add(task)
        await session.flush()
    except Exception as exc:
        await session.rollback()
        launcher.discard_run(run_id)
        raise HeadStartError(409, "task_move_failed") from exc


async def start_head(
    session: AsyncSession,
    *,
    task: Task,
    repo: Repo,
    harness: str,
    runtime_slug: str,
    user_id: str | None,
    answer: str | None = None,
    reason: str = "head_start",
    now: float | None = None,
    clear_hold_reason: bool = False,
) -> dict:
    """Write the run, hold + move the task, spool the start. Returns the spec
    summary ``{run_id, state, branch}``. Raises ``HeadStartError``.

    ``clear_hold_reason``: the night shift's start — the card was held only
    to wait for the night ("night shift"); once the head runs, the head's own
    hold stands and the old reason would only mislead."""
    now = time.time() if now is None else now
    with task_lock(str(task.id)):
        if active_run_for_task(str(task.id), now) is not None:
            raise HeadStartError(409, "head_active")
        pair, runtime = await checked_pair(session, harness, runtime_slug)
        try:
            spec = await launcher.write_run(
                session, task=task, repo=repo, harness=pair.harness, runtime=runtime,
                box_keys=pair.box_keys, user_id=user_id, answer=answer,
            )
        except OSError as exc:
            raise HeadStartError(503, "spool_unavailable") from exc
        # inbox → in_progress AND the hold in one transaction: no operator PATCH
        # from inbox can lift the hold afterwards (routers/tasks.py clears
        # manual_hold only when the old status is inbox).
        if clear_hold_reason:
            task.hold_reason = None
        await hold_and_move(session, task, spec["run_id"], reason=reason)
        try:
            launcher.spool("start", spec["run_id"])
        except launcher.SpoolUnavailable as exc:
            await session.rollback()
            # no half run left behind: it would count as "starting" for
            # 5 minutes and block the next click with head_active
            launcher.discard_run(spec["run_id"])
            raise HeadStartError(503, "spool_unavailable") from exc
        await session.commit()
    write_backend_file(spec["run_id"], "mirror.json", {"state": "starting", "at": now})
    return {"run_id": spec["run_id"], "state": "starting", "branch": spec["branch"]}
