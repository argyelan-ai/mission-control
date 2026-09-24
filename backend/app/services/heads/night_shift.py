"""Night shift job — runs in mc-worker next to ``heads_sync`` (ROADMAP E2).

Every tick (60 s):

1. **Tidy** — drop marks whose task is gone; drop unstarted marks whose task
   is already done/aborted.
2. **Blocked notices** — a night head that waits on a question, or has shown
   no heartbeat / output for 15 min, is reported once through the operator
   report channel. Nothing is stopped or restarted.
3. **Morning report** — after a window has ended, one message for that
   night: passed / failed / needs you / blocked (+ still running), with task
   and PR links, through ``operator_reports.send_report`` (the digest path).
   Started marks are archived into the report file; marks that never started
   stay queued for the next night.
4. **Start** — inside the window and with the night shift switched on: at
   most ONE head per tick, through ``heads.start.start_head`` (the same path
   as "Run as head"). Box lanes, marking order and the cloud share decide
   which mark (``night.pick_next``).

No new persistent agent, no restart logic, no approvals.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.agent import Agent
from app.models.repo import Repo
from app.models.task import Task
from app.services.heads import box_guard, files, night, night_store, pairs
from app.services.heads.night import CLOUD_LANE, Candidate, NightConfig
from app.services.heads.night_store import Mark
from app.services.heads.start import HeadStartError, start_head
from app.services.heads.state import ACTIVE_STATES, derive_for_run

logger = logging.getLogger("mc.night_shift")

FINISHED_TASK_STATUSES = frozenset({"done", "aborted"})


@dataclass
class TickResult:
    started: str | None = None  # task id
    reported: str | None = None  # night key
    notices: list[str] = field(default_factory=list)  # "<task_id>:<kind>"
    dropped: list[str] = field(default_factory=list)


async def _send(text: str) -> bool:
    from app.services.operator_reports import send_report

    delivered, results = await send_report(text)
    if not delivered:
        logger.warning("night shift: report not delivered (results=%s)", results)
    return delivered


async def _operator_language(session: AsyncSession) -> str:
    """The lead agent's operator_language (PRINCIPLES §6) — en when none."""
    lead = (await session.exec(select(Agent).where(Agent.is_board_lead == True).limit(1))).first()
    return (getattr(lead, "operator_language", None) or "en").lower()


def _run_facts(run, now: float) -> dict:
    """State of a night run for the notice and the report."""
    derived = derive_for_run(run, now)
    hb_age = None if run.heartbeat_mtime is None else now - run.heartbeat_mtime
    # While the wrapper still says "running", judge it as running: a missing
    # heartbeat is exactly what "blocked" is about (the backend cannot see pids).
    state_for_block = "running" if run.status.get("phase") == "running" else derived["state"]
    kind = night.blocked_kind(state_for_block, silent_s=derived["silent_s"], heartbeat_age_s=hb_age)
    silent = derived["silent_s"]
    if kind == "silent" and hb_age is not None:
        silent = max(silent or 0, int(hb_age))
    return {
        "state": derived["state"],
        "reason": derived["reason"],
        "blocked": kind,
        "silent_s": silent,
        "pr_url": run.pr_url,
    }


def _report_entry(m: Mark, task: Task, runs: dict, now_ts: float) -> dict:
    entry = {"task_id": m.task_id, "title": task.title, "started": m.run_id is not None,
             "harness": m.harness, "runtime_slug": m.runtime_slug, "run_id": m.run_id}
    run = runs.get(m.run_id) if m.run_id else None
    if run is not None:
        entry.update(_run_facts(run, now_ts))
    elif m.run_id:
        entry.update(state="failed", reason="run_missing")
    else:
        entry["reason"] = m.gave_up or m.last_error or "not_reached"
    entry["category"] = night.categorize(entry)
    return entry


async def _tasks(session: AsyncSession, marks: list[Mark]) -> dict[str, Task]:
    ids = []
    for m in marks:
        try:
            ids.append(uuid.UUID(m.task_id))
        except ValueError:
            continue
    if not ids:
        return {}
    rows = (await session.exec(select(Task).where(Task.id.in_(ids)))).all()
    return {str(t.id): t for t in rows}


