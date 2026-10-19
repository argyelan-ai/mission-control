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
   rows sharing an endpoint are deduplicated; a slot row defers entirely to
   the recipe row that names the model (verdict ``not_ours``), which mirrors
   its crash doc onto the slot row.
5. **Hand back**: running tasks of every agent on the dead PORT (live they
   are bound to the slot row, not the recipe row) go back to the inbox with
   a system comment saying why, instead of failing silently later — but only
   when the box confirmed the death. "Box not askable" (``unknown``: SSH
   timeout, network flap) is reported, and the work stays where it is. With ADR-096's local readiness gate (PR #884) the poll then holds
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

#: A memguard KILL line, not any mention (a start banner says "memguard armed").
#: Live wording (memguard.log 18.10.): "... under the 2.5 GiB floor: killed container".
_MEMGUARD_NEEDLE = "memguard"
_KILL_NEEDLE = "kill"
#: Reasons the box itself confirmed: the engine is really gone. Only these hand
#: running tasks back — "unknown" (box not askable: SSH timeout, network or
#: Tailscale flap, reboot) is no proof of a death.
CONFIRMED_DEAD = frozenset({OOM, MEMGUARD, KILLED, EXITED_ERROR, EXITED_CLEAN,
                            CONTAINER_GONE, PROCESS_GONE})
# Short SSH timeouts: diagnose runs inside the watcher tick, a hanging box must
# not stall the probes of every other runtime for long.
_INSPECT_TIMEOUT = 10
_LOGS_TIMEOUT = 15
_PGREP_TIMEOUT = 10


def _memguard_killed(logs: str) -> bool:
    return any(
        _MEMGUARD_NEEDLE in ln and _KILL_NEEDLE in ln
        for ln in (logs or "").lower().splitlines()
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _notified_key_for(endpoint: str | None, slug: str) -> str:
    return f"mc:runtime-death-notified:{(endpoint or slug).rstrip('/')}"


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
            host=host, timeout=_INSPECT_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — box stalling = cannot tell
        logger.debug("engine-death: inspect failed for %s: %s", runtime.slug, exc)
        return diag

    if ec != 0 or not (out or "").strip():
        if runtime.runtime_type in _docker_types():
            diag["reason_code"] = CONTAINER_GONE
            return diag
        try:
            _, _, pec = await _ssh_run(f"pgrep -x {q} >/dev/null 2>&1", host=host, timeout=_PGREP_TIMEOUT)
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
            f"docker logs {since}--tail {_LOG_TAIL} {q} 2>&1", host=host, timeout=_LOGS_TIMEOUT,
        )
        logs = lout or lerr or ""
    except Exception as exc:  # noqa: BLE001 — the reason is a bonus, not a gate
        logger.debug("engine-death: logs unreadable for %s: %s", runtime.slug, exc)
    diag["log_line"] = _last_error_line(logs)

    if status == "running":
        diag["reason_code"] = HUNG
    elif oom:
        diag["reason_code"] = OOM
    elif exit_code == 137 and _memguard_killed(logs):
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
    if runtime.is_slot and await _owning_sibling_exists(session, runtime, last_served):
        # The slot row is only the box's address. A recipe row on the same
        # port names the model that ran: it decides (died / stopped / evicted
        # / hung), raises the one alarm, hands back the tasks of EVERY agent
        # on this port (slot-bound ones included) and mirrors its crash doc
        # onto this row. A second verdict here could only disagree or double.
        return "not_ours"
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
    if runtime.is_slot and await _sibling_stopped_on_purpose(session, redis, runtime, last_served):
        # The slot row is only the box's address: when MC stopped or evicted
        # the recipe behind it, the silence on the address is planned too.
        return "plain"
    return "died"


async def _sibling_stopped_on_purpose(
    session: AsyncSession, redis, runtime: Runtime, last_served: str | None
) -> bool:
    """Only reached when no enabled recipe row names ``last_served`` (else the
    slot row defers, see ``verdict``). Only the sibling that SERVED counts:
    every switch marks the previous recipe evicted for an hour, and that
    marker must not silence a death of the recipe that came after it. With no
    known last answer, any planned stop on the port counts (conservative)."""
    stmt = select(Runtime.slug).where(
        Runtime.endpoint == runtime.endpoint,
        Runtime.id != runtime.id,
        Runtime.is_slot.is_(False),
    )
    if last_served:
        stmt = stmt.where(Runtime.model_identifier == last_served)
    rows = await session.exec(stmt)
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


async def _is_multi_box(session: AsyncSession, runtime_id) -> bool:
    """A duo/verbund row: it has a worker member on another box (runtime_hosts)."""
    from app.models.runtime_host import RuntimeHost

    try:
        rows = await session.exec(
            select(RuntimeHost.id).where(
                RuntimeHost.runtime_id == runtime_id, RuntimeHost.role == "worker"
            )
        )
        return rows.first() is not None
    except Exception:  # noqa: BLE001 — a hint, never a gate
        return False


