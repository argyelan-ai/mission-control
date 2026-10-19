"""Self-update API — Settings → System (ADR-094, update plan PR 4c).

    POST /api/v1/system/update/preflight   admin            checks (door + backend)
    POST /api/v1/system/update/start       admin + step-up  start a run → 202
    GET  /api/v1/system/update/status      admin            the run, drain, running work
    POST /api/v1/system/update/rollback    admin + step-up  cancel before the switch / go back
    POST /api/v1/system/update/resume      admin            finish an interrupted run
    GET  /api/v1/system/update/history     admin            past runs

The backend is only the messenger: the cli-bridge's update door on the host
decides and the engine does the work (``services/mc_update_runner.py``).

Gates, in this order, on every route:

1. admin role (403);
2. ``MC_UPDATE_MANAGED=deploy`` → **409** ``update_managed`` — the
   operator's own install is updated through its deploy procedure only;
3. ``MC_SELF_UPDATE_ENABLED`` off (default) → **404** ``update_disabled`` —
   the button stays off until it passed its acceptance (ADR-094 decision 13);
4. ``start`` and ``rollback``: step-up — a fresh password confirmation
   (``services/step_up.py``; missing → 403 ``step_up_required``, never 401,
   so the UI does not sign the operator out).

Errors carry ``detail.code`` (the UI words them in EN/DE) plus an English
``message``; where only the machine can help, ``detail.command`` is the
command to run there.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.config import settings
from app.database import get_session
from app.models.system_update import SystemUpdate
from app.redis_client import get_redis
from app.services import mc_update_runner as runner
from app.services import step_up, update_drain
from app.services.mc_update_runner import DoorReply, DoorUnreachable

router = APIRouter(prefix="/api/v1/system/update", tags=["system-update"])


def _err(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _gate() -> None:
    if runner.managed():
        raise _err(409, "update_managed",
                   "This install is updated through its deploy procedure (MC_UPDATE_MANAGED=deploy).",
                   managed=True)
    if not settings.mc_self_update_enabled:
        raise _err(404, "update_disabled", "The self-update is not enabled on this install.")


_ADMIN = [Depends(require_role(Role.ADMIN)), Depends(_gate)]


class TargetBody(BaseModel):
    expected_target: str | None = Field(default=None, max_length=32)


class StartBody(BaseModel):
    expected_target: str = Field(max_length=32)


class RunBody(BaseModel):
    run_id: str | None = Field(default=None, max_length=40)


def _valid_tag(tag: str | None) -> str | None:
    if tag is None:
        return None
    if not runner.RELEASE_TAG_RE.match(tag):
        raise _err(400, "update_bad_target", "expected_target must be a release tag vX.Y.Z")
    return tag


def _valid_run(run_id: str | None) -> str | None:
    if run_id is None:
        return None
    if not runner.RUN_ID_RE.match(run_id):
        raise _err(400, "update_bad_run", "run_id is not an update run id")
    return run_id


def _door_error(reply: DoorReply) -> HTTPException:
    """Turn a door refusal into the API's answer (codes the UI words)."""
    d = reply.data or {}
    msg = runner.scrub(str(d.get("error") or "")) or f"the update door answered {reply.status}"
    if reply.status == 401:
        return _err(502, "door_token",
                    "The update door rejected MC's token. On the machine, set the bridge up again.",
                    command=runner.CMD_BRIDGE)
    if reply.status == 409 and d.get("managed"):
        return _err(409, "update_managed", msg, managed=True)
    if reply.status == 409:
        return _err(409, "update_refused", msg)
    if reply.status == 422:
        return _err(422, "preflight_failed", msg, checks=runner.filter_checks(d.get("checks")))
    if reply.status == 423:
        extra = {}
        if d.get("run_id"):
            extra["run_id"] = runner.scrub(str(d["run_id"]), 40)
        if d.get("interrupted"):
            extra.update(interrupted=True, command=runner.CMD_RESUME)
        return _err(423, "update_busy", msg, **extra)
    if reply.status == 429:
        return _err(429, "update_rate_limited", msg, used=d.get("used"), limit=d.get("limit"))
    if reply.status == 400:
        return _err(400, "update_bad_request", msg)
    if reply.status == 404:
        return _err(404, "update_run_unknown", msg)
    return _err(502, "update_engine_error", msg)


