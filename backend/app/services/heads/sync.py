"""heads_sync — mirror head states onto task cards (spec §6.2, §6.5).

For every task only its LATEST run is mirrored (a restart stops the old run;
its "stopped" must not pull the card back to blocked while the new run
works). A state is mirrored once — after that the card belongs to the
operator again (e.g. review → done after merging). No restart, no kill, no
approvals: this job only reads files and writes task status.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.task import Task
from app.services.heads import files
from app.services.heads.mirror import apply_head_state
from app.services.heads.state import ACTIVE_STATES, derive_for_run

logger = logging.getLogger(__name__)


def latest_runs_by_task(runs: list) -> tuple[dict[str, object], list]:
    latest: dict[str, object] = {}
    for run in runs:  # sorted by created_ts ascending
        if run.task_id:
            latest[run.task_id] = run
    superseded = [r for r in runs if r.task_id and latest.get(r.task_id) is not r]
    return latest, superseded


async def sync_once(session: AsyncSession, now: float | None = None) -> int:
    """One pass. Returns how many task cards changed."""
    now = time.time() if now is None else now
    runs = files.list_runs()
    latest, superseded = latest_runs_by_task(runs)
    for run in superseded:
        if run.mirror.get("state") != "superseded":
            files.write_backend_file(run.run_id, "mirror.json", {"state": "superseded", "at": now})
    changed = 0
    for task_id, run in latest.items():
        derived = derive_for_run(run, now)
        if run.mirror.get("state") == derived["state"]:
            continue
        try:
            task = await session.get(Task, uuid.UUID(task_id))
        except ValueError:
            task = None
        if task is None:
            # Runs survive task deletion by design (no FK); keep the run
            # visible and stoppable, write nothing.
            files.write_backend_file(
                run.run_id, "mirror.json", {"state": derived["state"], "task_deleted": True, "at": now}
            )
            continue
        moved = await apply_head_state(session, task, run, derived)
        await session.commit()
        files.write_backend_file(run.run_id, "mirror.json", {"state": derived["state"], "at": now})
        changed += int(moved)
    await _write_task_status_hints(session, runs, now)
    return changed


async def _write_task_status_hints(session: AsyncSession, runs: list, now: float) -> None:
    """Mirrors each ENDED run's task's CURRENT status back onto that run's
    own folder as ``.backend/task.json`` (bauplan `heads-sichtbar` PR 4
    §5) — the one hint ``mc-head gc`` on the host is allowed to read to
    shorten a clean, already-pushed worktree's 14-day grace period once its
    card reaches `done`. Every ended run gets this (not just the latest run
    per task): a superseded run from BEFORE a "continue" restart has its own
    worktree and its own gc decision, and still carries the same `task_id`.

    One batched query for every distinct task behind an ended run this pass
    (never one query per run — the N+1 the docstring's own convention at the
    top of this module warns about), and a write only when the status
    actually changed, so a steady-state pass with nothing new touches no
    file. ``mc-head``'s own safety checks (clean tree, HEAD pushed to the
    scratch origin) are what actually protect a worktree; this hint can only
    ever make it consider removal EARLIER, never skip a real check."""
    ended = [r for r in runs if r.task_id and derive_for_run(r, now)["state"] not in ACTIVE_STATES]
    if not ended:
        return
    task_ids: set[uuid.UUID] = set()
    for r in ended:
        try:
            task_ids.add(uuid.UUID(r.task_id))
        except ValueError:
            continue
    if not task_ids:
        return
    rows = (await session.exec(select(Task).where(Task.id.in_(task_ids)))).all()
    status_by_task = {str(t.id): t.status for t in rows}
    for run in ended:
        status = status_by_task.get(run.task_id)
        current = files.read_backend_file(run.run_id, "task.json")
        if current.get("status") != status:
            files.write_backend_file(run.run_id, "task.json", {"status": status, "at": now})


class HeadsSync:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        interval = settings.heads_sync_interval
        if self._running or not interval or interval <= 0 or interval >= 99999:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(interval), name="heads_sync")
        logger.info("heads sync started (interval=%ss, enabled=%s)", interval, settings.heads_enabled)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self, interval: int) -> None:
        from app.database import engine

        while self._running:
            await asyncio.sleep(interval)
            if not settings.heads_enabled:
                continue
            try:
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    await sync_once(session)
            except Exception:  # noqa: BLE001 — never kill the loop
                logger.exception("heads sync pass failed")


heads_sync = HeadsSync()
