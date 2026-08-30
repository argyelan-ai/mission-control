"""Nodes API (Fleet & Rezepte v2, Phase 1) — pairing lifecycle + push telemetry.

Only RFC 5737 placeholder IPs — public repo, no real addresses in fixtures
(mirrors test_hosts_api.py's convention).
"""
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.host_pairing_code import HostPairingCode
from app.utils import utcnow
from tests.conftest import test_engine


async def _viewer_token() -> str:
    """JWT for a viewer user (pattern from test_hosts_api._viewer_token)."""
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"viewer-{uid.hex[:8]}@mc.local", name="Viewer",
                   role="viewer", is_active=True))
        await s.commit()
    return create_access_token(str(uid), "viewer")


def _pair_body(code: str, hostname: str = "gx10", **overrides) -> dict:
    body = {"code": code, "hostname": hostname, "os": "linux", "arch": "aarch64", "agent_version": "0.1.0"}
    body.update(overrides)
    return body


def _telemetry(**overrides) -> dict:
    body = {
        "ts": utcnow().isoformat(),
        "cpu_pct": 12.5,
        "load1": 0.8,
        "mem_used_mb": 4096,
        "mem_total_mb": 131072,
        "mem_available_mb": 120000,
        "gpu_util_pct": 35,
        "gpu_temp_c": 61,
        "vram_used_mb": 8806,
        "vram_total_mb": 131072,
    }
    body.update(overrides)
    return body


