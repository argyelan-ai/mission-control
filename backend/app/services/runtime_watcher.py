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
import time
from datetime import datetime, timezone

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.services.agent_runtime_switch import (
    _PROBEABLE_RUNTIME_TYPES,
    probe_runtime_model_info,
)
from app.services import host_memory_prep
from app.services.host_resolver import resolve_host_for_runtime
from app.services.runtime_grace import (
    SOURCE_AUTO_RECOVERY,
    clear_switching,
    get_switching,
)
from app.services.runtime_manager import DOCKER_ENGINE_TYPES
from app.services.runtime_model_resolver import (
    invalidate_cached_model,
    session_scope,
)
from app.services.runtime_propagation import (
    mark_agents_for_sync,
    recreate_pending_agents,
    sync_pending_agents,
)

logger = logging.getLogger(__name__)

# Emit runtime.unreachable only after this many consecutive failed probes
# (transient blips and engine restarts must not spam the activity feed).
UNREACHABLE_EVENT_THRESHOLD = 3
_STARTUP_GRACE = 20  # seconds — let DB/Redis/other services come up first

# Auto-recovery (PR5). One attempt per runtime per cooldown window; after this
# many consecutive attempts that did not bring the engine back, stop and hand
# over to the operator rather than restarting in a loop.
AUTO_RECOVERY_COOLDOWN = 900  # 15 min — longer than a normal warmup
AUTO_RECOVERY_MAX_ATTEMPTS = 2
AUTO_RECOVERY_FAILURE_TTL = 6 * 3600  # attempts "age out" after 6h of quiet

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


