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

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.task import Task
from app.services.heads import files
from app.services.heads.mirror import apply_head_state
from app.services.heads.state import derive_for_run

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
    latest, superseded = latest_runs_by_task(files.list_runs())
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
    return changed


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
