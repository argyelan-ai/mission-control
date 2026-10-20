"""Runtime Pulse (Runtimes-Buehne v2, PR 1 — docs/specs/runtimes-buehne-v2.md §6/§7).

Covers: Prometheus metric parsing (vLLM counter, SGLang counter fallback,
SGLang gauge fallback, TensorFold's GLM health counter, and its native
server — live scrape 20.10.2026 — whose speed is the engine's own rate from
GET /health), tok/s computation across probes (delta for counters, a
restart costs one 0 point; direct value for the sglang:gen_throughput gauge
— confirmed live against a running SGLang box, 06.09.2026; the native
build without a spike or dip on either handover of a finished reply, and
without a guess when /health has no rate), ring trim to 180 points, the
GET /hosts/{id}/pulse endpoint (empty + populated), and that unreachable
hosts are skipped without ever probing them.
"""
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.runtime import Runtime
from app.redis_client import RedisKeys
from app.services.runtime_pulse import RuntimePulse, advance_rate, parse_token_counter
from tests.conftest import test_engine

_VLLM_METRICS = """
# HELP vllm:generation_tokens_total Number of generation tokens processed.
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{model_name="qwen38-27b"} 12345.0
# HELP vllm:num_requests_running Running requests.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="qwen38-27b"} 2.0
"""

_VLLM_MULTI_LABEL_METRICS = """
vllm:generation_tokens_total{model_name="a",finished_reason="stop"} 100.0
vllm:generation_tokens_total{model_name="a",finished_reason="length"} 50.0
"""

_SGLANG_COUNTER_METRICS = """
# HELP sglang:generation_tokens_total total
# TYPE sglang:generation_tokens_total counter
sglang:generation_tokens_total{name="x"} 500
"""

_SGLANG_GAUGE_METRICS = """
# HELP sglang:gen_throughput Generation throughput (token/s).
# TYPE sglang:gen_throughput gauge
sglang:gen_throughput 87.3
"""

_NO_KNOWN_METRICS = """
# HELP some_other_metric something else
some_other_metric 42
"""


async def _mk_host(session, *, slug="gpu-box") -> Host:
    host = Host(id=uuid.uuid4(), slug=slug, display_name=slug, kind="ssh", ssh_host="192.0.2.10")
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


