"""Runtime Pulse (Runtimes-Buehne v2, PR 1 — see docs/specs/runtimes-buehne-v2.md §6/§7).

A Bühne card's "Lebenszeichen" heat strip needs live tokens/second per host.
This poller supplies it: every ``settings.runtime_pulse_interval`` seconds it
scrapes the Prometheus ``/metrics`` endpoint of every host that carries a slot
runtime (``runtimes.is_slot=True`` with a bound ``host_id`` — the fixed box
endpoint, e.g. a Duo's Head box), reads the engine's generation-token metric
(vLLM: ``vllm:generation_tokens_total``, a cumulative counter — turns the
delta between two probes into tokens/second; TensorFold: the GLM build's
``tensorfold_health:completion_tokens_total`` or, on the native server
without that family, ``tensorfold:generation_tokens_total`` +
``tensorfold:generation_tokens_running``; SGLang fallback:
``sglang:generation_tokens_total``, also a counter, same delta math — or
``sglang:gen_throughput``, which is **not** a counter but a gauge already
reporting tokens/second, confirmed live against a running SGLang box
2026-09-06: its value is used as the tok/s reading directly, no delta).
A Duo is scraped on its Head box only: the second box runs no API, every
reply — and so every counted token — leaves through the Head.

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
  - **A negative delta reports 0** rather than negative throughput, and the
    higher count is held for one probe (``advance_rate``): an engine restart
    then rates from the new count on, while a probe that only hit
    TensorFold's handover gap (a finished reply between its two figures)
    is averaged with the next one instead of leaving a dip and a spike.
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
from datetime import datetime, timezone
from typing import NamedTuple

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis
from app.services.runtime_model_resolver import session_scope
from app.services.runtime_token_ledger import parse_box_counters, token_ledger

logger = logging.getLogger(__name__)

_STARTUP_GRACE = 20  # seconds — let DB/Redis come up first, mirrors runtime_watcher
RING_MAX_POINTS = 180
SCRAPE_TIMEOUT = 3.0  # seconds

# Metric families to look for, in priority order. Each entry is
# (engine, metric_names, kind) — kind is "counter" (cumulative, needs the
# delta-over-time math) or "rate" (already tokens/second, used as-is).
# The first entry whose metric names ALL have at least one matching series
# in the scraped text wins; every series of every name is summed (a
# multi-model deployment can expose a metric with several label sets).
#
# sglang:gen_throughput is a GAUGE, not a counter — verified against a live
# SGLang box 2026-09-06 (team lead's cross-check on rev-436): treating it as
# a counter and taking a delta would silently divide an already-a-rate value
# by the poll interval, reporting throughput far too low.
_METRIC_CANDIDATES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("vllm", ("vllm:generation_tokens_total",), "counter"),
    # TensorFold (e.g. the GLM-5.3 two-Spark recipe). Its
    # ``tensorfold:generation_tokens_total`` only grows when a reply FINISHES
    # (live 01.10.2026: frozen for 24 s while a request was decoding, then one
    # jump) — the strip showed 0 while working and 800 tok/s spikes after.
    # ``tensorfold_health:completion_tokens_total`` counts the running replies'
    # tokens as they stream, so its delta is the real speed.
    ("tensorfold", ("tensorfold_health:completion_tokens_total",), "counter"),
    # TensorFold's native (Zig) server (e.g. the DeepSeek-V4.1-Flash
    # two-Spark recipe, live 20.10.2026) has no tensorfold_health:* family.
    # It does have ``tensorfold:generation_tokens_running`` ("Generated tokens
    # held by live streams", a gauge). When a reply ends its tokens move from
    # that gauge into the finished counter, so finished + running grows
    # steadily while replies stream and its delta is the real speed. Never
    # the finished counter alone: that is the spike problem above.
    (
        "tensorfold",
        ("tensorfold:generation_tokens_total", "tensorfold:generation_tokens_running"),
        "counter",
    ),
    ("sglang", ("sglang:generation_tokens_total",), "counter"),
    ("sglang", ("sglang:gen_throughput",), "rate"),
)


class TokenReading(NamedTuple):
    """One scrape's generated-token figure.

    ``source`` names the metric(s) it came from. Two readings are only diffed
    when their sources match: one engine can have several (the GLM build's
    health counter and the native build's pair are both "tensorfold", but
    the numbers do not compare)."""

    engine: str
    value: float
    kind: str
    source: str


# One throttled warning per host per outage, not one per tick.
_ERROR_LOG_TTL = 300


def _metric_line_pattern(metric_name: str) -> re.Pattern:
    escaped = re.escape(metric_name)
    # Prometheus text format: `name{labels} value` or bare `name value`.
    # Value can be int/float/exponent; trailing timestamp (rare) is ignored.
    return re.compile(
        rf"^{escaped}(?:\{{[^}}]*\}})?\s+([0-9eE+\-.]+)\s*(?:\d+)?\s*$"
    )


def _sum_series(lines: list[str], metric_name: str) -> float | None:
    """Sum of every series of ``metric_name``; None when it has none."""
    pattern = _metric_line_pattern(metric_name)
    total = 0.0
    found = False
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        try:
            total += float(match.group(1))
        except ValueError:
            continue
        found = True
    return total if found else None


def parse_token_counter(text: str) -> TokenReading | None:
    """The first matching entry of ``_METRIC_CANDIDATES`` in ``text``.

    Returns a ``TokenReading`` — ``kind`` is ``"counter"`` (caller must diff
    against the previous sample) or ``"rate"`` (the value already IS
    tokens/second) — or ``None`` if no entry is complete (engine not
    exposing generation metrics, or the scrape returned something unrelated).
    """
    lines = [
        stripped for line in text.splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    ]
    for engine, metric_names, kind in _METRIC_CANDIDATES:
        sums = [_sum_series(lines, name) for name in metric_names]
        if all(value is not None for value in sums):
            return TokenReading(engine, float(sum(sums)), kind, "+".join(metric_names))
    return None


def advance_rate(
    prev: dict | None, *, source: str, counter: float, now: float
) -> tuple[float, dict, float | None]:
    """One counter probe → ``(tok/s, sample to store, smooth_at)``.

    ``prev`` is the stored sample of the last probe (None if there is none).

      - No previous sample, or one from another source → 0 (no honest rate
        yet). A sample written before ``source`` existed counts as another
        source: one 0 point after the update.
      - Counter went down → 0 for this probe, and the higher previous count
        is HELD for one probe. Two causes look alike here: the engine
        restarted (the counter starts over), or the probe hit a handover gap
        — TensorFold reads each /metrics family on its own, not as one
        snapshot, so a scrape can land after a finished reply left
        ``generation_tokens_running`` but before it reached
        ``generation_tokens_total``. That scrape is short by the whole
        reply (thousands of tokens); the next one is whole again.
      - Next probe at or above the held count → it was a gap: the rate is
        the average since the held probe, and ``smooth_at`` (the time of
        the 0 point) tells the caller to give that point the same rate (no
        dip, no spike). Still below → it was a restart: rate from the lower
        count on.
    """
    sample: dict = {"t": now, "counter": counter, "source": source}
    if prev is None or prev.get("source") != source:
        return 0.0, sample, None
    held = prev.get("held")
    if held is not None and counter >= held["counter"] and now > held["t"]:
        return round((counter - held["counter"]) / (now - held["t"]), 2), sample, prev["t"]
    dt = now - prev["t"]
    if dt <= 0:
        return 0.0, sample, None
    delta = counter - prev["counter"]
    if delta < 0:
        sample["held"] = {"t": prev["t"], "counter": prev["counter"]}
        return 0.0, sample, None
    return round(delta / dt, 2), sample, None


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

        engine = parsed.engine
        now = time.time()
        await self._feed_ledger(runtime, engine, resp.text, now)
        if parsed.kind == "rate":
            # Already tokens/second (e.g. sglang:gen_throughput, a gauge) —
            # use it as-is, no delta, no counter-sample bookkeeping.
            tps = round(max(parsed.value, 0.0), 2)
        else:
            prev = await self._read_sample(redis, host_id)
            tps, sample, smooth_at = advance_rate(
                prev, source=parsed.source, counter=parsed.value, now=now
            )
            await self._write_sample(redis, host_id, sample)
            if smooth_at is not None:
                await self._set_last_point_tps(redis, host_id, t=smooth_at, tps=tps)
        await self._push_point(redis, host_id, t=now, tps=tps)
        await self._write_meta(redis, host_id, available=True, last_ok=now, engine=engine)

    async def _feed_ledger(self, runtime: Runtime, engine: str, text: str, now: float) -> None:
        """Hand the same scrape to the box token ledger (Insights' box totals).
        Never lets a ledger failure touch the tok/s strip."""
        try:
            await token_ledger.observe(
                runtime.host_id,
                engine,
                parse_box_counters(text, engine),
                datetime.fromtimestamp(now, tz=timezone.utc),
            )
        except Exception:  # noqa: BLE001 — the pulse must survive the ledger
            logger.exception("token ledger booking failed for runtime %s", runtime.slug)

    @staticmethod
    def _metrics_url(endpoint: str) -> str:
        base = endpoint.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return f"{base}/metrics"

    async def _read_sample(self, redis, host_id: str) -> dict | None:
        raw = await redis.get(RedisKeys.host_pulse_sample(host_id))
        if not raw:
            return None
        try:
            sample = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return sample if isinstance(sample, dict) else None

    async def _write_sample(self, redis, host_id: str, sample: dict) -> None:
        await redis.set(
            RedisKeys.host_pulse_sample(host_id),
            json.dumps(sample),
            ex=max(self._interval * 10, 60),
        )

    async def _push_point(self, redis, host_id: str, *, t: float, tps: float) -> None:
        key = RedisKeys.host_pulse(host_id)
        await redis.rpush(key, json.dumps({"t": t, "tps": tps}))
        await redis.ltrim(key, -RING_MAX_POINTS, -1)

    async def _set_last_point_tps(self, redis, host_id: str, *, t: float, tps: float) -> None:
        """Give the ring's newest point another rate, keeping its time — the
        0 of a held drop that turned out to be a handover gap. Only if that
        point IS the one written at ``t`` (nothing else is ever rewritten)."""
        key = RedisKeys.host_pulse(host_id)
        raw = await redis.lindex(key, -1)
        if not raw:
            return
        try:
            point = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(point, dict) or point.get("t") != t:
            return
        point["tps"] = tps
        await redis.lset(key, -1, json.dumps(point))

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