class RuntimeWatcher:
    def __init__(self, interval: int | None = None) -> None:
        self._interval = interval or settings.runtime_watcher_interval
        self._task: asyncio.Task | None = None
        self._running = False

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
        """One worker per tick. Redis down → run anyway (single-worker default)."""
        try:
            redis = await get_redis()
            return bool(
                await redis.set(
                    RedisKeys.runtime_watcher_lock(), "1",
                    nx=True, ex=max(self._interval - 5, 10),
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
        result = await session.exec(
            select(Runtime).where(
                Runtime.enabled.is_(True),
                Runtime.runtime_type.in_(sorted(_PROBEABLE_RUNTIME_TYPES)),
            )
        )
        for runtime in result.all():
            await self._probe_one(session, runtime)
        # A prep whose backend died mid-start leaves a lowered watermark and a
        # cache-dropper container on the box, with nobody left holding the
        # original value. This is the only place that ever notices (PR8).
        try:
            await host_memory_prep.recover_orphaned_preps()
        except Exception:  # noqa: BLE001
            logger.exception("orphaned memory-prep sweep failed")
        await sync_pending_agents(session)
        # CLI-Tool-Updates: recreate agents flagged by the CLI update check.
        # Runs after the model-sync pass so a same-tick model change is applied
        # by a plain restart before we (potentially) force-recreate.
        await recreate_pending_agents(session)

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
                await self._write_live(
                    redis, runtime.slug,
                    reachable=False, served_model=None, latency_ms=None,
                    consecutive_failures=await self._read_failures(redis, runtime.slug),
                    status="switching",
                    phase=switching.get("phase"),
                    switch_source=switching.get("source"),
                )
                return
            fails = await self._bump_failures(redis, runtime.slug)
            await self._write_live(
                redis, runtime.slug,
                reachable=False, served_model=None, latency_ms=None,
                consecutive_failures=fails,
            )
            if fails == UNREACHABLE_EVENT_THRESHOLD:
                await emit_event(
                    session,
                    "runtime.unreachable",
                    f"{runtime.slug}: endpoint unreachable "
                    f"({fails} consecutive probes)",
                    severity="warning",
                    detail={"slug": runtime.slug, "endpoint": runtime.endpoint},
                )
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
            # …and therefore also the honest end of the memory prep: the KV
            # cache is allocated, the box may have its page cache and its
            # watermark back (PR8).
            await self._finish_memory_prep(session, runtime, success=True)
        await redis.delete(RedisKeys.runtime_restart_baseline(runtime.slug))
        await redis.delete(self._fail_key(runtime.slug))
        await redis.delete(RedisKeys.runtime_recovery_failures(runtime.slug))
        await self._write_live(
            redis, runtime.slug,
            reachable=True, served_model=served, latency_ms=latency_ms,
            served_context_len=served_ctx,
            consecutive_failures=0,
        )
        if served != (runtime.model_identifier or ""):
            await self._handle_drift(session, redis, runtime, served)
        # Context drift is checked independently of model drift: an engine can
        # be restarted with a different --max-model-len while serving the same
        # model id, and a model change that keeps the window must not re-run
        # this. Both paths converge on mark_agents_for_sync, and _handle_drift
        # refreshed the row above, so the comparison here sees current values.
        if served_ctx is not None:
            await self._handle_context_drift(session, redis, runtime, served_ctx)

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
            await redis.setex(key, self._interval * 3, served)
            return

        await redis.delete(key)
        old = runtime.model_identifier
        runtime.model_identifier = served
        session.add(runtime)
        await session.commit()
        await session.refresh(runtime)
        await invalidate_cached_model(runtime.slug)
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
        if host is None or host.kind != "ssh":
            return False

        from app.services.runtime_manager import _ssh_run  # noqa: SLF001

        stdout, _, exit_code = await _ssh_run(
            f'docker inspect --format "{{{{.RestartCount}}}} {{{{.State.StartedAt}}}}" {container}',
            host=host,
            timeout=20,
        )
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

        await _ssh_run(f"docker update --restart=no {container}", host=host, timeout=20)
        _, stop_err, stop_code = await _ssh_run(
            f"docker stop {container}", host=host, timeout=60
        )
        stopped = stop_code == 0

        logger.error(
            "runtime %s: crash loop detected (restarts %s→%s, pattern=%r) — "
            "container %s stopped=%s",
            runtime.slug, baseline, restart_count, pattern_hit, container, stopped,
        )

        await clear_switching(runtime.slug)
        await redis.delete(RedisKeys.runtime_restart_baseline(runtime.slug))
        await self._finish_memory_prep(session, runtime, success=False)
        await self._write_live(
            redis, runtime.slug,
            reachable=False, served_model=None, latency_ms=None,
            consecutive_failures=await self._read_failures(redis, runtime.slug),
            status="failed",
            reason=reason,
            restart_count=restart_count,
            container_stopped=stopped,
        )
        await emit_event(
            session,
            "runtime.crash_loop_stopped",
            f"{runtime.slug}: Container startet in Endlosschleife neu "
            f"({delta} Neustarts) — angehalten. Grund: {reason}",
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
            await host_memory_prep.finish_for_host(host, success=success)
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
            await redis.setex(key, self._interval * 3, str(served_ctx))
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

    # ── Auto-recovery ────────────────────────────────────────────────────

    async def _maybe_auto_recover(
        self, session: AsyncSession, redis, runtime: Runtime, fails: int
    ) -> None:
        """One start attempt for a docker engine whose host is back but whose
        container is gone (PR5).

        The autostart flag file (``runtime_autostart``) only covers "start on
        host boot". After a hard crash the Spark comes back, the flag never
        fires for an already-running host, and the engine stays down until
        someone notices. This closes that gap — deliberately narrow:

        - only a CONFIRMED outage (fail counter at the unreachable threshold),
          never a single blip,
        - never during a planned switch (the caller returns early on grace),
        - only docker engines on an SSH-reachable host — nothing else can be
          started without operator context,
        - only when the box itself answers again, so we don't hammer a box
          that is simply off,
        - one attempt per 15 min (Redis SET-nx claim, which also makes this
          safe across workers),
        - and after two consecutive attempts that did not bring the engine
          back, we stop and tell the operator instead of retrying forever.

        Everything is best-effort: any failure here must leave the watcher
        exactly as functional as it was before.
        """
        if not settings.runtime_auto_recovery_enabled:
            return
        if fails < UNREACHABLE_EVENT_THRESHOLD:
            return
        # DOCKER_ENGINE_TYPES only — ssh_process is deliberately NOT recovered
        # automatically (PR6). A docker start is verifiable against a label the
        # daemon owns; a host process is verifiable only against the process
        # table, and the engines behind ssh_process today can spend an hour
        # loading 110 GiB. Auto-restarting one of those on a probe timeout could
        # relaunch a model that was mid-load, i.e. make the outage worse. Manual
        # start stays one click away; a verified auto-recovery is a follow-up.
        if not runtime.enabled or runtime.runtime_type not in DOCKER_ENGINE_TYPES:
            return
        try:
            host = await resolve_host_for_runtime(session, runtime)
        except Exception as exc:  # noqa: BLE001
            logger.debug("auto-recovery: host resolution failed for %s: %s",
                         runtime.slug, exc)
            return
        if host is None or host.kind != "ssh":
            return

        failures = await self._read_recovery_failures(redis, runtime.slug)
        if failures >= AUTO_RECOVERY_MAX_ATTEMPTS:
            return  # given up already — the event was emitted at the transition

        if not await self._host_answers(runtime, host):
            return  # box is down, not just the container — nothing to recover

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

        from app.services.runtime_manager import start_runtime
        from app.services.sparkrun_manager import _to_runtime_dict  # noqa: SLF001

        try:
            result = await start_runtime(
                _to_runtime_dict(runtime), host=host,
                grace_source=SOURCE_AUTO_RECOVERY,
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "message": f"start_runtime raised: {exc}"}

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
        await redis.expire(self._fail_key(slug), self._interval * 10)
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
            RedisKeys.runtime_live(slug), self._interval * 3, json.dumps(payload)
        )


runtime_watcher = RuntimeWatcher()
