"""The backend's side of the self-update (ADR-094, update plan PR 4c).

The backend never runs an update itself. It asks the cli-bridge on the host,
whose update door (``scripts/mc_update_door.py``) answers on five routes and
starts the engine (``scripts/mc_update.py``) detached. This module is the
client of that door plus the history mirror:

* :class:`Door` — HTTP calls to ``/mc-update/{preflight,start,status,
  rollback,resume}`` with the door's own token (``X-MC-Update-Token``, read
  from the 0600 file the bridge created — never from the environment).
* :func:`backend_checks` — what only the backend can see: scheduled jobs due
  within 15 minutes (red), running loops and group-chat rounds (notes),
  whether this account can confirm with its password (step-up).
* :func:`mirror_status` — the journal is the truth; every status read
  upserts the run into ``system_updates`` (the history) and Redis (a mirror
  the UI falls back to while the bridge is briefly unreachable).

Nothing from the door reaches the UI unfiltered: :func:`filter_status`
keeps a fixed set of fields and strips home paths, so neither tokens nor
the raw log nor machine paths leave the backend.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.system_update import SystemUpdate
from app.utils import utcnow

logger = logging.getLogger("mc.mc_update")

TOKEN_HEADER = "X-MC-Update-Token"
DOOR_API_VERSION = 1
RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
TERMINAL_PHASES = frozenset(("done", "aborted", "rolled_back", "rollback_failed"))

#: Scheduled jobs due within this window turn the preflight red (ADR-094 §7).
SCHEDULE_WINDOW = timedelta(minutes=15)

#: What the operator types on the machine when the door cannot help.
CMD_BRIDGE = "./install.sh --bridge"
CMD_RESUME = "./install.sh --resume"
CMD_ROLLBACK = "./install.sh --rollback"

# Timeouts: the door's synchronous engine calls may take up to 180 s
# (sync_timeout), a start waits up to 90 s for the journal (start_wait).
PREFLIGHT_TIMEOUT = 200.0
START_TIMEOUT = 120.0
STATUS_TIMEOUT = 200.0
SHORT_TIMEOUT = 5.0

MIRROR_KEY = "mc:update:status"
MIRROR_TTL = 7 * 86400


class DoorUnreachable(Exception):
    """The bridge did not answer (not running, wrong address, firewall).

    ``never_sent`` is True when no connection was made, so the request
    provably did not reach the bridge (connection refused / connect
    timeout). A read timeout is not that: the action may have started."""

    def __init__(self, message: str = "", *, never_sent: bool = False):
        super().__init__(message)
        self.never_sent = never_sent


def _unreachable_from(exc: httpx.HTTPError) -> DoorUnreachable:
    return DoorUnreachable(type(exc).__name__,
                           never_sent=isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)))


@dataclass(frozen=True)
class DoorReply:
    status: int
    data: dict


def token_file_path() -> Path:
    explicit = (settings.mc_update_token_file or "").strip()
    if explicit:
        return Path(explicit)
    return Path(settings.home_host) / ".mc" / "secrets" / "mc-update.token"


def door_token() -> str | None:
    """The door's token, or None. Read per call (a rotated file is picked up).
    The value never reaches a log line or an answer."""
    try:
        token = token_file_path().read_text().strip()
    except OSError:
        return None
    return token or None


def managed() -> bool:
    return (settings.mc_update_managed or "").strip() == "deploy"


class Door:
    """Client of the bridge's update door. ``transport`` is for tests."""

    def __init__(self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = (base_url or settings.free_code_bridge_url).rstrip("/")
        self.transport = transport

    def _client(self, timeout: float) -> httpx.AsyncClient:
        headers = {}
        token = door_token()
        if token:
            headers[TOKEN_HEADER] = token
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout, headers=headers,
                                 transport=self.transport)

    async def _call(self, method: str, path: str, *, timeout: float,
                    body: dict | None = None, params: dict | None = None) -> DoorReply:
        try:
            async with self._client(timeout) as client:
                r = await client.request(method, path, json=body if method == "POST" else None,
                                         params=params)
        except httpx.HTTPError as exc:
            raise _unreachable_from(exc) from exc
        try:
            data = r.json()
        except ValueError:
            data = {}
        return DoorReply(r.status_code, data if isinstance(data, dict) else {})

    async def ping(self) -> DoorReply:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=SHORT_TIMEOUT,
                                         transport=self.transport) as client:
                r = await client.get("/ping")
        except httpx.HTTPError as exc:
            raise _unreachable_from(exc) from exc
        try:
            data = r.json()
        except ValueError:
            data = {}
        return DoorReply(r.status_code, data if isinstance(data, dict) else {})

    async def preflight(self, expected_target: str | None) -> DoorReply:
        body = {"expected_target": expected_target} if expected_target else {}
        return await self._call("POST", "/mc-update/preflight", timeout=PREFLIGHT_TIMEOUT, body=body)

    async def start(self, expected_target: str) -> DoorReply:
        return await self._call("POST", "/mc-update/start", timeout=START_TIMEOUT,
                                body={"expected_target": expected_target})

    async def status(self, run_id: str | None = None) -> DoorReply:
        return await self._call("GET", "/mc-update/status", timeout=STATUS_TIMEOUT,
                                params={"run": run_id} if run_id else None)

    async def rollback(self, run_id: str | None = None) -> DoorReply:
        return await self._call("POST", "/mc-update/rollback", timeout=START_TIMEOUT,
                                body={"run_id": run_id} if run_id else {})

    async def resume(self, run_id: str | None = None) -> DoorReply:
        return await self._call("POST", "/mc-update/resume", timeout=START_TIMEOUT,
                                body={"run_id": run_id} if run_id else {})


