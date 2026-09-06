"""Telemetrie-Verlauf 1h je Host (Bühne v2 §6/§7 PR 3).

Only RFC 5737 placeholder IPs (192.0.2.x) — public repo, no real addresses.
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.redis_client import RedisKeys
from app.services import host_metrics_history as hmh

# nvidia-smi + free -m response in _SPARK_METRICS_CMD format (same fixture
# shape as test_hosts_api._SSH_METRICS_STDOUT)
_SSH_METRICS_STDOUT = (
    "35, 8806, 131072, 61\n"
    "---\n"
    "              total        used        free\n"
    "Mem:          119181       15230       90000\n"
    "Swap:              0           0           0"
)


def _ssh_host_body(slug: str = "gpu-box-hist", **overrides) -> dict:
    body = {
        "slug": slug,
        "display_name": "GPU Box",
        "kind": "ssh",
        "ssh_host": "192.0.2.11",
        "ssh_user": "mcuser",
        "ssh_key_path": "/home/mcuser/.ssh/id_rsa",
        "notes": "Testbox",
        "ui_order": 1,
    }
    body.update(overrides)
    return body


# ── Endpoint: metrics call feeds the ring ────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_call_writes_history_point(auth_client):
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body())).json()
    with patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(return_value=(_SSH_METRICS_STDOUT, "", 0)),
    ):
        resp = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")
    assert resp.status_code == 200, resp.text

    hist = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics/history")
    assert hist.status_code == 200, hist.text
    data = hist.json()
    assert data["window"] == hmh.HISTORY_WINDOW_SECONDS
    assert data["sample_seconds"] == hmh.HISTORY_DEDUPE_SECONDS
    assert len(data["points"]) == 1
    point = data["points"][0]
    assert point["gpu"] == 35
    assert point["ram_used"] == 15230
    assert point["ram_total"] == 119181
    assert point["temp"] == 61
    assert point["fan"] is None
    assert isinstance(point["t"], (int, float))


@pytest.mark.asyncio
async def test_metrics_dedupe_within_4s(auth_client):
    """Two /metrics calls milliseconds apart (well under
    HISTORY_DEDUPE_SECONDS) must produce exactly one history point."""
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body("gpu-box-dedupe"))).json()
    with patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(return_value=(_SSH_METRICS_STDOUT, "", 0)),
    ):
        r1 = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")
        r2 = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")
    assert r1.status_code == 200 and r2.status_code == 200

    hist = (await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics/history")).json()
    assert len(hist["points"]) == 1


@pytest.mark.asyncio
async def test_unreachable_metrics_not_recorded(auth_client):
    """SSH failure (reachable=False) must not write a history point —
    only successful GPU-bearing reads are worth keeping."""
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body("gpu-box-unreachable"))).json()
    with patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(side_effect=OSError("connect failed")),
    ):
        resp = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")
    assert resp.status_code == 200
    assert resp.json()["reachable"] is False

    hist = (await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics/history")).json()
    assert hist["points"] == []


@pytest.mark.asyncio
async def test_redis_write_failure_does_not_break_metrics_endpoint(auth_client):
    """Review-Fund rev-437: record_metrics_point_safe schluckt Fehler beim
    Ring-Schreiben (Redis down, Serializer kaputt, egal was) — der
    5s-Metrics-Poll der ganzen Bühne darf davon nichts merken, die Metriken
    kommen unverändert durch."""
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body("gpu-box-redis-down"))).json()
    with (
        patch(
            "app.services.runtime_manager._ssh_run",
            new=AsyncMock(return_value=(_SSH_METRICS_STDOUT, "", 0)),
        ),
        patch(
            "app.services.host_metrics_history.record_metrics_point",
            new=AsyncMock(side_effect=ConnectionError("redis unreachable")),
        ),
    ):
        resp = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reachable"] is True
    assert data["gpu_util_pct"] == 35


@pytest.mark.asyncio
async def test_get_redis_failure_before_write_does_not_break_metrics_endpoint(auth_client):
    """Auch wenn schon get_redis() selbst wirft (bevor record_metrics_point
    überhaupt aufgerufen wird), muss der Metrics-Poll durchgehen — der
    Router umschliesst den ganzen Ring-Schreibversuch, nicht nur den Aufruf
    innerhalb des Service."""
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body("gpu-box-get-redis-fails"))).json()
    with (
        patch(
            "app.services.runtime_manager._ssh_run",
            new=AsyncMock(return_value=(_SSH_METRICS_STDOUT, "", 0)),
        ),
        patch(
            "app.routers.hosts.get_redis",
            new=AsyncMock(side_effect=RuntimeError("no redis connection")),
        ),
    ):
        resp = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")
    assert resp.status_code == 200, resp.text
    assert resp.json()["reachable"] is True


@pytest.mark.asyncio
async def test_redis_read_failure_returns_empty_points_not_5xx(auth_client):
    """Review-Fund rev-437: ein Redis-Fehler beim LESEN des Rings (nicht nur
    beim Schreiben) darf GET /metrics/history nie in einen 5xx umwandeln —
    read_history_safe schluckt den Fehler, der Endpoint antwortet mit
    points: [] und HTTP 200."""
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body("gpu-box-read-fails"))).json()
    with patch(
        "app.services.host_metrics_history.read_history",
        new=AsyncMock(side_effect=ConnectionError("redis unreachable")),
    ):
        resp = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics/history")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "points": [],
        "window": hmh.HISTORY_WINDOW_SECONDS,
        "sample_seconds": hmh.HISTORY_DEDUPE_SECONDS,
    }


@pytest.mark.asyncio
async def test_get_redis_failure_before_read_does_not_break_history_endpoint(auth_client):
    """Wie oben, aber der Fehler passiert schon beim get_redis()-Aufruf
    selbst, bevor read_history_safe überhaupt läuft — auch das darf kein
    5xx auslösen (der Router umschliesst den ganzen Lesezugriff)."""
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body("gpu-box-get-redis-read-fails"))).json()
    with patch(
        "app.routers.hosts.get_redis",
        new=AsyncMock(side_effect=RuntimeError("no redis connection")),
    ):
        resp = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics/history")
    assert resp.status_code == 200, resp.text
    assert resp.json()["points"] == []


@pytest.mark.asyncio
async def test_history_empty_for_fresh_host(auth_client):
    """No /metrics call yet → 200 with empty points, never a 5xx."""
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body("gpu-box-fresh"))).json()
    resp = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics/history")
    assert resp.status_code == 200
    assert resp.json() == {
        "points": [],
        "window": hmh.HISTORY_WINDOW_SECONDS,
        "sample_seconds": hmh.HISTORY_DEDUPE_SECONDS,
    }


@pytest.mark.asyncio
async def test_history_unknown_host_404(auth_client):
    resp = await auth_client.get("/api/v1/hosts/does-not-exist/metrics/history")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_flask_wol_and_local_hosts_never_recorded(auth_client):
    """flask_wol (no GPU data, only awake/asleep) and local (no metrics at
    all) must never grow a history ring — record_metrics_point is gated on
    kind in (ssh, agent) in the router."""
    created = (
        await auth_client.post(
            "/api/v1/hosts",
            json={
                "slug": "porsche-hist-test",
                "display_name": "PORSCHE",
                "kind": "flask_wol",
                "control_url": "http://192.0.2.21:5555",
                "wol_mac_address": "00:00:5E:00:53:02",
                "power_managed": True,
            },
        )
    ).json()
    with patch("app.services.runtime_manager._porsche_reachable", new=AsyncMock(return_value=True)):
        await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")
    hist = (await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics/history")).json()
    assert hist["points"] == []


# ── Service-level: ring trim + window filter (fake_redis directly) ─────────


@pytest.mark.asyncio
async def test_record_metrics_point_trims_to_720(fake_redis):
    host_id = "trim-test-host"
    now = time.time()
    for i in range(hmh.HISTORY_MAX_POINTS + 30):
        point = {"t": now - (hmh.HISTORY_MAX_POINTS + 30 - i) * 5, "gpu": i % 100}
        await fake_redis.rpush(RedisKeys.host_metrics_history(host_id), json.dumps(point))
    await fake_redis.ltrim(
        RedisKeys.host_metrics_history(host_id), -hmh.HISTORY_MAX_POINTS, -1
    )

    raw = await fake_redis.lrange(RedisKeys.host_metrics_history(host_id), 0, -1)
    assert len(raw) == hmh.HISTORY_MAX_POINTS
    # oldest points were dropped — the ring keeps the newest ones
    first_kept = json.loads(raw[0])
    assert first_kept["gpu"] == 30 % 100


@pytest.mark.asyncio
async def test_read_history_filters_by_window(fake_redis):
    host_id = "window-test-host"
    now = time.time()
    old_point = {"t": now - 7200, "gpu": 10, "ram_used": 1, "ram_total": 2, "temp": 3, "fan": None}
    recent_point = {"t": now - 30, "gpu": 20, "ram_used": 1, "ram_total": 2, "temp": 3, "fan": None}
    key = RedisKeys.host_metrics_history(host_id)
    await fake_redis.rpush(key, json.dumps(old_point))
    await fake_redis.rpush(key, json.dumps(recent_point))

    points = await hmh.read_history(fake_redis, host_id, window_seconds=3600)
    assert len(points) == 1
    assert points[0]["gpu"] == 20


@pytest.mark.asyncio
async def test_record_metrics_point_dedupe_service_level(fake_redis):
    host_id = "dedupe-service-host"
    metrics = {"reachable": True, "gpu_util_pct": 1, "ram_used_mb": 1, "ram_total_mb": 2, "gpu_temp_c": 3}
    wrote_first = await hmh.record_metrics_point(fake_redis, host_id, metrics)
    wrote_second = await hmh.record_metrics_point(fake_redis, host_id, metrics)
    assert wrote_first is True
    assert wrote_second is False
    points = await hmh.read_history(fake_redis, host_id)
    assert len(points) == 1


@pytest.mark.asyncio
async def test_record_metrics_point_concurrent_calls_write_exactly_once(fake_redis):
    """Review-Fund rev-437: Dedupe muss atomar sein. Die alte Version las den
    letzten Zeitstempel und schrieb dann erst — zwei gleichzeitige Aufrufe
    (mehrere offene Tabs pollen denselben Host) konnten beide den Read VOR
    dem jeweils anderen Write sehen und beide schreiben. Mit dem
    ``SET NX EX``-Marker gewinnt garantiert genau einer, egal wie die beiden
    Coroutinen interleaven (asyncio.gather zwingt hier keine bestimmte
    Reihenfolge, das ist der Punkt)."""
    host_id = "concurrent-dedupe-host"
    metrics = {"reachable": True, "gpu_util_pct": 42, "ram_used_mb": 1, "ram_total_mb": 2, "gpu_temp_c": 3}

    results = await asyncio.gather(
        hmh.record_metrics_point(fake_redis, host_id, metrics),
        hmh.record_metrics_point(fake_redis, host_id, metrics),
    )

    assert sorted(results) == [False, True]
    points = await hmh.read_history(fake_redis, host_id)
    assert len(points) == 1


@pytest.mark.asyncio
async def test_metrics_to_history_point_serializes_missing_fields():
    point = hmh.metrics_to_history_point({"reachable": True}, t=123.0)
    assert point == {"t": 123.0, "gpu": None, "ram_used": None, "ram_total": None, "temp": None, "fan": None}