async def _slot_slugs_on_port(session: AsyncSession, endpoint: str | None, own_id) -> list[str]:
    if not endpoint:
        return []
    rows = await session.exec(
        select(Runtime.slug).where(
            Runtime.endpoint == endpoint,
            Runtime.id != own_id,
            Runtime.is_slot.is_(True),
        )
    )
    return list(rows.all())


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
    # Plain values first: a rollback inside the hand-back expires every ORM
    # object of the watcher's session; nothing below may depend on them.
    slug = runtime.slug
    display_name = runtime.display_name
    runtime_id = runtime.id
    endpoint = runtime.endpoint

    diag = await diagnose(runtime, host)
    if diag.get("reason_code") == HUNG:
        # Container/process still running, only the port is silent — a busy
        # or stuck engine, not a dead one. Handing its running tasks back
        # could cut off work that is still happening (a single-threaded
        # server under a long prefill answers nothing). The caller keeps the
        # plain "unreachable" alarm.
        logger.info("runtime %s: endpoint silent but engine alive — not a death", slug)
        return None
    confirmed = diag.get("reason_code") in CONFIRMED_DEAD
    multi_box = await _is_multi_box(session, runtime_id)
    crash = {
        **diag,
        "reason": reason_sentence(diag),
        "detected_at": _now_iso(),
        "at": diag.get("finished_at") or _now_iso(),
        "model": last_served,
        "autostart": autostart_on,
        "multi_box": multi_box,
        # Box not askable: no proof the engine is gone → running work stays.
        "tasks_kept": not confirmed,
    }
    requeued: list[str] = []
    rolled_back = False
    if confirmed and settings.runtime_engine_death_requeue_enabled:
        try:
            requeued, rolled_back = await requeue_tasks_on_port(
                session, endpoint=endpoint, runtime_id=runtime_id,
                runtime_slug=slug, display_name=display_name, crash=crash,
            )
        except Exception:  # noqa: BLE001 — never let the hand-back cost the alarm
            logger.exception("engine-death: requeue failed for %s", slug)
            rolled_back = True
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
    if rolled_back:
        # The caller (watcher) keeps using this row after we return.
        try:
            await session.refresh(runtime)
        except Exception:  # noqa: BLE001
            logger.exception("engine-death: refresh of %s after rollback failed", slug)
    crash["requeued_tasks"] = len(requeued)

    # The box's slot row is dark too (slot line, agent pickers): it shows the
    # same crash. A stop of this row acknowledges both (runtime_grace.mark_stopped).
    try:
        mirrors = await _slot_slugs_on_port(session, endpoint, runtime_id)
    except Exception:  # noqa: BLE001
        mirrors = []
    crash["mirrored_to"] = mirrors
    try:
        payload = json.dumps(crash)
        await redis.setex(runtime_grace.crash_key(slug), CRASH_TTL, payload)
        for mirror in mirrors:
            await redis.setex(runtime_grace.crash_key(mirror), CRASH_TTL, payload)
    except Exception as exc:  # noqa: BLE001 — the event below still fires
        logger.debug("engine-death: crash doc not stored for %s: %s", slug, exc)

    try:
        first = bool(await redis.set(
            _notified_key_for(endpoint, slug), slug, nx=True, ex=NOTIFY_DEDUP_TTL,
        ))
    except Exception:  # noqa: BLE001 — Redis down: better twice than never
        first = True
    if first:
        restart = (
            # Worded as permission, not a promise: _maybe_auto_recover has its
            # own cooldown, attempt cap and eviction gates.
            "Box-Autostart ist an — MC darf einen Neustart versuchen "
            "(mit Abkühlzeit, höchstens 2 Versuche)."
            if autostart_on
            else "Box-Autostart ist aus — MC startet nicht von selbst neu (Runtimes → Starten)."
        )
        if not confirmed:
            tasks = (
                " Laufende Tasks bleiben, wo sie sind — die Box ist nicht befragbar, "
                "ein Tod ist nicht belegt."
            )
        elif requeued:
            tasks = f" {len(requeued)} laufende Task(s) zurück in den Eingang."
        else:
            tasks = ""
        duo = (
            " Verbund: der Worker auf der zweiten Box läuft evtl. noch und belegt "
            "deren Speicher — Stoppen räumt beide Boxen."
            if multi_box else ""
        )
        await emit_event(
            session,
            "runtime.engine_died",
            f"{display_name}: Engine ausgefallen — {crash['reason']}.{tasks}{duo} {restart}",
            severity="error",
            detail={
                "slug": slug,
                "runtime_id": str(runtime_id),
                "endpoint": endpoint,
                "reason_code": crash["reason_code"],
                "reason": crash["reason"],
                "exit_code": crash["exit_code"],
                "oom_killed": crash["oom_killed"],
                "finished_at": crash["finished_at"],
                "log_line": crash["log_line"],
                "anchor": crash["anchor"],
                "model": last_served,
                "autostart": autostart_on,
                "multi_box": multi_box,
                "tasks_kept": crash["tasks_kept"],
                "requeued_task_ids": requeued,
            },
        )
    logger.warning(
        "runtime %s: engine died (%s) — %d task(s) requeued, notified=%s",
        slug, crash["reason_code"], len(requeued), first,
    )
    return crash


