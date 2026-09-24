"""Head launcher API (docs/specs/head-launcher.md §7).

  GET  /api/v1/heads/pairs                 viewer    pairs + default (always local)
  POST /api/v1/heads                       operator  start a head for a task
  POST /api/v1/heads/{run_id}/restart      operator  restart with another pair / mode
  GET  /api/v1/heads                       viewer    runs (?task_id= &active= &box=)
  GET  /api/v1/heads/occupancy             viewer    busy boxes
  GET  /api/v1/heads/{run_id}              viewer    spec + derived state
  GET  /api/v1/heads/{run_id}/log          viewer    last lines of head.log, masked
  GET  /api/v1/heads/{run_id}/run-record   viewer    run record markdown
  POST /api/v1/heads/{run_id}/stop         operator  stop request

Errors carry a ``code`` (pair_blocked, engine_not_ready, box_busy,
head_active, repo_required, spool_unavailable, heads_disabled,
task_move_failed); the
frontend renders them via i18n. Behind ``settings.heads_enabled``.
"""
from __future__ import annotations

import contextlib
import re
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.config import settings
from app.database import get_session
from app.log_redaction import redact_secrets
from app.models.repo import Repo
from app.models.task import Task
from app.services.heads import box_guard, files, launcher, pairs
from app.services.heads.files import write_backend_file
from app.services.heads.mirror import move_task
from app.services.heads.state import ACTIVE_STATES, derive_for_run

router = APIRouter(prefix="/api/v1/heads", tags=["heads"])


def _enabled() -> None:
    if not settings.heads_enabled:
        raise HTTPException(status_code=404, detail={"code": "heads_disabled"})


def _err(status: int, code: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, **extra})


def _current_user_id(user) -> str | None:
    uid = getattr(user, "id", None)
    return str(uid) if uid else None


