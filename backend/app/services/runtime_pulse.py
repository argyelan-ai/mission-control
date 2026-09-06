"""Runtime Pulse (Runtimes-Buehne v2, PR 1 — see docs/specs/runtimes-buehne-v2.md §6/§7).

A Bühne card's "Lebenszeichen" heat strip needs live tokens/second per host.
This poller supplies it: every ``settings.runtime_pulse_interval`` seconds it
scrapes the Prometheus ``/metrics`` endpoint of every host that carries a slot
runtime (``runtimes.is_slot=True`` with a bound ``host_id`` — the fixed box
endpoint, e.g. a Duo's Head box), reads the engine's cumulative generated-token
counter (vLLM: ``vllm:generation_tokens_total``; SGLang fallback:
``sglang:generation_tokens_total`` / ``sglang:gen_throughput``), and turns the
delta between two probes into tokens/second.

Samples land in a capped Redis ring (``RedisKeys.host_pulse``, 180 points —
15 minutes at the default 5s interval) plus a small meta doc
(``RedisKeys.host_pulse_meta``) telling the API endpoint whether the last
probe succeeded and which engine answered. ``GET /hosts/{id}/pulse``
(routers/hosts.py) reads both and never 5xx's — no data just means
``available: false``.

Design choices worth calling out:
  - **Reuses the watcher's liveness cache** (``RedisKeys.runtime_live``,
    written every tick by ``runtime_watcher``) instead of probing the host a
    second time — the spec is explicit ("Nur pollen, wenn die hosts-Runtime
    reachable"). An unreachable host is skipped outright: no HTTP call, no
    ring write, meta flips to ``available: false``.
  - **First probe after a gap always reports 0 tok/s.** There is no prior
    sample to diff against (or the engine changed under us), so the honest
    answer is "no rate yet", not a guess.
  - **A negative delta (counter reset, e.g. engine restart) clamps to 0**
    rather than reporting negative throughput or raising.
  - **Errors are swallowed, never raised** — one Prometheus scrape failing
    must never take the polling loop with it, and must never surface as a
    5xx on the card. Logging is throttled (one warning per host per outage,
    not one per tick) via a short-TTL Redis marker.

Same lifecycle pattern as ``RuntimeWatcher``/``IntelligenceService``:
singleton, asyncio loop, Redis lock for multi-worker dedup, session
injectable on ``tick()`` for tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis
from app.services.runtime_model_resolver import session_scope

logger = logging.getLogger(__name__)

_STARTUP_GRACE = 20  # seconds — let DB/Redis come up first, mirrors runtime_watcher
RING_MAX_POINTS = 180
SCRAPE_TIMEOUT = 3.0  # seconds

# Metric families to look for, in priority order. Each entry is
# (engine, metric_name). The first one with at least one matching series in
# the scraped text wins; all series for that metric name are summed (a
# multi-model deployment can expose the counter with several label sets).
_METRIC_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("vllm", "vllm:generation_tokens_total"),
    ("sglang", "sglang:generation_tokens_total"),
    ("sglang", "sglang:gen_throughput"),
)

# One throttled warning per host per outage, not one per tick.
_ERROR_LOG_TTL = 300


def _metric_line_pattern(metric_name: str) -> re.Pattern:
    escaped = re.escape(metric_name)
    # Prometheus text format: `name{labels} value` or bare `name value`.
    # Value can be int/float/exponent; trailing timestamp (rare) is ignored.
    return re.compile(
        rf"^{escaped}(?:\{{[^}}]*\}})?\s+([0-9eE+\-.]+)\s*(?:\d+)?\s*$"
    )


def parse_token_counter(text: str) -> tuple[str, float] | None:
    """Sum all series for the first matching metric family in ``text``.

    Returns ``(engine, total)`` or ``None`` if none of the known metric
    families appear at all (engine not exposing generation counters, or the
    scrape returned something unrelated).
    """
    for engine, metric_name in _METRIC_CANDIDATES:
        pattern = _metric_line_pattern(metric_name)
        total = 0.0
        found = False
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = pattern.match(line)
            if not match:
                continue
            try:
                total += float(match.group(1))
            except ValueError:
                continue
            found = True
        if found:
            return engine, total
    return None


class RuntimePulse:
    def __init__(self, interval: int | None = None) -> None:
        self._interval = (
            interval if interval is not None else settings.runtime_pulse_interval
        )
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if not self._interval or self._interval <= 0:
            logger.info("runtime pulse poller disabled (interval=%s)", self._interval)
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("runtime pulse poller started (interval=%ss)", self._interval)

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
                logger.exception("runtime pulse tick failed")
            await asyncio.sleep(self._interval)

    async def _acquire_lock(self) -> bool:
        """One worker per tick. Redis down → run anyway (single-worker default)."""
        try:
            redis = await get_redis()
            return bool(
                await redis.set(
                    RedisKeys.runtime_pulse_lock(), "1",
                    nx=True, ex=max(self._interval - 1, 2),
                )
            )
        except Exception:  # noqa: BLE001
            return True

    async def tick(self, session: AsyncSession | None = None) -> None:
        """One poll pass over every slot runtime. ``session`` injectable for tests."""
        if session is not None:
            await self._tick_inner(session)
            return
        async with session_scope() as own_session:
            await self._tick_inner(own_session)

    async def _tick_inner(self, session: AsyncSession) -> None:
        result = await session.exec(
            select(Runtime).where(
                Runtime.is_slot.is_(True),
                Runtime.host_id.isnot(None),
            )
        )
        for runtime in result.all():
            try:
                await self._poll_one(runtime)
            except Exception:  # noqa: BLE001 — one bad host must not skip the rest
                logger.exception("pulse poll failed for host of runtime %s", runtime.slug)

    async def _poll_one(self, runtime: Runtime) -> None:
        host_id = str(runtime.host_id)
        redis = await get_redis()

        if not await self._read_live_reachable(redis, runtime.slug):
            # Host unreachable per the watcher's own cache — do not probe a
            # second time, do not grow the ring, just say so.
            await self._write_meta(redis, host_id, available=False)
            return

        try:
            import httpx  # local import — mirrors agent_runtime_switch's probe

            url = self._metrics_url(runtime.endpoint)
            async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                raise ValueError(f"metrics endpoint returned {resp.status_code}")
            parsed = parse_token_counter(resp.text)
        except Exception as exc:  # noqa: BLE001 — never raise out of the poller
            await self._log_throttled(redis, host_id, exc)
            await self._write_meta(redis, host_id, available=False)
            return

        if parsed is None:
            await self._log_throttled(
                redis, host_id, "no known generation-token counter in /metrics"
            )
            await self._write_meta(redis, host_id, available=False)
            return

        engine, counter = parsed
        now = time.time()
        tps = await self._compute_tps(redis, host_id, engine=engine, counter=counter, now=now)
        await self._write_sample(redis, host_id, t=now, counter=counter, engine=engine)
        await self._push_point(redis, host_id, t=now, tps=tps)
        await self._write_meta(redis, host_id, available=True, last_ok=now, engine=engine)

    @staticmethod
    def _metrics_url(endpoint: str) -> str:
        base = endpoint.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return f"{base}/metrics"

    async def _compute_tps(
        self, redis, host_id: str, *, engine: str, counter: float, now: float
    ) -> float:
        prev = await self._read_sample(redis, host_id)
        if prev is None or prev.get("engine") != engine:
            # First sample ever, or the engine behind this host changed
            # (recipe switch) — no honest rate to report yet.
            return 0.0
        dt = now - prev["t"]
        if dt <= 0:
            return 0.0
        delta = counter - prev["counter"]
        if delta < 0:
            # Counter reset (engine restarted between probes) — clamp, don't
            # report negative throughput.
            return 0.0
        return round(delta / dt, 2)

    async def _read_sample(self, redis, host_id: str) -> dict | None:
        raw = await redis.get(RedisKeys.host_pulse_sample(host_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    async def _write_sample(
        self, redis, host_id: str, *, t: float, counter: float, engine: str
    ) -> None:
        await redis.set(
            RedisKeys.host_pulse_sample(host_id),
            json.dumps({"t": t, "counter": counter, "engine": engine}),
            ex=max(self._interval * 10, 60),
        )

    async def _push_point(self, redis, host_id: str, *, t: float, tps: float) -> None:
        key = RedisKeys.host_pulse(host_id)
        await redis.rpush(key, json.dumps({"t": t, "tps": tps}))
        await redis.ltrim(key, -RING_MAX_POINTS, -1)

    async def _write_meta(
        self,
        redis,
        host_id: str,
        *,
        available: bool,
        last_ok: float | None = None,
        engine: str | None = None,
    ) -> None:
        key = RedisKeys.host_pulse_meta(host_id)
        raw = await redis.get(key)
        try:
            doc: dict = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            doc = {}
        doc["available"] = available
        if last_ok is not None:
            doc["last_ok"] = last_ok
        if engine is not None:
            doc["engine"] = engine
        else:
            doc.setdefault("engine", None)
        doc.setdefault("last_ok", None)
        await redis.set(key, json.dumps(doc))

    async def _read_live_reachable(self, redis, slug: str) -> bool:
        """Mirrors RuntimeWatcher._read_live_reachable — same cache, read-only."""
        try:
            raw = await redis.get(RedisKeys.runtime_live(slug))
        except Exception:  # noqa: BLE001
            return False
        if not raw:
            return False
        try:
            doc = json.loads(raw)
        except (TypeError, ValueError):
            return False
        return bool(doc.get("reachable"))

    async def _log_throttled(self, redis, host_id: str, exc: object) -> None:
        try:
            got_slot = await redis.set(
                f"mc:host:{host_id}:pulse:err-logged", "1",
                nx=True, ex=_ERROR_LOG_TTL,
            )
        except Exception:  # noqa: BLE001
            got_slot = True
        if got_slot:
            logger.warning("runtime pulse: host %s scrape failed: %s", host_id, exc)
        else:
            logger.debug("runtime pulse: host %s scrape failed (throttled): %s", host_id, exc)


runtime_pulse = RuntimePulse()