async def tick(session: AsyncSession, now: datetime | None = None, *, send=None) -> TickResult:
    """One pass. ``send`` (async text → delivered) is injectable for tests."""
    send = send or _send
    now = now or datetime.now(UTC)
    now_ts = now.timestamp()
    out = TickResult()
    if not settings.heads_enabled:
        return out
    cfg = await night_store.load_config(session)
    marks = night_store.list_marks()
    if not marks:
        return out
    tasks = await _tasks(session, marks)
    runs = {r.run_id: r for r in files.list_runs()}

    # 1. Tidy
    kept: list[Mark] = []
    for m in marks:
        task = tasks.get(m.task_id)
        if task is None or (m.run_id is None and task.status in FINISHED_TASK_STATUSES):
            night_store.delete_mark(m.task_id)
            out.dropped.append(m.task_id)
            continue
        kept.append(m)
    marks = kept

    lang = None

    # 2. Blocked notices (once per kind and run)
    for m in marks:
        run = runs.get(m.run_id) if m.run_id else None
        if run is None:
            continue
        facts = _run_facts(run, now_ts)
        kind = facts["blocked"]
        if kind is None or kind in m.notified:
            continue
        lang = lang or await _operator_language(session)
        text = night.format_notice(kind, title=tasks[m.task_id].title, task_id=m.task_id,
                                   base_url=settings.mc_base_url, silent_s=facts["silent_s"], lang=lang)
        try:
            await send(text)
        except Exception:
            logger.exception("night shift: blocked notice failed")
            continue
        m.notified.append(kind)
        night_store.save_mark(m)
        out.notices.append(f"{m.task_id}:{kind}")

    # 3. Morning report for every night whose window has ended
    current = night.current_window(now, cfg)
    ended = sorted({
        m.night for m in marks
        if m.night and (current is None or m.night != current.night)
        and night.window_of_night(m.night, cfg).ends_at <= now
    })
    for night_key in ended:
        of_night = [m for m in marks if m.night == night_key]
        if night_store.reserve_report(night_key):
            lang = lang or await _operator_language(session)
            entries = [_report_entry(m, tasks[m.task_id], runs, now_ts) for m in of_night]
            text = night.format_report(night_key, entries, base_url=settings.mc_base_url, lang=lang)
            try:
                delivered = await send(text)
            except Exception:
                logger.exception("night shift: morning report failed")
                night_store.release_report(night_key)
                continue
            night_store.write_report(night_key, {
                "night": night_key, "sent_at": now.isoformat(), "delivered": bool(delivered),
                "entries": entries, "text": text,
            })
            out.reported = night_key
        # Sent now, or already sent before a crash: archive / re-queue.
        for m in of_night:
            if m.run_id:
                night_store.delete_mark(m.task_id)  # its outcome lives in the report
            else:
                m.night, m.gave_up = None, None  # never started: queued for the next night
                night_store.save_mark(m)

    # 4. Start (one per tick)
    if not cfg.enabled or current is None:
        return out
    marks = [m for m in night_store.list_marks() if m.task_id in tasks]
    out.started = await _start_next(session, cfg, current, marks, tasks, runs, now_ts)
    return out


async def _start_next(session: AsyncSession, cfg: NightConfig, window: night.Window, marks: list[Mark],
                      tasks: dict[str, Task], runs: dict, now_ts: float) -> str | None:
    queued = [m for m in marks if m.run_id is None]
    for m in queued:
        if m.night != window.night:
            m.night, m.gave_up = window.night, None
            night_store.save_mark(m)
    night_marks = [m for m in marks if m.night == window.night]
    cloud_started = sum(1 for m in night_marks if m.run_id and m.locality == "cloud")

    occupancy = box_guard.occupancy(now_ts)
    busy = set(occupancy)
    for m in night_marks:
        run = runs.get(m.run_id) if m.run_id else None
        if run is not None and m.locality == "cloud" and derive_for_run(run, now_ts)["state"] in ACTIVE_STATES:
            busy.add(CLOUD_LANE)

    listing = await pairs.list_pairs(session, occupancy)
    by_key = {(p["harness"], p["runtime_slug"]): p for p in listing["pairs"]}
    candidates: list[Candidate] = []
    by_task: dict[str, Mark] = {}
    for m in queued:
        if m.gave_up:
            continue
        pair = by_key.get((m.harness, m.runtime_slug))
        if pair is None:
            _note(m, "pair_gone", permanent=True)
            continue
        cloud = pair["locality"] == "cloud"
        lanes = [CLOUD_LANE] if cloud else (list(pair["box_keys"]) or [f"runtime:{m.runtime_slug}"])
        candidates.append(Candidate(task_id=m.task_id, locality="cloud" if cloud else "local",
                                    lanes=lanes, order=m.marked_at))
        by_task[m.task_id] = m

    pick = night.pick_next(candidates, busy, night_total=len(night_marks), cloud_started=cloud_started,
                           cloud_share=cfg.cloud_share)
    for task_id, why in pick.waiting.items():
        _note(by_task[task_id], why)
    for task_id in pick.ready:
        m, task = by_task[task_id], tasks[task_id]
        repo = await session.get(Repo, task.repo_id) if task.repo_id else None
        if repo is None:
            _note(m, "repo_required", permanent=True)
            continue
        try:
            result = await start_head(session, task=task, repo=repo, harness=m.harness,
                                      runtime_slug=m.runtime_slug, user_id=m.marked_by,
                                      reason="night_shift_start", now=now_ts)
        except HeadStartError as exc:
            _note(m, exc.code, permanent=exc.code in night.PERMANENT_START_ERRORS)
            continue
        m.run_id, m.started_at, m.last_error = result["run_id"], now_ts, None
        night_store.save_mark(m)
        logger.info("night shift: started head %s for task %s", result["run_id"], task_id)
        return task_id
    return None


def _note(mark: Mark, code: str, *, permanent: bool = False) -> None:
    changed = mark.last_error != code or (permanent and mark.gave_up != code)
    mark.last_error = code
    if permanent:
        mark.gave_up = code
    if changed:
        night_store.save_mark(mark)


class NightShift:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        interval = settings.night_shift_interval
        if self._running or not interval or interval <= 0 or interval >= 99999:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(interval), name="night_shift")
        logger.info("night shift job started (interval=%ss)", interval)

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
                    await tick(session)
            except Exception:
                logger.exception("night shift pass failed")


night_shift = NightShift()