#: The door the routes use; tests swap it.
door = Door()


def set_door(new: Door) -> Door:
    global door
    previous, door = door, new
    return previous


# ── filtering ─────────────────────────────────────────────────────────────

_HOME_PATH = re.compile(r"(?:/Users|/home)/[^/\s'\"]+")


def scrub(text: Any, limit: int = 500) -> Any:
    """No machine paths, bounded length. Non-strings pass through."""
    if not isinstance(text, str):
        return text
    return _HOME_PATH.sub("~", text)[:limit]


def _pick(d: Any, keys: tuple[str, ...]) -> dict:
    if not isinstance(d, dict):
        return {}
    return {k: scrub(d.get(k)) for k in keys if k in d}


def filter_checks(checks: Any) -> list[dict]:
    out = []
    for c in checks if isinstance(checks, list) else []:
        if isinstance(c, dict) and c.get("status") in ("ok", "warn", "fail"):
            item = {"id": scrub(str(c.get("id", "")), 40), "status": c["status"],
                    "message": scrub(str(c.get("message", "")))}
            if c.get("command"):
                item["command"] = scrub(str(c["command"]), 200)
            out.append(item)
    return out


def filter_status(data: dict) -> dict:
    """The run as the UI may see it: fixed fields, no paths, no raw log."""
    out = _pick(data, ("run_id", "phase", "phase_state", "status", "source", "created_at",
                       "updated_at", "finished_at", "error", "failure_kind", "backup", "interrupted"))
    out["from"] = _pick(data.get("from"), ("tag", "version"))
    out["target"] = _pick(data.get("target"), ("tag", "version"))
    lock = data.get("lock") if isinstance(data.get("lock"), dict) else {}
    out["running"] = bool(lock.get("held"))
    out["events"] = [
        _pick(e, ("at", "phase", "event", "detail"))
        for e in (data.get("events") or [])[-20:]
        if isinstance(e, dict)
    ]
    bridge = data.get("bridge") if isinstance(data.get("bridge"), dict) else {}
    boot = bridge.get("boot") if isinstance(bridge.get("boot"), dict) else {}
    starts = bridge.get("starts") if isinstance(bridge.get("starts"), dict) else {}
    out["bridge"] = {
        "api_version": bridge.get("api_version"),
        "boot": _pick(boot, ("state", "run_id", "phase", "at")),
        "starts": _pick(starts, ("used", "limit")),
    }
    return out


# ── backend checks ────────────────────────────────────────────────────────


async def due_schedules(session: AsyncSession, now: datetime | None = None) -> list[dict]:
    """Enabled, not snoozed, supported jobs whose next run falls within
    :data:`SCHEDULE_WINDOW`."""
    from app.models.scheduled_job import ScheduledJob

    now = now or utcnow()
    rows = await session.exec(
        select(ScheduledJob).where(
            ScheduledJob.enabled == True,  # noqa: E712
            ScheduledJob.next_run_at.isnot(None),  # type: ignore[union-attr]
            ScheduledJob.next_run_at >= now,  # type: ignore[operator]
            ScheduledJob.next_run_at <= now + SCHEDULE_WINDOW,  # type: ignore[operator]
            ScheduledJob.action_type.in_(("create_task", "start_loop")),  # type: ignore[union-attr]
        )
    )
    out = []
    for job in rows.all():
        if job.snoozed_until is not None and _aware(job.snoozed_until) > _aware(job.next_run_at):
            continue
        out.append({"id": str(job.id), "name": job.name, "next_run_at": _aware(job.next_run_at).isoformat()})
    return out


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


