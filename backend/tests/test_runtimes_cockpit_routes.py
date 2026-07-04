"""Cockpit routes (ADR-053) — live-status, probe-endpoint, force-sync."""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis


async def _mk_rt(session, *, slug="cockpit-rt", model="row-model"):
    rt = Runtime(
        slug=slug, display_name=slug, runtime_type="vllm_docker",
        endpoint="http://spark:8000/v1", model_identifier=model, enabled=True,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


@pytest.mark.asyncio
async def test_live_status_merges_redis_and_flags_drift(async_session, auth_client):
    await _mk_rt(async_session, slug="drifty", model="row-model")
    redis = await get_redis()
    await redis.setex(
        RedisKeys.runtime_live("drifty"), 300,
        json.dumps({
            "reachable": True, "served_model": "engine-model",
            "latency_ms": 12, "last_probe_at": "2026-07-04T00:00:00+00:00",
            "consecutive_failures": 0,
        }),
    )

    resp = await auth_client.get("/api/v1/runtimes/live-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["live"]["drifty"]["drift"] is True
    assert body["live"]["drifty"]["served_model"] == "engine-model"


@pytest.mark.asyncio
async def test_probe_endpoint_returns_detection(auth_client):
    with patch(
        "app.routers.runtimes.probe_endpoint_url",
        new=AsyncMock(return_value={
            "reachable": True, "models": ["m1", "m2"],
            "detected_type": "lmstudio", "suggested_model": "m1", "error": None,
        }),
    ):
        resp = await auth_client.post(
            "/api/v1/runtimes/probe-endpoint",
            json={"url": "http://localhost:1234/v1"},
        )
    assert resp.status_code == 200
    assert resp.json()["detected_type"] == "lmstudio"


@pytest.mark.asyncio
async def test_probe_endpoint_rejects_bad_scheme(auth_client):
    resp = await auth_client.post(
        "/api/v1/runtimes/probe-endpoint", json={"url": "ftp://nope"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_force_sync_route_calls_propagation(async_session, auth_client):
    rt = await _mk_rt(async_session, slug="force-rt")
    with patch(
        "app.routers.runtimes.sync_pending_agents", new=AsyncMock()
    ) as mock_sync:
        resp = await auth_client.post(
            f"/api/v1/runtimes/db/{rt.slug}/sync-agents"
        )
    assert resp.status_code == 200
    mock_sync.assert_awaited_once()