# ── Pairing-code minting ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_pairing_code_admin(auth_client):
    resp = await auth_client.post("/api/v1/nodes/pairing-codes", json={"display_name_hint": "GX10 Test"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["code"]) == 8
    assert data["host_id"] is None
    assert "install_command" in data and data["code"] in data["install_command"]
    assert "expires_at" in data


@pytest.mark.asyncio
async def test_create_pairing_code_forbidden_for_viewer(client):
    token = await _viewer_token()
    resp = await client.post(
        "/api/v1/nodes/pairing-codes",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_pairing_code_unknown_host_id_404(auth_client):
    resp = await auth_client.post(
        "/api/v1/nodes/pairing-codes", json={"host_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_pairing_code_for_existing_host(auth_client, async_session):
    host = Host(slug="preexisting-agent", display_name="Pre-existing", kind="agent")
    async_session.add(host)
    await async_session.commit()
    await async_session.refresh(host)

    resp = await auth_client.post(
        "/api/v1/nodes/pairing-codes", json={"host_id": str(host.id)}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["host_id"] == str(host.id)


# ── Pairing lifecycle (unauthenticated /pair) ────────────────────────────────


@pytest.mark.asyncio
async def test_pair_creates_host_and_returns_token(client, auth_client):
    minted = (await auth_client.post("/api/v1/nodes/pairing-codes", json={})).json()

    resp = await client.post("/api/v1/nodes/pair", json=_pair_body(minted["code"]))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["heartbeat_interval_s"] == 15
    assert len(data["node_token"]) > 20
    assert data["host_id"]

    hosts = (await auth_client.get("/api/v1/hosts")).json()
    assert len(hosts) == 1
    assert hosts[0]["slug"] == "gx10"
    assert hosts[0]["kind"] == "agent"
    assert hosts[0]["id"] == data["host_id"]


@pytest.mark.asyncio
async def test_pair_slug_deduplicated_from_hostname(client, auth_client):
    m1 = (await auth_client.post("/api/v1/nodes/pairing-codes", json={})).json()
    m2 = (await auth_client.post("/api/v1/nodes/pairing-codes", json={})).json()

    r1 = await client.post("/api/v1/nodes/pair", json=_pair_body(m1["code"], hostname="dup-box"))
    r2 = await client.post("/api/v1/nodes/pair", json=_pair_body(m2["code"], hostname="dup-box"))
    assert r1.status_code == 200 and r2.status_code == 200

    hosts = (await auth_client.get("/api/v1/hosts")).json()
    slugs = sorted(h["slug"] for h in hosts)
    assert slugs == ["dup-box", "dup-box-2"]


@pytest.mark.asyncio
async def test_pair_uses_preexisting_host_and_display_name_hint(client, auth_client, async_session):
    host = Host(slug="gpu-box-known", display_name="Placeholder", kind="agent")
    async_session.add(host)
    await async_session.commit()
    await async_session.refresh(host)

    minted = (
        await auth_client.post(
            "/api/v1/nodes/pairing-codes",
            json={"host_id": str(host.id), "display_name_hint": "GX10 (Marks Büro)"},
        )
    ).json()

    resp = await client.post("/api/v1/nodes/pair", json=_pair_body(minted["code"], hostname="whatever"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["host_id"] == str(host.id)

    # No second host was created — the pairing code's host_id wins, hostname is ignored.
    hosts = (await auth_client.get("/api/v1/hosts")).json()
    assert len(hosts) == 1
    assert hosts[0]["slug"] == "gpu-box-known"


@pytest.mark.asyncio
async def test_pair_unknown_code_404(client):
    resp = await client.post("/api/v1/nodes/pair", json=_pair_body("NOPE0000"))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pair_double_redeem_409(client, auth_client):
    minted = (await auth_client.post("/api/v1/nodes/pairing-codes", json={})).json()
    assert (await client.post("/api/v1/nodes/pair", json=_pair_body(minted["code"]))).status_code == 200
    resp = await client.post("/api/v1/nodes/pair", json=_pair_body(minted["code"], hostname="second"))
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_pair_expired_code_410(client, auth_client, async_session):
    minted = (await auth_client.post("/api/v1/nodes/pairing-codes", json={})).json()

    pairing = (
        await async_session.exec(
            select(HostPairingCode).where(HostPairingCode.code == minted["code"])
        )
    ).first()
    pairing.expires_at = utcnow() - timedelta(minutes=1)
    async_session.add(pairing)
    await async_session.commit()

    resp = await client.post("/api/v1/nodes/pair", json=_pair_body(minted["code"]))
    assert resp.status_code == 410


# ── Heartbeat ────────────────────────────────────────────────────────────────


async def _paired_token(client, auth_client, hostname: str = "gx10") -> tuple[str, str]:
    minted = (await auth_client.post("/api/v1/nodes/pairing-codes", json={})).json()
    paired = (await client.post("/api/v1/nodes/pair", json=_pair_body(minted["code"], hostname=hostname))).json()
    return paired["node_token"], paired["host_id"]


@pytest.mark.asyncio
async def test_heartbeat_updates_host_and_returns_placeholder_commands(client, auth_client):
    token, host_id = await _paired_token(client, auth_client)

    resp = await client.post(
        "/api/v1/nodes/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"telemetry": _telemetry(), "agent_version": "0.1.1"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data == {"ok": True, "heartbeat_interval_s": 15, "commands": []}

    hosts = (await auth_client.get("/api/v1/hosts")).json()
    row = next(h for h in hosts if h["id"] == host_id)
    assert row["agent_version"] == "0.1.1"


@pytest.mark.asyncio
async def test_heartbeat_missing_token_401(client, auth_client):
    await _paired_token(client, auth_client)
    resp = await client.post("/api/v1/nodes/heartbeat", json={"telemetry": _telemetry()})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_heartbeat_wrong_token_401(client, auth_client):
    await _paired_token(client, auth_client)
    resp = await client.post(
        "/api/v1/nodes/heartbeat",
        headers={"Authorization": "Bearer totally-not-a-real-token"},
        json={"telemetry": _telemetry()},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_heartbeat_rate_limit_429(client, auth_client):
    token, _ = await _paired_token(client, auth_client)
    headers = {"Authorization": f"Bearer {token}"}

    r1 = await client.post("/api/v1/nodes/heartbeat", headers=headers, json={"telemetry": _telemetry()})
    assert r1.status_code == 200
    r2 = await client.post("/api/v1/nodes/heartbeat", headers=headers, json={"telemetry": _telemetry()})
    assert r2.status_code == 429


# ── get_host_metrics integration — agent telemetry vs. SSH fallback ─────────


@pytest.mark.asyncio
async def test_host_metrics_serves_fresh_agent_telemetry_without_ssh(client, auth_client):
    token, host_id = await _paired_token(client, auth_client)
    await client.post(
        "/api/v1/nodes/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"telemetry": _telemetry()},
    )

    with patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(side_effect=AssertionError("fresh agent telemetry must not fall back to SSH")),
    ):
        resp = await auth_client.get(f"/api/v1/hosts/{host_id}/metrics")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["kind"] == "agent"
    assert data["reachable"] is True
    assert data["gpu_util_pct"] == 35
    assert data["ram_used_mb"] == 4096
    assert data["ram_total_mb"] == 131072
    assert data["vram_total_mb"] == 131072


@pytest.mark.asyncio
async def test_host_metrics_falls_back_to_ssh_when_telemetry_stale(client, auth_client, async_session):
    token, host_id = await _paired_token(client, auth_client)
    await client.post(
        "/api/v1/nodes/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"telemetry": _telemetry()},
    )

    host = await async_session.get(Host, uuid.UUID(host_id))
    host.agent_last_seen_at = utcnow() - timedelta(seconds=61)
    async_session.add(host)
    await async_session.commit()

    with patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(side_effect=OSError("no ssh key configured for this agent box")),
    ) as ssh_mock:
        resp = await auth_client.get(f"/api/v1/hosts/{host_id}/metrics")
    assert resp.status_code == 200
    assert resp.json()["reachable"] is False
    ssh_mock.assert_awaited()
