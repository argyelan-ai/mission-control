"""Runtime Pulse (Runtimes-Buehne v2, PR 1 — docs/specs/runtimes-buehne-v2.md §6/§7).

Covers: Prometheus counter parsing (vLLM + SGLang fallback), tok/s delta
computation across two probes, ring trim to 180 points, the
GET /hosts/{id}/pulse endpoint (empty + populated), and that unreachable
hosts are skipped without ever probing them.
"""
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.runtime import Runtime
from app.redis_client import RedisKeys
from app.services.runtime_pulse import RuntimePulse, parse_token_counter
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

_SGLANG_METRICS = """
# HELP sglang:generation_tokens_total total
# TYPE sglang:generation_tokens_total counter
sglang:generation_tokens_total{name="x"} 500
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
    assert result == ("vllm", 12345.0)


def test_parse_sums_multiple_label_series():
    result = parse_token_counter(_VLLM_MULTI_LABEL_METRICS)
    assert result == ("vllm", 150.0)


def test_parse_sglang_fallback_when_no_vllm():
    result = parse_token_counter(_SGLANG_METRICS)
    assert result == ("sglang", 500.0)


def test_parse_returns_none_for_unknown_metrics():
    assert parse_token_counter(_NO_KNOWN_METRICS) is None


# ── tok/s delta computation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_probe_reports_zero_tps(fake_redis):
    poller = RuntimePulse(interval=5)
    tps = await poller._compute_tps(fake_redis, "host-1", engine="vllm", counter=100.0, now=time.time())
    assert tps == 0.0


@pytest.mark.asyncio
async def test_second_probe_computes_rate(fake_redis):
    poller = RuntimePulse(interval=5)
    t0 = time.time()
    await poller._write_sample(fake_redis, "host-1", t=t0, counter=100.0, engine="vllm")
    tps = await poller._compute_tps(fake_redis, "host-1", engine="vllm", counter=600.0, now=t0 + 5.0)
    assert tps == 100.0  # (600-100)/5


@pytest.mark.asyncio
async def test_counter_reset_clamps_to_zero(fake_redis):
    poller = RuntimePulse(interval=5)
    t0 = time.time()
    await poller._write_sample(fake_redis, "host-1", t=t0, counter=1000.0, engine="vllm")
    tps = await poller._compute_tps(fake_redis, "host-1", engine="vllm", counter=50.0, now=t0 + 5.0)
    assert tps == 0.0


@pytest.mark.asyncio
async def test_engine_change_resets_rate_to_zero(fake_redis):
    poller = RuntimePulse(interval=5)
    t0 = time.time()
    await poller._write_sample(fake_redis, "host-1", t=t0, counter=100.0, engine="vllm")
    tps = await poller._compute_tps(fake_redis, "host-1", engine="sglang", counter=200.0, now=t0 + 5.0)
    assert tps == 0.0


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
