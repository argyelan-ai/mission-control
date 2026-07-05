"""Engine Control v0 (ADR-057): GET/POST /api/v1/runtimes/db/{slug}/autostart.

Only RFC 5737 placeholder IPs (192.0.2.x) — public repo, no real addresses.
The autostart service itself (SSH) is patched out — these tests pin routing,
validation, and the activity event, not asyncssh behavior (see
test_runtime_autostart_service.py for that).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.runtime import Runtime
from app.services.runtime_autostart import AutostartHostUnreachable, AutostartStatus
from tests.conftest import test_engine


async def _make_runtime(**overrides) -> Runtime:
    defaults = dict(
        id=uuid.uuid4(),
        slug=f"rt-{uuid.uuid4().hex[:8]}",
        display_name="Test Runtime",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.5:8000",
    )
    defaults.update(overrides)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rt = Runtime(**defaults)
        s.add(rt)
        await s.commit()
        await s.refresh(rt)
        return rt


@pytest.mark.asyncio
async def test_get_autostart_404_unknown_runtime(auth_client):
    resp = await auth_client.get("/api/v1/runtimes/db/does-not-exist/autostart")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_autostart_422_when_not_supported(auth_client):
    rt = await _make_runtime(autostart_supported=False)
    resp = await auth_client.get(f"/api/v1/runtimes/db/{rt.slug}/autostart")
    assert resp.status_code == 422
    assert "autostart_supported" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_autostart_422_when_no_flag_path(auth_client):
    rt = await _make_runtime(autostart_supported=True, autostart_flag_path=None)
    resp = await auth_client.get(f"/api/v1/runtimes/db/{rt.slug}/autostart")
    assert resp.status_code == 422
    assert "autostart_flag_path" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_autostart_status_ok(auth_client):
    rt = await _make_runtime(
        autostart_supported=True,
        autostart_flag_path="/home/mcuser/scripts/vllm-autostart.enabled",
    )
    with patch(
        "app.routers.runtimes.get_autostart_status",
        AsyncMock(return_value=AutostartStatus(enabled=True, reachable=True)),
    ):
        resp = await auth_client.get(f"/api/v1/runtimes/db/{rt.slug}/autostart")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["reachable"] is True
    assert body["flag_path"] == rt.autostart_flag_path


@pytest.mark.asyncio
async def test_post_autostart_enables_and_emits_activity_event(auth_client):
    rt = await _make_runtime(
        autostart_supported=True,
        autostart_flag_path="/home/mcuser/scripts/vllm-autostart.enabled",
    )
    with patch(
        "app.routers.runtimes.set_autostart",
        AsyncMock(return_value=AutostartStatus(enabled=True, reachable=True)),
    ):
        resp = await auth_client.post(
            f"/api/v1/runtimes/db/{rt.slug}/autostart", json={"enabled": True}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True

    events = await auth_client.get("/api/v1/activity")
    assert events.status_code == 200
    types = [e["event_type"] for e in events.json()]
    assert "runtime.autostart_changed" in types


@pytest.mark.asyncio
async def test_post_autostart_host_unreachable_returns_502_no_stacktrace(auth_client):
    rt = await _make_runtime(
        autostart_supported=True,
        autostart_flag_path="/home/mcuser/scripts/vllm-autostart.enabled",
    )
    with patch(
        "app.routers.runtimes.set_autostart",
        AsyncMock(side_effect=AutostartHostUnreachable("connection refused")),
    ):
        resp = await auth_client.post(
            f"/api/v1/runtimes/db/{rt.slug}/autostart", json={"enabled": True}
        )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "nicht erreichbar" in detail
    assert "Traceback" not in detail
    assert "connection refused" not in detail


@pytest.mark.asyncio
async def test_patch_runtime_sets_autostart_fields(auth_client):
    rt = await _make_runtime()
    resp = await auth_client.patch(
        f"/api/v1/runtimes/db/{rt.slug}",
        json={
            "autostart_supported": True,
            "autostart_flag_path": "/home/mcuser/scripts/vllm-autostart.enabled",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["autostart_supported"] is True
    assert body["autostart_flag_path"] == "/home/mcuser/scripts/vllm-autostart.enabled"


@pytest.mark.asyncio
async def test_patch_runtime_rejects_relative_flag_path(auth_client):
    rt = await _make_runtime()
    resp = await auth_client.patch(
        f"/api/v1/runtimes/db/{rt.slug}",
        json={"autostart_flag_path": "relative/path"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_runtime_rejects_shell_metacharacters_in_flag_path(auth_client):
    rt = await _make_runtime()
    resp = await auth_client.patch(
        f"/api/v1/runtimes/db/{rt.slug}",
        json={"autostart_flag_path": "/tmp/x; rm -rf /"},
    )
    assert resp.status_code == 422
