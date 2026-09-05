"""Regression guard for the proxy-headers trust fix (PR #404 Rex review,
MEDIUM/LOW + HOCH-1 der Nachlese).

Two things have to hold at once:

1. Caddy must be trusted. Without `--proxy-headers` plus a trust list that
   contains Caddy's container IP, `request.client.host` is always the Caddy
   container's IP, never the real client. Both the login rate limiter
   (routers/auth.py::login) and slowapi's `get_remote_address` key on
   `request.client.host` — with the wrong value, every user shares one bucket
   (unauthenticated remote DoS on login) and per-attacker limiting does nothing.

2. The host must NOT be trusted. `docker-compose.yml` publishes
   `127.0.0.1:8000:8000` (host agents need it), and traffic arriving through a
   published port shows up as the bridge **gateway** address — measured live
   inside the running stack. With the original `--forwarded-allow-ips=*` any
   local process could forge `X-Forwarded-For` and land in someone else's
   bucket. `app/proxy_trust.py` therefore trusts "own subnets minus gateways".

uvicorn's ProxyHeadersMiddleware is applied by the uvicorn CLI/Config layer
(the `--proxy-headers` flag reading FORWARDED_ALLOW_IPS from the environment),
not registered via `app.add_middleware` — it never wraps `app.main:app` when
tests import it directly via httpx.ASGITransport. These tests exercise the
exact middleware + trust config the entrypoint computes, independent of the
full app, to prove the mechanism the Dockerfile flag relies on does what the
fix assumes.
"""
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.proxy_trust import compute_trusted_hosts, parse_proc_net_route

# Verbatim copy of /proc/net/route from the running backend container
# (`docker exec mission-control-backend-1 cat /proc/net/route`): eth0 on
# 172.18.0.0/16 (the compose default network, where Caddy lives), eth1 on
# 172.30.99.0/24 (cdpnet), default route via 172.30.99.1.
LIVE_ROUTE_TABLE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth1\t00000000\t01631EAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    "eth1\t00631EAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
)


async def _client_seen_by_app(trusted_hosts, peer_ip: str, forwarded_for: str):
    """Run one request through ProxyHeadersMiddleware, return scope['client']."""
    captured: dict = {}

    async def inner_app(scope, receive, send):
        captured["client"] = scope.get("client")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wrapped = ProxyHeadersMiddleware(inner_app, trusted_hosts=trusted_hosts)

    scope = {
        "type": "http",
        "client": (peer_ip, 54321),
        "headers": [(b"x-forwarded-for", forwarded_for.encode())],
    }

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    await wrapped(scope, receive, send)
    return captured["client"]


def test_parses_the_live_container_routing_table():
    networks, gateways = parse_proc_net_route(LIVE_ROUTE_TABLE)

    assert {str(n) for n in networks} == {"172.18.0.0/16", "172.30.99.0/24"}
    assert {str(g) for g in gateways} == {"172.30.99.1"}


def test_trust_list_covers_containers_but_not_the_gateways():
    import ipaddress

    trusted = compute_trusted_hosts(LIVE_ROUTE_TABLE)
    assert trusted, "no trust list computed from a valid routing table"

    networks = [ipaddress.ip_network(part) for part in trusted.split(",")]

    def is_trusted(address: str) -> bool:
        ip = ipaddress.ip_address(address)
        return any(ip in net for net in networks)

    # Caddy and any other sibling container: trusted.
    assert is_trusted("172.18.0.5"), "Caddy's container IP must stay trusted"
    assert is_trusted("172.18.7.200"), "any container address in the subnet is trusted"
    assert is_trusted("172.30.99.7"), "the second attached network is covered too"

    # The gateways — where host traffic through the published port arrives.
    assert not is_trusted("172.18.0.1"), (
        "the bridge gateway must NOT be trusted: that is how a process on the "
        "host reaches the published 127.0.0.1:8000 port (PR #404 HOCH-1)"
    )
    assert not is_trusted("172.30.99.1"), "the default-route gateway must not be trusted"

    # Anything outside our own networks was never trusted to begin with.
    assert not is_trusted("203.0.113.7")
    assert not is_trusted("10.0.0.5")


def test_no_routing_table_yields_no_trust_list():
    """Header-only/garbage input must return None so the entrypoint can fall
    back loudly instead of silently shipping a wrong list."""
    assert compute_trusted_hosts("Iface\tDestination\tGateway\n") is None
    assert compute_trusted_hosts("") is None


@pytest.mark.asyncio
async def test_caddy_gets_x_forwarded_for_applied():
    trusted = compute_trusted_hosts(LIVE_ROUTE_TABLE)

    client = await _client_seen_by_app(trusted.split(","), "172.18.0.5", "203.0.113.7")

    assert client is not None
    assert client[0] == "203.0.113.7", (
        f"expected the real client IP from X-Forwarded-For, got {client!r} "
        "(still Caddy's own address) — the computed trust list does not "
        "contain Caddy's container IP"
    )


@pytest.mark.asyncio
async def test_host_process_cannot_forge_x_forwarded_for():
    """The HOCH-1 fix itself: a local process talking to the published
    127.0.0.1:8000 port arrives as the bridge gateway and must be ignored."""
    trusted = compute_trusted_hosts(LIVE_ROUTE_TABLE)

    client = await _client_seen_by_app(trusted.split(","), "172.18.0.1", "203.0.113.7")

    assert client[0] == "172.18.0.1", (
        "a host process forged X-Forwarded-For and got away with it — the "
        "gateway address must not be in the trust list"
    )


@pytest.mark.asyncio
async def test_without_proxy_headers_trust_client_is_the_proxy():
    """Sanity check: this is the broken pre-fix behavior — proves the tests
    above are actually exercising the trust boundary, not a no-op."""
    # uvicorn's own default when --proxy-headers is passed without any trust
    # list: only 127.0.0.1 is trusted, so a container IP like Caddy's is NOT
    # trusted and the header is ignored.
    client = await _client_seen_by_app("127.0.0.1", "172.18.0.5", "203.0.113.7")

    assert client[0] == "172.18.0.5", (
        "expected Caddy's own IP to survive untouched when it isn't in "
        "the trusted set — this is the bug PR #404 found"
    )