async def _mk_slot_runtime(session, host: Host, *, slug="head-slot") -> Runtime:
    rt = Runtime(
        slug=slug, display_name=slug, runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1", enabled=True,
        is_slot=True, host_id=host.id,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis
    return _get


async def _mark_reachable(fake_redis, slug: str, reachable: bool = True) -> None:
    await fake_redis.set(
        RedisKeys.runtime_live(slug), json.dumps({"reachable": reachable})
    )


# ── Prometheus parsing ────────────────────────────────────────────────────


def test_parse_vllm_counter():
    result = parse_token_counter(_VLLM_METRICS)
    assert result[:3] == ("vllm", 12345.0, "counter")


def test_parse_sums_multiple_label_series():
    result = parse_token_counter(_VLLM_MULTI_LABEL_METRICS)
    assert result[:3] == ("vllm", 150.0, "counter")


def test_parse_sglang_counter_fallback_when_no_vllm():
    result = parse_token_counter(_SGLANG_COUNTER_METRICS)
    assert result[:3] == ("sglang", 500.0, "counter")


def test_parse_sglang_gauge_fallback_is_a_rate():
    """sglang:gen_throughput is a gauge (token/s), not a counter — verified
    live against a running SGLang box (06.09.2026). Only used when the
    counter metric is absent (priority order in _METRIC_CANDIDATES)."""
    result = parse_token_counter(_SGLANG_GAUGE_METRICS)
    assert result[:3] == ("sglang", 87.3, "rate")


_TENSORFOLD_METRICS = """\
# HELP tensorfold:requests_running Requests in prefill or decode.
# TYPE tensorfold:requests_running gauge
tensorfold:requests_running 0
tensorfold:prompt_tokens_total 68015
tensorfold:generation_tokens_total 4392
tensorfold_health:completion_tokens_total 44
"""


def test_parse_tensorfold_counter():
    """TensorFold: speed comes from ``tensorfold_health:completion_tokens_total``,
    which grows while a reply streams. ``tensorfold:generation_tokens_total``
    only moves when a reply finishes (live, 01.10.2026) — reading it showed
    0 tok/s during work and spikes afterwards."""
    result = parse_token_counter(_TENSORFOLD_METRICS)
    # The streaming counter (running replies included), not the
    # finished-replies counter tensorfold:generation_tokens_total (4392).
    assert result[:3] == ("tensorfold", 44.0, "counter")


def test_parse_returns_none_for_unknown_metrics():
    assert parse_token_counter(_NO_KNOWN_METRICS) is None


# TensorFold's native (Zig) server, as the DeepSeek-V4.1-Flash two-Spark
# recipe runs it — a verbatim excerpt of a live scrape (20.10.2026). It has
# no tensorfold_health:* family at all; the finished-replies counter and the
# in-flight gauge are the only generated-token figures.
_TENSORFOLD_NATIVE_METRICS = """\
# HELP tensorfold:requests_running Requests in prefill or decode.
# TYPE tensorfold:requests_running gauge
tensorfold:requests_running 0
# HELP tensorfold:requests_waiting Requests queued or held until a lane is free.
# TYPE tensorfold:requests_waiting gauge
tensorfold:requests_waiting 0
# HELP tensorfold:generation_tokens_running Generated tokens held by live streams.
# TYPE tensorfold:generation_tokens_running gauge
tensorfold:generation_tokens_running 0
# HELP tensorfold:prompt_tokens_total Prompt tokens of finished requests.
# TYPE tensorfold:prompt_tokens_total counter
tensorfold:prompt_tokens_total 7957562
# HELP tensorfold:generation_tokens_total Generated tokens of finished requests.
# TYPE tensorfold:generation_tokens_total counter
tensorfold:generation_tokens_total 216401
# HELP tensorfold:kv_cache_usage_ratio Tokens in a stream cache divided by that stream's context window.
# TYPE tensorfold:kv_cache_usage_ratio gauge
tensorfold:kv_cache_usage_ratio{pool="0"} 0
# HELP tensorfold:mtp_drafted_total Draft tokens verified on finished requests.
# TYPE tensorfold:mtp_drafted_total counter
tensorfold:mtp_drafted_total 223506
"""


def _native(total: int, running: int) -> str:
    """The native excerpt with other token figures (one request in flight)."""
    return (
        _TENSORFOLD_NATIVE_METRICS
        .replace("tensorfold:generation_tokens_total 216401", f"tensorfold:generation_tokens_total {total}")
        .replace("tensorfold:generation_tokens_running 0", f"tensorfold:generation_tokens_running {running}")
        .replace("tensorfold:requests_running 0", f"tensorfold:requests_running {1 if running else 0}")
    )


def test_parse_tensorfold_native_build_takes_the_engine_rate():
    """Native build (no tensorfold_health:*): its /metrics has no figure
    whose delta is the speed, so the reading only names the build — the
    poller then asks the engine for its own rate (GET /health)."""
    result = parse_token_counter(_native(216401, 5904))
    assert result is not None
    assert (result.engine, result.kind) == ("tensorfold", "engine_rate")
    assert result.source == "tensorfold:generation_tokens_running"


def test_parse_tensorfold_native_idle_scrape_is_available():
    """The verbatim idle scrape is a reading too — before the fix it was
    None, so the card said 'no data' while the box was idle."""
    result = parse_token_counter(_TENSORFOLD_NATIVE_METRICS)
    assert result is not None
    assert (result.engine, result.kind) == ("tensorfold", "engine_rate")


def test_parse_tensorfold_finished_counter_alone_is_not_a_speed_source():
    """Without the native server's in-flight gauge the finished-replies
    counter is all there is, and it only jumps when a reply ends (0 tok/s
    while working, then a spike) — no reading beats a wrong one."""
    only_finished = "\n".join(
        line for line in _TENSORFOLD_NATIVE_METRICS.splitlines()
        if "generation_tokens_running" not in line
    )
    assert parse_token_counter(only_finished) is None


def test_parse_tensorfold_health_counter_keeps_priority_over_the_native_gauge():
    """A build that has both (the GLM-5.3 recipe's health family next to the
    native names) keeps its streaming health counter — no /health call."""
    both = _TENSORFOLD_METRICS + "tensorfold:generation_tokens_running 120\n"
    result = parse_token_counter(both)
    assert result[:3] == ("tensorfold", 44.0, "counter")
    assert result.source == "tensorfold_health:completion_tokens_total"


# ── tok/s delta computation (advance_rate) ────────────────────────────────

_SRC = "vllm:generation_tokens_total"


def _run(readings, *, source=_SRC, step=5.0, prev=None):
    """Feed readings 5 s apart through advance_rate like the poller does."""
    ring: list[float] = []
    t = 1000.0
    for counter in readings:
        tps, prev = advance_rate(prev, source=source, counter=float(counter), now=t)
        ring.append(tps)
        t += step
    return ring, prev


def test_first_probe_reports_zero_tps():
    tps, sample = advance_rate(None, source=_SRC, counter=100.0, now=time.time())
    assert tps == 0.0
    assert sample["counter"] == 100.0 and sample["source"] == _SRC


def test_second_probe_computes_rate():
    ring, _ = _run([100, 600])
    assert ring == [0.0, 100.0]  # (600-100)/5


def test_counter_reset_clamps_to_zero():
    ring, _ = _run([1000, 50])
    assert ring == [0.0, 0.0]


def test_engine_restart_costs_one_zero_point_then_counts_from_the_new_start():
    # 1000 → engine restart → 50 → 550: the drop reports 0 and the new count
    # is the baseline, so the next probe rates 500 tokens / 5 s.
    ring, _ = _run([500, 1000, 50, 550])
    assert ring == [0.0, 100.0, 0.0, 100.0]


def test_source_change_resets_rate_to_zero():
    _, prev = _run([100])
    tps, _ = advance_rate(prev, source="sglang:generation_tokens_total", counter=200.0, now=2000.0)
    assert tps == 0.0


def test_same_engine_other_counter_resets_rate_to_zero():
    """Two counters of one engine do not compare: a sample from the GLM
    build's health counter is no baseline for another TensorFold counter."""
    _, prev = _run([5000], source="tensorfold_health:completion_tokens_total")
    tps, _ = advance_rate(prev, source="tensorfold:some_other_counter", counter=216401.0, now=1005.0)
    assert tps == 0.0


def test_sample_written_before_this_change_reports_zero_once():
    legacy = {"t": 1000.0, "counter": 100.0, "engine": "vllm"}  # no "source" yet
    tps, sample = advance_rate(legacy, source=_SRC, counter=600.0, now=1005.0)
    assert tps == 0.0
    tps, _ = advance_rate(sample, source=_SRC, counter=1100.0, now=1010.0)
    assert tps == 100.0


# Why the native build's speed is NOT read from /metrics. A steady 40 tok/s;
# one reply (3 200 tokens) ends in the middle of a 5 s window.
_REQUEST_ENDS_MID_WINDOW = [
    # (finished_total, running)
    (216401, 2800),
    (216401, 3000),
    (216401 + 3200, 0),
    (216401 + 3200, 200),
    (216401 + 3200, 400),
]


def test_finished_counter_alone_would_have_jumped():
    """The finished counter alone reads 0 while the reply streams and
    640 tok/s the moment it ends."""
    ring, _ = _run([total for total, _ in _REQUEST_ENDS_MID_WINDOW])
    assert ring == [0.0, 0.0, 640.0, 0.0, 0.0]


def test_finished_plus_running_jumps_on_either_handover():
    """finished + running is steady only when both figures change at the
    same moment, and in the engine they do not (see the _OVERLAP_ / _GAP_
    scenarios below): one order dips and spikes, the other spikes and dips —
    the spike as big as the reply over the probe interval."""
    overlap, _ = _run([t + r for t, r, _ in _OVERLAP_AFTER_A_FINISHED_REPLY])
    gap, _ = _run([t + r for t, r, _ in _GAP_BEFORE_A_FINISHED_REPLY])
    assert overlap == [0.0, 40.0, 40.0, 1180.0, 0.0, 40.0]
    assert gap == [0.0, 40.0, 0.0, 680.0, 40.0]


# ── Ring trim ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ring_trims_to_180_points(fake_redis):
    poller = RuntimePulse(interval=5)
    for i in range(200):
        await poller._push_point(fake_redis, "host-1", t=float(i), tps=1.0)
    raw = await fake_redis.lrange(RedisKeys.host_pulse("host-1"), 0, -1)
    assert len(raw) == 180
    points = [json.loads(p) for p in raw]
    # oldest 20 dropped — first surviving point is t=20
    assert points[0]["t"] == 20.0
    assert points[-1]["t"] == 199.0


# ── Poller skips unreachable hosts ────────────────────────────────────────


@pytest.mark.asyncio
async def test_poller_skips_unreachable_host_no_probe(async_session, fake_redis):
    host = await _mk_host(async_session, slug="dead-box")
    rt = await _mk_slot_runtime(async_session, host, slug="dead-slot")
    await _mark_reachable(fake_redis, rt.slug, reachable=False)

    poller = RuntimePulse(interval=5)
    with patch("app.services.runtime_pulse.get_redis", _fake_get_redis(fake_redis)), \
         patch("httpx.AsyncClient") as mock_client_cls:
        await poller.tick(session=async_session)
        mock_client_cls.assert_not_called()

    meta_raw = await fake_redis.get(RedisKeys.host_pulse_meta(str(host.id)))
    meta = json.loads(meta_raw)
    assert meta["available"] is False


@pytest.mark.asyncio
async def test_poller_polls_reachable_host_and_fills_ring(async_session, fake_redis):
    host = await _mk_host(async_session, slug="live-box")
    rt = await _mk_slot_runtime(async_session, host, slug="live-slot")
    await _mark_reachable(fake_redis, rt.slug, reachable=True)

    mock_response = MagicMock(status_code=200, text=_VLLM_METRICS)
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    poller = RuntimePulse(interval=5)
    with patch("app.services.runtime_pulse.get_redis", _fake_get_redis(fake_redis)), \
         patch("httpx.AsyncClient", return_value=mock_client):
        await poller.tick(session=async_session)

    mock_client.get.assert_awaited_once_with("http://192.0.2.10:8000/metrics")
    raw = await fake_redis.lrange(RedisKeys.host_pulse(str(host.id)), 0, -1)
    assert len(raw) == 1
    point = json.loads(raw[0])
    assert point["tps"] == 0.0  # first sample, no prior counter to diff

    meta = json.loads(await fake_redis.get(RedisKeys.host_pulse_meta(str(host.id))))
    assert meta["available"] is True
    assert meta["engine"] == "vllm"


@pytest.mark.asyncio
async def test_poller_sglang_gauge_used_directly_as_tps_no_delta(async_session, fake_redis):
    """sglang:gen_throughput is a gauge already in tok/s — the FIRST probe
    must already report the real value, unlike a counter (which needs a
    second sample). No counter-sample bookkeeping key is written either."""
    host = await _mk_host(async_session, slug="sglang-box")
    rt = await _mk_slot_runtime(async_session, host, slug="sglang-slot")
    await _mark_reachable(fake_redis, rt.slug, reachable=True)

    mock_response = MagicMock(status_code=200, text=_SGLANG_GAUGE_METRICS)
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    poller = RuntimePulse(interval=5)
    with patch("app.services.runtime_pulse.get_redis", _fake_get_redis(fake_redis)), \
         patch("httpx.AsyncClient", return_value=mock_client):
        await poller.tick(session=async_session)

    raw = await fake_redis.lrange(RedisKeys.host_pulse(str(host.id)), 0, -1)
    assert len(raw) == 1
    point = json.loads(raw[0])
    assert point["tps"] == 87.3  # gauge value used as-is, even on the first probe

    meta = json.loads(await fake_redis.get(RedisKeys.host_pulse_meta(str(host.id))))
    assert meta["available"] is True
    assert meta["engine"] == "sglang"

    # No counter-delta sample was written for a rate metric.
    sample = await fake_redis.get(RedisKeys.host_pulse_sample(str(host.id)))
    assert sample is None


def _health(rate: float | None) -> dict:
    """GET /health of the native server. ``live.decode_tokens_per_second`` is
    the engine's own rate (tokens delivered in its last 2 s / 2, counted in
    lane_host.zig noteDecoded). ``None`` = the body a server with API keys
    on sends: no ``live`` block at all."""
    if rate is None:
        return {"status": "ok"}
    return {
        "status": "ok", "model": "m", "model_ids": ["m"], "max_batch_size": 4, "warming": False,
        "memory": {}, "live": {"connections": 1, "waiting": 0,
                               "decode_tokens_per_second": rate, "prefill_tokens_per_second": 0.0},
    }


def _engine_client(scrapes):
    """An httpx.AsyncClient stand-in for one box: per tick, the /metrics text
    and the /health body (``scrapes`` = [(metrics_text, health_body)])."""
    metrics = iter([text for text, _ in scrapes])
    health = iter([body for _, body in scrapes])

    async def _get(url, **_):
        if url.endswith("/metrics"):
            return MagicMock(status_code=200, text=next(metrics))
        if url.endswith("/health"):
            body = next(health)
            resp = MagicMock(status_code=200, text=json.dumps(body))
            resp.json = MagicMock(return_value=body)
            return resp
        raise AssertionError(f"unexpected probe {url}")

    client = AsyncMock()
    client.get = AsyncMock(side_effect=_get)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def _ticks(poller, session, fake_redis, scrapes, *, t0=1_000_000.0, step=5.0, ledger=None):
    """One poller tick per scrape, ``step`` seconds apart on the poller's own
    clock (only the module's ``time`` is replaced). Returns the client."""
    client = _engine_client(scrapes)
    clock = SimpleNamespace(now=t0)
    with patch("app.services.runtime_pulse.get_redis", _fake_get_redis(fake_redis)), \
         patch("app.services.runtime_pulse.token_ledger", ledger or MagicMock(observe=AsyncMock())), \
         patch("app.services.runtime_pulse.time", SimpleNamespace(time=lambda: clock.now)), \
         patch("httpx.AsyncClient", return_value=client):
        for _ in scrapes:
            await poller.tick(session=session)
            clock.now += step
    return client


async def _ring(fake_redis, host) -> list[float]:
    raw = await fake_redis.lrange(RedisKeys.host_pulse(str(host.id)), 0, -1)
    return [json.loads(p)["tps"] for p in raw]


@pytest.mark.asyncio
async def test_poller_tensorfold_native_build_shows_speed(async_session, fake_redis):
    """The live bug (20.10.2026): a box on TensorFold's native server stayed
    'available: false' — the strip empty, the speed tile '–'. The engine's
    own rate is real from the first probe on (no earlier sample needed)."""
    host = await _mk_host(async_session, slug="duo-head")
    rt = await _mk_slot_runtime(async_session, host, slug="duo-slot")
    await _mark_reachable(fake_redis, rt.slug, reachable=True)

    client = await _ticks(RuntimePulse(interval=5), async_session, fake_redis, [
        (_native(216401, 2800), _health(40.0)),
        (_native(216401, 3000), _health(41.5)),
    ])

    meta = json.loads(await fake_redis.get(RedisKeys.host_pulse_meta(str(host.id))))
    assert meta["available"] is True
    assert meta["engine"] == "tensorfold"
    assert await _ring(fake_redis, host) == [40.0, 41.5]
    urls = [call.args[0] for call in client.get.await_args_list]
    assert urls == ["http://192.0.2.10:8000/metrics", "http://192.0.2.10:8000/health"] * 2


# The two handovers of one finished reply (N = 5900 tokens) between the
# native server's figures, both seen in the engine source (TensorFold
# lane_host.zig run loop + server/chat.zig): the HTTP thread adds the reply
# to the finished counter when it leaves; the in-flight gauge is only
# recomputed by noteLive(), which runs AFTER admitOne() has prefilled the
# next queued request. A box with all 4 lanes busy and one request waiting:
_OVERLAP_AFTER_A_FINISHED_REPLY = [
    # (finished_total, running, engine decode tok/s on /health)
    (216401, 5500, 40.0),
    (216401, 5700, 40.0),
    (216401, 5900, 40.0),
    (216401 + 5900, 5900, 0.0),  # counted twice: finished, and still "running" while the next prompt prefills
    (216401 + 5900, 80, 40.0),  # gauge recomputed; the next reply streams
    (216401 + 5900, 280, 40.0),
]
# ...and the other order: the gauge recomputed before the HTTP thread has
# added the reply to the finished counter.
_GAP_BEFORE_A_FINISHED_REPLY = [
    (216401, 2800, 40.0),
    (216401, 3000, 40.0),
    (216401, 0, 40.0),  # the reply left the gauge, not yet in the finished counter
    (216401 + 3200, 200, 40.0),
    (216401 + 3200, 400, 40.0),
]


@pytest.mark.asyncio
async def test_poller_native_build_overlap_after_a_finished_reply_is_no_spike(async_session, fake_redis):
    """Critic finding (PR #909): with finished + running the overlap gave a
    1 180 tok/s point and a 0 after it, and the one bright cell dimmed every
    other cell of the strip for 15 minutes (HeatStrip ceiling = max cell).
    The engine's own decode rate has no handover in it."""
    host = await _mk_host(async_session, slug="duo-head")
    rt = await _mk_slot_runtime(async_session, host, slug="duo-slot")
    await _mark_reachable(fake_redis, rt.slug, reachable=True)

    await _ticks(RuntimePulse(interval=5), async_session, fake_redis, [
        (_native(total, running), _health(rate)) for total, running, rate in _OVERLAP_AFTER_A_FINISHED_REPLY
    ])

    ring = await _ring(fake_redis, host)
    assert ring == [40.0, 40.0, 40.0, 0.0, 40.0, 40.0]
    assert max(ring) < 100.0


@pytest.mark.asyncio
async def test_poller_native_build_gap_before_a_finished_reply_is_no_dip(async_session, fake_redis):
    host = await _mk_host(async_session, slug="duo-head")
    rt = await _mk_slot_runtime(async_session, host, slug="duo-slot")
    await _mark_reachable(fake_redis, rt.slug, reachable=True)

    await _ticks(RuntimePulse(interval=5), async_session, fake_redis, [
        (_native(total, running), _health(rate)) for total, running, rate in _GAP_BEFORE_A_FINISHED_REPLY
    ])

    assert await _ring(fake_redis, host) == [40.0] * 5


@pytest.mark.asyncio
async def test_poller_native_build_without_a_live_rate_shows_no_speed_but_books_tokens(async_session, fake_redis):
    """With API keys on, the native server's /health says only
    {"status": "ok"}. Then there is no honest speed: no point, 'available:
    false' — never the finished + running guess. The box tokens (/metrics,
    open with keys on too) are still booked."""
    host = await _mk_host(async_session, slug="duo-head")
    rt = await _mk_slot_runtime(async_session, host, slug="duo-slot")
    await _mark_reachable(fake_redis, rt.slug, reachable=True)
    ledger = MagicMock(observe=AsyncMock())

    await _ticks(RuntimePulse(interval=5), async_session, fake_redis, [
        (_native(216401, 2800), _health(None)),
        (_native(216401, 3000), _health(None)),
    ], ledger=ledger)

    meta = json.loads(await fake_redis.get(RedisKeys.host_pulse_meta(str(host.id))))
    assert meta["available"] is False
    assert await _ring(fake_redis, host) == []
    assert ledger.observe.await_count == 2


@pytest.mark.asyncio
async def test_poller_native_build_health_error_shows_no_speed_but_books_tokens(async_session, fake_redis):
    host = await _mk_host(async_session, slug="duo-head")
    rt = await _mk_slot_runtime(async_session, host, slug="duo-slot")
    await _mark_reachable(fake_redis, rt.slug, reachable=True)
    ledger = MagicMock(observe=AsyncMock())

    async def _get(url, **_):
        if url.endswith("/metrics"):
            return MagicMock(status_code=200, text=_native(216401, 2800))
        raise TimeoutError("health timed out")

    client = AsyncMock()
    client.get = AsyncMock(side_effect=_get)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch("app.services.runtime_pulse.get_redis", _fake_get_redis(fake_redis)), \
         patch("app.services.runtime_pulse.token_ledger", ledger), \
         patch("httpx.AsyncClient", return_value=client):
        await RuntimePulse(interval=5).tick(session=async_session)  # must not raise

    meta = json.loads(await fake_redis.get(RedisKeys.host_pulse_meta(str(host.id))))
    assert meta["available"] is False
    assert await _ring(fake_redis, host) == []
    ledger.observe.assert_awaited_once()


@pytest.mark.parametrize("bad", [-3.0, "40", True, float("nan"), float("inf"), None])
def test_engine_rate_reader_rejects_what_is_not_a_rate(bad):
    from app.services.runtime_pulse import engine_rate_from_health

    assert engine_rate_from_health({"live": {"decode_tokens_per_second": bad}}) is None
    assert engine_rate_from_health({"live": "x"}) is None
    assert engine_rate_from_health(["live"]) is None
    assert engine_rate_from_health({"live": {"decode_tokens_per_second": 0}}) == 0.0
    assert engine_rate_from_health({"live": {"decode_tokens_per_second": 162.4}}) == 162.4


@pytest.mark.asyncio
async def test_poller_scrape_error_sets_unavailable_without_raising(async_session, fake_redis):
    host = await _mk_host(async_session, slug="flaky-box")
    rt = await _mk_slot_runtime(async_session, host, slug="flaky-slot")
    await _mark_reachable(fake_redis, rt.slug, reachable=True)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=TimeoutError("scrape timed out"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    poller = RuntimePulse(interval=5)
    with patch("app.services.runtime_pulse.get_redis", _fake_get_redis(fake_redis)), \
         patch("httpx.AsyncClient", return_value=mock_client):
        await poller.tick(session=async_session)  # must not raise

    meta = json.loads(await fake_redis.get(RedisKeys.host_pulse_meta(str(host.id))))
    assert meta["available"] is False


# ── GET /hosts/{id}/pulse endpoint ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_pulse_endpoint_empty_without_data(auth_client, async_session):
    host = await _mk_host(async_session, slug="fresh-box")
    resp = await auth_client.get(f"/api/v1/hosts/{host.id}/pulse")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data == {
        "available": False, "engine": None, "now_tps": 0.0,
        "idle_seconds": None, "points": [],
    }


@pytest.mark.asyncio
async def test_pulse_endpoint_unknown_host_404(auth_client):
    resp = await auth_client.get(f"/api/v1/hosts/{uuid.uuid4()}/pulse")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pulse_endpoint_returns_points_and_idle_seconds(auth_client, async_session, fake_redis):
    host = await _mk_host(async_session, slug="busy-box")
    now = time.time()
    points = [
        {"t": now - 120, "tps": 30.0},
        {"t": now - 60, "tps": 25.0},
        {"t": now - 30, "tps": 0.0},  # went idle 30s ago
    ]
    for p in points:
        await fake_redis.rpush(RedisKeys.host_pulse(str(host.id)), json.dumps(p))
    await fake_redis.set(
        RedisKeys.host_pulse_meta(str(host.id)),
        json.dumps({"available": True, "last_ok": now, "engine": "vllm"}),
    )

    resp = await auth_client.get(f"/api/v1/hosts/{host.id}/pulse")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["available"] is True
    assert data["engine"] == "vllm"
    assert data["now_tps"] == 0.0
    assert len(data["points"]) == 3
    # last point above 0.5 tps (the t=now-60 sample) was ~60s ago
    assert data["idle_seconds"] is not None
    assert 58 <= data["idle_seconds"] <= 62