async def running_work(session: AsyncSession) -> dict[str, int]:
    """Same definition as the SQL function update_running_work() (migration 0219), in
    the ORM so the SQLite lane counts it too: tasks in progress + open rounds
    of running group chats. A Postgres-lane test pins both to each other."""
    from app.models.group import AgentGroup, GroupRound
    from app.models.task import Task

    tasks = (await session.exec(select(func.count(Task.id)).where(Task.status == "in_progress"))).one()
    rounds = (await session.exec(
        select(func.count(GroupRound.id))
        .join(AgentGroup, AgentGroup.id == GroupRound.group_id)
        .where(GroupRound.finished_at.is_(None), AgentGroup.status == "running")  # type: ignore[union-attr]
    )).one()
    return {"tasks": int(tasks or 0), "group_rounds": int(rounds or 0)}


async def running_loops(session: AsyncSession) -> int:
    from app.models.loop import Loop

    return int((await session.exec(select(func.count(Loop.id)).where(Loop.status == "running"))).one() or 0)


async def backend_checks(session: AsyncSession, step_up_reason: str | None) -> list[dict]:
    """Checks only the backend can answer, in the door's check format."""
    from app.services import step_up

    checks: list[dict] = []
    if step_up_reason is None:
        checks.append({"id": "step_up", "status": "ok",
                       "message": "you can confirm the update with your password"})
    else:
        checks.append({"id": "step_up", "status": "fail",
                       "message": step_up._MESSAGES.get(step_up_reason, "step-up is not available"),
                       "code": step_up_reason, "command": "./install.sh --update"})

    due = await due_schedules(session)
    if due:
        names = ", ".join(d["name"] for d in due[:3]) + (" …" if len(due) > 3 else "")
        checks.append({"id": "schedules", "status": "fail",
                       "message": (f"{len(due)} scheduled job(s) due within 15 minutes ({names}) — "
                                   "update after they ran, or snooze them"),
                       "items": due})
    else:
        checks.append({"id": "schedules", "status": "ok", "message": "no scheduled job due within 15 minutes"})

    work = await running_work(session)
    if work["group_rounds"]:
        checks.append({"id": "group_rounds", "status": "warn",
                       "message": (f"{work['group_rounds']} group-chat round(s) running — the update waits "
                                   "until they are done; no new round starts meanwhile")})
    loops = await running_loops(session)
    if loops:
        checks.append({"id": "loops", "status": "warn",
                       "message": (f"{loops} loop(s) active — a round in progress is waited for, the next "
                                   "round starts after the update")})
    return checks


# ── history ───────────────────────────────────────────────────────────────


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _aware(ts)


async def record_start(session: AsyncSession, *, run_id: str, user, target: str) -> SystemUpdate:
    row = (await session.exec(select(SystemUpdate).where(SystemUpdate.run_id == run_id))).first()
    if row is None:
        row = SystemUpdate(run_id=run_id, source="ui")
    row.from_version = row.from_version or settings.app_version
    row.target_version = target
    row.phase = row.phase or "preflight"
    row.started_by_user_id = getattr(user, "id", None)
    row.started_by_name = (getattr(user, "name", None) or getattr(user, "email", None) or "")[:200] or None
    row.started_at = row.started_at or utcnow()
    row.updated_at = utcnow()
    session.add(row)
    await session.commit()
    return row


async def record_rollback_request(session: AsyncSession, *, run_id: str | None, user) -> None:
    if not run_id:
        return
    row = (await session.exec(select(SystemUpdate).where(SystemUpdate.run_id == run_id))).first()
    if row is None:
        row = SystemUpdate(run_id=run_id, source="ui")
    row.rollback_requested_by_user_id = getattr(user, "id", None)
    row.rollback_requested_by_name = (getattr(user, "name", None) or getattr(user, "email", None) or "")[:200] or None
    row.rollback_requested_at = utcnow()
    row.updated_at = utcnow()
    session.add(row)
    await session.commit()


