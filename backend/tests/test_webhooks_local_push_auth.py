"""POST /api/v1/webhooks/local/push must only accept genuinely local callers.

The endpoint has no user auth: it exists for a git post-commit hook on the
host. Its docstring claimed "local-only (Docker network + loopback)", but
nothing enforced it — the route was reachable through the Caddy reverse
proxy on :80 and on the TLS site, so anyone who could reach Caddy could inject
fake commit events into the activity feed.

Enforced twice (like /api/v1/internal/*, PR #404):

1. The endpoint rejects any request that went through a proxy (Caddy always
   sets X-Forwarded-For) and any peer outside loopback + this container's own
   Docker subnets.
2. The Caddyfiles answer /api/v1/webhooks/local/* with 403 before the generic
   /api/* handler.
"""
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[2]
URL = "/api/v1/webhooks/local/push"
PAYLOAD = {
    "repo": "mission-control",
    "branch": "main",
    "commit_sha": "0123456789abcdef",
    "commit_message": "test commit",
    "author": "tester",
}

# Same shape as the live backend container: eth0 on 172.18.0.0/16 (compose
# default network), eth1 on 172.30.99.0/24 (cdpnet), default route via
# 172.30.99.1 — see tests/test_proxy_headers_trust.py.
ROUTE_TABLE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth1\t00000000\t01631EAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    "eth1\t00631EAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
)


@pytest.fixture
def container_route_table(monkeypatch):
    """Pretend we run inside the compose network (deterministic on any CI host)."""
    import app.local_only as local_only

    monkeypatch.setattr(local_only, "_read_route_table", lambda: ROUTE_TABLE)
    local_only.local_networks.cache_clear()
    yield
    local_only.local_networks.cache_clear()


def _client_from(client: AsyncClient, host: str) -> AsyncClient:
    """Reuse the conftest app + overrides, but connect from `host`."""
    transport = ASGITransport(app=client._transport.app, client=(host, 40000))
    return AsyncClient(transport=transport, base_url="http://test")


# ── rejected ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    [
        "100.100.200.50",  # tailnet peer (invented test address, see .gitleaks.toml)
        "192.168.1.50",  # LAN peer
        "203.0.113.9",  # public
    ],
)
async def test_non_local_peer_is_rejected(client, container_route_table, host):
    async with _client_from(client, host) as c:
        resp = await c.post(URL, json=PAYLOAD)
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    ["X-Forwarded-For", "X-Forwarded-Host", "Forwarded", "X-Real-IP"],
)
async def test_request_through_a_proxy_is_rejected_even_from_loopback(
    client, container_route_table, header
):
    # Behind Caddy the TCP peer is Caddy (a Docker-network address) and, with
    # --proxy-headers, request.client.host becomes whatever Caddy saw. Neither
    # proves locality, so any proxy header is a hard no — its VALUE is never
    # trusted, only its presence is used to fail closed.
    resp = await client.post(URL, json=PAYLOAD, headers={header: "127.0.0.1"})
    assert resp.status_code == 403


# ── accepted ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loopback_caller_is_accepted(client, container_route_table):
    # conftest's ASGITransport connects from 127.0.0.1.
    resp = await client.post(URL, json=PAYLOAD)
    assert resp.status_code == 200
    assert resp.json() == {"status": "received"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    [
        "172.18.0.1",  # host process via the published 127.0.0.1:8000 port
        "172.18.0.7",  # sibling container on the compose network
    ],
)
async def test_docker_network_caller_is_accepted(client, container_route_table, host):
    async with _client_from(client, host) as c:
        resp = await c.post(URL, json=PAYLOAD)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_without_a_route_table_only_loopback_is_local(client, monkeypatch):
    import app.local_only as local_only

    monkeypatch.setattr(local_only, "_read_route_table", lambda: None)
    local_only.local_networks.cache_clear()
    try:
        async with _client_from(client, "172.18.0.7") as c:
            assert (await c.post(URL, json=PAYLOAD)).status_code == 403
        assert (await client.post(URL, json=PAYLOAD)).status_code == 200
    finally:
        local_only.local_networks.cache_clear()


# ── Caddy layer ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("caddyfile", ["Caddyfile", "caddy/Caddyfile.tls.example"])
def test_caddyfiles_block_local_webhooks(caddyfile):
    text = (REPO_ROOT / caddyfile).read_text()
    block = "handle /api/v1/webhooks/local/* {\n        respond 403\n    }"
    assert block in text, f"{caddyfile} must answer /api/v1/webhooks/local/* with 403"