async def requeue_tasks_on_port(
    session: AsyncSession,
    *,
    endpoint: str | None,
    runtime_id,
    runtime_slug: str,
    display_name: str,
    crash: dict,
) -> tuple[list[str], bool]:
    """Running work of every agent that talks to the dead port goes back to
    the inbox. Returns ``(requeued task ids, rolled_back)``.

    "Every agent on the port": agents bound to the dead row AND to any other
    row with the same endpoint — live, the local agents are bound to the box's
    slot row, never to the recipe row that names the model.

    - ``in_progress`` → ``inbox`` with ``dispatched_at``/``ack_at``/attempt
      cleared (the same reset as ``task_lifecycle.requeue_unblocked_task``),
      so the bridge's heartbeat reads the card as withdrawn and stops the run
      instead of retrying a dead endpoint for minutes.
    - ``inbox`` already delivered but never claimed (ADR-096 shape) → only the
      delivery is reset.
    One healer per card per round (``try_claim_heal``); a card another healer
    owns this round is skipped, not fought over.

    Each card is re-read under its row lock (``lock_task``) — never trusted
    from an earlier read — and committed on its own. A lost race (409/404)
    is answered with a COMMIT of the empty transaction (releases the lock,
    expires nothing). Any other error rolls back, which expires the session's
    objects: the loop works on plain ids only, and the caller refreshes what
    it still holds.
    """
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment
    from app.redis_client import get_redis, try_claim_heal
    from app.services import dispatch_attempt_audit, task_state
    from app.services.task_lifecycle import record_task_event

    row_ids = [runtime_id]
    if endpoint:
        row_ids = list((await session.exec(
            select(Runtime.id).where(Runtime.endpoint == endpoint)
        )).all()) or [runtime_id]
    agent_ids = list((await session.exec(
        select(Agent.id).where(Agent.runtime_id.in_(row_ids))
    )).all())
    if not agent_ids:
        return [], False
    candidates = list((await session.exec(
        select(Task.id).where(
            Task.assigned_agent_id.in_(agent_ids),
            Task.status.in_(("in_progress", "inbox")),
        )
    )).all())
    redis = await get_redis()
    reason_text = crash.get("reason") or "Grund unbekannt"
    done: list[str] = []
    rolled_back = False
    for task_id in candidates:
        try:
            task = await task_state.lock_task(session, task_id)
            if task is None:
                await session.commit()
                continue
            delivered_unclaimed = (
                task.status == "inbox" and task.dispatched_at is not None and task.ack_at is None
            )
            if task.status != "in_progress" and not delivered_unclaimed:
                await session.commit()  # release the row lock, nothing written
                continue
            if not await try_claim_heal(redis, str(task_id)):
                await session.commit()
                logger.info("engine-death: task %s skipped — another healer owns it this round", task_id)
                continue
            if task.status == "in_progress":
                task, old_status = await task_state.lock_and_set(
                    session, task_id, "inbox", actor="system",
                )
            else:
                old_status = task.status
            task.dispatched_at = None
            task.ack_at = None
            session.add(task)
            agent = await session.get(Agent, task.assigned_agent_id, populate_existing=True)
            if agent is not None and agent.current_task_id == task.id:
                agent.current_task_id = None
                session.add(agent)
            session.add(TaskComment(
                task_id=task.id,
                author_type="system",
                comment_type="system",
                content=(
                    f"Modell ausgefallen: Runtime '{display_name}' — {reason_text}. "
                    "Der Task ist zurück im Eingang und wird wieder zugestellt, "
                    "sobald das Modell läuft. Bisherige Arbeit im Workspace bleibt erhalten."
                ),
            ))
            if old_status != "inbox":
                await record_task_event(
                    session, task.id, old_status, "inbox",
                    changed_by="system", reason="runtime_engine_died",
                )
            await dispatch_attempt_audit.clear_dispatch_attempt_id(
                session, task, caller="engine_death", reason="runtime_engine_died",
            )
            title, board_id, agent_id = task.title, task.board_id, task.assigned_agent_id
            await session.commit()
        except HTTPException as exc:  # lost a race with the agent's own PATCH
            # Nothing was written yet (lock_and_set raises before its write):
            # commit the empty transaction to release the lock. A rollback
            # would expire every object the watcher still holds.
            try:
                await session.commit()
            except Exception:  # noqa: BLE001
                await session.rollback()
                rolled_back = True
            logger.info("engine-death: task %s not requeued: %s", task_id, exc.detail)
            continue
        except Exception:  # noqa: BLE001 — one card must not cost the others
            logger.exception("engine-death: task %s not requeued", task_id)
            await session.rollback()
            rolled_back = True
            continue
        await emit_event(
            session,
            "task.requeued_runtime_down",
            f"'{title}' zurück im Eingang — Modell ausgefallen ({display_name})",
            severity="warning",
            board_id=board_id,
            task_id=task_id,
            agent_id=agent_id,
            detail={"runtime": runtime_slug, "reason_code": crash.get("reason_code"),
                    "from_status": old_status},
        )
        done.append(str(task_id))
    return done, rolled_back