async def mirror_status(session: AsyncSession, status: dict, redis=None) -> SystemUpdate | None:
    """Upsert the run the door reported into the history (+ Redis mirror)."""
    run_id = status.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        return None
    row = (await session.exec(select(SystemUpdate).where(SystemUpdate.run_id == run_id))).first()
    if row is None:
        source = status.get("source")
        row = SystemUpdate(run_id=run_id, source=source if source in ("bridge", "terminal", "ui") else "terminal")
    frm, tgt = status.get("from") or {}, status.get("target") or {}
    row.from_version = frm.get("tag") or row.from_version
    row.target_version = tgt.get("tag") or row.target_version
    row.phase = (status.get("phase") or row.phase or "")[:24] or None
    row.phase_state = (status.get("phase_state") or "")[:24] or None
    row.error = status.get("error")
    row.failure_kind = (status.get("failure_kind") or "")[:32] or None
    row.backup = (status.get("backup") or "")[:200] or None
    row.started_at = row.started_at or _ts(status.get("created_at"))
    row.finished_at = _ts(status.get("finished_at")) or (row.finished_at if row.phase in TERMINAL_PHASES else None)
    row.updated_at = utcnow()
    session.add(row)
    await session.commit()
    if redis is not None:
        try:
            await redis.set(MIRROR_KEY, json.dumps({**status, "mirrored_at": utcnow().isoformat()}),
                            ex=MIRROR_TTL)
        except Exception as exc:  # noqa: BLE001 — a mirror is never required
            logger.debug("update status mirror failed: %s", exc)
    return row


async def record_drain_sweep(session: AsyncSession, lifted: Any) -> int:
    """The bridge's start routine lifted drains no run owned — note it on
    those runs (ADR-094 decision 7: "records that in the history")."""
    n = 0
    for item in lifted if isinstance(lifted, list) else []:
        run_id = item.get("run_id") if isinstance(item, dict) else None
        if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
            continue
        row = (await session.exec(select(SystemUpdate).where(SystemUpdate.run_id == run_id))).first()
        if row is None:
            row = SystemUpdate(run_id=run_id, source="terminal")
        if row.drain_lifted_at is None:
            row.drain_lifted_at = utcnow()
            row.updated_at = utcnow()
            session.add(row)
            n += 1
    if n:
        await session.commit()
    return n


async def read_mirror(redis) -> dict | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(MIRROR_KEY)
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def history_view(row: SystemUpdate) -> dict:
    return {
        "run_id": row.run_id, "source": row.source,
        "from_version": row.from_version, "target_version": row.target_version,
        "phase": row.phase, "error": scrub(row.error), "failure_kind": row.failure_kind,
        "backup": row.backup,
        "started_by": row.started_by_name, "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "rollback_requested_by": row.rollback_requested_by_name,
        "rollback_requested_at": _iso(row.rollback_requested_at),
        "drain_lifted_at": _iso(row.drain_lifted_at),
    }


def _iso(ts: datetime | None) -> str | None:
    return _aware(ts).isoformat() if ts is not None else None


async def reconcile_on_boot(delay: float = 5.0) -> str:
    """Backend start: bring the history in line with the journal on the
    machine (ADR-094 decision 3 — the backend compares at start through the
    bridge's status). After an update this records how the run ended even if
    nobody had the Settings page open. Never raises; returns what it did."""
    import asyncio

    from app.database import engine
    from app.redis_client import get_redis

    if managed() or not settings.mc_self_update_enabled:
        return "skipped"
    await asyncio.sleep(delay)
    try:
        reply = await door.status()
    except DoorUnreachable:
        return "bridge_unreachable"
    except Exception as exc:  # noqa: BLE001
        logger.warning("update history reconcile failed: %s", type(exc).__name__)
        return "error"
    if reply.status != 200 or not isinstance(reply.data, dict):
        return f"door_{reply.status}"
    try:
        redis = await get_redis()
    except Exception:  # noqa: BLE001
        redis = None
    async with AsyncSession(engine, expire_on_commit=False) as session:
        boot = (reply.data.get("bridge") or {}).get("boot") if isinstance(reply.data.get("bridge"), dict) else None
        sweep = boot.get("drain_sweep") if isinstance(boot, dict) else None
        if isinstance(sweep, dict):
            await record_drain_sweep(session, sweep.get("lifted"))
        if reply.data.get("run_id"):
            await mirror_status(session, reply.data, redis)
            return "mirrored"
    return "no_run"