def _unreachable() -> HTTPException:
    return _err(503, "bridge_unreachable",
                "The update service on the machine (cli-bridge) does not answer.",
                command=runner.CMD_BRIDGE)


async def _drain_view(session: AsyncSession) -> dict:
    state = await update_drain.current(session)
    row = await update_drain.read_row(session) if state.active else None
    return {
        "active": state.active,
        "run_id": state.run_id,
        "expires_at": row.expires_raw if row is not None else None,
    }


# ── preflight ─────────────────────────────────────────────────────────────


@router.post("/preflight", dependencies=_ADMIN)
async def update_preflight(
    request: Request,
    body: TargetBody | None = None,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Read-only. Every check: ``id``, ``status`` ok|warn|fail, ``message``
    (and ``command`` where only the machine can help). ``ok`` is false as
    soon as one check fails; then the start is refused."""
    expected = _valid_tag(body.expected_target if body else None)
    if expected is None:
        from app.services.update_check import get_latest_release

        latest = (await get_latest_release()).get("tag")
        expected = latest if isinstance(latest, str) and runner.RELEASE_TAG_RE.match(latest) else None

    backend = await runner.backend_checks(session, step_up.unavailable_reason(request, current_user))
    result: dict = {"current": settings.app_version, "target": expected, "up_to_date": False,
                    "bridge": {"reachable": False, "api_version": None}}

    door_checks: list[dict]
    try:
        ping = await runner.door.ping()
        api_version = ping.data.get("api_version") if ping.status == 200 else None
        result["bridge"] = {"reachable": True, "api_version": api_version}
        if not isinstance(api_version, int) or api_version < runner.DOOR_API_VERSION:
            door_checks = [{"id": "bridge", "status": "fail",
                            "message": "the cli-bridge on the machine is too old for updates from Settings — "
                                       "set it up again",
                            "command": runner.CMD_BRIDGE}]
        else:
            reply = await runner.door.preflight(expected)
            if reply.status == 200:
                door_checks = runner.filter_checks(reply.data.get("checks"))
                for key in ("current", "target", "up_to_date"):
                    if key in reply.data:
                        result[key] = runner.scrub(reply.data[key])
                starts = reply.data.get("starts")
                if isinstance(starts, dict):
                    result["starts"] = {"used": starts.get("used"), "limit": starts.get("limit")}
            elif reply.status == 409 and reply.data.get("managed"):
                raise _door_error(reply)
            elif reply.status == 401:
                door_checks = [{"id": "bridge", "status": "fail",
                                "message": "the update door rejected MC's token — set the bridge up again",
                                "command": runner.CMD_BRIDGE}]
            else:
                err = _door_error(reply).detail
                door_checks = [{"id": "release" if reply.status == 409 else "engine", "status": "fail",
                                "message": err["message"]}]
    except DoorUnreachable:
        door_checks = [{"id": "bridge", "status": "fail",
                        "message": "the update service on the machine (cli-bridge) does not answer",
                        "command": runner.CMD_BRIDGE}]

    checks = door_checks + backend
    result["checks"] = checks
    result["ok"] = not any(c["status"] == "fail" for c in checks)
    result["running_work"] = await runner.running_work(session)
    result["drain"] = await _drain_view(session)
    return result


# ── start ─────────────────────────────────────────────────────────────────


@router.post("/start", status_code=202, dependencies=_ADMIN)
async def update_start(
    body: StartBody,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(step_up.require_recent_auth()),
):
    """Start an update to ``expected_target`` (the release the UI offered —
    only compared; the engine picks the target itself, a mismatch is 409).
    Due scheduled jobs refuse the start untouched (422)."""
    target = _valid_tag(body.expected_target)
    due = await runner.due_schedules(session)
    if due:
        raise _err(422, "preflight_failed", "a scheduled job is due within 15 minutes — nothing was changed",
                   checks=[c for c in await runner.backend_checks(session, None) if c["id"] == "schedules"])
    try:
        reply = await runner.door.start(target)
    except DoorUnreachable:
        raise _unreachable() from None
    if reply.status == 202:
        run_id = reply.data.get("run_id")
        if isinstance(run_id, str) and runner.RUN_ID_RE.match(run_id):
            await runner.record_start(session, run_id=run_id, user=current_user, target=target)
        return {"run_id": run_id, "state": reply.data.get("state", "started"), "target": target}
    if reply.status == 200:
        # Nothing to do (already on the latest release).
        return JSONResponse(status_code=200, content={
            "started": False, "up_to_date": bool(reply.data.get("up_to_date")),
            "current": runner.scrub(reply.data.get("current")),
        })
    raise _door_error(reply)


# ── status ────────────────────────────────────────────────────────────────


@router.get("/status", dependencies=_ADMIN)
async def update_status(
    run: str | None = Query(default=None, max_length=40),
    session: AsyncSession = Depends(get_session),
):
    """The latest (or the given) run as the journal on the machine tells it,
    plus the drain and the running work MC waits for. Bridge unreachable →
    still 200: the last known run from the Redis mirror, ``bridge_reachable``
    false and the command to run on the machine."""
    run = _valid_run(run)
    redis = await get_redis()
    base = {"drain": await _drain_view(session), "running_work": await runner.running_work(session)}
    try:
        reply = await runner.door.status(run)
    except DoorUnreachable:
        mirror = await runner.read_mirror(redis)
        if run and mirror and mirror.get("run_id") != run:
            mirror = None
        return {
            **base,
            "bridge_reachable": False,
            "stale": True,
            "run": runner.filter_status(mirror) if mirror else None,
            "command": runner.CMD_RESUME if base["drain"]["active"] else runner.CMD_BRIDGE,
        }
    if reply.status != 200:
        raise _door_error(reply)
    data = reply.data
    boot = ((data.get("bridge") or {}).get("boot") or {}) if isinstance(data.get("bridge"), dict) else {}
    sweep = boot.get("drain_sweep") if isinstance(boot, dict) else None
    if isinstance(sweep, dict):
        await runner.record_drain_sweep(session, sweep.get("lifted"))
    if not data.get("run_id"):
        return {**base, "bridge_reachable": True, "stale": False, "run": None}
    await runner.mirror_status(session, data, redis)
    view = runner.filter_status(data)
    out = {**base, "bridge_reachable": True, "stale": False, "run": view}
    if view.get("interrupted"):
        out["command"] = runner.CMD_RESUME
    elif view.get("phase") == "rollback_failed":
        out["command"] = runner.CMD_ROLLBACK
    return out


# ── rollback / resume ─────────────────────────────────────────────────────


@router.post("/rollback", dependencies=_ADMIN)
async def update_rollback(
    body: RunBody | None = None,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(step_up.require_recent_auth()),
):
    """A live run before the switch: cancel (nothing changed). A finished
    run within 14 days: back to the version before it, from its own
    pre-update backup — everything written since is lost (the UI says so)."""
    run_id = _valid_run(body.run_id if body else None)
    try:
        reply = await runner.door.rollback(run_id)
    except DoorUnreachable:
        raise _unreachable() from None
    if reply.status in (200, 202):
        known = run_id or (reply.data.get("run_id") if isinstance(reply.data.get("run_id"), str) else None)
        if known and runner.RUN_ID_RE.match(known):
            await runner.record_rollback_request(session, run_id=known, user=current_user)
        out = {k: runner.scrub(v) for k, v in reply.data.items()
               if k in ("run_id", "state", "phase", "cancel_requested", "ok", "exit")}
        return JSONResponse(status_code=reply.status, content=out)
    raise _door_error(reply)


@router.post("/resume", dependencies=_ADMIN)
async def update_resume(body: RunBody | None = None):
    """Finish an interrupted run (the bridge does this on its own after a
    reboot; this is the button for when it could not)."""
    run_id = _valid_run(body.run_id if body else None)
    try:
        reply = await runner.door.resume(run_id)
    except DoorUnreachable:
        raise _err(503, "bridge_unreachable",
                   "The update service on the machine (cli-bridge) does not answer.",
                   command=runner.CMD_RESUME) from None
    if reply.status in (200, 202):
        out = {k: runner.scrub(v) for k, v in reply.data.items()
               if k in ("resumed", "run_id", "state", "phase", "finished", "exit")}
        return JSONResponse(status_code=reply.status, content=out)
    raise _door_error(reply)


# ── history ───────────────────────────────────────────────────────────────


@router.get("/history", dependencies=_ADMIN)
async def update_history(
    limit: int = Query(default=20, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
):
    rows = await session.exec(
        select(SystemUpdate).order_by(SystemUpdate.created_at.desc()).limit(limit)  # type: ignore[union-attr]
    )
    return {"runs": [runner.history_view(r) for r in rows.all()]}
