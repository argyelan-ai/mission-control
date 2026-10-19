"""The update drain in Postgres (ADR-094 decision 7, update plan PR 4c).

While an MC update runs, MC hands out **no new work**: running work may
finish, nothing new starts. The update engine on the host sets the drain as
one row in ``system_state`` (``key = 'update_drain'``, value
``{"run_id", "expires_at", "set_by"}``, see ``app/models/system_update.py``)
and lifts it when the run ends. This module is the backend's side of that
contract:

* :func:`current` — "is the system drained right now?" for the async places
  that hand out work: dispatch (``operations.check_dispatch_allowed``), the
  poll claim of fresh inbox tasks, the watchdog's re-dispatch, the group
  runner and the loop runner (new rounds), the scheduler (due jobs). With a
  session it reads the row fresh (one primary-key lookup); without one it
  answers from the cache the watcher keeps.
* :class:`PostgresDrainSource` — the same drain for the Docker gateway
  (``services/docker_gateway.py``), whose reads are synchronous. It serves
  the cached row; :func:`watch` keeps the cache fresh in every process.
* :func:`defer` / :func:`catch_up` — work the drain held back is recorded as
  ``deferred:<kind>:<id>`` rows and handed out again once the drain is gone
  (ADR-094: "due jobs are caught up after the update"). Rows live in
  Postgres, so they survive the backend and worker restarts an update does.

Why not Redis: the drain must travel with the data the pre-update backup
holds, and a deliberate ``FLUSHALL`` must not lift it. Redis only mirrors
the run status for the UI (``routers/system_update.py``).

Expiry: a drain counts only while ``expires_at`` lies in the future. The
engine pushes it out with every phase; if a crashed run leaves one behind,
it lifts itself at the latest after its expiry (and the bridge's start
routine lifts it earlier). A row whose expiry cannot be read counts as
drained — an unreadable drain must not hand out work during an update.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.system_update import DEFERRED_PREFIX, DRAIN_KEY, SystemState
from app.services import docker_gateway
from app.services.docker_gateway import NOT_DRAINED, DrainState

logger = logging.getLogger("mc.update_drain")

#: How often the watcher re-reads the drain row (the gateway trusts a read
#: for docker_gateway.DRAIN_CACHE_SECONDS on top of that).
WATCH_INTERVAL_SECONDS = 5.0

#: Kinds of deferred work and what catches them up.
KIND_DISPATCH = "dispatch"
KIND_SCHEDULE = "schedule"

REASON = "an MC update is in progress — new work starts again after it"


@dataclass(frozen=True)
class DrainRow:
    """The drain row as read, before its expiry is judged."""

    run_id: str | None
    expires_at: datetime | None
    expires_raw: Any
    set_by: str | None

    def state(self, now: datetime | None = None) -> DrainState:
        now = now or datetime.now(timezone.utc)
        if self.expires_at is None:
            # Present but unreadable: fail closed (see module docstring).
            return DrainState(active=True, run_id=self.run_id, reason="update drain (no readable expiry)")
        if self.expires_at <= now:
            return NOT_DRAINED
        return DrainState(active=True, run_id=self.run_id, reason=f"update run {self.run_id or '?'}")


def _parse_expiry(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def parse_row(value: Any) -> DrainRow:
    data = value if isinstance(value, dict) else {}
    run_id = data.get("run_id")
    return DrainRow(
        run_id=str(run_id) if run_id else None,
        expires_at=_parse_expiry(data.get("expires_at")),
        expires_raw=data.get("expires_at"),
        set_by=str(data.get("set_by")) if data.get("set_by") else None,
    )


# ── cache ─────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_row: DrainRow | None = None  # None = no drain row
_loaded = False


def _remember(row: DrainRow | None) -> None:
    global _row, _loaded
    with _lock:
        _row = row
        _loaded = True


def cached_state(now: datetime | None = None) -> DrainState:
    """The drain as last read, with its expiry judged against ``now``.

    Before the first successful read this is "not drained" — the boot path
    reads once before anything hands out work (:func:`prime`)."""
    with _lock:
        row = _row
    return row.state(now) if row is not None else NOT_DRAINED


def reset_cache() -> None:
    """Tests: forget what was read."""
    global _row, _loaded
    with _lock:
        _row = None
        _loaded = False


# ── reads ─────────────────────────────────────────────────────────────────


async def read_row(session: AsyncSession) -> DrainRow | None:
    row = await session.get(SystemState, DRAIN_KEY, populate_existing=True)
    if row is None:
        return None
    return parse_row(row.value)


async def _fresh(bind) -> DrainRow | None:
    async with AsyncSession(bind, expire_on_commit=False) as s:
        return await read_row(s)


async def current(session: AsyncSession | None = None) -> DrainState:
    """The drain right now.

    With ``session``: a fresh read of the row through that session (a
    primary-key lookup, autoflush off so the caller's pending changes stay
    untouched) — the dispatch decision must not be up to seconds stale, or
    work could be handed out just after the engine set the drain. Without:
    the cached state. A failed read keeps the last known state — a drain
    must not lift because one read hiccupped. (The table exists from
    migration 0219 on; the backend migrates before it serves.)"""
    if session is None:
        return cached_state()
    try:
        with session.no_autoflush:
            row = await read_row(session)
    except Exception as exc:  # noqa: BLE001 — never let a read break dispatch
        logger.warning("update drain read failed (%s) — keeping the last known state", type(exc).__name__)
        return cached_state()
    _remember(row)
    return row.state() if row is not None else NOT_DRAINED


async def is_drained(session: AsyncSession | None = None) -> bool:
    return (await current(session)).active


# ── the Docker gateway's source ───────────────────────────────────────────


class PostgresDrainSource:
    """``docker_gateway.DrainSource`` backed by the cached Postgres row.

    ``read`` is called synchronously from the event loop and from worker
    threads alike, so it never touches the database itself; :func:`watch`
    keeps the cache fresh. Before the first successful read it raises — the
    gateway then keeps its own last known state."""

    def read(self) -> DrainState:
        with _lock:
            loaded = _loaded
        if not loaded:
            raise RuntimeError("update drain not read yet")
        return cached_state()


def install_gateway_source() -> None:
    """Make the Docker gateway honour the Postgres drain (next to the
    manual ``MC_UPDATE_DRAIN`` switch). Idempotent."""
    source = docker_gateway._guard.source
    if isinstance(source, docker_gateway.AnyDrainSource) and any(
        isinstance(s, PostgresDrainSource) for s in source.sources
    ):
        return
    docker_gateway.set_drain_source(
        docker_gateway.AnyDrainSource(docker_gateway.SettingsDrainSource(), PostgresDrainSource())
    )


async def prime(bind=None) -> DrainState:
    """First read at boot, before anything hands out work."""
    try:
        row = await _fresh(bind or engine)
    except Exception as exc:  # noqa: BLE001
        logger.warning("update drain: first read failed (%s) — will retry", type(exc).__name__)
        return cached_state()
    _remember(row)
    docker_gateway.reset_drain_cache()
    return cached_state()


# ── deferred work ─────────────────────────────────────────────────────────


def _deferred_key(kind: str, ref_id: uuid.UUID | str) -> str:
    return f"{DEFERRED_PREFIX}{kind}:{ref_id}"


async def defer(
    kind: str,
    ref_id: uuid.UUID | str,
    *,
    bind=None,
    run_id: str | None = None,
    detail: dict | None = None,
) -> bool:
    """Record work the drain held back. Idempotent (one row per item).
    Returns False when it could not be recorded (logged, never raised)."""
    value = {
        "kind": kind,
        "id": str(ref_id),
        "run_id": run_id,
        "deferred_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **(detail or {}),
    }
    try:
        async with AsyncSession(bind or engine, expire_on_commit=False) as s:
            if await s.get(SystemState, _deferred_key(kind, ref_id)) is not None:
                return True
            s.add(SystemState(key=_deferred_key(kind, ref_id), value=value))
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()  # a parallel defer won the race — same row
        logger.info("update drain: deferred %s %s", kind, ref_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("update drain: could not record deferred %s %s (%s)", kind, ref_id, type(exc).__name__)
        return False


async def pending_deferred(session: AsyncSession) -> list[SystemState]:
    rows = await session.exec(
        select(SystemState).where(SystemState.key.startswith(DEFERRED_PREFIX))  # type: ignore[union-attr]
    )
    return list(rows.all())


async def _take(bind, key: str) -> dict | None:
    """Consume one deferred row (delete-then-act: at most once)."""
    async with AsyncSession(bind, expire_on_commit=False) as s:
        row = await s.get(SystemState, key)
        if row is None:
            return None
        value = dict(row.value or {})
        await s.delete(row)
        await s.commit()
        return value


async def _catch_up_dispatch(bind, task_id: str) -> bool:
    from app.models.task import Task
    from app.services.dispatch import auto_dispatch_task

    async with AsyncSession(bind, expire_on_commit=False) as s:
        task = await s.get(Task, uuid.UUID(task_id))
        if task is None or task.status != "inbox" or task.run_control is not None:
            return False
        board_id = task.board_id
    await auto_dispatch_task(task.id, board_id)
    return True


async def _catch_up_schedule(job_id: str) -> bool:
    from app.services.scheduler import scheduler

    await scheduler.execute_deferred(job_id)
    return True


async def catch_up(bind=None) -> dict[str, int]:
    """Hand out what the drain held back — only when no drain is active.

    Each row is deleted before its work runs, so a crash in between loses
    that item rather than running it twice."""
    bind = bind or engine
    counts = {KIND_DISPATCH: 0, KIND_SCHEDULE: 0, "skipped": 0}
    async with AsyncSession(bind, expire_on_commit=False) as s:
        row = await read_row(s)
        if row is not None and row.state().active:
            return counts
        keys = [r.key for r in await pending_deferred(s)]
    for key in keys:
        value = await _take(bind, key)
        if value is None:
            continue
        kind, ref = value.get("kind"), value.get("id")
        try:
            if kind == KIND_DISPATCH and ref:
                done = await _catch_up_dispatch(bind, ref)
            elif kind == KIND_SCHEDULE and ref:
                done = await _catch_up_schedule(ref)
            else:
                done = False
        except Exception:  # noqa: BLE001 — one bad item must not stop the rest
            logger.exception("update drain: catching up %s failed", key)
            done = False
        if done:
            counts[kind] += 1
        else:
            counts["skipped"] += 1
    if any(counts[k] for k in (KIND_DISPATCH, KIND_SCHEDULE)):
        logger.info(
            "update drain lifted — caught up %d dispatch(es), %d scheduled job(s)",
            counts[KIND_DISPATCH], counts[KIND_SCHEDULE],
        )
    return counts


# ── watcher ───────────────────────────────────────────────────────────────


async def watch(*, catch_up_work: bool, interval: float = WATCH_INTERVAL_SECONDS) -> None:
    """Keep the cache fresh (every process) and, in the process that runs the
    background services, catch up deferred work once the drain is gone."""
    was_active: bool | None = None
    while True:
        try:
            state = await prime()
            if was_active is not None and state.active != was_active:
                logger.info(
                    "update drain %s%s",
                    "set" if state.active else "lifted",
                    f" (run {state.run_id})" if state.run_id else "",
                )
            was_active = state.active
            if catch_up_work and not state.active:
                await catch_up()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the watcher must never die
            logger.exception("update drain watcher tick failed")
        await asyncio.sleep(interval)


async def start(*, catch_up_work: bool) -> asyncio.Task:
    """Boot hook (``background.prepare_process``): wire the gateway, read
    once, then keep watching."""
    from app.utils import create_tracked_task

    install_gateway_source()
    await prime()
    return create_tracked_task(watch(catch_up_work=catch_up_work), name="update_drain_watch")
