"""Phase 16 / ADR-028 follow-up — runtime mutation endpoints are DB-aware.

Regression guard for the 2026-05-15 incident where
``POST /api/v1/runtimes/{uuid}/start`` returned 404 because the endpoints
still called ``runtime_manager.get_runtime()`` (a JSON-only lookup) while
the frontend already sent DB UUIDs.

Covered endpoints:
    GET  /api/v1/runtimes/{id}/health
    POST /api/v1/runtimes/{id}/start
    POST /api/v1/runtimes/{id}/stop
    POST /api/v1/runtimes/{id}/restart

Each is exercised both by slug and by UUID. Unknown IDs must 404.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.models.runtime import Runtime


async def _stub_state(*_args, **_kwargs):
    return {"state": "ready", "http_reachable": True, "container_status": None}


async def _stub_ok(*_args, **_kwargs):
    return {"ok": True, "message": "stub"}


@pytest.fixture
async def vllm_runtime(async_session):
    rt = Runtime(
        slug="endpoints-test-rt",
        display_name="Endpoints Test",
        runtime_type="vllm_docker",
        endpoint="http://localhost:9000/v1",
        container_name="mc-endpoints-test",
        ui_order=42,
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    return rt


# ── /health ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_resolves_by_slug(vllm_runtime, auth_client):
    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=_stub_state,
    ):
        resp = await auth_client.get(
            f"/api/v1/runtimes/{vllm_runtime.slug}/health"
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["runtime_id"] == vllm_runtime.slug
    assert data["state"] == "ready"


@pytest.mark.asyncio
async def test_health_resolves_by_uuid(vllm_runtime, auth_client):
    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=_stub_state,
    ):
        resp = await auth_client.get(
            f"/api/v1/runtimes/{vllm_runtime.id}/health"
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["state"] == "ready"


@pytest.mark.asyncio
async def test_health_unknown_returns_404(auth_client):
    resp = await auth_client.get("/api/v1/runtimes/no-such-runtime/health")
    assert resp.status_code == 404


# ── /start ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_resolves_by_slug(vllm_runtime, auth_client):
    with patch(
        "app.services.runtime_manager.start_runtime",
        new=AsyncMock(side_effect=_stub_ok),
    ):
        resp = await auth_client.post(
            f"/api/v1/runtimes/{vllm_runtime.slug}/start"
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_start_resolves_by_uuid(vllm_runtime, auth_client):
    """Regression: UI sends UUID, must not 404."""
    with patch(
        "app.services.runtime_manager.start_runtime",
        new=AsyncMock(side_effect=_stub_ok),
    ):
        resp = await auth_client.post(
            f"/api/v1/runtimes/{vllm_runtime.id}/start"
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_start_unknown_returns_404(auth_client):
    resp = await auth_client.post("/api/v1/runtimes/no-such-runtime/start")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_start_passes_context_length_override(vllm_runtime, auth_client):
    """Body.context_length is layered onto the dict before runtime_manager."""
    captured = {}

    async def capture(rt_dict, **_kw):  # **_kw: host= kwarg (ADR-048)
        captured.update(rt_dict)
        return {"ok": True}

    with patch(
        "app.services.runtime_manager.start_runtime",
        new=AsyncMock(side_effect=capture),
    ):
        resp = await auth_client.post(
            f"/api/v1/runtimes/{vllm_runtime.slug}/start",
            json={"context_length": 8192},
        )
    assert resp.status_code == 200, resp.text
    assert captured.get("context_length") == 8192


# ── /stop ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_resolves_by_slug(vllm_runtime, auth_client):
    with patch(
        "app.services.runtime_manager.stop_runtime",
        new=AsyncMock(side_effect=_stub_ok),
    ):
        resp = await auth_client.post(
            f"/api/v1/runtimes/{vllm_runtime.slug}/stop"
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_stop_resolves_by_uuid(vllm_runtime, auth_client):
    with patch(
        "app.services.runtime_manager.stop_runtime",
        new=AsyncMock(side_effect=_stub_ok),
    ):
        resp = await auth_client.post(
            f"/api/v1/runtimes/{vllm_runtime.id}/stop"
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_stop_unknown_returns_404(auth_client):
    resp = await auth_client.post("/api/v1/runtimes/no-such-runtime/stop")
    assert resp.status_code == 404


# ── /restart ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_resolves_by_slug(vllm_runtime, auth_client):
    with patch(
        "app.services.runtime_manager.restart_runtime",
        new=AsyncMock(side_effect=_stub_ok),
    ):
        resp = await auth_client.post(
            f"/api/v1/runtimes/{vllm_runtime.slug}/restart"
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_restart_resolves_by_uuid(vllm_runtime, auth_client):
    with patch(
        "app.services.runtime_manager.restart_runtime",
        new=AsyncMock(side_effect=_stub_ok),
    ):
        resp = await auth_client.post(
            f"/api/v1/runtimes/{vllm_runtime.id}/restart"
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_restart_unknown_returns_404(auth_client):
    resp = await auth_client.post("/api/v1/runtimes/no-such-runtime/restart")
    assert resp.status_code == 404


# ── Bubble-up of runtime_manager failures ──────────────────────────────────


@pytest.mark.asyncio
async def test_start_failure_returns_400(vllm_runtime, auth_client):
    async def fail(_rt, **_kw):  # **_kw: host= kwarg (ADR-048)
        return {"ok": False, "message": "boom"}

    with patch(
        "app.services.runtime_manager.start_runtime",
        new=AsyncMock(side_effect=fail),
    ):
        resp = await auth_client.post(
            f"/api/v1/runtimes/{vllm_runtime.slug}/start"
        )
    assert resp.status_code == 400
    assert "boom" in resp.text