def run_view(run, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    derived = derive_for_run(run, now)
    spec = run.spec
    return {
        "run_id": run.run_id,
        "task_id": run.task_id,
        "title": spec.get("title"),
        "harness": spec.get("harness"),
        "runtime_slug": spec.get("runtime_slug"),
        "model": spec.get("model"),
        "repo_full_name": spec.get("repo_full_name"),
        "branch": spec.get("branch"),
        "mode": spec.get("mode"),
        "restarted_from": spec.get("restarted_from"),
        "box_keys": spec.get("box_keys") or [],
        "created_at": spec.get("created_at"),
        "started_at": run.status.get("started_at"),
        "exited_at": run.status.get("exited_at"),
        "state": derived["state"],
        "reason": derived["reason"],
        "silent_s": derived["silent_s"],
        "heartbeat_stale": derived["heartbeat_stale"],
        "step": run.step,
        "question": run.question,
        "pr_url": run.pr_url,
        "tmux": run.status.get("tmux"),
        "run_record": bool(run.run_record_path),
        "task_deleted": bool(run.mirror.get("task_deleted")),
    }


def _load(run_id: str):
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise _err(404, "run_not_found")
    run = files.load_run(run_id)
    if run is None:
        raise _err(404, "run_not_found")
    return run


@contextlib.contextmanager
def _task_lock(task_id: str):
    try:
        marker = launcher.acquire_task_lock(task_id)
    except launcher.TaskStartBusy:
        raise _err(409, "head_active")
    try:
        yield
    finally:
        launcher.release_task_lock(marker)


def _active_run_for_task(task_id: str, now: float):
    for run in reversed(files.list_runs()):
        if run.task_id == task_id and derive_for_run(run, now)["state"] in ACTIVE_STATES:
            return run
    return None


async def _checked_pair(session: AsyncSession, harness: str, runtime_slug: str, ignore_run_id: str | None = None):
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
        raise _err(422, "pair_blocked", reason_code=code)
    if pair.status == "blocked":
        if pair.reason_code == "engine_not_ready":
            raise _err(409, "engine_not_ready")
        if pair.reason_code == "box_busy":
            raise _err(409, "box_busy", busy_by=pair.busy_by)
        raise _err(422, "pair_blocked", reason_code=pair.reason_code)
    return pair, runtime


async def _task_and_repo(session: AsyncSession, task_id: uuid.UUID) -> tuple[Task, Repo]:
    task = await session.get(Task, task_id)
    if task is None:
        raise _err(404, "task_not_found")
    repo = await session.get(Repo, task.repo_id) if task.repo_id else None
    if repo is None:
        raise _err(422, "repo_required")
    return task, repo


async def _hold_and_move(session: AsyncSession, task: Task, run_id: str, reason: str) -> None:
    """Hold the task and walk it to in_progress BEFORE the spool goes out.

    move_task flushes every hop, so a refused transition surfaces here — and
    then the run folder is removed: the host must never start a run whose
    task change did not happen (live: HTTP 500 after the spool, head ran).
    Any failure becomes 409 ``task_move_failed`` (an HTTPException, so
    ``hold_on_failure`` still holds the card).
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
        raise _err(409, "task_move_failed") from exc


class StartBody(BaseModel):
    task_id: uuid.UUID
    harness: str = Field(max_length=32)
    runtime_slug: str = Field(max_length=64)
    answer: str | None = Field(default=None, max_length=8000)
    #: "Run as head" in New task created this inbox card only for a head. If
    #: the start fails, hold the card so the fleet never picks it up later.
    hold_on_failure: bool = False


HOLD_REASON_START_FAILED = "head start failed"


async def _hold_after_failed_start(session: AsyncSession, task_id: uuid.UUID) -> None:
    task = await session.get(Task, task_id)
    if task is None or task.status != "inbox" or task.run_control is not None:
        return
    task.run_control = "manual_hold"
    task.hold_reason = HOLD_REASON_START_FAILED
    session.add(task)
    await session.commit()


class RestartBody(BaseModel):
    harness: str = Field(max_length=32)
    runtime_slug: str = Field(max_length=64)
    mode: Literal["fresh", "continue"] = "continue"
    answer: str | None = Field(default=None, max_length=8000)


@router.get("/pairs", dependencies=[Depends(require_role(Role.VIEWER))])
async def get_pairs(repo_id: uuid.UUID | None = None, session: AsyncSession = Depends(get_session)):
    _enabled()
    return await pairs.list_pairs(session, box_guard.occupancy())


@router.post("", status_code=201)
async def start_head(
    body: StartBody,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_role(Role.OPERATOR)),
):
    _enabled()
    now = time.time()
    task, repo = await _task_and_repo(session, body.task_id)
    try:
        return await _start(session, body, task, repo, user, now)
    except HTTPException:
        if body.hold_on_failure:
            await session.rollback()
            await _hold_after_failed_start(session, body.task_id)
        raise


async def _start(session: AsyncSession, body: StartBody, task: Task, repo: Repo, user, now: float) -> dict:
    with _task_lock(str(task.id)):
        if _active_run_for_task(str(task.id), now) is not None:
            raise _err(409, "head_active")
        pair, runtime = await _checked_pair(session, body.harness, body.runtime_slug)
        try:
            spec = await launcher.write_run(
                session, task=task, repo=repo, harness=pair.harness, runtime=runtime,
                box_keys=pair.box_keys, user_id=_current_user_id(user), answer=body.answer,
            )
        except OSError as exc:
            raise _err(503, "spool_unavailable") from exc
        # inbox → in_progress AND the hold in one transaction: no operator PATCH
        # from inbox can lift the hold afterwards (routers/tasks.py clears
        # manual_hold only when the old status is inbox).
        await _hold_and_move(session, task, spec["run_id"], reason="head_start")
        try:
            launcher.spool("start", spec["run_id"])
        except launcher.SpoolUnavailable as exc:
            await session.rollback()
            # no half run left behind: it would count as "starting" for
            # 5 minutes and block the next click with head_active
            launcher.discard_run(spec["run_id"])
            raise _err(503, "spool_unavailable") from exc
        await session.commit()
    write_backend_file(spec["run_id"], "mirror.json", {"state": "starting", "at": now})
    return {"run_id": spec["run_id"], "state": "starting", "branch": spec["branch"]}


@router.post("/{run_id}/restart", status_code=202)
async def restart_head(
    run_id: str,
    body: RestartBody,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_role(Role.OPERATOR)),
):
    _enabled()
    old = _load(run_id)
    if not old.task_id:
        raise _err(422, "task_not_found")
    task, repo = await _task_and_repo(session, uuid.UUID(old.task_id))
    now = time.time()
    with _task_lock(old.task_id):
        newest = _active_run_for_task(old.task_id, now)
        if newest is not None and newest.run_id != old.run_id:
            raise _err(409, "head_active")
        pair, runtime = await _checked_pair(session, body.harness, body.runtime_slug, ignore_run_id=old.run_id)
        derived = derive_for_run(old, now)
        try:
            spec = await launcher.write_run(
                session, task=task, repo=repo, harness=pair.harness, runtime=runtime,
                box_keys=pair.box_keys, user_id=_current_user_id(user), answer=body.answer,
                restarted_from={
                    "spec": old.spec, "state": derived["state"], "reason": derived["reason"],
                    "run_record": old.run_record_text, "question": old.question,
                },
                mode=body.mode,
            )
        except OSError as exc:
            raise _err(503, "spool_unavailable") from exc
        await _hold_and_move(session, task, spec["run_id"], reason="head_restart")
        try:
            launcher.spool("restart", spec["run_id"], from_run_id=old.run_id)
        except launcher.SpoolUnavailable as exc:
            # same order as start: the host only hears of a run whose task
            # change is about to be committed; a failed spool leaves nothing
            await session.rollback()
            launcher.discard_run(spec["run_id"])
            raise _err(503, "spool_unavailable") from exc
        await session.commit()
    write_backend_file(spec["run_id"], "mirror.json", {"state": "starting", "at": now})
    return {"run_id": spec["run_id"], "state": "starting", "restarted_from": old.run_id}


@router.get("", dependencies=[Depends(require_role(Role.VIEWER))])
async def list_heads(
    task_id: uuid.UUID | None = None,
    active: bool | None = None,
    box: str | None = None,
):
    _enabled()
    now = time.time()
    out = []
    for run in files.list_runs():
        if task_id and run.task_id != str(task_id):
            continue
        if box and box not in (run.spec.get("box_keys") or []):
            continue
        view = run_view(run, now)
        if active is not None and (view["state"] in ACTIVE_STATES) != active:
            continue
        out.append(view)
    return {"runs": out}


@router.get("/occupancy", dependencies=[Depends(require_role(Role.VIEWER))])
async def get_occupancy():
    _enabled()
    return {"boxes": box_guard.occupancy()}


@router.get("/{run_id}", dependencies=[Depends(require_role(Role.VIEWER))])
async def get_head(run_id: str):
    _enabled()
    return run_view(_load(run_id))


# Extra masks on top of log_redaction for tokens that appear bare in a
# harness transcript (no key= / Bearer prefix).
_BARE_TOKENS = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,})\b"
)


def mask_log(text: str) -> str:
    return _BARE_TOKENS.sub("<redacted>", redact_secrets(text))


@router.get("/{run_id}/log", dependencies=[Depends(require_role(Role.VIEWER))])
async def get_head_log(run_id: str, tail: int = Query(200, ge=1, le=2000)):
    _enabled()
    run = _load(run_id)
    # the head can write head.log — never follow a symlink (files.read_head_file)
    data = files.read_head_file(run.folder / "head.log", 512_000, tail=True) or ""
    lines = data.splitlines()[-tail:]
    return PlainTextResponse(mask_log("\n".join(lines)))


@router.get("/{run_id}/run-record", dependencies=[Depends(require_role(Role.VIEWER))])
async def get_head_run_record(run_id: str):
    _enabled()
    run = _load(run_id)
    if not run.run_record_text:
        raise _err(404, "run_record_missing")
    return PlainTextResponse(run.run_record_text, media_type="text/markdown")


@router.post("/{run_id}/stop", status_code=202, dependencies=[Depends(require_role(Role.OPERATOR))])
async def stop_head(run_id: str):
    _enabled()
    run = _load(run_id)
    # A finished run keeps its result — a late Stop (stale UI, second tab,
    # direct API call) must not turn "passed" into "stopped".
    if derive_for_run(run, time.time())["state"] not in ACTIVE_STATES:
        raise _err(409, "head_not_active")
    write_backend_file(run.run_id, "stop-requested", {"at": time.time()})
    try:
        launcher.spool("stop", run.run_id)
    except launcher.SpoolUnavailable as exc:
        raise _err(503, "spool_unavailable") from exc
    return {"run_id": run.run_id, "state": "stopping"}
