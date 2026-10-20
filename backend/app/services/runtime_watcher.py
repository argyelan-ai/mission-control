"""Runtime Watcher (Runtime & Model Management v1, ADR-054).

"Engine leads, MC follows": the active model is changed at the inference
engine (vLLM / LM Studio / OpenAI-compatible); this service detects it.

Every tick it probes all enabled probeable runtimes via ``/v1/models``:
  1. writes a live status snapshot to Redis (cockpit feed for /runtimes),
  2. confirms model drift with TWO consecutive identical probes (guards
     against flapping during engine warm-up), then persists the new
     ``model_identifier``, invalidates the resolver cache, emits
     ``runtime.model_changed`` and flags bound cli-bridge agents,
  2b. confirms a changed served context window (``max_model_len`` from the
     same probe) the same way and persists it to ``max_context_len`` /
     ``preferred_context_len`` — the window is rendered into agent env just
     like the model id, so a stale one misconfigures turns just as badly,
  3. runs the propagation sync pass for flagged agents that are now idle.

PR5 adds two operational behaviours on top:
  - switch grace: a runtime marked in-flight by ``runtime_grace`` (recipe
    switch, manual start, cold load) is reported as ``switching`` instead of
    counted as a failure — planned downtime no longer pages the operator,
  - auto-recovery: a confirmed outage on a docker engine whose host answers
    again gets exactly ONE start attempt per cooldown window, giving up after
    two consecutive attempts (see :meth:`RuntimeWatcher._maybe_auto_recover`).

PR8 adds two more, both from the sparkinfer live session:
  - crash-loop detection: a compose stack with ``restart: unless-stopped``
    that dies on every boot looks IDENTICAL to a slow cold load from the
    outside — the endpoint is down either way, and grace suppresses the alarm.
    Docker knows the difference (``RestartCount``), so the watcher asks it
    (see :meth:`RuntimeWatcher._check_crash_loop`).
  - it closes the memory-prep window: the probe that first sees an engine
    serving is the honest end of a start, so that is where the page-cache
    dropper is removed and the free-memory watermark restored
    (``services/host_memory_prep``), plus a sweep for preps whose backend died
    mid-start.

Supersedes decision D-22 (periodic probing rejected) — see ADR-054.
Same lifecycle pattern as IntelligenceService: singleton, asyncio loop,
Redis lock for multi-worker dedup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from shlex import quote as shlex_quote

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis
from app.services import address_classify
from app.services.activity import emit_event
from app.services.agent_runtime_switch import (
    _PROBEABLE_RUNTIME_TYPES,
    FAIL_REFUSED,
    probe_runtime_model_info,
)
from app.services import engine_death, host_memory_prep
from app.services.host_resolver import resolve_host_for_runtime, ssh_capable
from app.services.runtime_grace import (
    INTENT_CRASH_LOOP,
    PHASE_LOADING,
    SOURCE_AUTO_RECOVERY,
    SOURCE_SWITCH,
    STOP_INTENT_SETTLE,
    clear_evicted,
    clear_start_pending,
    clear_stop_intent,
    clear_switching,
    get_evicted,
    get_start_pending,
    get_stop_intent,
    get_switching,
    marker_age_seconds,
    mark_stop_intent,
    mark_switching,
)
from app.services.runtime_manager import DOCKER_ENGINE_TYPES, SSH_PROCESS_TYPE
from app.services.runtime_model_resolver import (
    invalidate_cached_model,
    session_scope,
)
from app.services.runtime_propagation import (
    is_model_sync_target,
    mark_agents_for_sync,
    recreate_pending_agents,
    sync_pending_agents,
)
from app.utils import ensure_aware

logger = logging.getLogger(__name__)

# Emit runtime.unreachable only after this many consecutive failed probes
# (transient blips and engine restarts must not spam the activity feed).
# Counted in PROBES, not seconds — deliberately kept when the tick went from
# 90 to 30 s (2026-10-20): ADR-054 point 5 and ADR-096 §11.2 define it as
# "3 consecutive probes", and the stage card turns "failed" at the same count
# (frontend Stage.tsx, ``consecutive_failures >= 3``) — so do the "since when"
# clock, the death check on a timeout and auto-recovery. A blip shorter than
# two ticks still never alarms; a real outage now alarms 60–90 s after it
# began instead of 180–270 s.
UNREACHABLE_EVENT_THRESHOLD = 3
_STARTUP_GRACE = 20  # seconds — let DB/Redis/other services come up first

#: What one tick writes for the readers between ticks — the live snapshot
#: (runtimes page, dispatch readiness gate, runtime pulse, auto-recovery's
#: sibling check), the two drift candidates and the members mark — lives
#: 3 ticks, but never less than 270 s: its lifetime at the old 90 s tick.
#: A tick is not as short as the pause after it when it does real work: an
#: auto-recovery start waits up to ``memory_prep_wait_timeout_seconds``
#: (180 s) for free memory, a crash-loop stop spends up to ~130 s on SSH, a
#: stalling box adds its timeouts. The gap between two writes is that run
#: time plus the tick. With a plain 3 × 30 s every other runtime's snapshot
#: would expire inside one such tick: cards go blank, the readiness gate reads
#: "never probed" and lets work through, and a model seen once just before it
#: could never be confirmed by the second probe.
STATE_TTL_FLOOR = 270
#: The outage counter lives 10 ticks, but never less than 900 s (its lifetime
#: at the old tick). It has to outlast the longest gap between two probes of
#: the same runtime in one outage (a long tick; a switch window reads it
#: without bumping it). A reset mid-outage would start the count again: the
#: card falls back from "failed" to "unreachable (1)" and the threshold steps
#: run a second time for the same outage.
FAIL_COUNT_TTL_FLOOR = 900
#: How long into an outage the death check may still ask the row's anchor
#: (ADR-096 §11.2) — 270 s, its window at the old 90 s tick (probes 1..3).
#: Counted in probes it would shrink to 60–90 s at 30 s, and two real cases
#: would lose their cause: an engine whose container outlives its closed port
#: (slow teardown, a duo shutting down) and a wedged engine killed later. So
#: the probe count follows the tick: max(3, ceil(270 s / tick)) — 9 at 30 s,
#: 3 at 90 s (unchanged). Connection refused is checked at every probe in the
#: window; a timeout only at the threshold, as before (a stalling box looks
#: the same, and SSH to it is what stalls). Each check is bounded
#: (_ANCHOR_CHECK_TIMEOUT); past the window the box is never asked again.
DEATH_CHECK_WINDOW_S = 270

# Auto-recovery (PR5). One attempt per runtime per cooldown window; after this
# many consecutive attempts that did not bring the engine back, stop and hand
# over to the operator rather than restarting in a loop.
#: Rezept-Umschalter P3: die Engines, die MC über einen Startbefehl hochfährt —
#: genau die drei Engines, die ein Rezept haben kann (models/local_recipe.ENGINES).
#: Bis P3 war die Auto-Wiederbelebung auf Docker beschränkt, weil niemand einen
#: Host-Prozess ungefragt neu starten wollte. Mit dem Schalter an der Box ist es
#: eine bewusste Entscheidung des Betreibers, also darf ``ssh_process`` mit.
COMMAND_DRIVEN_ENGINE_TYPES = frozenset(DOCKER_ENGINE_TYPES) | {SSH_PROCESS_TYPE}

AUTO_RECOVERY_COOLDOWN = 900  # 15 min — longer than a normal warmup
AUTO_RECOVERY_MAX_ATTEMPTS = 2
AUTO_RECOVERY_FAILURE_TTL = 6 * 3600  # attempts "age out" after 6h of quiet

#: Drosselung des Sicherheitsnetzes „gebundener Agent trägt noch den alten
#: Modellnamen" (ADR-078, Nachlese 06.09.2026). 10 Minuten: lang genug, dass
#: ein laufender Sync (Reload = Sekunden, Neustart = bis 60 s Health-Frist) in
#: Ruhe durchkommt, kurz genug, dass ein verlorener Sync von allein wiederkehrt.
AGENT_MODEL_GUARD_TTL = 600
#: ADR-096 §2 — one "members incomplete" event per runtime and day, not per cycle.
MEMBERS_NOTICE_TTL = 24 * 3600

# Crash-loop detection (PR8, hardened by Task #24). `restart: unless-stopped`
# in a compose stack means a container that dies on boot is restarted
# forever, and from outside that is indistinguishable from a model still
# loading. These are the two signals that tell them apart.
#
# THREE restarts, not one: a single restart happens for benign reasons (a
# manual `docker restart`, a host reboot, an OOM the box recovered from) —
# and, live on the Spark (09.08.26), a single failed *first* attempt followed
# by a clean retry, which the crash-loop check killed mid-load because a log
# pattern from that one failed attempt was treated as sufficient on its own.
# 3 is not arbitrary: it is Local Studio's own published budget
# (`LAUNCH_FAILURE_LIMIT = 3`, see EVAL-LOCAL-STUDIO.md §9.2a) for exactly
# this class of problem, and it is comfortably above 2 — leaving room for one
# benign restart plus one unlucky-but-real retry before anything is stopped.
CRASH_LOOP_RESTART_THRESHOLD = 3
# The restart count is only meaningful within a bounded lookback: a Spark box
# that has been up for weeks legitimately carries restarts from unrelated,
# long-resolved incidents, and those must not silently contribute to today's
# delta forever. 10 minutes mirrors Local Studio's own window
# (`LAUNCH_FAILURE_WINDOW_MS = 10 * 60 * 1000`, same file) — long enough to
# span a real boot-crash-reboot cycle (vLLM's own retries are on the order of
# seconds to low minutes), short enough that an old, already-resolved restart
# has aged out by the time a genuinely new incident starts.
CRASH_LOOP_WINDOW_SECONDS = 10 * 60
# vLLM's own words when the engine process could not initialise — the line that
# was scrolling past on the Spark while MC reported a healthy "switching".
# NOTE: this is reason text only (see `_check_crash_loop`), never a trigger —
# a pattern match with a restart delta under threshold means "one failed
# attempt with visible symptoms", not "a loop", and must not stop anything.
CRASH_LOOP_LOG_PATTERNS = ("Engine core initialization failed",)
# How much log to read for the pattern and for the reason line. 200 lines is
# roughly one crashed vLLM boot including its traceback.
_CRASH_LOG_LINES = 200


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_inspect(stdout: str) -> tuple[int | None, str | None]:
    """``"7 2026-08-08T00:12:03Z"`` → ``(7, "2026-08-08T00:12:03Z")``.

    A shape we cannot read yields ``(None, None)`` and the check stands down —
    guessing a restart count is how a healthy engine gets stopped.
    """
    parts = (stdout or "").strip().split(None, 1)
    if not parts:
        return (None, None)
    try:
        count = int(parts[0])
    except ValueError:
        return (None, None)
    return (count, parts[1].strip() if len(parts) > 1 else None)


def _last_error_line(logs: str) -> str | None:
    """The most useful single line out of a crashed boot.

    ``ValueError`` first because that is what vLLM raises when the KV cache
    does not fit — the exact failure from the live session, and the one an
    operator can act on ("lower gpu_memory_utilization"). Any other Error/
    Traceback line is the fallback so a different crash still says something.
    """
    lines = [ln.strip() for ln in (logs or "").splitlines() if ln.strip()]
    for needle in ("ValueError", "Error:", "Exception"):
        hit = next((ln for ln in reversed(lines) if needle in ln), None)
        if hit:
            return hit[:500]
    return None


#: ADR-096 §11.2/§11.3: the death check is bounded so a stalling box cannot
#: hold the tick — anchor check (node-agent / one SSH ``docker inspect`` / one
#: handle check) plus the log read stay under 30 s from the failed probe.
_ANCHOR_SSH_TIMEOUT = 8
_ANCHOR_CHECK_TIMEOUT = 15
_EXPLAIN_TIMEOUT = 10


def _parse_state_inspect(stdout: str) -> dict:
    """``status|exit|oomkilled|finished|started`` → dict. Docker's "never"
    timestamp (``0001-…``) reads as ``None``."""
    parts = (stdout or "").strip().split("|")

    def _ts(i: int) -> str | None:
        raw = parts[i].strip() if len(parts) > i else ""
        return None if not raw or raw.startswith("0001-") else raw

    try:
        exit_code = int(parts[1]) if len(parts) > 1 else None
    except ValueError:
        exit_code = None
    return {
        "status": parts[0].strip() if parts else "",
        "exit_code": exit_code,
        "oom_killed": (parts[2].strip().lower() == "true") if len(parts) > 2 else None,
        "finished_at": _ts(3),
        "started_at": _ts(4),
    }


class RuntimeWatcher:
    def __init__(self, interval: int | None = None) -> None:
        self._interval = interval or settings.runtime_watcher_interval
        self._task: asyncio.Task | None = None
        self._running = False

    def _state_ttl(self) -> int:
        """Lifetime of what a tick writes for the next one and for readers
        between ticks: 3 ticks, never under ``STATE_TTL_FLOOR``."""
        return max(self._interval * 3, STATE_TTL_FLOOR)

    def _fail_count_ttl(self) -> int:
        """Lifetime of the outage counter: 10 ticks, never under
        ``FAIL_COUNT_TTL_FLOOR``."""
        return max(self._interval * 10, FAIL_COUNT_TTL_FLOOR)

    def _death_check_probes(self) -> int:
        """Up to which failed probe of an outage the death check may ask the
        anchor: ``DEATH_CHECK_WINDOW_S`` of wall-clock, never under the
        threshold."""
        return max(UNREACHABLE_EVENT_THRESHOLD, math.ceil(DEATH_CHECK_WINDOW_S / self._interval))

    async def start(self) -> None:
        if not settings.runtime_watcher_enabled:
            logger.info("runtime watcher disabled via settings")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("runtime watcher started (interval=%ss)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        await asyncio.sleep(_STARTUP_GRACE)
        while self._running:
            try:
                if await self._acquire_lock():
                    await self.tick()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                logger.exception("runtime watcher tick failed")
            await asyncio.sleep(self._interval)

    async def _acquire_lock(self) -> bool:
        """One worker per tick. Redis down → run anyway (single-worker default).

        Never released — it simply expires. So it follows the tick and stays
        BELOW it: a lock that outlived the tick would make this worker's own
        next tick find it and skip, stretching the tick it stands for. Tick
        minus 5 s, but at least half the tick (and 1 s) — never a fixed floor,
        which would outlive a short tick (the setting has no lower bound).
        """
        try:
            redis = await get_redis()
            return bool(
                await redis.set(
                    RedisKeys.runtime_watcher_lock(), "1",
                    nx=True, ex=max(self._interval - 5, self._interval // 2, 1),
                )
            )
        except Exception:  # noqa: BLE001
            return True

    async def tick(self, session: AsyncSession | None = None) -> None:
        """One probe + sync pass. ``session`` is injectable for tests."""
        if session is not None:
            await self._tick_inner(session)
            return
        async with session_scope() as own_session:
            await self._tick_inner(own_session)

    async def _tick_inner(self, session: AsyncSession) -> None:
        # Recipe rows before slot rows (ADR-096 §11.4): a death found at the
        # threshold (timeout path) must be declared before the slot row on the
        # same port reaches the same threshold — otherwise the port gets a
        # plain alarm AND the death. It also lets a loading recipe refresh its
        # slot row's grace before that slot row is probed in the same tick.
        result = await session.exec(
            select(Runtime).where(
                Runtime.enabled.is_(True),
                Runtime.runtime_type.in_(sorted(_PROBEABLE_RUNTIME_TYPES)),
            ).order_by(Runtime.is_slot)
        )
        runtimes = result.all()
        for idx, runtime in enumerate(runtimes):
            try:
                await self._probe_one(session, runtime)
            except Exception:  # noqa: BLE001 — one runtime's broken probe
                # must never take down the orphan sweep or the runtimes
                # after it in this tick.
                logger.exception("probe failed for %s", runtime.slug)
                # A DB error mid-probe leaves the session poisoned
                # (PendingRollbackError) — without a rollback, every
                # runtime probed after this one in the same tick would
                # fail with the same error, and so would sync_pending_agents
                # right after the loop.
                try:
                    await session.rollback()
                except Exception:  # noqa: BLE001
                    logger.exception("session rollback failed after probe error")
                    continue
                # rollback() expires every object this session was tracking
                # — not just the one that failed — so a plain attribute
                # read on a runtime queued after this one (already fetched
                # by the select above) would hit AsyncSession's version of
                # the same problem in a different shape: MissingGreenlet
                # instead of PendingRollbackError, since expired attributes
                # need an explicit awaited refresh. Refresh them now so the
                # rest of this tick sees ordinary, usable objects.
                for later in runtimes[idx + 1:]:
                    try:
                        await session.refresh(later)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "failed to refresh %s after rollback",
                            getattr(later, "slug", "?"),
                        )
        # A prep whose backend died mid-start leaves a lowered watermark and a
        # cache-dropper container on the box, with nobody left holding the
        # original value. This is the only place that ever notices (PR8).
        try:
            await host_memory_prep.recover_orphaned_preps()
        except Exception:  # noqa: BLE001
            logger.exception("orphaned memory-prep sweep failed")
        # Members vs. topology (ADR-096 §2): read-only here — the healer
        # writes only at backend start, so it never races a running start.
        try:
            await self._mark_membership(session)
        except Exception:  # noqa: BLE001
            logger.exception("membership check failed")
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                logger.exception("session rollback failed after membership check")
        await sync_pending_agents(session)
        # CLI-Tool-Updates: recreate agents flagged by the CLI update check.
        # Runs after the model-sync pass so a same-tick model change is applied
        # by a plain restart before we (potentially) force-recreate.
        await recreate_pending_agents(session)

    async def _mark_membership(self, session: AsyncSession) -> None:
        """Mark runtimes whose members do not fit their topology. Never writes rows.

        Per broken runtime: a short-lived Redis mark (``members_incomplete``
        with the sentence, refreshed every cycle, gone a few cycles after the
        fix) and ONE activity event per day — not one per cycle.
        """
        from app.services import runtime_membership

        findings = await runtime_membership.membership_audit(session, repair=False)
        if not findings:
            return
        redis = await get_redis()
        for item in findings:
            await redis.setex(
                RedisKeys.runtime_members_issue(item.slug),
                self._state_ttl(),
                json.dumps({"code": item.code, "sentence": item.sentence}),
            )
            first = await redis.set(
                RedisKeys.runtime_members_notice(item.slug),
                item.code,
                nx=True,
                ex=MEMBERS_NOTICE_TTL,
            )
            if not first:
                continue
            await emit_event(
                session,
                "runtime.members_incomplete",
                item.sentence,
                severity="warning",
                detail={
                    "runtime_id": str(item.runtime_id),
                    "slug": item.slug,
                    "code": item.code,
                },
            )

    async def _probe_one(self, session: AsyncSession, runtime: Runtime) -> None:
        started = time.monotonic()
        probed = await probe_runtime_model_info(runtime)
        served, served_ctx = probed.model_id, probed.context_len
        latency_ms = int((time.monotonic() - started) * 1000)
        redis = await get_redis()
        switching = await get_switching(runtime.slug, redis)

        if served is None:
            # Before deciding whether this silence is planned: is the container
            # actually loading, or is it dying and being restarted? Runs inside
            # the grace window too — that is precisely where the crash loop was
            # invisible, because grace suppresses every other signal.
            try:
                if await self._check_crash_loop(session, redis, runtime):
                    return
            except Exception:  # noqa: BLE001 — an add-on may not cost the tick
                logger.exception("crash-loop check failed for %s", runtime.slug)

            if switching is not None:
                # Planned downtime (recipe switch, cold load, recovery start).
                # No failure counting, no event — that combination is what
                # produced the notification storm on every switch. The 20-min
                # TTL on the marker guarantees we fall back to normal
                # alerting even if nobody ever clears it.
                remembered = await engine_death.last_answer(redis, runtime.slug)
                await self._write_live(
                    redis, runtime.slug,
                    reachable=False, served_model=None, latency_ms=None,
                    consecutive_failures=await self._read_failures(redis, runtime.slug),
                    status="switching",
                    phase=switching.get("phase"),
                    switch_source=switching.get("source"),
                    last_served_model=remembered.get("served_model"),
                    was_own=bool(remembered.get("own")),
                )
                # ADR-078: solange DIESE Instanz lädt, lädt auch die Box —
                # also bekommt die Slot-Zeile der Box denselben Marker mit
                # derselben Restlaufzeit. Ohne diese Zeilen lief der
                # Slot-Marker nach 20 Minuten aus (``SWITCHING_TTL``) und ein
                # ehrlicher 30-Minuten-Kaltstart feuerte ab Minute 21
                # ``runtime.unreachable`` für die Slot-Zeile — Fehlalarm bei
                # jedem langsamen Wechsel (Review 05.09.2026, M1).
                await self._refresh_slot_grace(session, runtime, switching)
                return
            # What did the port say last, and was it THIS row's own answer?
            # (ADR-096 §11.2): only a row whose last healthy probe was its own
            # answer can die — the GLM row that never ran on the shared :8000
            # cannot. Read from the durable answer memory, not the live
            # snapshot: that one expires after minutes (_state_ttl), so a backend that
            # was down longer would forget who held the port.
            remembered = await engine_death.last_answer(redis, runtime.slug)
            last_served = remembered.get("served_model")
            was_own = bool(remembered.get("own"))
            fails = await self._bump_failures(redis, runtime.slug)
            failure = getattr(probed, "failure", None)
            if fails in (1, UNREACHABLE_EVENT_THRESHOLD):
                # Root cause of the silent 23 minutes on 19.10.: every failed
                # probe was logged at DEBUG only. The first and the threshold
                # probe of an outage now leave a line.
                logger.info(
                    "runtime %s: probe failed (%s) — %d in a row",
                    runtime.slug, failure or "no answer", fails,
                )
            death = await engine_death.get_death(redis, runtime.slug)
            if death is None and settings.runtime_engine_death_enabled:
                try:
                    death = await self._maybe_declare_death(
                        session, redis, runtime,
                        fails=fails, failure=failure, was_own=was_own,
                        last_served=last_served,
                    )
                except Exception:  # noqa: BLE001 — never let it cost the alarm
                    logger.exception("engine-death handling failed for %s", runtime.slug)
                    death = None
            await self._write_live(
                redis, runtime.slug,
                reachable=False, served_model=None, latency_ms=None,
                consecutive_failures=fails,
                failure=failure,
                last_served_model=last_served,
                was_own=was_own,
                **(engine_death.live_fields(death) if death else {}),
            )
            if fails == UNREACHABLE_EVENT_THRESHOLD and death is None:
                if await self._plain_alarm_wanted(
                    session, redis, runtime, was_own=was_own,
                ):
                    message = (
                        f"{runtime.slug}: endpoint unreachable "
                        f"({fails} consecutive probes)"
                    )
                    detail: dict = {"slug": runtime.slug, "endpoint": runtime.endpoint,
                                    "failure": failure}
                    # Live incident this closes: an endpoint pointing at a box's
                    # LAN IP can fail from a host agent while the box itself is
                    # fine — a Tailscale route on the Mac hijacks that LAN IP
                    # (spark-tailscale-route-hijack-host-agents). If the host has
                    # a Tailscale address on file, say so with the exact fix
                    # instead of leaving the operator to rediscover it by hand.
                    try:
                        host = await resolve_host_for_runtime(session, runtime)
                    except Exception:  # noqa: BLE001 — the alert must fire either way
                        host = None
                    suggestion = address_classify.suggest_endpoint_fix(
                        runtime.endpoint, getattr(host, "tailscale_host", None)
                    )
                    if suggestion:
                        detail["suggested_endpoint"] = suggestion
                        message += (
                            f" — Endpoint ist eine LAN-Adresse, Host hat eine "
                            f"Tailscale-Adresse: '{suggestion}' probieren"
                        )
                    await emit_event(
                        session,
                        "runtime.unreachable",
                        message,
                        severity="warning",
                        detail=detail,
                    )
                # Laufzeit-Anzeige (W3): dieselbe Schwelle, die den Alarm
                # auslöst, beendet auch die "seit wann erreichbar"-Uhr — drei
                # Fehlproben sind derselbe Vertrauensverlust, ob er als
                # Ereignis gemeldet wird oder nur die Bühne beeinflusst.
                if runtime.serving_since is not None:
                    runtime.serving_since = None
                    session.add(runtime)
                    await session.commit()
            try:
                await self._maybe_auto_recover(session, redis, runtime, fails)
            except Exception:  # noqa: BLE001 — an add-on must never be the
                # reason the remaining runtimes in this tick go unprobed.
                logger.exception("auto-recovery failed for %s", runtime.slug)
            return

        if switching is not None:
            # The engine answers again — whatever start put us in grace has
            # finished. This is the single place a switch window ends, so no
            # caller has to poll for readiness itself.
            await clear_switching(runtime.slug)
        # The honest end of the memory prep: the KV cache is allocated, the
        # box may have its page cache and its watermark back (PR8). This runs
        # on EVERY first successful probe, not only ones inside a recorded
        # switch window — the marker itself can be lost (TTL of 20 min, a
        # backend restart mid-load, a preempted prep, a failed recipe
        # switch) while the box still has an outstanding dropper/watermark.
        # finish_for_host is idempotent (no handle stored: a no-op, see
        # host_memory_prep.py), so calling it unconditionally costs nothing
        # extra on the far more common case of "nothing to finish".
        await self._finish_memory_prep(session, runtime, success=True)
        await redis.delete(RedisKeys.runtime_restart_baseline(runtime.slug))
        await redis.delete(self._fail_key(runtime.slug))
        await redis.delete(RedisKeys.runtime_recovery_failures(runtime.slug))
        # The port answers again: this row's death record and the endpoint's
        # one-death / one-alarm keys are history (the event stays in the
        # feed). A stop intent is NOT touched — a neighbour's answer on a
        # shared port says nothing about whether this row was stopped on
        # purpose (ADR-096 §11.1: only its own start lifts it).
        await engine_death.endpoint_answered(redis, runtime)
        # Is this answer THIS row's own engine (ADR-096 §11.2)? Same question
        # and same SSH round trip the probe guard below always asked when the
        # answer does not match the row — asked once here, used twice: by the
        # guard, and remembered so a later outage knows whether this row was
        # up at all. A matching answer (or a slot row) is own — unless another
        # row on this port carries the same model id (two recipe variants of
        # one model): then only the anchor can tell whose engine answered.
        ctx_mismatch = served_ctx is not None and self._context_would_change(runtime, served_ctx)
        anchor_checked = False
        if runtime.is_slot:
            own_answer = True
        elif (
            served == (runtime.model_identifier or "")
            and not ctx_mismatch
            and not await self._model_id_shared_on_port(session, runtime)
        ):
            own_answer = True
        else:
            own_answer = await self._served_answer_is_own(session, runtime)
            anchor_checked = True
        await engine_death.remember_answer(redis, runtime.slug, served=served, own=own_answer)
        if not runtime.is_slot:
            # Whoever answers, a start of this row is no longer pending: its
            # own engine came up, or another engine holds the port now.
            await clear_start_pending(runtime.slug, redis)
            try:
                await self._maybe_lift_stop_intent(
                    session, redis, runtime,
                    own_verified=own_answer if anchor_checked else None,
                )
            except Exception:  # noqa: BLE001 — bookkeeping may not cost the probe
                logger.exception("stop-intent check failed for %s", runtime.slug)
        await self._write_live(
            redis, runtime.slug,
            reachable=True, served_model=served, latency_ms=latency_ms,
            served_context_len=served_ctx,
            consecutive_failures=0,
            own_answer=own_answer,
        )
        # Laufzeit-Anzeige (W3): erste erfolgreiche Probe nach einem
        # Ausfall/Start setzt die Uhr — NUR wenn sie leer ist. Ein bereits
        # laufendes Modell behält seinen (vom Umschalter gesetzten) Wert über
        # jede weitere ruhige Probe hinweg; erst der Übergang von "nicht
        # erreichbar"/"unbekannt" löst ein neues "seit wann" aus.
        if runtime.serving_since is None:
            runtime.serving_since = datetime.now(timezone.utc)
            session.add(runtime)
            await session.commit()
        try:
            await self._confirm_autostart_recipe(session, runtime)
        except Exception:  # noqa: BLE001 — Buchhaltung darf die Probe nicht kosten
            logger.exception("autostart bookkeeping failed for %s", runtime.slug)
        # Engine silent about its window (neither /v1/models nor /health said
        # it)? A slot row then takes the window of the recipe the box has
        # confirmed running — the last source before "leave it alone". Only for
        # the PERSISTED row; the live status above keeps showing what the
        # engine itself reported.
        if served_ctx is None:
            try:
                served_ctx = await self._slot_recipe_context(session, runtime, served)
            except Exception:  # noqa: BLE001 — a fallback may not cost the tick
                logger.exception("slot recipe window lookup failed for %s", runtime.slug)
        model_would_change = served != (runtime.model_identifier or "")
        ctx_would_change = served_ctx is not None and self._context_would_change(
            runtime, served_ctx
        )
        if model_would_change or ctx_would_change:
            # Probe-Guard (Paket 2, live-belegt 15.08.26): mehrere Zeilen können
            # sich einen Endpoint teilen (Spark-Konvention: alle exklusiven
            # Motoren auf :8000). Die Antwort von /v1/models sagt nur, was der
            # PORT serviert — nicht, DASS es dieser Eintrag ist. Eine Zeile mit
            # eigenem Anker (Container/Prozess) bekommt ihre Identität nur
            # geschrieben, wenn der Anker wirklich läuft; sonst gehört die
            # Antwort dem fremden Motor, der den Port gerade hält (so bekamen
            # ds4 und omp-qwen das Qwen-Modell samt 1M-Fenster verpasst).
            if not own_answer:
                logger.info(
                    "runtime %s: drift ignored — own anchor not running, the "
                    "shared-endpoint answer (%r) belongs to another engine",
                    runtime.slug, served,
                )
                return
        if model_would_change:
            await self._handle_drift(session, redis, runtime, served)
        # Context drift is checked independently of model drift: an engine can
        # be restarted with a different --max-model-len while serving the same
        # model id, and a model change that keeps the window must not re-run
        # this. Both paths converge on mark_agents_for_sync, and _handle_drift
        # refreshed the row above, so the comparison here sees current values.
        if served_ctx is not None:
            await self._handle_context_drift(session, redis, runtime, served_ctx)
        # Sicherheitsnetz, ganz zum Schluss: die Zeile stimmt, aber trägt ein
        # gebundener Agent noch den alten Namen? (Nachlese 06.09.2026)
        try:
            await self._sync_stale_agent_models(session, redis, runtime)
        except Exception:  # noqa: BLE001 — ein Netz darf die Runde nie kosten
            logger.exception("agent model guard failed for %s", runtime.slug)

    async def _sync_stale_agent_models(
        self, session: AsyncSession, redis, runtime: Runtime
    ) -> None:
        """Antwortet die Runtime, muss jeder Agent daran ihr Modell fahren.

        Der Drift-Pfad (``_handle_drift``) flaggt die Agenten nur, wenn sich
        die ZEILE ändert. Seit ADR-078 schreibt der Umschalter das neue Modell
        aber sofort selbst in die Slot-Zeile — danach gibt es keine Drift mehr,
        und vor dieser Runde gab es damit auch keinen Sync (Live-Befund
        06.09.2026: ``omp.env`` blieb auf dem alten Modell, jede Anfrage 404).
        Der Umschalter flaggt inzwischen selbst; dies hier ist das Netz für
        jeden anderen Weg, auf dem die beiden auseinanderlaufen können
        (Backend-Neustart mitten im Wechsel, aufgegebener Sync, Handarbeit an
        der Zeile).

        Gedrosselt über einen SET-nx-Anspruch: sonst flaggte ein Sync, der
        nicht durchkommt, die ganze Flotte in jeder Runde neu.

        Ein Agent OHNE eingetragenes Modell (``agents.model`` leer) zählt
        bewusst nicht als veraltet: leer ist kein Beleg für „fährt etwas
        anderes", sondern nur fehlende Buchhaltung — und ein Netz, das auf
        fehlende Buchhaltung reagiert, startet Container ohne Anlass neu.
        """
        model = (runtime.model_identifier or "").strip()
        if not model:
            return
        from app.models.agent import Agent

        bound = (
            await session.exec(select(Agent).where(Agent.runtime_id == runtime.id))
        ).all()
        # Stale and flagged are the SAME set (live 2026-10-20: a host bridge
        # restarted every ~10.5 min, all day):
        # * counted are only agents a sync can reach (is_model_sync_target,
        #   the filter mark_agents_for_sync uses too) — an archived agent with
        #   the previous model was never synced, stayed "stale" forever and
        #   re-fired the net once per guard window;
        # * flagged are only the stale ones (only_ids) — flagging the whole
        #   runtime sent every converged agent through a sync, and a host
        #   agent through a process restart (adapter reload), each window for
        #   as long as ONE agent could not converge.
        stale = [
            agent
            for agent in bound
            if is_model_sync_target(agent, runtime)
            and not agent.pending_runtime_sync
            and (agent.model or "").strip()
            and agent.model.strip() != model
        ]
        if not stale:
            return
        claimed = await redis.set(
            RedisKeys.runtime_agent_model_guard(runtime.slug),
            model,
            nx=True,
            ex=AGENT_MODEL_GUARD_TTL,
        )
        if not claimed:
            return
        flagged = await mark_agents_for_sync(
            session, runtime, only_ids={agent.id for agent in stale}
        )
        logger.info(
            "runtime %s: %s Agent(en) trugen noch ein anderes Modell als %r "
            "(%s) — %s für den Sync geflaggt",
            runtime.slug, len(stale), model,
            ", ".join(agent.name for agent in stale), flagged,
        )

    async def _refresh_slot_grace(
        self, session: AsyncSession, runtime: Runtime, switching: dict
    ) -> None:
        """Den Marker der Slot-Zeile mitziehen, solange die Instanz lädt.

        Der Marker der Rezept-Instanz wird bei jedem Startversuch neu gesetzt;
        der Slot-Marker hatte bisher nur die eine Startzeit. Diese Runde ist der
        Beleg „die Box ist gerade beim Laden" — also wird er hier erneuert. Er
        stirbt damit genau dann, wenn auch der Marker der Instanz stirbt.

        Nichts davon darf eine Wächter-Runde kosten: alles best effort, und
        Zeilen ohne Box oder Slot-Zeilen selbst werden übersprungen.
        """
        if runtime.is_slot or runtime.host_id is None:
            return
        try:
            from app.services.slot_runtimes import find_slot_runtime

            slot = await find_slot_runtime(session, runtime.host_id)
            if slot is None or slot.slug == runtime.slug:
                return
            await mark_switching(
                slot.slug,
                switching.get("phase") or PHASE_LOADING,
                switching.get("source") or SOURCE_SWITCH,
            )
        except Exception:  # noqa: BLE001
            logger.exception("slot grace refresh failed for %s", runtime.slug)

    @staticmethod
    async def _slot_recipe_context(
        session: AsyncSession, runtime: Runtime, served: str | None
    ) -> int | None:
        """The window of the recipe a slot row's box is confirmed to serve.

        Live 01.10.2026: TensorFold's ``/v1/models`` carries no window, and
        the box's slot row kept the 250000 of the previous vLLM recipe although
        the engine served 1048576 — nothing ever told the row otherwise.

        Only a slot row (ADR-078: "whatever the box serves") and only the box's
        confirmed recipe (``hosts.autostart_recipe_slug``, written by
        ``_confirm_autostart_recipe`` once that recipe's own instance answered)
        — and only when that recipe's model is the one the port serves right
        now. Anything else returns ``None`` and the row stays as it is.
        """
        if not runtime.is_slot or runtime.host_id is None or not served:
            return None
        from app.models.host import Host
        from app.models.local_recipe import LocalRecipe

        host = await session.get(Host, runtime.host_id)
        recipe_slug = (getattr(host, "autostart_recipe_slug", None) or "").strip()
        if not recipe_slug:
            return None
        recipe = (
            await session.exec(select(LocalRecipe).where(LocalRecipe.slug == recipe_slug))
        ).first()
        if recipe is None or (recipe.model_identifier or "").strip() != served.strip():
            return None
        ctx = recipe.context_len
        if isinstance(ctx, bool) or not isinstance(ctx, int) or ctx <= 0:
            return None
        return ctx

    @staticmethod
    def _context_would_change(runtime: Runtime, served_ctx: int) -> bool:
        """Mirror of ``_handle_context_drift``'s no-op guard, evaluated BEFORE
        the ownership check so a row that matches the served window anyway
        never pays for an SSH round trip."""
        old_max = runtime.max_context_len
        old_preferred = runtime.preferred_context_len
        return not (
            old_max == served_ctx
            and (old_preferred is None or old_preferred <= served_ctx)
        )

    async def _served_answer_is_own(
        self, session: AsyncSession, runtime: Runtime
    ) -> bool:
        """Does the /v1/models answer actually come from THIS row's engine?

        Anchor rules:
        - docker engines → ``container_name`` must be ``running``,
        - ``ssh_process`` → jedes hinterlegte Handle (``process_name`` und
          ``container_name``) wird von ``runtime_manager.anchor_running``
          auf BEIDE Arten geprueft: ``pgrep -x`` ODER ``docker inspect``.
          Seit #396 darf ``process_name`` naemlich ein Containername sein —
          viele Rezept-Startskripte starten in Wahrheit einen Container.
          Nur ``pgrep`` zu fragen war dafuer blind: die eigene Engine galt
          als fremd, ``model_identifier`` blieb auf dem HF-Pfad stehen und ein
          Agent bekam 404 (Live-Befund 05.09.2026),
        - rows WITHOUT an anchor (omp & friends — deliberately "switchbar"
          pointers at whatever the box serves) keep following the engine:
          for them drift IS the feature, not the bug.

        Unknown state (host unresolvable, non-SSH host, SSH failing — e.g.
        the box mid-stall like 15.08.) counts as NOT own: rewriting a row's
        identity on evidence we could not verify is exactly the poisoning
        this guard exists to stop. The next healthy tick re-evaluates.
        """
        from shlex import quote as shlex_quote

        if runtime.is_slot:
            # ADR-078: eine Slot-Zeile IST der Zeiger auf „was die Box gerade
            # serviert". Für sie ist die Antwort des Ports per Definition die
            # eigene — sie hat keinen Anker und soll auch keinen haben. Diese
            # Prüfung steht VOR der Typ-Verzweigung, damit die Regel nicht
            # daran hängt, welchen runtime_type jemand der Zeile gegeben hat.
            return True

        container = (runtime.container_name or "").strip()
        process = (runtime.process_name or "").strip()
        if runtime.runtime_type in DOCKER_ENGINE_TYPES:
            anchor, mode = container, "docker"
        elif runtime.runtime_type == SSH_PROCESS_TYPE:
            anchor, mode = (process or container), "handle"
        else:
            return True
        if not anchor:
            # Host-Engine ohne Handle: es gibt keinen Beleg, dass DIESE Instanz
            # antwortet. Auf einem geteilten Port wäre es sonst das Modell des
            # Nachbarn, das hier eingetragen wird (Live-Befund 04.09.2026).
            return runtime.runtime_type != SSH_PROCESS_TYPE

        try:
            host = await resolve_host_for_runtime(session, runtime)
        except Exception as exc:  # noqa: BLE001
            logger.debug("probe-guard: host resolution failed for %s: %s",
                         runtime.slug, exc)
            return False
        if not ssh_capable(host):
            return False

        from app.services.runtime_manager import _ssh_run, anchor_running  # noqa: SLF001

        try:
            if mode == "docker":
                out, _, ec = await _ssh_run(
                    f'docker inspect --format "{{{{.State.Status}}}}" '
                    f"{shlex_quote(anchor)} 2>/dev/null",
                    host=host, timeout=20,
                )
                return ec == 0 and out.strip() == "running"
            # Eine Regel fuer Host-Engines: dieselbe Pruefung, die auch Start,
            # Zustand und Stopp benutzen — Prozess ODER Container, ein Treffer
            # genuegt. Wirft sie (SSH tot, Pruefung selbst fehlerhaft), faellt
            # das unten auf „nicht eigen" zurueck.
            return await anchor_running(
                {
                    "runtime_type": runtime.runtime_type,
                    "process_name": process,
                    "container_name": container,
                },
                host=host,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("probe-guard: anchor check failed for %s (%s): %s",
                         runtime.slug, anchor, exc)
            return False

    async def _handle_drift(
        self, session: AsyncSession, redis, runtime: Runtime, served: str
    ) -> None:
        key = RedisKeys.runtime_drift_candidate(runtime.slug)
        candidate = await redis.get(key)
        if isinstance(candidate, bytes):
            candidate = candidate.decode()
        if candidate != served:
            # First sighting (or the engine flapped to yet another model):
            # remember the candidate and wait for one confirming probe.
            await redis.setex(key, self._state_ttl(), served)
            return

        await redis.delete(key)
        old = runtime.model_identifier
        runtime.model_identifier = served
        session.add(runtime)
        await session.commit()
        await session.refresh(runtime)
        await invalidate_cached_model(runtime.slug)
        if runtime.is_slot:
            # ADR-078: der Name einer Slot-Zeile trägt das laufende Modell
            # („<Box> :8000 (aktuell: …)"). Wandert das Modell und der Name
            # nicht mit, zeigt die Oberfläche nach dem ersten Rezeptwechsel
            # etwas Falsches an. Best effort — Kosmetik kostet nie eine Runde.
            try:
                from app.services.slot_runtimes import refresh_slot_display_name

                await refresh_slot_display_name(session, runtime)
            except Exception:  # noqa: BLE001
                logger.exception("slot display name refresh failed for %s", runtime.slug)
        logger.info("runtime %s model drift confirmed: %r → %r",
                    runtime.slug, old, served)
        await emit_event(
            session,
            "runtime.model_changed",
            f"{runtime.slug}: {old or 'n/a'} → {served}",
            severity="info",
            detail={"slug": runtime.slug, "old_model": old, "new_model": served},
        )
        await mark_agents_for_sync(session, runtime)

    # ── Crash-loop detection (PR8) ───────────────────────────────────────

    async def _check_crash_loop(
        self, session: AsyncSession, redis, runtime: Runtime
    ) -> bool:
        """Is this container dying and being restarted rather than loading?

        The gap this closes was live on the Spark: the sparkinfer compose stack
        ships ``restart: unless-stopped``. When the engine could not allocate
        its KV cache it exited, docker restarted it, it exited again — for
        hours. Every signal MC had said the same thing a healthy cold load
        says ("endpoint down, we are in grace"), so nothing was ever raised.

        Docker is the one party that knows: ``RestartCount`` counts exactly
        this. We remember the count at the first unreachable probe and look at
        the DELTA, because an absolute count is meaningless (a container that
        has been up for weeks legitimately carries a few restarts). That
        baseline lives for ``CRASH_LOOP_WINDOW_SECONDS`` (see module constant)
        and is then allowed to reset — an old, already-resolved restart from
        outside the window must not keep contributing to today's delta.

        Task #24 (live-belegt 09.08.26): the delta alone decides whether this
        is a loop (``>= CRASH_LOOP_RESTART_THRESHOLD``). Log content is read
        ONLY after that decision is already "yes" — as a reason string for the
        operator, never as an alternate trigger. The previous version let a
        log-pattern match stop the container on its own, even at delta 0 or 1;
        that is exactly what killed a successful retry mid-load on the Spark
        (restarts 0→1, a single failed first attempt). It also read whatever
        ``docker logs --tail N`` returned without ``--since``, which — for a
        container restarted in place rather than recreated — is the
        concatenation of ALL previous boots, so a resolved failure from hours
        ago could still surface as "the" reason. Logs are now scoped to the
        CURRENT boot via ``--since {{.State.StartedAt}}`` (from the same
        ``docker inspect`` already run below, no extra round trip). Both
        changes are Local Studio's pattern (EVAL-LOCAL-STUDIO.md §9.2a/b):
        count failures, don't parse them; read logs to explain, not to decide.

        When a loop is confirmed the loop is BROKEN first
        (``docker update --restart=no`` + ``docker stop``): leaving it spinning
        while telling the operator about it would mean the box keeps burning
        an engine boot every few seconds for as long as nobody reads the feed.

        ``--restart=no`` is never put back to ``unless-stopped`` — that is
        deliberate, not an oversight. Every future start of this container
        goes through MC (manual start, recipe start, or PR10's exclusivity-
        aware auto-recovery), each of which runs the memory prep from PR8
        first. Restoring the compose autostart would let Docker itself
        relaunch the container straight past that prep on the very next host
        boot, which is exactly the blind boot-autostart that crash-loops
        today.

        Returns True when it acted — the caller then skips the normal
        unreachable/grace handling, because "failed, and here is why" is a
        better statement than either.
        """
        if runtime.runtime_type not in DOCKER_ENGINE_TYPES:
            return False
        container = (runtime.container_name or "").strip()
        if not container:
            # No name, no `docker inspect`. Recipe-switched runtimes are in
            # this state; they are covered by the existing start verification.
            return False

        host = await resolve_host_for_runtime(session, runtime)
        if not ssh_capable(host):
            return False

        from app.services.runtime_manager import _ssh_run  # noqa: SLF001

        try:
            stdout, _, exit_code = await _ssh_run(
                f'docker inspect --format "{{{{.RestartCount}}}} {{{{.State.StartedAt}}}}" {container}',
                host=host,
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001 — a stalling box (SSH reset/
            # banner timeout, live 15.08.26) is "cannot tell", not a crash loop.
            logger.debug("crash-loop: inspect unreachable for %s: %s",
                         runtime.slug, exc)
            return False
        if exit_code != 0:
            # Container gone — that is the auto-recovery case, not this one.
            await redis.delete(RedisKeys.runtime_restart_baseline(runtime.slug))
            return False

        restart_count, started_at = _parse_inspect(stdout)
        if restart_count is None:
            return False

        baseline = await self._restart_baseline(redis, runtime.slug, restart_count)
        delta = restart_count - baseline

        # The decision is delta-only. A pattern match with too few restarts
        # is "one attempt had a visible symptom", not a loop — see docstring.
        if delta < CRASH_LOOP_RESTART_THRESHOLD:
            return False

        # Only now — loop already confirmed — do we pay for reading logs, and
        # only to explain the stop, scoped to the CURRENT boot so a resolved
        # failure from an earlier restart cannot be reported as "the" cause.
        logs = await self._read_container_logs(container, host, since=started_at)
        pattern_hit = next(
            (p for p in CRASH_LOOP_LOG_PATTERNS if p in logs), None
        )
        reason = _last_error_line(logs) or pattern_hit or (
            f"{delta} Neustarts im Startfenster"
        )

        # ADR-096 §11.1: intent BEFORE the stop (same rule as stop_runtime) —
        # no probe may ever see this container gone without it.
        await mark_stop_intent(runtime.slug, INTENT_CRASH_LOOP, by="runtime_watcher")
        try:
            await _ssh_run(f"docker update --restart=no {container}", host=host, timeout=20)
            _, stop_err, stop_code = await _ssh_run(
                f"docker stop {container}", host=host, timeout=60
            )
            stopped = stop_code == 0
        except Exception as exc:  # noqa: BLE001 — the loop is confirmed either
            # way; report it with stopped=False instead of losing the event.
            logger.warning("crash-loop: stop failed for %s: %s", runtime.slug, exc)
            stop_err, stopped = str(exc), False

        logger.error(
            "runtime %s: crash loop detected (restarts %s→%s, pattern=%r) — "
            "container %s stopped=%s",
            runtime.slug, baseline, restart_count, pattern_hit, container, stopped,
        )

        await clear_switching(runtime.slug)
        await redis.delete(RedisKeys.runtime_restart_baseline(runtime.slug))
        await self._finish_memory_prep(session, runtime, success=False)
        # ADR-096 §11.1: MC's own crash-loop stop is a stop with intent — its
        # runtime.crash_loop_stopped below stays THE report; the silence that
        # follows is never counted as an engine death a second time (the
        # intent was set before `docker stop` above). The record keeps the card on "failed — restart loop" across later ticks
        # and keeps the rows sharing the port from alarming.
        loop_doc = {
            "reason": reason,
            "reason_en": f"{runtime.display_name}: restart loop ({delta} restarts), stopped by MC",
            "reason_code": engine_death.CRASH_LOOP,
            "box": getattr(host, "display_name", None) or getattr(host, "slug", None),
            "at": _utcnow_iso(),
            "detected_at": _utcnow_iso(),
            "exit_code": None,
            "log_line": _last_error_line(logs),
            "autostart": False,
        }
        await engine_death.claim_death(redis, runtime, loop_doc)
        await self._write_live(
            redis, runtime.slug,
            reachable=False, served_model=None, latency_ms=None,
            consecutive_failures=await self._read_failures(redis, runtime.slug),
            status="failed",
            reason=reason,
            reason_code=engine_death.CRASH_LOOP,
            restart_count=restart_count,
            container_stopped=stopped,
        )
        await emit_event(
            session,
            "runtime.crash_loop_stopped",
            f"{runtime.slug}: Container startet in Endlosschleife neu "
            f"({delta} Neustarts) — angehalten. Grund: {reason}. "
            f"restart-Policy bleibt auf 'no' — künftige Starts laufen über MC "
            f"(mit Memory-Prep), nicht mehr über Dockers Boot-Autostart.",
            severity="warning",
            detail={
                "slug": runtime.slug,
                "container": container,
                "restart_count": restart_count,
                "restart_baseline": baseline,
                "restarts_observed": delta,
                "log_pattern": pattern_hit,
                "reason": reason,
                "started_at": started_at,
                "container_stopped": stopped,
                "stop_error": None if stopped else (stop_err or None),
                "restart_policy": "no — not restored; future starts go through MC",
            },
        )
        return True

    async def _restart_baseline(self, redis, slug: str, current: int) -> int:
        """RestartCount at the first unreachable probe of this outage.

        ``SET nx`` so the first tick of an outage records it and every later
        one reads it back. The TTL is ``CRASH_LOOP_WINDOW_SECONDS`` (Task
        #24) — deliberately its own constant, not the switch-grace TTL: a
        baseline that outlives the crash-loop window starts counting fresh,
        so a restart from an unrelated, long-resolved incident cannot keep
        contributing to today's delta forever. This is the rolling-window
        half of the fix; the other half is that the trigger is ``delta >=
        CRASH_LOOP_RESTART_THRESHOLD`` alone (see ``_check_crash_loop``).
        """
        key = RedisKeys.runtime_restart_baseline(slug)
        try:
            await redis.set(key, str(current), nx=True, ex=CRASH_LOOP_WINDOW_SECONDS)
            raw = await redis.get(key)
            return int(raw) if raw is not None else current
        except (TypeError, ValueError):
            return current

    async def _read_container_logs(
        self, container: str, host, *, since: str | None = None
    ) -> str:
        """Task #24: scoped to the CURRENT boot when ``since`` is known.

        For a container restarted in place (not recreated), ``docker logs``
        returns the concatenation of every boot the container has ever had —
        without ``--since {{.State.StartedAt}}`` a resolved failure from
        hours or days ago reads exactly like today's cause. ``since`` is
        untrusted-shape but not untrusted-source: it always comes straight
        from the same ``docker inspect`` call this check already made.
        """
        from app.services.runtime_manager import _ssh_run  # noqa: SLF001

        since_flag = f"--since {since} " if since else ""
        try:
            stdout, stderr, _ = await _ssh_run(
                f"docker logs {since_flag}--tail {_CRASH_LOG_LINES} {container} 2>&1",
                host=host,
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("crash-loop: logs unreadable for %s: %s", container, exc)
            return ""
        return stdout or stderr or ""

    async def _finish_memory_prep(
        self, session: AsyncSession, runtime: Runtime, *, success: bool
    ) -> None:
        """Close the pre-start memory prep for this runtime's box (PR8)."""
        try:
            host = await resolve_host_for_runtime(session, runtime)
            if host is None:
                # host_key(None) resolves to the shared "default" bucket —
                # calling finish_for_host(None) for a hostless (cloud)
                # runtime would close out whatever unrelated prep happens
                # to be sitting there, closing it for the wrong runtime.
                return
            await host_memory_prep.finish_for_host(
                host, success=success, runtime_id=runtime.slug
            )
        except Exception:  # noqa: BLE001 — never at the cost of the tick
            logger.exception("memory prep cleanup failed for %s", runtime.slug)

    async def _handle_context_drift(
        self, session: AsyncSession, redis, runtime: Runtime, served_ctx: int
    ) -> None:
        """Persist a changed served context window — same two-probe contract.

        "Engine leads, MC follows" applies to the WINDOW too, not just the
        model id. When the Spark was switched to a 262k engine, drift detection
        moved ``model_identifier`` but left ``max_context_len`` at the 98304 of
        a previous profile. That number is not cosmetic: build_runtime_env
        renders it as omp's ``OMP_CONTEXT_WINDOW`` / ``OMP_MAX_TOKENS``
        (routers/internal.py:122), so a stale window sizes every turn against a
        model that no longer has it.

        ``preferred_context_len`` follows only where it was expressing "use the
        whole window" (it equalled the old max) or where it would now exceed
        the new max and has to be clamped. A deliberately smaller preferred
        value is left alone — the engine owns the ceiling, the operator owns
        the working size below it.
        """
        old_max = runtime.max_context_len
        old_preferred = runtime.preferred_context_len
        if old_max == served_ctx and (
            old_preferred is None or old_preferred <= served_ctx
        ):
            return

        key = RedisKeys.runtime_context_drift_candidate(runtime.slug)
        candidate = await redis.get(key)
        if isinstance(candidate, bytes):
            candidate = candidate.decode()
        if candidate != str(served_ctx):
            await redis.setex(key, self._state_ttl(), str(served_ctx))
            return

        await redis.delete(key)
        runtime.max_context_len = served_ctx
        if old_preferred is None or old_preferred == old_max or old_preferred > served_ctx:
            runtime.preferred_context_len = served_ctx
        session.add(runtime)
        await session.commit()
        await session.refresh(runtime)
        logger.info(
            "runtime %s context drift confirmed: max %r → %r (preferred %r → %r)",
            runtime.slug, old_max, served_ctx,
            old_preferred, runtime.preferred_context_len,
        )
        await emit_event(
            session,
            "runtime.context_changed",
            f"{runtime.slug}: context window {old_max or 'n/a'} → {served_ctx}",
            severity="info",
            detail={
                "slug": runtime.slug,
                "old_max_context_len": old_max,
                "new_max_context_len": served_ctx,
                "old_preferred_context_len": old_preferred,
                "new_preferred_context_len": runtime.preferred_context_len,
                "model": runtime.model_identifier,
            },
        )
        # Same propagation as a model change: the rendered env carries the
        # window, so agents on this runtime need the identical
        # render-then-restart pass. Flagging twice in one tick is harmless —
        # pending_runtime_sync is a boolean, and _tick_inner's single
        # sync_pending_agents pass runs after every runtime has been probed.
        await mark_agents_for_sync(session, runtime)

    # ── Engine death (ADR-096 §11) ───────────────────────────────────────

    async def _maybe_declare_death(
        self,
        session: AsyncSession,
        redis,
        runtime: Runtime,
        *,
        fails: int,
        failure: str | None,
        was_own: bool,
        last_served: str | None,
    ) -> dict | None:
        """Declare an engine death — or return ``None`` and leave the existing
        threshold of 3 to the plain ``runtime.unreachable``.

        Candidate (§11.1/§11.2): not a slot row (it reports through its recipe
        row), its last healthy probe was its OWN answer, ``serving_since`` set,
        no stop intent (operator stop, switched away, schedule, idle unload,
        crash-loop stop) and no eviction marker.

        When to ask the box, at most once per probe and only while the outage
        is young (``DEATH_CHECK_WINDOW_S``, 270 s — 9 probes at 30 s, 3 at
        90 s): at every probe on ``connection refused`` (nothing listens — the
        fast path), and once, at the threshold, on any other failure (a
        timeout is what a stalling box produces too, so the threshold of 3
        applies unchanged — ADR-096 §11.2). The row's own anchor decides:
        exited / gone → death at THIS probe; running, or not checkable (box
        mid-stall, SSH down) → no death now, the plain threshold alarm at
        probe 3. A death the anchor confirms later in the window (a container
        that outlives its closed port, a wedged engine killed later) follows
        that alarm as the error with the cause. Never starts or stops anything.
        """
        if runtime.is_slot or not was_own:
            return None
        if fails > self._death_check_probes():
            return None  # the window is over — no SSH every tick of a long outage
        if failure != FAIL_REFUSED and fails != UNREACHABLE_EVENT_THRESHOLD:
            return None  # a timeout: asked once, at the threshold
        if runtime.serving_since is None:
            # Up to the threshold an empty clock means the row was not up.
            # Past it, this outage's own threshold emptied it (the plain-alarm
            # path) — unless MC launched the row and it never answered: a
            # start that never came up ends in its one plain alarm (§11.2),
            # never in a death, even when its container exits while loading.
            if fails <= UNREACHABLE_EVENT_THRESHOLD:
                return None
            if await get_start_pending(runtime.slug, redis) is not None:
                return None
        if await get_stop_intent(runtime.slug, redis) is not None:
            return None
        if await get_evicted(runtime.slug, redis) is not None:
            return None
        if await engine_death.endpoint_had_death(redis, runtime.endpoint):
            return None  # one engine death = one death on that port
        dark_since = await self._last_dark_probe_at(redis, runtime.slug)
        try:
            anchor = await asyncio.wait_for(
                self._anchor_exit_state(session, runtime, running_report_after=dark_since),
                timeout=_ANCHOR_CHECK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.info("runtime %s: anchor check timed out — no verdict", runtime.slug)
            return None
        if anchor.get("state") not in ("exited", "gone"):
            return None
        # Explain once, bounded: the crash-loop readers, current boot only.
        marker = None
        log_line = None
        container = anchor.get("container")
        if container and anchor.get("host") is not None:
            try:
                logs = await asyncio.wait_for(
                    self._read_container_logs(
                        container, anchor["host"], since=anchor.get("started_at"),
                    ),
                    timeout=_EXPLAIN_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logs = ""
            patterns = CRASH_LOOP_LOG_PATTERNS + engine_death.DEATH_LOG_PATTERNS
            marker = next((p for p in patterns if p in logs), None)
            log_line = _last_error_line(logs) or (
                next(
                    (ln.strip()[:500] for ln in reversed(logs.splitlines())
                     if marker and marker in ln),
                    None,
                )
            )
        code = engine_death.classify(
            oom_killed=anchor.get("oom_killed"),
            exit_code=anchor.get("exit_code"),
            marker=marker,
            process_gone=anchor.get("process_gone", False),
        )
        at = anchor.get("finished_at") or _utcnow_iso()
        box = anchor.get("box")
        reason_de, reason_en = engine_death.sentences(
            code, display=runtime.display_name, box=box, at=at,
            exit_code=anchor.get("exit_code"), marker=marker,
        )
        autostart_host, autostart_recipe = await self._autostart_target(session, runtime)
        doc = {
            "reason": reason_de,
            "reason_en": reason_en,
            "reason_code": code,
            "box": box,
            "at": at,
            "detected_at": _utcnow_iso(),
            "exit_code": anchor.get("exit_code"),
            "oom_killed": anchor.get("oom_killed"),
            "log_line": log_line,
            "anchor": anchor.get("name"),
            "anchor_source": anchor.get("source"),
            "model": last_served,
            "failure": failure,
            "probes": fails,
            "autostart": bool(
                settings.runtime_auto_recovery_enabled
                and autostart_host is not None
                and autostart_recipe is not None
            ),
        }
        if not await engine_death.claim_death(redis, runtime, doc):
            return None
        # Plain values: the comment pass commits, and nothing below may
        # depend on expired ORM state.
        slug, endpoint, runtime_id = runtime.slug, runtime.endpoint, runtime.id
        try:
            commented = await engine_death.comment_running_tasks(
                session, runtime, reason_de=reason_de,
            )
        except Exception:  # noqa: BLE001 — the alarm must fire either way
            logger.exception("engine-death: task comments failed for %s", slug)
            commented = []
            try:
                await session.rollback()
                await session.refresh(runtime)
            except Exception:  # noqa: BLE001
                logger.exception("engine-death: refresh of %s failed", slug)
        doc["tasks_commented"] = len(commented)
        try:
            await redis.set(
                RedisKeys.runtime_death(slug), json.dumps(doc), xx=True,
                ex=engine_death.DEATH_TTL,
            )
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "runtime %s: engine death (%s, probe %d, %s) — %s",
            slug, code, fails, failure or "no answer", reason_de,
        )
        # §11.4: the one notification — runtime.unreachable, with the cause,
        # for the dead row only (rows sharing the port add none).
        await emit_event(
            session,
            "runtime.unreachable",
            reason_de,
            severity="error",
            detail={
                "slug": slug,
                "runtime_id": str(runtime_id),
                "endpoint": endpoint,
                "engine_death": True,
                "reason_code": code,
                "reason": reason_de,
                "reason_en": reason_en,
                "box": box,
                "at": at,
                "exit_code": doc["exit_code"],
                "oom_killed": doc["oom_killed"],
                "log_line": log_line,
                "anchor": doc["anchor"],
                "model": last_served,
                "failure": failure,
                "consecutive_failures": fails,
                "autostart": doc["autostart"],
                "commented_task_ids": commented,
            },
        )
        if runtime.serving_since is not None:
            runtime.serving_since = None
            session.add(runtime)
            await session.commit()
        return doc

    async def _last_dark_probe_at(self, redis, slug: str) -> datetime | None:
        """When the previous probe of this row found the port dark — read from
        the live snapshot before this probe overwrites it. ``None`` when that
        probe got an answer (this is the outage's first failed probe) or
        nothing is known."""
        try:
            raw = await redis.get(RedisKeys.runtime_live(slug))
            doc = json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(doc, dict) or doc.get("reachable") is not False:
            return None
        try:
            return ensure_aware(datetime.fromisoformat(doc["last_probe_at"]))
        except (KeyError, TypeError, ValueError):
            return None

    async def _anchor_exit_state(
        self, session: AsyncSession, runtime: Runtime, *,
        running_report_after: datetime | None = None,
    ) -> dict:
        """Is this row's own engine still there? Read-only.

        ``state``: ``running`` | ``exited`` (with exit code / OOMKilled /
        finish time when Docker knows them) | ``gone`` (container removed or
        host process not alive) | ``unknown`` (no anchor, box not askable,
        Docker not readable). The node-agent report (§3.2) is asked first —
        a "running" there needs no SSH, but only when it was reported after
        ``running_report_after`` (the previous probe that found the port
        dark). A report counts as fresh for 120 s and arrives every 15 s: one
        taken just before the death still says "running" and would hide the
        death for a tick. ``None`` (the outage's first failed probe) → the
        one SSH inspect decides.
        """
        from app.models.host import Host
        from app.services import device_state
        from app.services.runtime_manager import (  # noqa: SLF001
            _ssh_run,
            anchor_running,
            runtime_anchor_names,
        )

        handles = {
            "runtime_type": runtime.runtime_type,
            "process_name": runtime.process_name,
            "container_name": runtime.container_name,
        }
        names = runtime_anchor_names(handles)
        if not names:
            return {"state": "unknown", "why": "no_anchor"}
        out: dict = {"name": names[0], "source": "ssh"}
        host_row = None
        if runtime.host_id is not None:
            try:
                host_row = await session.get(Host, runtime.host_id)
            except Exception:  # noqa: BLE001
                host_row = None
        reported = device_state.ANCHOR_UNKNOWN
        if host_row is not None:
            out["box"] = host_row.display_name or host_row.slug
            reported = device_state.handle_status(
                host_row.agent_device_state, host_row.agent_device_state_updated_at, names,
            )
            reported_at = host_row.agent_device_state_updated_at
            if (
                reported == device_state.ANCHOR_RUNNING
                and running_report_after is not None
                and reported_at is not None
                and ensure_aware(reported_at) > running_report_after
            ):
                return {**out, "state": "running", "source": "node_agent"}
        # A fresh node-agent "stopped" (every handle, both kinds) is evidence
        # on its own when SSH cannot add the exit details.
        agent_gone = {**out, "state": "gone", "source": "node_agent"} \
            if reported == device_state.ANCHOR_STOPPED else None
        try:
            host = await resolve_host_for_runtime(session, runtime)
        except Exception:  # noqa: BLE001
            host = None
        if host is not None and "box" not in out:
            out["box"] = getattr(host, "display_name", None) or getattr(host, "slug", None)
        if host is None or not ssh_capable(host):
            return agent_gone or {**out, "state": "unknown", "why": "no_ssh"}
        out["host"] = host
        docker_unreadable = False
        for name in names:
            try:
                stdout, stderr, ec = await _ssh_run(
                    'docker inspect --format '
                    '"{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|'
                    '{{.State.FinishedAt}}|{{.State.StartedAt}}" '
                    f"{shlex_quote(name)}",
                    host=host, timeout=_ANCHOR_SSH_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001 — box stalling = cannot tell
                logger.debug("engine-death: inspect failed for %s: %s", runtime.slug, exc)
                return agent_gone or {**out, "state": "unknown", "why": "ssh_failed"}
            if ec == 0 and (stdout or "").strip():
                parsed = _parse_state_inspect(stdout)
                if parsed["status"] in ("running", "restarting", "created", "paused"):
                    # "restarting" is the crash-loop check's case, not a death.
                    return {**out, "name": name, "state": "running"}
                return {**out, **parsed, "name": name, "container": name, "state": "exited"}
            if "no such" not in f"{stdout} {stderr}".lower():
                docker_unreadable = True
        if runtime.runtime_type == SSH_PROCESS_TYPE:
            try:
                alive = await anchor_running(handles, host=host)
            except Exception as exc:  # noqa: BLE001
                logger.debug("engine-death: handle check failed for %s: %s", runtime.slug, exc)
                return {**out, "state": "unknown", "why": "handle_check_failed"}
            if alive:
                return {**out, "state": "running"}
            return {**out, "state": "gone", "process_gone": True}
        if docker_unreadable:
            return {**out, "state": "unknown", "why": "docker_unreadable"}
        return {**out, "state": "gone"}

    async def _stopped_on_purpose(self, slug: str, redis) -> bool:
        """MC stopped this row on purpose: a stop intent, or a (1 h) eviction
        marker from a switch (ADR-096 §11.1)."""
        if await get_stop_intent(slug, redis) is not None:
            return True
        return await get_evicted(slug, redis) is not None

    async def _plain_alarm_wanted(
        self, session: AsyncSession, redis, runtime: Runtime, *, was_own: bool,
    ) -> bool:
        """The plain ``runtime.unreachable`` at the threshold — once per
        endpoint outage, never for planned silence (ADR-096 §11.1/§11.4).

        Quiet only for what MC KNOWS is planned:
        - this row was stopped on purpose (stop intent or eviction marker);
        - a death on this endpoint already said it, with its cause;
        - the port's holder was stopped on purpose. Holders are the recipe
          rows whose last healthy probe was their OWN answer (durable answer
          memory) plus rows MC launched that never answered (pending start —
          a switch whose new recipe never came up is NOT planned, whatever
          the grace said before it ran out);
        - a recipe row that is not a holder, while a holder is not stopped on
          purpose: that holder (or the slot row) speaks for the port — the
          GLM row that never ran on 19.10. adds nothing.
        Nothing known about the holder (memory gone) → the alarm fires, as on
        main. Exactly one row per endpoint outage raises it.
        """
        if await self._stopped_on_purpose(runtime.slug, redis):
            return False
        if await engine_death.endpoint_had_death(redis, runtime.endpoint):
            return False
        rows = [] if runtime.is_slot else [runtime]
        if runtime.endpoint:
            rows += list((await session.exec(
                select(Runtime).where(
                    Runtime.enabled.is_(True),
                    Runtime.endpoint == runtime.endpoint,
                    Runtime.id != runtime.id,
                    Runtime.is_slot.is_(False),
                )
            )).all())
        holders: list[Runtime] = []
        for row in rows:
            if row.id == runtime.id:
                own = was_own
            else:
                own = bool((await engine_death.last_answer(redis, row.slug)).get("own"))
            if own or await get_start_pending(row.slug, redis) is not None:
                holders.append(row)
        if holders:
            wanted = [h for h in holders if not await self._stopped_on_purpose(h.slug, redis)]
            if not wanted:
                return False
            if not runtime.is_slot and all(h.id != runtime.id for h in wanted):
                return False
        return await engine_death.claim_endpoint_alarm(redis, runtime)

    async def _model_id_shared_on_port(self, session: AsyncSession, runtime: Runtime) -> bool:
        """Does another enabled recipe row on this endpoint carry the same
        ``model_identifier``? Then a matching answer proves nothing."""
        if not runtime.endpoint or not runtime.model_identifier:
            return False
        other = (await session.exec(
            select(Runtime.id).where(
                Runtime.enabled.is_(True),
                Runtime.endpoint == runtime.endpoint,
                Runtime.id != runtime.id,
                Runtime.is_slot.is_(False),
                Runtime.model_identifier == runtime.model_identifier,
            ).limit(1)
        )).first()
        return other is not None

    async def _maybe_lift_stop_intent(
        self, session: AsyncSession, redis, runtime: Runtime, *, own_verified: bool | None,
    ) -> bool:
        """ADR-096 §11.1: a stop intent ends with the row's own next start —
        also one MC did not issue (``start.sh`` by hand after a stop via MC, a
        ``--restart always`` container after a box reboot). Without this, the
        old stop would silence that engine's next real death for good.

        Evidence, all of it: the row has an anchor, the anchor runs and the
        port answers (checked by the same probe guard as drift), and the stop
        was asked at least ``STOP_INTENT_SETTLE`` ago — an engine still
        answering while its stop runs is not a start. A neighbour's answer on
        a shared port never counts (its anchor is not this row's). Lifts the
        eviction marker with it: the switch it described is undone.
        """
        intent = await get_stop_intent(runtime.slug, redis)
        evicted = await get_evicted(runtime.slug, redis)
        if intent is None and evicted is None:
            return False
        ages = [
            marker_age_seconds(intent, "since") if intent is not None else None,
            marker_age_seconds(evicted, "at") if evicted is not None else None,
        ]
        known = [a for a in ages if a is not None]
        if known and min(known) < STOP_INTENT_SETTLE:
            return False
        from app.services.runtime_manager import runtime_anchor_names

        if not runtime_anchor_names({
            "runtime_type": runtime.runtime_type,
            "process_name": runtime.process_name,
            "container_name": runtime.container_name,
        }):
            return False  # nothing on the box to tell this row's engine apart
        own = own_verified if own_verified is not None else \
            await self._served_answer_is_own(session, runtime)
        if not own:
            return False
        await clear_stop_intent(runtime.slug)
        await clear_evicted(runtime.slug)
        logger.info(
            "runtime %s: stop intent lifted (%s) — its own engine answers again, "
            "started outside MC", runtime.slug, (intent or {}).get("why") or "evicted",
        )
        return True

    # ── Auto-recovery ────────────────────────────────────────────────────

    async def _maybe_auto_recover(
        self, session: AsyncSession, redis, runtime: Runtime, fails: int
    ) -> None:
        """One start attempt for an engine whose host is back but whose
        process/container is gone (PR5; umgebaut in P3, 04.09.2026).

        WER ENTSCHEIDET, OB HIER ETWAS STARTET (P3)
        --------------------------------------------
        Bis P3 entschied das die Engine-Art: Docker ja, alles andere nie. Wer
        eine Wiederbelebung verhindern wollte (Handtest auf der Box!), musste
        ``runtimes.enabled`` umlegen — ein Trick mit Nebenwirkungen.

        Jetzt entscheidet die BOX: ``hosts.autostart_enabled``. Aus (Standard)
        heisst „MC startet hier von sich aus gar nichts". An heisst „starte
        genau das Rezept, das zuletzt über den Umschalter lief"
        (``hosts.autostart_recipe_slug``) — keine andere Runtime auf derselben
        Box, auch keine, die zufällig gerade ausfällt.

        Eine Runtime OHNE ``host_id`` (Cloud, Settings-Fallback) hat keine Box,
        die entscheiden könnte: für sie gilt unverändert die alte Regel
        (nur Docker-Engines).

        Die Schwellen bleiben, wie sie waren:

        - nur ein BESTÄTIGTER Ausfall (Fehlerzähler an der Schwelle),
        - nie während eines geplanten Wechsels (der Aufrufer steigt bei
          Grace vorher aus),
        - nur wenn die Box selbst wieder antwortet,
        - ein Versuch je 15 Minuten (Redis-Claim, auch über Worker hinweg),
        - nach zwei erfolglosen Versuchen Schluss, mit Meldung.

        Alles best effort: ein Fehler hier lässt den Wächter genau so
        funktionsfähig zurück, wie er vorher war.
        """
        if not settings.runtime_auto_recovery_enabled:
            return
        if fails < UNREACHABLE_EVENT_THRESHOLD:
            return
        if not runtime.enabled:
            return
        if runtime.is_slot:
            # ADR-078: eine Slot-Zeile hat nichts, was man starten könnte —
            # keinen Startbefehl, keinen Anker. Ohne diesen Riegel meldeten
            # Slot- UND Rezeptzeile denselben Ausfall und es liefen ZWEI
            # Startpfade auf dieselbe Box (Betriebs-Review 05.09.2026, A3).
            return
        intent = await get_stop_intent(runtime.slug, redis)
        if intent is not None:
            # ADR-096 §11.1: MC stopped this on purpose (operator, switch,
            # schedule, idle unload, crash-loop stop). That intent has no
            # expiry and only the runtime's own next start lifts it — a
            # restart from here would overwrite the operator's decision.
            logger.info(
                "auto-recovery: skipping %s — stopped on purpose (%s)",
                runtime.slug, intent.get("why"),
            )
            return
        evicted = await get_evicted(runtime.slug, redis)
        if evicted is not None:
            # Ein Start hat diese Runtime ABSICHTLICH gestoppt, um die Box
            # freizumachen (Rezeptwechsel). Sie jetzt „wiederzubeleben" hiesse,
            # den Wechsel des Betreibers rückgängig zu machen — und die neue
            # Engine mitten im Laden zu verdrängen (Live 01.10.2026). Erst ein
            # neuer Start dieser Runtime oder das Ablaufen der Frist hebt das auf.
            logger.info(
                "auto-recovery: skipping %s — stopped on purpose by the start of %s",
                runtime.slug, evicted.get("by"),
            )
            return

        host_row, recipe = await self._autostart_target(session, runtime)
        if runtime.host_id is not None:
            # Die Box hat das letzte Wort — und sie hat es abgelehnt.
            if host_row is None or recipe is None:
                return
            # Auch mit Schalter nur Engines, die MC über einen BEFEHL startet
            # (das sind genau die drei Rezept-Engines). LM Studio startet über
            # ``lms load``, Cloud-Runtimes gar nicht — für die gäbe es hier
            # nichts zu starten.
            if runtime.runtime_type not in COMMAND_DRIVEN_ENGINE_TYPES:
                return
        elif runtime.runtime_type not in DOCKER_ENGINE_TYPES:
            # Runtime ohne Box: wie bisher NUR Docker. Ein Host-Prozess ist nur
            # gegen die Prozesstabelle prüfbar, und die Engines dahinter laden
            # schon mal eine Stunde — ein blinder Neustart könnte den Ausfall
            # verschlimmern.
            return
        try:
            host = await resolve_host_for_runtime(session, runtime)
        except Exception as exc:  # noqa: BLE001
            logger.debug("auto-recovery: host resolution failed for %s: %s",
                         runtime.slug, exc)
            return
        if not ssh_capable(host):
            return

        failures = await self._read_recovery_failures(redis, runtime.slug)
        if failures >= AUTO_RECOVERY_MAX_ATTEMPTS:
            return  # given up already — the event was emitted at the transition

        if not await self._host_answers(runtime, host):
            return  # box is down, not just the container — nothing to recover

        # After the cheap gates: the sibling check may ask the box over SSH
        # (anchor of each exclusive sibling), so it runs only for a runtime
        # that would otherwise really be started now.
        sibling = await self._active_exclusive_sibling(session, redis, runtime)
        if sibling is not None:
            # The live reboot-test failure: qwen-general (a deliberately
            # parked solo resident) got auto-recovered while its exclusive
            # sibling deepseek-v4-flash-sparkinfer was loading on the same
            # box. A successful recovery here would evict exactly that
            # engine via ensure_exclusive_host's exclusivity sweep — the
            # single box is spoken for by whichever exclusive_memory runtime
            # is already running, loading, or mid memory-prep, and
            # auto-recovery must not overrule that automatically.
            logger.debug(
                "auto-recovery: skipping %s — exclusive sibling %s is active "
                "on the same host", runtime.slug, sibling,
            )
            return

        # A duo needs its recorded worker box, and that box must not be held by
        # another active engine. Both are DETERMINISTIC refusals, not failed
        # attempts: counting them burned the two attempts and ended in
        # ``auto_recovery_given_up`` ("der Wächter schaltet einfach ab",
        # Mark 19.10.) although nothing had been tried. A missing worker is
        # reported once a day as ``runtime.members_incomplete`` by the
        # membership check of this cycle; a busy worker box is the operator's
        # choice (same rule as the head box above) — auto-recovery never
        # evicts it.
        if host_row is not None and recipe is not None:
            blocked = await self._duo_worker_blocked(session, redis, runtime)
            if blocked is not None:
                logger.info("auto-recovery: skipping %s — %s", runtime.slug, blocked)
                return

        if not await self._claim_recovery_cooldown(redis, runtime.slug):
            return

        # Counted BEFORE the attempt: a start call that returns ok but never
        # produces a serving engine must not be able to loop forever. The
        # counter is cleared by the next probe that actually sees the engine.
        attempt = await self._bump_recovery_failures(redis, runtime.slug)
        await emit_event(
            session,
            "runtime.auto_recovery_started",
            f"{runtime.slug}: host reachable but engine down — starting "
            f"(attempt {attempt}/{AUTO_RECOVERY_MAX_ATTEMPTS})",
            severity="info",
            detail={"slug": runtime.slug, "attempt": attempt,
                    "consecutive_failures": fails},
        )

        result = await self._autostart_start(session, runtime, host, host_row, recipe)
        await self._record_autostart_attempt(session, host_row, recipe, result)

        if result.get("ok"):
            await emit_event(
                session,
                "runtime.auto_recovery_succeeded",
                f"{runtime.slug}: start accepted — {result.get('message')}",
                severity="info",
                detail={"slug": runtime.slug, "attempt": attempt},
            )
            return

        await emit_event(
            session,
            "runtime.auto_recovery_failed",
            f"{runtime.slug}: auto-recovery start failed — {result.get('message')}",
            severity="warning",
            detail={"slug": runtime.slug, "attempt": attempt,
                    "reason": result.get("message")},
        )
        if attempt >= AUTO_RECOVERY_MAX_ATTEMPTS:
            await emit_event(
                session,
                "runtime.auto_recovery_given_up",
                f"{runtime.slug}: {attempt} auto-recovery attempts failed — "
                f"no further attempts until an operator starts it",
                severity="warning",
                detail={"slug": runtime.slug, "attempts": attempt},
            )

    async def _confirm_autostart_recipe(self, session: AsyncSession, runtime: Runtime) -> None:
        """Das Autostart-Rezept der Box folgt dem Rezept, das WIRKLICH läuft.

        Live-Befund 05.09.2026: ein Klick auf DeepSeek setzte das Rezept sofort
        um; DeepSeek scheiterte an seiner Speicherprüfung, der Wächter versuchte
        4× DeepSeek — und GLM, das vorher lief, kam nie von selbst zurück.
        Darum zählt erst die bestätigte Antwort des Endpunkts, und nur wenn
        die Antwort nachweislich von DIESER Instanz kommt (Anker am Host),
        nicht vom Nachbarn auf demselben Port.
        """
        if runtime.host_id is None:
            return
        topology = runtime.topology if isinstance(runtime.topology, dict) else {}
        recipe_slug = str(topology.get("recipe_slug") or "").strip()
        if not recipe_slug:
            return
        from app.models.host import Host

        host = await session.get(Host, runtime.host_id)
        if host is None or host.autostart_recipe_slug == recipe_slug:
            return
        if not await self._served_answer_is_own(session, runtime):
            return
        old = host.autostart_recipe_slug
        host.autostart_recipe_slug = recipe_slug
        session.add(host)
        await session.commit()
        await emit_event(
            session,
            "host.autostart_recipe_confirmed",
            f"{host.slug}: Autostart folgt jetzt {recipe_slug} (läuft bestätigt; vorher {old or '—'})",
            severity="info",
            detail={"host_id": str(host.id), "slug": host.slug, "recipe_slug": recipe_slug, "previous": old},
        )

    async def _autostart_target(
        self, session: AsyncSession, runtime: Runtime
    ) -> tuple[object | None, object | None]:
        """Darf diese Runtime auf IHRER Box automatisch starten? (P3)

        Liefert ``(host, recipe)``, wenn die Box den Autostart eingeschaltet
        hat UND diese Runtime die Instanz des dort hinterlegten Rezepts ist.
        Sonst ``(None, None)`` — und das heisst: nicht anfassen.

        Die Zuordnung Runtime ↔ Rezept macht ``recipe_switcher`` und niemand
        sonst (ADR-077: ein Ort rechnet, einer zeigt).
        """
        if runtime.host_id is None:
            return None, None
        if runtime.is_slot:
            # ADR-078: die Slot-Zeile ist nie die Instanz eines Rezepts — sie
            # darf also auch nie das Autostart-Ziel einer Box sein. Doppelt
            # geriegelt (recipe_matches_runtime sagt für sie ohnehin nein),
            # weil ein Autostart auf einer Zeile ohne Startbefehl nichts
            # anderes als eine Endlosschleife wäre.
            return None, None
        from app.models.host import Host
        from app.models.local_recipe import LocalRecipe
        from app.services.recipe_switcher import recipe_matches_runtime

        host = await session.get(Host, runtime.host_id)
        if host is None or not host.autostart_enabled or not host.autostart_recipe_slug:
            return None, None
        recipe = (
            await session.exec(
                select(LocalRecipe).where(LocalRecipe.slug == host.autostart_recipe_slug)
            )
        ).first()
        if recipe is None or not recipe_matches_runtime(recipe, runtime):
            return None, None
        return host, recipe

    async def _autostart_start(
        self, session: AsyncSession, runtime: Runtime, host, host_row, recipe
    ) -> dict:
        """Der Start selbst — Solo wie bisher, Verbund über den Umschalter.

        Ein Zweibox-Rezept darf NICHT einfach ``start_runtime`` bekommen: seine
        `.env` muss vorher die Adressen der beiden Boxen tragen und die
        Worker-Box muss frei sein. Genau das macht
        ``recipe_switcher.start_recipe_on_host`` — derselbe Weg wie ein Klick
        des Betreibers, kein zweiter Startpfad.
        """
        from app.services.runtime_manager import runtime_row_to_dict, start_runtime

        topology = runtime.topology if isinstance(runtime.topology, dict) else {}
        nodes = 1
        try:
            nodes = int(topology.get("nodes", 1) or 1)
        except (TypeError, ValueError):
            nodes = 1

        if nodes >= 2 and host_row is not None and recipe is not None:
            from app.services import recipe_switcher, runtime_membership

            # The worker comes from runtime_hosts (ADR-096 §1.5), never from a
            # guess: a duo without a recorded worker is not revived on
            # whichever box happens to be free.
            worker_id = await runtime_membership.recorded_worker_id(session, runtime.id)
            if worker_id is None:
                return {
                    "ok": False,
                    "message": (
                        f"{runtime.slug}: Verbund ohne eingetragene Worker-Box — "
                        "nicht automatisch gestartet (Worker wählen)"
                    ),
                }
            try:
                return await recipe_switcher.start_recipe_on_host(
                    session,
                    host_row,
                    recipe,
                    worker_host_id=str(worker_id),
                )
            except recipe_switcher.RecipeStartError as exc:
                return {"ok": False, "message": exc.detail}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"Verbund-Start scheiterte: {exc}"}

        try:
            return await start_runtime(
                runtime_row_to_dict(runtime), host=host,
                grace_source=SOURCE_AUTO_RECOVERY,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"start_runtime raised: {exc}"}

    async def _record_autostart_attempt(
        self, session: AsyncSession, host_row, recipe, result: dict
    ) -> None:
        """Was beim letzten Versuch passierte, an der Box festhalten (P3).

        Die Kachel zeigt genau einen Satz. Best effort: eine misslungene Notiz
        darf einen gelungenen Start nicht in einen Fehler verwandeln.
        """
        if host_row is None:
            return
        ok = bool(result.get("ok"))
        message = str(result.get("message") or "")
        sentence = (
            f"Gestartet — {message}" if ok else f"Fehlgeschlagen: {message or 'kein Grund gemeldet'}"
        )
        try:
            host_row.autostart_last_attempt_at = datetime.now(timezone.utc)
            host_row.autostart_last_result = sentence[:500]
            session.add(host_row)
            await session.commit()
            await emit_event(
                session,
                "host.autostart_attempt",
                f"{host_row.slug}: {sentence}",
                severity="info" if ok else "warning",
                detail={
                    "host_id": str(host_row.id),
                    "slug": host_row.slug,
                    "ok": ok,
                    "recipe_slug": getattr(recipe, "slug", None),
                    "message": message,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("autostart: Notiz an %s fehlgeschlagen: %s", host_row.slug, exc)

    async def _duo_worker_blocked(
        self, session: AsyncSession, redis, runtime: Runtime
    ) -> str | None:
        """Why a duo cannot be revived right now without a decision — or None.

        Solo runtimes: always None (nothing to check, no query beyond one).
        """
        from app.services import runtime_membership

        if runtime_membership.topology_nodes(runtime.topology) < 2:
            return None
        worker_id = await runtime_membership.recorded_worker_id(session, runtime.id)
        if worker_id is None:
            return "Verbund ohne eingetragene Worker-Box"
        sibling = await self._active_exclusive_sibling(
            session, redis, runtime, box_id=worker_id
        )
        notice_key = RedisKeys.runtime_worker_blocked_notice(runtime.slug)
        if sibling is None:
            # Block over: the next one is a new episode and is reported again.
            await redis.delete(notice_key)
            return None
        await self._report_worker_blocked(session, redis, runtime, worker_id, sibling)
        return f"Worker-Box ist durch {sibling} belegt"

    async def _report_worker_blocked(
        self, session: AsyncSession, redis, runtime: Runtime, worker_id: uuid.UUID, sibling: str
    ) -> None:
        """ADR-096 "the operator sees it": the duo stays down because its worker
        box is held — an activity event, not only a log line.

        The existing exclusivity event (``runtime.exclusive_blocked``,
        ``box=worker``) — the same one a start that cannot free a box writes.
        Once per blocking episode and blocker: the key holds the blocker, is
        dropped as soon as the box is free again, and ages out after a day.
        """
        notice_key = RedisKeys.runtime_worker_blocked_notice(runtime.slug)
        previous = await redis.get(notice_key)
        if isinstance(previous, bytes):
            previous = previous.decode()
        if previous == sibling:
            return
        await redis.set(notice_key, sibling, ex=MEMBERS_NOTICE_TTL)
        from app.models.host import Host
        from app.services import runtime_manager

        worker = await session.get(Host, worker_id)
        box = worker.slug if worker is not None else str(worker_id)
        sentence = {
            "de": (
                f"{runtime.slug} wird nicht automatisch neu gestartet: die Worker-Box "
                f"{box} ist durch {sibling} belegt. Der Wächter stoppt nie, was dort "
                f"läuft — {sibling} stoppen oder {runtime.slug} selbst starten."
            ),
            "en": (
                f"{runtime.slug} is not restarted automatically: its worker box {box} "
                f"is held by {sibling}. The watcher never stops what runs there — "
                f"stop {sibling} or start {runtime.slug} yourself."
            ),
        }
        event_type, title, severity, detail = runtime_manager.exclusive_event(
            runtime.slug,
            {"ok": False, "message": sentence["de"]},
            box="worker",
            extra={
                "source": "auto_recovery",
                "blocked_by": sibling,
                "worker_host_id": str(worker_id),
                "worker_slug": box,
                "sentence": sentence,
            },
        )
        await emit_event(session, event_type, title, severity=severity, detail=detail)

    async def _active_exclusive_sibling(
        self,
        session: AsyncSession,
        redis,
        runtime: Runtime,
        *,
        box_id: uuid.UUID | None = None,
    ) -> str | None:
        """Slug of an ``exclusive_memory`` sibling on the same box that is
        running, loading, or mid memory-prep — or ``None`` if the box has no
        such claim on it (PR 10).

        Deliberately reads the SAME truth the rest of the system already
        keeps rather than inventing new bookkeeping of "who was last
        started":

        - ``runtime_grace.get_switching`` — a sibling launching or loading
          right now, the exact state DeepSeek was in during the live
          incident,
        - ``runtime_live`` — a sibling already confirmed serving. Recovering
          a second exclusive resident onto a box with one already running
          would just evict it a moment later via
          ``runtime_manager.ensure_exclusive_host``, which is not a decision
          auto-recovery gets to make unattended,
        - an outstanding ``host_memory_prep`` handle for the box, in case a
          start's switching marker already cleared (start failed) or was
          never set (a host-boot autostart outside MC) but the prep is still
          mid-flight.

        Same host scoping as ``runtime_manager._ensure_exclusive_host``: NULL
        ``host_id`` means the settings-fallback box, so two NULLs are the
        same box.

        ``box_id`` checks another box than the runtime's own — a duo's worker
        box (ADR-096): the same question, asked about the second box.

        Deliberately NOT gated on ``runtime.exclusive_memory`` (the 15.08.26
        incident): a parked, non-exclusive row was auto-recovered while the
        exclusive vLLM engine held the same box — two engines loading at
        once, box frozen for 10 minutes. An active exclusive resident claims
        the WHOLE box; what kind of row wants to be revived next to it makes
        no difference.
        """
        statement = select(Runtime).where(
            Runtime.enabled == True,  # noqa: E712
            Runtime.exclusive_memory == True,  # noqa: E712
        )
        box = box_id if box_id is not None else runtime.host_id
        statement = (
            statement.where(Runtime.host_id.is_(None))
            if box is None
            else statement.where(Runtime.host_id == box)
        )
        siblings = [
            rt for rt in (await session.exec(statement)).all() if rt.slug != runtime.slug
        ]
        if box is not None:
            # Ein Verbund, dessen Head woanders steht, belegt diese Box als
            # Worker (``runtime_hosts``) — dieselbe Sicht wie die Verdrängung
            # in ``runtime_manager._ensure_exclusive_host``.
            from app.models.runtime_host import RuntimeHost

            member_ids = [
                m.runtime_id
                for m in (
                    await session.exec(
                        select(RuntimeHost).where(RuntimeHost.host_id == box)
                    )
                ).all()
            ]
            known = {rt.id for rt in siblings} | {runtime.id}
            if member_ids:
                siblings.extend(
                    rt
                    for rt in (
                        await session.exec(
                            select(Runtime).where(
                                Runtime.id.in_(member_ids),
                                Runtime.enabled == True,  # noqa: E712
                                Runtime.exclusive_memory == True,  # noqa: E712
                            )
                        )
                    ).all()
                    if rt.id not in known
                )
        siblings = [rt for rt in siblings if not rt.is_slot]
        if not siblings:
            return None

        for sibling in siblings:
            if await get_switching(sibling.slug, redis) is not None:
                return sibling.slug
            if await self._read_live_reachable(redis, sibling.slug):
                return sibling.slug

        try:
            if box_id is not None and box_id != runtime.host_id:
                from app.models.host import Host
                from app.services.host_resolver import resolved_host_from_row

                box_row = await session.get(Host, box_id)
                host = resolved_host_from_row(box_row) if box_row is not None else None
            else:
                host = await resolve_host_for_runtime(session, runtime)
        except Exception as exc:  # noqa: BLE001
            logger.debug("auto-recovery: sibling host resolution failed for %s: %s",
                         runtime.slug, exc)
            return None
        if host is not None:
            for handle in await host_memory_prep.load_host_handles(host):
                if handle.slug and handle.slug != runtime.slug:
                    return handle.slug

        # Zuletzt die Box selbst fragen (Live 01.10.2026): alle Marker oben
        # können fehlen, während eine Engine trotzdem lädt — ein Start, der
        # „gescheitert" meldete, obwohl die Engine hochkam, räumt Grace-Marker
        # und Prep-Handle weg; ein Kaltstart über 20 min überlebt den
        # Grace-Marker nicht. Läuft der ANKER eines Geschwisters (Container
        # oder Prozess), gehört die Box ihm. Eine Prüfung, die nicht antwortet,
        # zählt als belegt: lieber eine Wiederbelebung verpassen als eine
        # ladende Engine verdrängen.
        from app.services.runtime_manager import anchor_running, runtime_anchor_names

        for sibling in siblings:
            sibling_dict = sibling.to_registry_dict()
            if not runtime_anchor_names(sibling_dict):
                continue
            try:
                sibling_host = await resolve_host_for_runtime(session, sibling) or host
                if await anchor_running(sibling_dict, host=sibling_host):
                    return sibling.slug
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "auto-recovery: anchor of sibling %s not checkable (%s) — "
                    "treating the box as occupied", sibling.slug, exc,
                )
                return sibling.slug
        return None

    async def _read_live_reachable(self, redis, slug: str) -> bool:
        try:
            raw = await redis.get(RedisKeys.runtime_live(slug))
        except Exception as exc:  # noqa: BLE001
            logger.debug("auto-recovery: live snapshot unreadable for %s: %s", slug, exc)
            return False
        if not raw:
            return False
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            doc = json.loads(raw)
        except ValueError:
            return False
        return bool(doc.get("reachable"))

    async def _host_answers(self, runtime: Runtime, host) -> bool:
        """Is the box itself up (container missing) or the whole box down?

        A trivial SSH command is the cheapest honest answer — `get_runtime_state`
        would do the same round trip plus a docker inspect we don't need.
        """
        from app.services.runtime_manager import _ssh_run  # noqa: SLF001

        try:
            _, _, exit_code = await _ssh_run("true", host=host, timeout=15)
        except Exception as exc:  # noqa: BLE001 — box unreachable is the norm here
            logger.debug("auto-recovery: %s host not reachable: %s",
                         runtime.slug, exc)
            return False
        return exit_code == 0

    # ── Redis helpers ────────────────────────────────────────────────────

    @staticmethod
    def _fail_key(slug: str) -> str:
        return f"{RedisKeys.runtime_live(slug)}:fails"

    async def _bump_failures(self, redis, slug: str) -> int:
        fails = int(await redis.incr(self._fail_key(slug)))
        await redis.expire(self._fail_key(slug), self._fail_count_ttl())
        return fails

    async def _read_failures(self, redis, slug: str) -> int:
        """Current fail count WITHOUT incrementing (grace snapshots)."""
        try:
            return int(await redis.get(self._fail_key(slug)) or 0)
        except (TypeError, ValueError):
            return 0

    async def _read_recovery_failures(self, redis, slug: str) -> int:
        try:
            return int(await redis.get(RedisKeys.runtime_recovery_failures(slug)) or 0)
        except (TypeError, ValueError):
            return 0

    async def _bump_recovery_failures(self, redis, slug: str) -> int:
        key = RedisKeys.runtime_recovery_failures(slug)
        count = int(await redis.incr(key))
        await redis.expire(key, AUTO_RECOVERY_FAILURE_TTL)
        return count

    async def _claim_recovery_cooldown(self, redis, slug: str) -> bool:
        """SET-nx claim — the winner is the single worker that may act."""
        return bool(
            await redis.set(
                RedisKeys.runtime_recovery_cooldown(slug), "1",
                nx=True, ex=AUTO_RECOVERY_COOLDOWN,
            )
        )

    async def _write_live(self, redis, slug: str, **fields) -> None:
        payload = {"last_probe_at": _utcnow_iso(), **fields}
        await redis.setex(
            RedisKeys.runtime_live(slug), self._state_ttl(), json.dumps(payload)
        )


runtime_watcher = RuntimeWatcher()
