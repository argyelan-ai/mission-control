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
async def test_create_pairing_code_install_command_preserves_https(auth_client):
    """Review finding #1 (30.08.2026): the install one-liner must use
    node_agent_base_url(), not phone_test_url() — the latter hardcodes
    http:// and would downgrade an https-only box (Tailscale cert)."""
    from app.config import settings

    with patch.object(settings, "mc_node_agent_base_url", "https://mini-1.tailnet-name.ts.net"), \
         patch.object(settings, "public_host", "mini-1.tailnet-name.ts.net"):
        resp = await auth_client.post("/api/v1/nodes/pairing-codes", json={})
    assert resp.status_code == 200, resp.text
    install_command = resp.json()["install_command"]
    assert "https://mini-1.tailnet-name.ts.net" in install_command
    assert "http://mini-1.tailnet-name.ts.net" not in install_command


@pytest.mark.asyncio
async def test_install_command_serves_agent_script_from_this_instance(auth_client):
    """Review finding #10 (30.08.2026): no more hardcoded GitHub raw URL —
    the install one-liner must pull the agent from THIS instance's own
    /api/v1/nodes/agent-script, so a fork/self-host/different-branch
    deployment always hands out the agent that actually matches its API."""
    resp = await auth_client.post("/api/v1/nodes/pairing-codes", json={})
    install_command = resp.json()["install_command"]
    assert "/api/v1/nodes/agent-script" in install_command
    assert "raw.githubusercontent.com" not in install_command


@pytest.mark.asyncio
async def test_agent_script_served_unauthenticated_when_mounted(client):
    """The endpoint is deliberately auth-free — an unpaired device has no
    credential yet, that's the whole point of the flow it kicks off."""
    fake_source = "#!/usr/bin/env python3\nprint('mc-node-agent stand-in for the mount test')\n"
    with patch("app.routers.nodes._AGENT_SCRIPT_PATH") as mock_path:
        mock_path.read_text.return_value = fake_source
        resp = await client.get("/api/v1/nodes/agent-script")
    assert resp.status_code == 200
    assert resp.text == fake_source
    assert resp.headers["content-type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_agent_script_404_when_mount_missing(client):
    """A plain image run without the docker-compose bind mount must get a
    clean 404 (feature-gated), never an unhandled 500 (jarvis_core convention)."""
    with patch("app.routers.nodes._AGENT_SCRIPT_PATH") as mock_path:
        mock_path.read_text.side_effect = FileNotFoundError("no such file")
        resp = await client.get("/api/v1/nodes/agent-script")
    assert resp.status_code == 404


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
async def test_pair_slug_race_returns_409_not_500(client, auth_client):
    """Review finding #3 (30.08.2026): two concurrent /pair calls for the
    same hostname can both pass _unique_slug's "not taken" check before
    either inserts — the DB's unique index on hosts.slug is the actual
    referee. Forced deterministically here (instead of real concurrency,
    which SQLite's single test connection can't exercise meaningfully) by
    making _unique_slug hand back an ALREADY-taken slug, exactly what a
    lost race would look like from pair()'s point of view."""
    await auth_client.post(
        "/api/v1/hosts", json={"slug": "collision-box", "display_name": "Taken", "kind": "agent"}
    )
    minted = (await auth_client.post("/api/v1/nodes/pairing-codes", json={})).json()

    with patch("app.routers.nodes._unique_slug", new=AsyncMock(return_value="collision-box")):
        resp = await client.post("/api/v1/nodes/pair", json=_pair_body(minted["code"], hostname="whatever"))
    assert resp.status_code == 409, resp.text


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


@pytest.mark.asyncio
async def test_host_metrics_ignores_agent_telemetry_on_non_agent_kind_host(
    client, auth_client, async_session
):
    """Review finding #8 (30.08.2026): POST /nodes/pairing-codes lets an
    admin mint a code against ANY pre-existing host_id regardless of kind —
    if that host is kind='ssh', its agent_telemetry (however it got there)
    must never mask the real SSH probe."""
    created = (
        await auth_client.post(
            "/api/v1/hosts",
            json={"slug": "ssh-box", "display_name": "SSH Box", "kind": "ssh", "ssh_host": "192.0.2.10"},
        )
    ).json()
    host = await async_session.get(Host, uuid.UUID(created["id"]))
    host.agent_telemetry = {"gpu_util_pct": 99, "mem_used_mb": 1}  # fresh, but on the WRONG kind
    host.agent_last_seen_at = utcnow()
    async_session.add(host)
    await async_session.commit()

    with patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(return_value=("35, 8806, 131072, 61\n---\n              total\nMem:          119181       15230       90000", "", 0)),
    ) as ssh_mock:
        resp = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")
    assert resp.status_code == 200, resp.text
    ssh_mock.assert_awaited()  # the SSH probe ran — the fresh agent_telemetry was NOT used instead
    assert resp.json()["gpu_util_pct"] == 35  # real SSH value, not the 99 planted above


