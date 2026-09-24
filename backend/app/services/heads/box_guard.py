"""Box occupancy + the switch lock (docs/specs/head-launcher.md §6.7).

Box keys are HOST IDS (not host:port): a duo recipe holds both member boxes,
and an exclusive recipe on another port still displaces the engine.

A box is busy when
- the host wrapper holds ``heads/locks/<host-id>`` for a run whose heartbeat
  is fresh (the backend cannot see host pids — the heartbeat is the sign of
  life the wrapper writes every 30 s), or
- a run for that box is spooled / starting and has not exited yet.

``check_displacement`` is called from every path that can end an engine
(recipe start, runtime stop, runtime restart). It refuses only a
DISPLACEMENT: another recipe, or stopping/restarting an engine that answers.
Recovering a dead engine (same recipe) stays allowed.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Iterable

from fastapi import HTTPException

from app.config import settings
from app.services.heads import files, paths
from app.services.heads.state import ACTIVE_STATES, HEARTBEAT_FRESH_S, derive_for_run


def occupancy(now: float | None = None) -> dict[str, dict]:
    """``{box_key: {run_id, task_id, harness, runtime_slug, since}}`` for live heads."""
    now = time.time() if now is None else now
    busy: dict[str, dict] = {}
    runs = {r.run_id: r for r in files.list_runs()}
    for run in runs.values():
        derived = derive_for_run(run, now)
        if derived["state"] not in ACTIVE_STATES:
            continue
        for key in run.spec.get("box_keys") or []:
            busy[str(key)] = _entry(run)
    # Locks whose owner run is not in the list (folder removed) but whose
    # heartbeat is still fresh would be odd — count them only via their run.
    locks = paths.locks_dir()
    if locks.is_dir():
        for lock in locks.iterdir():
            try:
                owner = json.loads((lock / "owner.json").read_text())
            except (OSError, ValueError):
                continue
            run = runs.get(str(owner.get("run_id")))
            if run is None:
                continue
            hb = run.heartbeat_mtime
            if hb is not None and now - hb < HEARTBEAT_FRESH_S:
                busy.setdefault(lock.name, _entry(run))
    return busy


def _entry(run) -> dict:
    return {
        "run_id": run.run_id,
        "task_id": run.task_id,
        "title": run.spec.get("title"),
        "harness": run.spec.get("harness"),
        "runtime_slug": run.spec.get("runtime_slug"),
        "since": run.status.get("started_at") or run.spec.get("created_at"),
    }


def heads_on(host_ids: Iterable) -> list[dict]:
    if not settings.heads_enabled:
        return []
    occ = occupancy()
    seen, out = set(), []
    for hid in host_ids:
        entry = occ.get(str(hid))
        if entry and entry["run_id"] not in seen:
            seen.add(entry["run_id"])
            out.append(entry)
    return out


def refuse(entry: dict) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "head_on_box",
            "run_id": entry["run_id"],
            "task_id": entry.get("task_id"),
            "title": entry.get("title"),
        },
    )


def check_displacement(
    host_ids: Iterable[uuid.UUID | str],
    action: str,
    *,
    displaces_engine: bool,
) -> None:
    """Raise 409 ``head_on_box`` when ``action`` would cut off a working head.

    ``displaces_engine``: the caller's verdict whether the action ends an
    engine that currently answers (a different recipe on the box, or a
    stop/restart of a live engine). Recovery of a dead engine → False.
    """
    if not settings.heads_enabled or not displaces_engine:
        return
    entries = heads_on(host_ids)
    if entries:
        raise refuse(entries[0])


async def check_engine_idle(endpoints: Iterable[str]) -> None:
    """ADR-085 box manager rule (b): refuse a model switch while the engine
    reports running requests. vLLM-family engines only (``/metrics``); where
    the metric is missing only rule (a) — the head lock — applies."""
    if not settings.heads_enabled:
        return
    from app.services.heads.engine import running_requests

    for endpoint in endpoints:
        n = await running_requests(endpoint)
        if n:
            raise HTTPException(status_code=409, detail={"code": "engine_busy", "running_requests": n})


async def guard_runtime_action(session, runtime_id: uuid.UUID, action: str) -> None:
    """Runtime stop / restart: refuse when a head works on the runtime's boxes
    AND the engine answers (a dead engine may be restarted — recovery)."""
    if not settings.heads_enabled:
        return
    from app.models.runtime import Runtime
    from app.services.heads.engine import served_models
    from app.services.heads.pairs import box_keys_for

    runtime = await session.get(Runtime, runtime_id)
    if runtime is None:
        return
    entries = heads_on(await box_keys_for(session, runtime))
    if not entries:
        return
    live = bool(runtime.endpoint) and (await served_models(runtime.endpoint)) is not None
    if live:
        raise refuse(entries[0])
