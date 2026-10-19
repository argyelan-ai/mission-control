"""Engine death: an engine that was serving is gone, and nobody planned it.

The gap this closes (live 18./19.10.2026): a recipe's own memory guard killed
the engine container (exit 137). MC's runtime watcher saw only "endpoint
unreachable" — and said so three times, once per runtime row sharing the
box's port (slot row plus every recipe row on :8000, even the ones that were
not running). The Runtimes page then showed the box as free ("nothing
running"), the agents' running tasks kept retrying a dead endpoint until
they ended blocked, and nothing restarted the engine because the box's
autostart switch was off. From the operator's side it looked like "the
watcher just switched it off".

What this module adds, called from ``runtime_watcher._probe_one`` at the same
three-probe threshold that already raised ``runtime.unreachable``:

1. **Decide**: it is a death only when the row was serving
   (``serving_since`` set), the last answer on the port was THIS row's model
   (or it is the box's slot row, which always follows the engine), it was not
   stopped through MC (``runtime_grace.was_stopped``) and not evicted by a
   start (``runtime_grace.get_evicted``). Planned windows never reach here —
   the watcher returns earlier while a switch-grace marker is set.
2. **Explain**: one read-only look at the box — ``docker inspect`` of the
   row's anchor (exit code, OOMKilled, finish time) and the tail of its logs,
   or ``pgrep`` for a host process. The reason is a code the UI translates
   plus one German sentence for the event; the time is the container's own
   finish time when Docker knows it.
3. **Record**: a crash document in Redis (``runtime_grace.crash_key``), shown
   by the watcher's live snapshot as ``status="crashed"`` until the engine
   serves again or MC stops the runtime (a stop is the acknowledgement).
4. **Tell**: exactly one ``runtime.engine_died`` event per death and port —
   rows sharing an endpoint are deduplicated, a slot row leaves the event to
   the recipe row that names the model.
5. **Hand back**: running tasks of agents bound to the dead row go back to
   the inbox with a system comment saying why, instead of failing silently
   later. With ADR-096's local readiness gate (PR #884) the poll then holds
   them until the model answers again; without it they are delivered again
   on the next poll exactly as before.

What it deliberately does NOT do: restart anything. That remains the box's
autostart switch (``hosts.autostart_enabled``, default off) in
``runtime_watcher._maybe_auto_recover`` — one cooldown, one attempt cap, one
place. The event says which of the two applies, so "why did it not come
back?" has an answer on screen.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from shlex import quote as shlex_quote

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.runtime import Runtime
from app.services import runtime_grace
from app.services.activity import emit_event

logger = logging.getLogger(__name__)

#: A crash record outlives a weekend: the operator should still see on Monday
#: why the box is dark. It goes away earlier on the first serving probe or a stop.
CRASH_TTL = 7 * 24 * 3600
#: One notification per death and port. Longer than the three-probe threshold
#: (3 × 90 s) so every row sharing the port in the same outage lands inside it.
NOTIFY_DEDUP_TTL = 30 * 60
_LOG_TAIL = 120

# Reason codes — the frontend translates them (runtimes.stage.crashReason.*).
OOM = "oom"
MEMGUARD = "memguard"
KILLED = "killed"
EXITED_ERROR = "exited_error"
EXITED_CLEAN = "exited_clean"
HUNG = "hung"
CONTAINER_GONE = "container_gone"
PROCESS_GONE = "process_gone"
UNKNOWN = "unknown"

_MEMGUARD_NEEDLES = ("memguard",)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _notified_key(runtime: Runtime) -> str:
    return f"mc:runtime-death-notified:{(runtime.endpoint or runtime.slug).rstrip('/')}"


def _last_error_line(logs: str) -> str | None:
    lines = [ln.strip() for ln in (logs or "").splitlines() if ln.strip()]
    for needle in ("ValueError", "Error:", "Exception", "Killed", "killed"):
        hit = next((ln for ln in reversed(lines) if needle in ln), None)
        if hit:
            return hit[:300]
    return None


def reason_sentence(diag: dict) -> str:
    """One German sentence for the event title (backend event titles are German
    in both UI languages, existing convention; the card translates the code)."""
    code = diag.get("reason_code")
    exit_code = diag.get("exit_code")
    return {
        OOM: f"vom Kernel wegen Speichermangel beendet (OOM, Exit {exit_code})",
        MEMGUARD: f"vom Speicherwächter des Rezepts beendet (Exit {exit_code})",
        KILLED: (
            f"von aussen hart beendet (SIGKILL, Exit {exit_code}) — "
            "oft der Speicherwächter des Rezepts"
        ),
        EXITED_ERROR: f"mit Fehler beendet (Exit {exit_code})",
        EXITED_CLEAN: "hat sich selbst beendet (Exit 0), nicht über MC gestoppt",
        HUNG: "Container läuft noch, der Endpunkt antwortet aber nicht",
        CONTAINER_GONE: "Container ist nicht mehr vorhanden",
        PROCESS_GONE: "Prozess läuft nicht mehr",
    }.get(code, "Box nicht befragbar — Grund unbekannt")


def _anchor(runtime: Runtime) -> str:
    from app.services.runtime_manager import DOCKER_ENGINE_TYPES, SSH_PROCESS_TYPE

    container = (runtime.container_name or "").strip()
    process = (runtime.process_name or "").strip()
    if runtime.runtime_type in DOCKER_ENGINE_TYPES:
        return container
    if runtime.runtime_type == SSH_PROCESS_TYPE:
        # Since #396 process_name may be a container name (most recipe start
        # scripts start a container) — docker inspect first, pgrep second.
        return process or container
    return ""


async def diagnose(runtime: Runtime, host) -> dict:
    """Read-only: why is the engine behind ``runtime`` gone?

    Never raises; a box that cannot be asked yields ``reason_code=unknown``.
    """
    from app.services.host_resolver import ssh_capable
    from app.services.runtime_manager import _ssh_run  # noqa: SLF001

    diag: dict = {"reason_code": UNKNOWN, "exit_code": None, "oom_killed": None,
                  "finished_at": None, "log_line": None, "anchor": None}
    anchor = _anchor(runtime)
    if not anchor or host is None or not ssh_capable(host):
        return diag
    diag["anchor"] = anchor
    q = shlex_quote(anchor)
    try:
        out, _, ec = await _ssh_run(
            'docker inspect --format '
            '"{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|'
            '{{.State.FinishedAt}}|{{.State.StartedAt}}" '
            f"{q} 2>/dev/null",
            host=host, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 — box stalling = cannot tell
        logger.debug("engine-death: inspect failed for %s: %s", runtime.slug, exc)
        return diag

    if ec != 0 or not (out or "").strip():
        if runtime.runtime_type in _docker_types():
            diag["reason_code"] = CONTAINER_GONE
            return diag
        try:
            _, _, pec = await _ssh_run(f"pgrep -x {q} >/dev/null 2>&1", host=host, timeout=15)
        except Exception as exc:  # noqa: BLE001
            logger.debug("engine-death: pgrep failed for %s: %s", runtime.slug, exc)
            return diag
        diag["reason_code"] = HUNG if pec == 0 else PROCESS_GONE
        return diag

    parts = (out or "").strip().split("|")
    status = parts[0] if parts else ""
    try:
        exit_code = int(parts[1]) if len(parts) > 1 else None
    except ValueError:
        exit_code = None
    oom = (parts[2].strip().lower() == "true") if len(parts) > 2 else None
    finished_at = parts[3].strip() if len(parts) > 3 else None
    started_at = parts[4].strip() if len(parts) > 4 else None
    if finished_at and finished_at.startswith("0001-"):
        finished_at = None  # Docker's "never finished" value
    diag.update(exit_code=exit_code, oom_killed=oom, finished_at=finished_at)

    logs = ""
    try:
        since = f"--since {shlex_quote(started_at)} " if started_at and not started_at.startswith("0001-") else ""
        lout, lerr, _ = await _ssh_run(
            f"docker logs {since}--tail {_LOG_TAIL} {q} 2>&1", host=host, timeout=30,
        )
        logs = lout or lerr or ""
    except Exception as exc:  # noqa: BLE001 — the reason is a bonus, not a gate
        logger.debug("engine-death: logs unreadable for %s: %s", runtime.slug, exc)
    diag["log_line"] = _last_error_line(logs)

    if status == "running":
        diag["reason_code"] = HUNG
    elif oom:
        diag["reason_code"] = OOM
    elif any(n in logs.lower() for n in _MEMGUARD_NEEDLES):
        diag["reason_code"] = MEMGUARD
    elif exit_code == 137:
        diag["reason_code"] = KILLED
    elif exit_code == 0:
        diag["reason_code"] = EXITED_CLEAN
    elif exit_code is not None:
        diag["reason_code"] = EXITED_ERROR
    return diag


def _docker_types() -> frozenset:
    from app.services.runtime_manager import DOCKER_ENGINE_TYPES

    return frozenset(DOCKER_ENGINE_TYPES)


async def get_crash(redis, slug: str) -> dict | None:
    try:
        raw = await redis.get(runtime_grace.crash_key(slug))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


async def clear_after_serving(redis, slug: str) -> None:
    """The engine answers again: the death is over, a stop marker is spent."""
    try:
        await redis.delete(runtime_grace.crash_key(slug))
    except Exception:  # noqa: BLE001
        pass
    await runtime_grace.clear_stopped(slug, redis)


async def verdict(
    session: AsyncSession, redis, runtime: Runtime, *, last_served: str | None
) -> str:
    """``"died"`` | ``"not_ours"`` | ``"plain"`` for a row at the outage threshold.

    - ``died``: this row's engine was serving and is gone without a plan.
    - ``not_ours``: the port's last answer was ANOTHER row's model and another
      enabled row shares the endpoint — this row was never running, its
      "unreachable" is noise (live: the GLM row alarmed while DeepSeek died).
    - ``plain``: anything else — the watcher keeps its old ``runtime.unreachable``.
    """
    if not settings.runtime_engine_death_enabled:
        return "plain"
    own = runtime.is_slot or (
        last_served is not None and last_served == (runtime.model_identifier or "")
    )
    if last_served is not None and not own:
        if await _shares_endpoint(session, runtime):
            return "not_ours"
        return "plain"
    if not own or runtime.serving_since is None:
        return "plain"
    if await runtime_grace.was_stopped(runtime.slug, redis):
        return "plain"
    if await runtime_grace.get_evicted(runtime.slug, redis) is not None:
        return "plain"
    if runtime.is_slot and await _sibling_stopped_on_purpose(session, redis, runtime):
        # The slot row is only the box's address: when MC stopped or evicted
        # the recipe behind it, the silence on the address is planned too.
        return "plain"
    return "died"


async def _sibling_stopped_on_purpose(session: AsyncSession, redis, runtime: Runtime) -> bool:
    rows = await session.exec(
        select(Runtime.slug).where(
            Runtime.endpoint == runtime.endpoint,
            Runtime.id != runtime.id,
            Runtime.is_slot.is_(False),
        )
    )
    for slug in rows.all():
        if await runtime_grace.was_stopped(slug, redis):
            return True
        if await runtime_grace.get_evicted(slug, redis) is not None:
            return True
    return False


async def _shares_endpoint(session: AsyncSession, runtime: Runtime) -> bool:
    rows = await session.exec(
        select(Runtime.id).where(
            Runtime.enabled.is_(True),
            Runtime.endpoint == runtime.endpoint,
            Runtime.id != runtime.id,
        )
    )
    return rows.first() is not None


async def _owning_sibling_exists(session: AsyncSession, runtime: Runtime, last_served: str | None) -> bool:
    """For a slot row: is there a recipe row on the same port that will name
    the model in the event? Then the slot row stays quiet."""
    if not last_served:
        return False
    rows = await session.exec(
        select(Runtime.id).where(
            Runtime.enabled.is_(True),
            Runtime.endpoint == runtime.endpoint,
            Runtime.id != runtime.id,
            Runtime.is_slot.is_(False),
            Runtime.model_identifier == last_served,
        )
    )
    return rows.first() is not None


async def record_death(
    session: AsyncSession,
    redis,
    runtime: Runtime,
    *,
    host,
    last_served: str | None,
    autostart_on: bool,
) -> dict | None:
    """Diagnose, store, hand tasks back, notify once. Returns the crash doc,
    or ``None`` when the box says the engine is still alive (``hung``)."""
    diag = await diagnose(runtime, host)
    if diag.get("reason_code") == HUNG:
        # Container/process still running, only the port is silent — a busy
        # or stuck engine, not a dead one. Handing its running tasks back
        # could cut off work that is still happening (a single-threaded
        # server under a long prefill answers nothing). The caller keeps the
        # plain "unreachable" alarm.
        logger.info("runtime %s: endpoint silent but engine alive — not a death", runtime.slug)
        return None
    crash = {
        **diag,
        "reason": reason_sentence(diag),
        "detected_at": _now_iso(),
        "at": diag.get("finished_at") or _now_iso(),
        "model": last_served,
        "autostart": autostart_on,
    }
    requeued: list[str] = []
    if settings.runtime_engine_death_requeue_enabled:
        try:
            requeued = await requeue_tasks_of_dead_runtime(session, runtime, crash)
        except Exception:  # noqa: BLE001 — never let the hand-back cost the alarm
            logger.exception("engine-death: requeue failed for %s", runtime.slug)
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
    crash["requeued_tasks"] = len(requeued)
    try:
        await redis.setex(runtime_grace.crash_key(runtime.slug), CRASH_TTL, json.dumps(crash))
    except Exception as exc:  # noqa: BLE001 — the event below still fires
        logger.debug("engine-death: crash doc not stored for %s: %s", runtime.slug, exc)

    quiet = runtime.is_slot and await _owning_sibling_exists(session, runtime, last_served)
    first = False
    if not quiet:
        try:
            first = bool(await redis.set(_notified_key(runtime), runtime.slug, nx=True, ex=NOTIFY_DEDUP_TTL))
        except Exception:  # noqa: BLE001 — Redis down: better twice than never
            first = True
    if first:
        restart = (
            "Box-Autostart ist an — MC versucht einen Neustart (höchstens 2 Versuche)."
            if autostart_on
            else "Box-Autostart ist aus — MC startet nicht von selbst neu (Runtimes → Starten)."
        )
        tasks = f" {len(requeued)} laufende Task(s) zurück in den Eingang." if requeued else ""
        await emit_event(
            session,
            "runtime.engine_died",
            f"{runtime.display_name}: Engine ausgefallen — {crash['reason']}.{tasks} {restart}",
            severity="error",
            detail={
                "slug": runtime.slug,
                "runtime_id": str(runtime.id),
                "endpoint": runtime.endpoint,
                "reason_code": crash["reason_code"],
                "reason": crash["reason"],
                "exit_code": crash["exit_code"],
                "oom_killed": crash["oom_killed"],
                "finished_at": crash["finished_at"],
                "log_line": crash["log_line"],
                "anchor": crash["anchor"],
                "model": last_served,
                "autostart": autostart_on,
                "requeued_task_ids": requeued,
            },
        )
    logger.warning(
        "runtime %s: engine died (%s) — %d task(s) requeued, notified=%s",
        runtime.slug, crash["reason_code"], len(requeued), first,
    )
    return crash


async def requeue_tasks_of_dead_runtime(
    session: AsyncSession, runtime: Runtime, crash: dict
) -> list[str]:
    """Running work of agents bound to ``runtime`` goes back to the inbox.

    - ``in_progress`` → ``inbox`` with ``dispatched_at``/``ack_at``/attempt
      cleared (the same reset as ``task_lifecycle.requeue_unblocked_task``),
      so the bridge's heartbeat reads the card as withdrawn and stops the run
      instead of retrying a dead endpoint for minutes.
    - ``inbox`` already delivered but never claimed (ADR-096 shape) → only the
      delivery is reset.
    One healer per card per round (``try_claim_heal``); a card another healer
    owns this round is skipped, not fought over.
    """
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment
    from app.redis_client import get_redis, try_claim_heal
    from app.services.dispatch_attempt_audit import clear_dispatch_attempt_id
    from app.services.task_lifecycle import record_task_event
    from app.services.task_state import lock_and_set

    agents = (await session.exec(select(Agent).where(Agent.runtime_id == runtime.id))).all()
    if not agents:
        return []
    by_id = {a.id: a for a in agents}
    tasks = (await session.exec(
        select(Task).where(
            Task.assigned_agent_id.in_(list(by_id)),
            Task.status.in_(("in_progress", "inbox")),
        )
    )).all()
    redis = await get_redis()
    reason_text = crash.get("reason") or "Grund unbekannt"
    done: list[str] = []
    for task in tasks:
        delivered_unclaimed = task.status == "inbox" and task.dispatched_at is not None and task.ack_at is None
        if task.status != "in_progress" and not delivered_unclaimed:
            continue
        task_id: uuid.UUID = task.id
        if not await try_claim_heal(redis, str(task_id)):
            logger.info("engine-death: task %s skipped — another healer owns it this round", task_id)
            continue
        try:
            if task.status == "in_progress":
                task, old_status = await lock_and_set(session, task_id, "inbox", actor="system")
            else:
                old_status = task.status
            task.dispatched_at = None
            task.ack_at = None
            session.add(task)
            agent = by_id.get(task.assigned_agent_id)
            if agent is not None and agent.current_task_id == task.id:
                agent.current_task_id = None
                session.add(agent)
            session.add(TaskComment(
                task_id=task.id,
                author_type="system",
                comment_type="system",
                content=(
                    f"Modell ausgefallen: Runtime '{runtime.display_name}' — {reason_text}. "
                    "Der Task ist zurück im Eingang und wird wieder zugestellt, "
                    "sobald das Modell läuft. Bisherige Arbeit im Workspace bleibt erhalten."
                ),
            ))
            if old_status != "inbox":
                await record_task_event(
                    session, task.id, old_status, "inbox",
                    changed_by="system", reason="runtime_engine_died",
                )
            await clear_dispatch_attempt_id(
                session, task, caller="engine_death", reason="runtime_engine_died",
            )
            await session.commit()
        except HTTPException as exc:  # lost a race with the agent's own PATCH
            await session.rollback()
            logger.info("engine-death: task %s not requeued: %s", task_id, exc.detail)
            continue
        await emit_event(
            session,
            "task.requeued_runtime_down",
            f"'{task.title}' zurück im Eingang — Modell ausgefallen ({runtime.display_name})",
            severity="warning",
            board_id=task.board_id,
            task_id=task.id,
            agent_id=task.assigned_agent_id,
            detail={"runtime": runtime.slug, "reason_code": crash.get("reason_code"),
                    "from_status": old_status},
        )
        done.append(str(task.id))
    return done