# ── Model-weights inventory (Nachtrag 30.08.2026) ────────────────────────────


def _inventory() -> list[dict]:
    return [
        {
            "name": "models--meta-llama--Llama-3-70B",
            "total_bytes": 140_000_000_000,
            "file_count": 12,
            "mtime_max": 1_800_000_000.0,
            "hf_repo_id": "meta-llama/Llama-3-70B",
            "model_type": "llama",
        },
        {
            "name": "my-local-gguf",
            "total_bytes": 4_000_000_000,
            "file_count": 1,
            "mtime_max": None,
            "hf_repo_id": None,
            "model_type": None,
        },
    ]


@pytest.mark.asyncio
async def test_heartbeat_with_inventory_lands_on_host(client, auth_client):
    token, host_id = await _paired_token(client, auth_client)

    resp = await client.post(
        "/api/v1/nodes/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"telemetry": _telemetry(), "inventory": _inventory()},
    )
    assert resp.status_code == 200, resp.text

    inv = (await auth_client.get(f"/api/v1/nodes/{host_id}/inventory")).json()
    assert inv["host_id"] == host_id
    assert inv["agent_inventory_updated_at"] is not None
    assert len(inv["agent_inventory"]) == 2
    by_name = {e["name"]: e for e in inv["agent_inventory"]}
    assert by_name["models--meta-llama--Llama-3-70B"]["hf_repo_id"] == "meta-llama/Llama-3-70B"
    assert by_name["my-local-gguf"]["model_type"] is None


@pytest.mark.asyncio
async def test_heartbeat_without_inventory_leaves_previous_inventory_untouched(
    client, auth_client, async_session
):
    """The agent only attaches `inventory` every ~40th heartbeat when it
    changed — every other heartbeat must NOT wipe the last stored snapshot."""
    token, host_id = await _paired_token(client, auth_client)

    await client.post(
        "/api/v1/nodes/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"telemetry": _telemetry(), "inventory": _inventory()},
    )
    first = (await auth_client.get(f"/api/v1/nodes/{host_id}/inventory")).json()

    # Clear the 5s rate guard so the next heartbeat is actually accepted —
    # what's under test here is the inventory=None handling, not the guard.
    host = await async_session.get(Host, uuid.UUID(host_id))
    host.agent_last_seen_at = utcnow() - timedelta(seconds=10)
    async_session.add(host)
    await async_session.commit()

    resp = await client.post(
        "/api/v1/nodes/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"telemetry": _telemetry()},  # no inventory field this time
    )
    assert resp.status_code == 200, resp.text

    second = (await auth_client.get(f"/api/v1/nodes/{host_id}/inventory")).json()
    assert second == first


@pytest.mark.asyncio
async def test_inventory_endpoint_unknown_host_404(auth_client):
    resp = await auth_client.get(f"/api/v1/nodes/{uuid.uuid4()}/inventory")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_inventory_endpoint_empty_before_first_heartbeat(client, auth_client):
    _, host_id = await _paired_token(client, auth_client)
    inv = (await auth_client.get(f"/api/v1/nodes/{host_id}/inventory")).json()
    assert inv == {"host_id": host_id, "agent_inventory": None, "agent_inventory_updated_at": None}
